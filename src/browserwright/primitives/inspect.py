"""Page inspection: page_info / capture_screenshot / raw cdp, plus the two
stateless perception primitives ``snapshot`` (what can I act on + where) and
``describe_page`` (what paints / styles this page).

Both perception primitives are single ``js()`` round-trips, return bounded /
truncated output, carry no ref store (coordinates feed straight into
``click_at_xy``), and hardcode no site/selector/class.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Any, Optional

from ..session import current_session


def cdp(method: str, session_id: Optional[str] = None, **params) -> dict:
    """Pass-through to the underlying CDP transport."""
    sess = current_session()
    if session_id is None and sess.current_target_id:
        session_id = sess.cdp.attach(sess.current_target_id)
    return sess.cdp.send(method, session=session_id, **params)


def page_info() -> dict:
    """Snapshot of the current page state. Mirrors browser-harness shape."""
    from .interact import js  # avoid import cycle

    return js("""
        return {
          url: location.href,
          title: document.title,
          w: window.innerWidth,
          h: window.innerHeight,
          sx: window.scrollX,
          sy: window.scrollY,
          pw: document.documentElement.scrollWidth,
          ph: document.documentElement.scrollHeight,
          ready: document.readyState
        }
    """)


def capture_screenshot(path: Optional[str] = None, *, full: bool = False,
                       max_dim: Optional[int] = None, annotate: bool = False):
    """Capture a PNG screenshot. Writes to ``path`` (or /tmp/screenshot-N.png)
    and returns the absolute path. Set ``full=True`` for a full-page capture.

    Set ``annotate=True`` for a **set-of-mark** capture: numbered ``[N]`` labels
    are overlaid on the page's interactive elements (the ones ``snapshot()``
    reports), and the return value becomes a dict
    ``{"path": <png path>, "legend": [{"n", "role", "name", "x", "y"}, ...]}``.
    Each ``[N]`` maps to that element's center ``(x, y)`` — feed it straight
    into ``click_at_xy(x, y)``. This is coordinate-keyed, not ref-keyed: there
    is no element handle to store, the marks are just a visual index over the
    same coordinates ``snapshot()`` already returns.

    Without ``annotate`` the return value is a bare path string (unchanged).
    """
    sess = current_session()
    sid = sess.cdp.attach(sess.current_target_id) if sess.current_target_id else None
    if sid is None:
        from .page import current_page
        current_page()
        sid = sess.cdp.attach(sess.current_target_id)

    legend: Optional[list] = None
    mark_error: Optional[str] = None
    if annotate:
        legend, mark_error = _draw_set_of_mark()

    try:
        params: dict[str, Any] = {"format": "png"}
        if full:
            params["captureBeyondViewport"] = True
        res = sess.cdp.send("Page.captureScreenshot", session=sid, **params)
        raw = base64.b64decode(res["data"])
    finally:
        if annotate:
            _clear_set_of_mark()

    if max_dim:
        raw = _downscale_png(raw, max_dim=max_dim)
    if not path:
        # Pick a /tmp file that doesn't collide if the agent runs many shots.
        i = 0
        while True:
            cand = Path("/tmp") / f"browserwright-shot-{os.getpid()}-{i}.png"
            if not cand.exists():
                path = str(cand)
                break
            i += 1
    Path(path).write_bytes(raw)
    abs_path = str(Path(path).resolve())
    if annotate:
        out: dict = {"path": abs_path, "legend": legend or []}
        if isinstance(legend, list) and len(legend) >= 120:
            out["truncated"] = True
            out["total_count"] = len(legend)
        if mark_error:
            # The overlay failed to paint; the legend coords are still valid but
            # the agent must NOT assume numbered marks are visible on the image.
            out["mark_error"] = mark_error
        return out
    return abs_path


# ---------------------------------------------------------------------------
# B3: set-of-mark annotation. Overlay numbered [N] badges on the interactive
# nodes snapshot() reports, keyed to their center coordinates (no ref store).
# ---------------------------------------------------------------------------

_MARK_CONTAINER_ID = "__bs_setofmark__"

_DRAW_MARK_JS = r"""
return (function(nodes){
  var prev = document.getElementById("__bs_setofmark__");
  if (prev) prev.remove();
  var box = document.createElement("div");
  box.id = "__bs_setofmark__";
  box.style.cssText = "position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none";
  for (var i=0;i<nodes.length;i++){
    var n = nodes[i];
    var tag = document.createElement("div");
    tag.textContent = "" + n.n;
    tag.style.cssText =
      "position:fixed;transform:translate(-50%,-50%);left:"+n.x+"px;top:"+n.y+"px;"+
      "background:#ff0066;color:#fff;font:bold 12px/1 monospace;padding:2px 4px;"+
      "border-radius:3px;box-shadow:0 0 0 1px #fff;white-space:nowrap";
    box.appendChild(tag);
  }
  (document.body || document.documentElement).appendChild(box);
  return nodes.length;
})(__NODES__);
"""

_CLEAR_MARK_JS = (
    "var e=document.getElementById('%s'); if(e) e.remove(); return true;"
    % _MARK_CONTAINER_ID
)


def _draw_set_of_mark() -> tuple:
    """Compute the legend from ``snapshot()``'s interactive nodes and draw a
    numbered badge at each node's center. Returns ``(legend, error)`` where
    ``error`` is ``None`` on success or a short string if the overlay draw failed.

    The legend is derived from the SAME snapshot the marks are drawn from, so
    each ``[n]``'s ``(x, y)`` is exactly the center ``snapshot()`` reports (and
    that ``click_at_xy`` expects). Generic: works for any page's interactive
    set, no site/selector hardcoded.
    """
    from .interact import js  # avoid import cycle

    snap = snapshot(text=False)
    nodes = snap.get("nodes", []) if isinstance(snap, dict) else []
    legend = []
    for i, n in enumerate(nodes):
        legend.append({
            "n": i,
            "role": n.get("role"),
            "name": n.get("name"),
            "x": n.get("x"),
            "y": n.get("y"),
        })
    if isinstance(snap, dict) and snap.get("truncated"):
        legend.append({
            "n": len(legend),
            "role": "status",
            "name": f"truncated after {len(nodes)} nodes",
            "x": None,
            "y": None,
        })
    code = _DRAW_MARK_JS.replace("__NODES__", json.dumps(legend))
    err: Optional[str] = None
    try:
        js(code)
    except Exception as e:
        # Drawing is best-effort; the legend (coordinates) is the load-bearing
        # output, so we still return it even if the overlay failed to paint —
        # but report the failure so the caller can flag that the marks aren't
        # actually on the image (see capture_screenshot's ``mark_error``).
        err = f"{type(e).__name__}: {e}"
    return legend, err


def _clear_set_of_mark() -> None:
    from .interact import js  # avoid import cycle

    try:
        js(_CLEAR_MARK_JS)
    except Exception:
        pass


def _downscale_png(data: bytes, *, max_dim: int) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return data
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale >= 1.0:
        return data
    new = im.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    new.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Perception primitive 1: snapshot() — interaction-oriented observation
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = r"""
return (function(opts){
  var interactiveOnly = opts.interactiveOnly !== false; // default true
  var maxNodes = opts.maxNodes || 120;
  var includeHref = opts.includeHref !== false;
  var scopeSel = opts.scope || null;
  var maxDepth = opts.maxDepth || 0; // 0 = unbounded

  var INTERACTIVE_TAGS = {A:1,BUTTON:1,INPUT:1,SELECT:1,TEXTAREA:1,SUMMARY:1,OPTION:1};
  var INTERACTIVE_ROLES = {button:1,link:1,checkbox:1,radio:1,tab:1,menuitem:1,
    menuitemcheckbox:1,menuitemradio:1,switch:1,option:1,textbox:1,combobox:1,
    searchbox:1,slider:1,spinbutton:1,treeitem:1};
  var NAME_ROLES = {heading:1,img:1,alert:1,dialog:1}; // structural-but-named, kept when !interactiveOnly

  function trunc(s, n){ if(s==null) return null; s=(""+s).replace(/\s+/g," ").trim();
    return s.length>n ? s.slice(0,n)+"…" : s; }

  function roleOf(el){
    var r = el.getAttribute && el.getAttribute("role");
    if(r) return r.toLowerCase().split(/\s+/)[0];
    var t = el.tagName;
    if(t==="A") return el.hasAttribute("href") ? "link" : "generic";
    if(t==="BUTTON") return "button";
    if(t==="SELECT") return "combobox";
    if(t==="TEXTAREA") return "textbox";
    if(t==="SUMMARY") return "summary";
    if(t==="INPUT"){
      var ty=(el.getAttribute("type")||"text").toLowerCase();
      if(ty==="checkbox") return "checkbox";
      if(ty==="radio") return "radio";
      if(ty==="button"||ty==="submit"||ty==="reset"||ty==="image") return "button";
      if(ty==="range") return "slider";
      return "textbox";
    }
    if(/^H[1-6]$/.test(t)) return "heading";
    if(t==="IMG") return "img";
    return "generic";
  }

  // Accessible name: aria-label > aria-labelledby > alt/value/placeholder >
  // visible text (trimmed). Cheap approximation of the a11y name algorithm.
  function nameOf(el){
    var al = el.getAttribute && el.getAttribute("aria-label");
    if(al) return al;
    var lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if(lb){
      var parts=[];
      lb.split(/\s+/).forEach(function(id){
        var n=document.getElementById(id); if(n) parts.push(n.textContent||"");
      });
      if(parts.length) return parts.join(" ");
    }
    var t = el.tagName;
    if(t==="IMG") return el.getAttribute("alt")||"";
    if(t==="INPUT"){
      var ty=(el.getAttribute("type")||"text").toLowerCase();
      if(ty==="submit"||ty==="button"||ty==="reset") return el.value||"";
      return el.getAttribute("placeholder")||el.getAttribute("name")||"";
    }
    var title = el.getAttribute && el.getAttribute("title");
    var txt = (el.textContent||"").trim();
    if(txt) return txt;
    if(title) return title;
    return "";
  }

  function visible(el, r){
    if(!r) return false;
    if(r.width<1 || r.height<1) return false;
    if(r.bottom<0 || r.right<0) return false;
    if(r.top>(window.innerHeight||0) || r.left>(window.innerWidth||0)) return false;
    var cs = getComputedStyle(el);
    if(cs.visibility==="hidden" || cs.display==="none") return false;
    if(parseFloat(cs.opacity)===0) return false;
    return true;
  }

  function isInteractive(el, role){
    if(INTERACTIVE_TAGS[el.tagName]) return true;
    if(INTERACTIVE_ROLES[role]) return true;
    if(el.hasAttribute && el.hasAttribute("onclick")) return true;
    if(el.hasAttribute && el.hasAttribute("tabindex") &&
       el.getAttribute("tabindex")!=="-1") return true;
    var cs = getComputedStyle(el);
    if(cs.cursor==="pointer" && el.children.length===0) return true;
    return false;
  }

  var roots = [];
  if(scopeSel){
    document.querySelectorAll(scopeSel).forEach(function(n){ roots.push(n); });
  } else {
    roots.push(document.body || document.documentElement);
  }

  var out = [];
  var truncated = false;
  var iframeCount = 0;

  function walk(root, depth, frameTag){
    if(out.length>=maxNodes){ truncated=true; return; }
    var stack = [];
    for(var i=0;i<root.children.length;i++) stack.push([root.children[i], depth]);
    // BFS-ish using a queue keeps shallow (more salient) nodes first.
    var qi=0;
    var queue = stack;
    while(qi<queue.length){
      if(out.length>=maxNodes){ truncated=true; return; }
      var pair = queue[qi++]; var el=pair[0]; var d=pair[1];
      if(!(el instanceof Element)) continue;
      var role = roleOf(el);
      var keep = interactiveOnly ? isInteractive(el, role)
                                 : (isInteractive(el, role)||NAME_ROLES[role]||role==="heading");
      var r = el.getBoundingClientRect();
      if(keep && visible(el, r)){
        var entry = {
          role: role,
          tag: el.tagName.toLowerCase(),
          name: trunc(nameOf(el), 80),
          x: Math.round(r.left + r.width/2),
          y: Math.round(r.top + r.height/2),
        };
        if(frameTag) entry.frame = frameTag;
        var ty = el.getAttribute && el.getAttribute("type");
        if(el.tagName==="INPUT" && ty) entry.type = ty.toLowerCase();
        if(includeHref && el.tagName==="A" && el.getAttribute("href"))
          entry.href = trunc(el.href, 100);
        if(el.disabled || el.getAttribute && el.getAttribute("aria-disabled")==="true")
          entry.disabled = true;
        var checked = el.getAttribute && el.getAttribute("aria-checked");
        if(el.tagName==="INPUT" && (el.type==="checkbox"||el.type==="radio"))
          entry.checked = !!el.checked;
        else if(checked) entry.checked = checked;
        out.push(entry);
      }
      // Same-origin iframe: inline one level.
      if(el.tagName==="IFRAME" && !frameTag && iframeCount<3){
        try{
          var doc = el.contentDocument;
          if(doc && doc.body){
            iframeCount++;
            var fr = el.getBoundingClientRect();
            // Recurse but offset coords to top-level viewport.
            walkFrame(doc.body, "iframe#"+iframeCount, fr.left, fr.top);
          }
        }catch(e){ /* cross-origin: omit */ }
      }
      if(maxDepth && d>=maxDepth) continue;
      for(var j=0;j<el.children.length;j++) queue.push([el.children[j], d+1]);
    }
  }

  function walkFrame(body, frameTag, offX, offY){
    var queue=[]; for(var i=0;i<body.children.length;i++) queue.push(body.children[i]);
    var qi=0;
    while(qi<queue.length){
      if(out.length>=maxNodes){ truncated=true; return; }
      var el=queue[qi++]; if(!(el instanceof Element)) continue;
      var role=roleOf(el);
      var keep = interactiveOnly ? isInteractive(el,role)
                                 : (isInteractive(el,role)||role==="heading");
      var r=el.getBoundingClientRect();
      var vis = r.width>=1 && r.height>=1;
      if(keep && vis){
        out.push({
          role:role, tag:el.tagName.toLowerCase(), name:trunc(nameOf(el),80),
          x:Math.round(offX + r.left + r.width/2),
          y:Math.round(offY + r.top + r.height/2),
          frame:frameTag,
        });
      }
      for(var j=0;j<el.children.length;j++) queue.push(el.children[j]);
    }
  }

  roots.forEach(function(rt){ walk(rt, 0, null); });

  return {
    url: location.href,
    title: document.title,
    viewport: {w: window.innerWidth, h: window.innerHeight},
    count: out.length,
    truncated: truncated,
    nodes: out,
  };
})(__OPTS__);
"""


def snapshot(*, interactive_only=True, max_nodes=120, max_depth=0,
             scope=None, include_href=True, text=True):
    """What can I act on, and where? Interaction-oriented digest of the
    actionable elements currently in the viewport.

    Stateless and coordinate-based: each node carries role, accessible name,
    center ``(x, y)`` (top-level viewport coords — feed straight into
    ``click_at_xy``), and useful attrs (type, href, disabled, checked). No
    ref store; scroll to reveal more.

    Args:
      interactive_only: only buttons/links/inputs/role-interactive nodes
        (default). False also keeps headings and named structural nodes.
      max_nodes: hard cap on returned nodes (bounds token cost).
      max_depth: DOM depth cap (0 = unbounded).
      scope: CSS selector to restrict the scan to matching subtrees.
      include_href: include resolved href for links.
      text: also return a compact text rendering under ``["text"]``.

    Returns a dict: url, title, viewport, count, truncated, nodes[],
    and (when text=True) a ``text`` block of ``[i] role "name" (x,y) attrs``.

    Limits: same-origin iframes are inlined one level (up to 3 frames);
    cross-origin iframes, shadow DOM, and canvas-drawn UI are not traversed.
    Only viewport-visible nodes are returned (scroll to reveal more).
    """
    from .interact import js  # avoid import cycle

    opts = {
        "interactiveOnly": bool(interactive_only),
        "maxNodes": int(max_nodes),
        "maxDepth": int(max_depth),
        "includeHref": bool(include_href),
        "scope": scope,
    }
    code = _SNAPSHOT_JS.replace("__OPTS__", json.dumps(opts))
    res = js(code)
    if text and isinstance(res, dict):
        lines = []
        for i, n in enumerate(res.get("nodes", [])):
            bits = [f'[{i}]', n.get("role", "?")]
            nm = n.get("name")
            bits.append(f'"{nm}"' if nm else '""')
            extra = []
            if n.get("type"):
                extra.append(f'type={n["type"]}')
            if n.get("disabled"):
                extra.append("disabled")
            if "checked" in n:
                extra.append(f'checked={n["checked"]}')
            if n.get("href"):
                extra.append(f'href={n["href"]}')
            if n.get("frame"):
                extra.append(n["frame"])
            tail = (" " + " ".join(extra)) if extra else ""
            bits.append(f'({n.get("x")},{n.get("y")}){tail}')
            lines.append(" ".join(bits))
        res = dict(res)
        res["text"] = "\n".join(lines)
    return res


# ---------------------------------------------------------------------------
# Perception primitive 2: describe_page() — visual / style-forensics
# ---------------------------------------------------------------------------

_DESCRIBE_JS = r"""
return (function(opts){
  var maxNodes = opts.maxNodes || 40;
  var maxVars = opts.maxVars || 60;
  var minAreaFrac = opts.minAreaFrac || 0.03; // fraction of viewport area
  var viewportOnly = !!opts.viewportOnly;       // S1: only rank nodes that
                                                // intersect the viewport

  function trunc(s, n){ if(s==null) return null; s=(""+s);
    return s.length>n ? s.slice(0,n)+"…("+s.length+")" : s; }
  function classList(el){
    var c = (el.className && el.className.baseVal!=null) ? el.className.baseVal
            : (typeof el.className==="string" ? el.className : "");
    c=(c||"").trim();
    if(!c) return null;
    var parts=c.split(/\s+/).slice(0,6);
    return trunc(parts.join(" "), 80);
  }

  var vw = window.innerWidth, vh = window.innerHeight;
  var vArea = Math.max(1, vw*vh);

  function pseudo(el, which){
    var cs = getComputedStyle(el, which);
    if(!cs) return null;
    var bg = cs.backgroundImage;
    var content = cs.content;
    var hasBg = bg && bg!=="none";
    var hasContent = content && content!=="none" && content!=="normal" && content!=='""';
    if(!hasBg && !hasContent) return null;
    var o = {};
    if(hasBg) o.backgroundImage = trunc(bg, 120);
    if(hasContent) o.content = trunc(content, 40);
    var mb = cs.mixBlendMode; if(mb && mb!=="normal") o.mixBlendMode = mb;
    return o;
  }

  // Visible (viewport-clamped) area — a 14000px-tall wrapper is NOT salient;
  // what paints the *screen* is. We clamp the rect to the viewport so plain
  // full-document wrappers don't dominate the ranking by raw height.
  function visibleAreaFrac(r){
    var l=Math.max(0,r.left), t=Math.max(0,r.top);
    var rr=Math.min(vw,r.right), bb=Math.min(vh,r.bottom);
    var w=Math.max(0,rr-l), h=Math.max(0,bb-t);
    return (w*h)/vArea;
  }
  function intersectsViewport(r){
    return r.bottom>0 && r.right>0 && r.top<vh && r.left<vw &&
           r.width>0 && r.height>0;
  }

  // Salience scoring: visible area, fixed/absolute overlays, high z,
  // non-trivial background, blend/filter/backdrop, pseudo-elements.
  var cands = [];
  var all = document.querySelectorAll("body *");
  // Hard scan cap: never walk a pathologically large DOM node-by-node
  // (getComputedStyle + getBoundingClientRect per element) — on an
  // infinite-scroll/huge page that can blow the CDP eval timeout. 20k elements
  // covers any real page's salient layer; salient nodes are then ranked + capped.
  var scanN = Math.min(all.length, 20000);
  for(var i=0;i<scanN;i++){
    var el = all[i];
    if(el.tagName==="SCRIPT"||el.tagName==="STYLE"||el.tagName==="NOSCRIPT") continue;
    var cs = getComputedStyle(el);
    if(cs.display==="none"||cs.visibility==="hidden") continue;
    var r = el.getBoundingClientRect();
    // S1 viewport_only: skip nodes that don't intersect the viewport at all.
    // Off-screen style-bearing nodes are noise when the agent is asking
    // "what paints the screen I'm looking at".
    if(viewportOnly && !intersectsViewport(r)) continue;
    var rawFrac = (Math.max(0,r.width)*Math.max(0,r.height))/vArea;
    var visFrac = visibleAreaFrac(r);
    var pos = cs.position;
    var z = parseInt(cs.zIndex, 10); if(isNaN(z)) z=null;
    var bgImg = cs.backgroundImage;
    var bgCol = cs.backgroundColor;
    var blend = cs.mixBlendMode;
    var filter = cs.filter;
    var backdrop = cs.backdropFilter || cs.webkitBackdropFilter;

    var hasBgImg = bgImg && bgImg!=="none";
    var hasBgCol = bgCol && bgCol!=="rgba(0, 0, 0, 0)" && bgCol!=="transparent";
    var hasBlend = blend && blend!=="normal";
    var hasFilter = filter && filter!=="none";
    var hasBackdrop = backdrop && backdrop!=="none";
    var overlay = (pos==="fixed"||pos==="absolute") && visFrac>=0.1;
    var bef = pseudo(el, "::before");
    var aft = pseudo(el, "::after");

    // A "style signal" = this node visibly paints something beyond plain layout.
    var styleBearing = hasBgImg||hasBlend||hasFilter||hasBackdrop||bef||aft||
                       (hasBgCol && (overlay || z!=null)) || (z!=null&&z>=10);

    // Drop nodes that are neither style-bearing nor a meaningful background fill.
    // Plain structural wrappers (no style signal) are kept ONLY if they paint a
    // non-trivial background color over a big visible area.
    if(!styleBearing){
      if(!(hasBgCol && visFrac>=0.2)) continue;
    }
    if(visFrac<minAreaFrac && !styleBearing) continue;
    if(r.width<1 && r.height<1 && !bef && !aft) continue;

    var score = 0;
    score += visFrac*60;            // visible coverage matters most
    if(overlay) score += 45;
    if(z!=null) score += Math.min(Math.max(z,0), 1000)/20;
    if(hasBgImg) score += 40;       // gradients/textures are the usual answer
    if(hasBlend) score += 45;
    if(hasBackdrop) score += 30;
    if(hasFilter) score += 12;
    if(bef||aft) score += 25;
    if(hasBgCol && !styleBearing) score += visFrac*10; // plain fill: mild

    var node = {
      tag: el.tagName.toLowerCase(),
      cls: classList(el),
      rect: {x:Math.round(r.left), y:Math.round(r.top),
             w:Math.round(r.width), h:Math.round(r.height)},
      visFrac: Math.round(visFrac*1000)/1000,
      areaFrac: Math.round(rawFrac*1000)/1000,
      position: pos,
      zIndex: z,
    };
    if(hasBgImg) node.backgroundImage = trunc(bgImg, 140);
    if(hasBgCol) node.backgroundColor = bgCol;
    if(hasBlend) node.mixBlendMode = blend;
    if(hasFilter) node.filter = trunc(filter, 80);
    if(hasBackdrop) node.backdropFilter = trunc(backdrop, 80);
    if(bef) node.before = bef;
    if(aft) node.after = aft;
    node._score = score;
    cands.push(node);
  }

  cands.sort(function(a,b){ return b._score - a._score; });
  var truncated = cands.length > maxNodes;
  cands = cands.slice(0, maxNodes);
  cands.forEach(function(n){ delete n._score; });

  // :root / documentElement CSS custom properties, gathered from three
  // sources (most reliable first): inline html style attr, computed-style
  // enumeration (Chromium exposes custom props on the CSSStyleDeclaration),
  // then same-origin stylesheet :root/html rules.
  var vars = {};
  var nVars = 0;
  var rootEl = document.documentElement;
  var rootStyle = getComputedStyle(rootEl);
  var declared = {};
  try{
    // 1. inline style on <html> (frameworks set theme vars here).
    var inline = rootEl.style;
    for(var ii=0; ii<inline.length; ii++){
      var p0=inline[ii]; if(p0 && p0.indexOf("--")===0) declared[p0]=true;
    }
    // 2. computed style enumeration (Chromium lists --vars).
    for(var ci=0; ci<rootStyle.length; ci++){
      var p1=rootStyle[ci]; if(p1 && p1.indexOf("--")===0) declared[p1]=true;
    }
    // 3. same-origin stylesheet :root / html rules.
    for(var s=0;s<document.styleSheets.length;s++){
      var rules;
      try{ rules = document.styleSheets[s].cssRules; }catch(e){ continue; }
      if(!rules) continue;
      for(var ri=0;ri<rules.length;ri++){
        var rule = rules[ri];
        if(!rule.style || !rule.selectorText) continue;
        if(!/(^|,)\s*(:root|html)\b/.test(rule.selectorText)) continue;
        for(var pi=0;pi<rule.style.length;pi++){
          var prop = rule.style[pi];
          if(prop && prop.indexOf("--")===0) declared[prop]=true;
        }
      }
    }
  }catch(e){}
  var names = Object.keys(declared);
  for(var k=0;k<names.length && nVars<maxVars;k++){
    var v = rootStyle.getPropertyValue(names[k]).trim();
    if(v){ vars[names[k]] = trunc(v, 60); nVars++; }
  }

  var htmlCs = getComputedStyle(document.documentElement);
  var bodyCs = document.body ? getComputedStyle(document.body) : null;

  return {
    url: location.href,
    viewport: {w:vw, h:vh},
    root: {
      htmlBackground: trunc(htmlCs.background || htmlCs.backgroundColor, 120),
      htmlBackgroundColor: htmlCs.backgroundColor,
      bodyBackgroundImage: bodyCs ? trunc(bodyCs.backgroundImage,140) : null,
      bodyBackgroundColor: bodyCs ? bodyCs.backgroundColor : null,
      bodyBefore: document.body ? pseudo(document.body,"::before") : null,
      bodyAfter: document.body ? pseudo(document.body,"::after") : null,
    },
    cssVars: vars,
    cssVarCount: nVars,
    nodeCount: cands.length,
    truncated: truncated,
    nodes: cands,
  };
})(__OPTS__);
"""


def describe_page(*, max_nodes=40, max_vars=60, min_area_frac=0.03,
                  viewport_only=False):
    """What paints / styles this page? Visual / style-forensics digest, in
    one round-trip.

    The ``snapshot``/a11y view deliberately omits decorative, non-interactive,
    style-bearing nodes. This surfaces them: large-area / fixed / absolute
    overlays, high z-index, full-viewport nodes, and any node with a
    non-trivial ``background-image``, non-transparent ``background-color``,
    ``mix-blend-mode``, ``filter``, or ``backdrop-filter`` — including
    ``::before`` / ``::after`` background-image and content.

    Also returns ``:root`` / ``<html>`` CSS custom properties (variables,
    pulled from stylesheet :root rules) and the ``<html>`` / ``<body>``
    computed background + pseudo-elements.

    Args:
      max_nodes: cap on salient nodes returned (ranked by salience score).
      max_vars: cap on CSS variables returned.
      min_area_frac: nodes smaller than this fraction of the viewport are
        dropped unless they carry a style signal.
      viewport_only: when True, only rank/return nodes that intersect the
        current viewport. Off-screen style-bearing nodes (e.g. a gradient
        8000px down) are noise when you only care about what paints the
        screen in front of you; the default scan keeps them.

    Returns a dict: url, viewport, root{html/body bg + pseudos}, cssVars,
    cssVarCount, nodeCount, truncated, nodes[] (each: tag, cls, rect, visFrac,
    areaFrac, position, zIndex, + whichever style fields are non-trivial).

    Limits: only same-origin stylesheets contribute CSS vars (cross-origin
    sheets are unreadable). Canvas/WebGL paint and shadow-DOM styles are not
    inspected. Computed backgrounds are post-cascade snapshots, not authored
    rules.
    """
    from .interact import js  # avoid import cycle

    opts = {
        "maxNodes": int(max_nodes),
        "maxVars": int(max_vars),
        "minAreaFrac": float(min_area_frac),
        "viewportOnly": bool(viewport_only),
    }
    code = _DESCRIBE_JS.replace("__OPTS__", json.dumps(opts))
    return js(code)


# ---------------------------------------------------------------------------
# Verification primitive: diff_snapshot() — did my action change the page?
# ---------------------------------------------------------------------------

# Attributes whose change (for a node of stable identity) we report as a
# "change". Keep this small + meaningful: an agent acts to toggle enablement,
# rename a control, swap a link target, or move/reveal something.
_DIFF_ATTRS = ("name", "disabled", "checked", "href", "type", "frame")
# A center that moves more than this many px (Chebyshev) counts as a "moved"
# change even when every reported attr is identical — surfaces show/relayout.
_DIFF_MOVE_PX = 24


def _diff_identity(node: dict, *, bucket: int = 32) -> tuple:
    """Identity used to match a node across two snapshots.

    role + accessible name + a coarse position bucket. role+name is the
    semantic anchor (a "Submit" button stays the same control across a
    re-render); the bucketed center disambiguates several same-role/same-name
    nodes (e.g. three identical "Add" buttons in a list) without making the
    identity so precise that a small relayout reads as remove+add. Bucket size
    is intentionally coarse (``bucket`` px) so sub-bucket jitter is treated as
    the *same* node and reported via the moved-attr path instead.
    """
    x = node.get("x")
    y = node.get("y")
    bx = int(x) // bucket if isinstance(x, (int, float)) else None
    by = int(y) // bucket if isinstance(y, (int, float)) else None
    return (node.get("role"), node.get("name") or "", bx, by)


def _node_attrs(node: dict) -> dict:
    """Comparable attribute view of a node (the fields whose change we care
    about). Missing attrs are normalized to None so toggles read cleanly."""
    return {k: node.get(k) for k in _DIFF_ATTRS}


def _slim(node: dict) -> dict:
    """Compact node view for diff output: role/name/center + reported attrs
    that are present. Keeps the summary cheap to read."""
    out = {"role": node.get("role"), "name": node.get("name"),
           "x": node.get("x"), "y": node.get("y")}
    for k in ("disabled", "checked", "href", "type", "frame"):
        if node.get(k) is not None:
            out[k] = node.get(k)
    return out


def diff_snapshot(before, after=None, *, max_items: int = 40, bucket: int = 32):
    """Did my action change the page? Cheap post-action verification: diff two
    ``snapshot()`` results and report what appeared, disappeared, or changed.

    Stateless by design — there is no stored "last snapshot". You pass the
    prior snapshot explicitly::

        before = snapshot()
        click_at_xy(x, y)
        diff_snapshot(before)            # fresh snapshot() taken internally
        # or diff_snapshot(before, after) with an explicit second snapshot

    **Compare like for like.** When ``after`` is omitted, the internal snapshot
    uses *default* args (``interactive_only=True``, ``max_nodes=120``, no
    ``scope``). If you captured ``before`` with non-default args (e.g.
    ``snapshot(interactive_only=False)`` or a ``scope``), pass an explicit
    ``after=snapshot(<same args>)`` — otherwise the two sides cover different
    node sets and the diff reports spurious added/removed nodes.

    Node identity for matching across the two snapshots is
    ``role + accessible name + a coarse position bucket`` (default 32px). The
    role+name pair is the semantic anchor; the position bucket only
    disambiguates several same-role/same-name nodes (e.g. repeated "Add"
    buttons) — it is deliberately coarse so a small relayout is reported as a
    *moved/changed* node rather than a remove+add pair.

    Buckets:
      added   — identity present in ``after`` but not ``before``.
      removed — identity present in ``before`` but not ``after``.
      changed — same identity, but a reported attribute differs
                (``disabled`` / ``checked`` / ``name`` / ``href`` / ``type`` /
                ``frame``) or the center moved more than ~24px.
      unchanged — count of stable, attribute-identical nodes.

    Args:
      before: a dict previously returned by ``snapshot()``.
      after: a second ``snapshot()`` dict; if None, a fresh ``snapshot()`` is
        taken now (the common verify-after-action case).
      max_items: cap on entries in each of added/removed/changed (bounds token
        cost; the counts in ``summary`` are not capped).
      bucket: position-bucket size in px for identity disambiguation.

    Returns a dict: ``added[]`` (slim nodes), ``removed[]`` (slim nodes),
    ``changed[]`` ({role,name,x,y, changes:{attr:[old,new]}, moved?}),
    ``unchanged`` (int), and ``summary`` ("N added, M removed, K changed").

    Limits: identity collides when several nodes truly share role+name within
    the same position bucket (they net out by count but individual matching is
    arbitrary). Inherits ``snapshot()``'s scope: viewport-visible nodes only,
    same-origin iframes one level, no shadow DOM / canvas.
    """
    if after is None:
        after = snapshot()

    before_nodes = (before or {}).get("nodes", []) if isinstance(before, dict) else []
    after_nodes = (after or {}).get("nodes", []) if isinstance(after, dict) else []

    # Build identity -> list of nodes (lists handle duplicate identities).
    def index(nodes):
        idx: dict[tuple, list] = {}
        for n in nodes:
            idx.setdefault(_diff_identity(n, bucket=bucket), []).append(n)
        return idx

    bi = index(before_nodes)
    ai = index(after_nodes)

    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []
    unchanged = 0

    all_ids = set(bi) | set(ai)
    for ident in all_ids:
        b_list = bi.get(ident, [])
        a_list = ai.get(ident, [])
        # Pair up min(len) nodes of this identity; surplus is added/removed.
        paired = min(len(b_list), len(a_list))
        for i in range(paired):
            bn, an = b_list[i], a_list[i]
            ba, aa = _node_attrs(bn), _node_attrs(an)
            changes = {k: [ba[k], aa[k]] for k in _DIFF_ATTRS if ba[k] != aa[k]}
            moved = False
            bx, by = bn.get("x"), bn.get("y")
            ax, ay = an.get("x"), an.get("y")
            if all(isinstance(v, (int, float)) for v in (bx, by, ax, ay)):
                if max(abs(ax - bx), abs(ay - by)) > _DIFF_MOVE_PX:
                    moved = True
            if changes or moved:
                entry = {"role": an.get("role"), "name": an.get("name"),
                         "x": an.get("x"), "y": an.get("y")}
                if changes:
                    entry["changes"] = changes
                if moved:
                    entry["moved"] = [[bx, by], [ax, ay]]
                changed.append(entry)
            else:
                unchanged += 1
        # Surplus after-nodes = added; surplus before-nodes = removed.
        for an in a_list[paired:]:
            added.append(_slim(an))
        for bn in b_list[paired:]:
            removed.append(_slim(bn))

    n_added, n_removed, n_changed = len(added), len(removed), len(changed)
    summary = f"{n_added} added, {n_removed} removed, {n_changed} changed"

    return {
        "added": added[:max_items],
        "removed": removed[:max_items],
        "changed": changed[:max_items],
        "unchanged": unchanged,
        "summary": summary,
    }
