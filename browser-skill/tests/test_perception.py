"""S1 gate: the two perception primitives (``snapshot`` / ``describe_page``)
are promoted into the core package and behave correctly against a real DOM.

Two layers of assertion:

1. **Surface contract (offline, always runs).** ``snapshot`` and
   ``describe_page`` are in ``browser_skill.EXPORTS``, importable from the
   top-level namespace, and appear in the assembled REPL globals. This is
   what makes them free names inside an agent heredoc.

2. **Behaviour (live, needs a Chrome/Chromium binary).** We launch a
   headless Chromium with a remote-debugging port, drive it through the
   skill's *own* CDP transport (so we exercise the real ``js()`` ->
   ``Runtime.evaluate`` round-trip the agent uses), inject a controlled DOM,
   and assert on the *shape* of the digests — never on site/selector/class
   strings. The extension backend ignores ``goto_url("data:...")`` (see the
   improvements report), so the test page is built by in-page injection.

The behaviour layer skips cleanly when no Chromium binary is discoverable,
keeping the surface gate runnable offline.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Surface contract — offline, deterministic, the durable regression core.
# ---------------------------------------------------------------------------


def test_primitives_in_exports():
    import browser_skill

    assert "snapshot" in browser_skill.EXPORTS
    assert "describe_page" in browser_skill.EXPORTS


def test_primitives_importable_from_namespace():
    from browser_skill import describe_page, snapshot  # noqa: F401

    assert callable(snapshot)
    assert callable(describe_page)


def test_primitives_in_repl_globals(tmp_bs_home):
    """They must land in the exec globals an agent heredoc runs against —
    that is the definition of 'in core' for this stack."""
    from browser_skill.repl._namespace import build_globals

    g = build_globals()
    assert "snapshot" in g and callable(g["snapshot"])
    assert "describe_page" in g and callable(g["describe_page"])


def test_describe_page_accepts_viewport_only_kwarg():
    """The new S1 mode must exist as a real keyword (not silently ignored)."""
    import inspect as _inspect

    from browser_skill import describe_page

    sig = _inspect.signature(describe_page)
    assert "viewport_only" in sig.parameters


# ---------------------------------------------------------------------------
# Behaviour — live headless Chromium driven through the skill's CDP transport.
# ---------------------------------------------------------------------------


def _find_chromium() -> str | None:
    """Best-effort discovery of a Chromium-family binary for headless CDP.

    Prefers a Playwright full Chromium (renders gradients / blend / pseudo),
    falls back to system Google Chrome, then anything on PATH. Returns None
    when nothing is found — the live tests then skip.
    """
    pw = Path.home() / "Library/Caches/ms-playwright"
    cands: list[Path] = []
    if pw.exists():
        # Newest chromium-<rev> first.
        for d in sorted(pw.glob("chromium-*"), reverse=True):
            cands += list(d.glob("chrome-*/Chromium.app/Contents/MacOS/Chromium"))
            cands += list(d.glob("chrome-*/chrome"))  # linux layout
    cands += [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for c in cands:
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _browser_ws_url(port: int, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1.0
            ) as r:
                return json.loads(r.read())["webSocketDebuggerUrl"]
        except (urllib.error.URLError, OSError, KeyError) as e:  # noqa: PERF203
            last = e
            time.sleep(0.1)
    raise RuntimeError(f"chromium devtools never came up on {port}: {last}")


@pytest.fixture(scope="module")
def live_session(tmp_path_factory):
    """A skill ``Session`` attached to a freshly-launched headless Chromium,
    with the current target pointing at a real page. Yields the session;
    tears down both the CDP transport and the browser process.
    """
    binary = _find_chromium()
    if binary is None:
        pytest.skip("no Chromium-family binary found for live CDP test")

    port = _free_port()
    profile = tmp_path_factory.mktemp("chrome-profile")
    proc = subprocess.Popen(
        [
            binary,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws = _browser_ws_url(port)
    except Exception:
        proc.terminate()
        pytest.skip("chromium failed to expose a devtools endpoint")

    from browser_skill.cdp import CDPSession
    from browser_skill.session import Session

    sess = Session(daemon=object())
    sess._cdp = CDPSession(ws)
    sess._owns_cdp = True
    sess._backend_name_cache = "rdp"

    # Find (or create) a real page target and make it current.
    targets = sess._cdp.send("Target.getTargets")["targetInfos"]
    page = next((t for t in targets if t["type"] == "page"), None)
    if page is None:
        tid = sess._cdp.send("Target.createTarget", url="about:blank")["targetId"]
    else:
        tid = page["targetId"]
    sess.current_target_id = tid

    from browser_skill.session import with_session

    with with_session(sess):
        yield sess

    try:
        sess.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


# A controlled DOM with the three style signals the S1 gate names, plus an
# off-screen styled node placed far below the viewport. No site/class/selector
# from any real page — pure injection. We give the gradient/blend nodes large
# on-screen rects so they survive the salience min-area filter.
_INJECT = r"""
return (function(){
  var st = document.createElement('style');
  st.textContent =
    ":root{--gate-accent:#ff0066;--gate-space:12px}" +
    "body{margin:0}" +
    "body::before{content:'';position:fixed;inset:0;" +
      "background-image:linear-gradient(45deg,#102040,#a0c0ff);z-index:0}" +
    "#gate-blend{position:fixed;top:0;left:0;width:60vw;height:60vh;" +
      "mix-blend-mode:multiply;background:#3366ff;z-index:5}" +
    "#gate-off{position:absolute;top:8000px;left:0;width:50vw;height:400px;" +
      "background-image:radial-gradient(#0f0,#00f);mix-blend-mode:screen}";
  document.head.appendChild(st);

  var blend = document.createElement('div'); blend.id='gate-blend';
  document.body.appendChild(blend);

  // Off-screen style-bearing node (8000px down): the full scan should rank it,
  // viewport_only should drop it.
  var off = document.createElement('div'); off.id='gate-off';
  document.body.appendChild(off);

  // Interactive nodes for snapshot(): a button + a link, both on-screen.
  var b = document.createElement('button');
  b.textContent='Submit'; b.style.cssText='position:fixed;top:100px;left:40px';
  document.body.appendChild(b);
  var a = document.createElement('a');
  a.textContent='Docs'; a.href='https://example.org/docs';
  a.style.cssText='position:fixed;top:160px;left:40px';
  document.body.appendChild(a);
  return true;
})();
"""


@pytest.fixture
def injected(live_session):
    from browser_skill import js

    # Reset to a clean about:blank-ish DOM each test, then inject.
    js("document.documentElement.innerHTML = '<head></head><body></body>';")
    js(_INJECT)
    return live_session


def test_describe_page_detects_injected_style_signals(injected):
    """describe_page() must surface a body::before gradient, a mix-blend-mode
    node, and a :root CSS variable. Asserted by *shape*, not by class name."""
    from browser_skill import describe_page

    d = describe_page()
    assert isinstance(d, dict)

    # 1. body::before gradient — reported under root.bodyBefore.
    before = (d.get("root") or {}).get("bodyBefore")
    assert before, "expected a body::before pseudo to be reported"
    assert "gradient" in (before.get("backgroundImage") or "").lower()

    # 2. mix-blend-mode somewhere among ranked nodes.
    blends = [n for n in d["nodes"] if n.get("mixBlendMode")]
    assert blends, "expected at least one node carrying mix-blend-mode"
    assert any(n["mixBlendMode"] != "normal" for n in blends)

    # 3. :root CSS variable surfaced (we don't assert the value's exact text).
    assert d.get("cssVarCount", 0) >= 1
    assert any(k.startswith("--") for k in (d.get("cssVars") or {}))


def test_describe_page_viewport_only_excludes_offscreen(injected):
    """The full scan should include the off-screen gradient/blend node;
    viewport_only=True should exclude it. Compared by node count + presence
    of an off-screen blend node, not by id/class."""
    from browser_skill import describe_page, js

    # Sanity: the off-screen node is genuinely below the viewport.
    off_top = js(
        "var e=document.getElementById('gate-off');"
        "return e?e.getBoundingClientRect().top:null;"
    )
    assert off_top is not None and off_top > js("return window.innerHeight;")

    full = describe_page()
    vp = describe_page(viewport_only=True)

    def offscreen_blend_nodes(d):
        out = []
        for n in d["nodes"]:
            rect = n.get("rect") or {}
            # below the viewport bottom and carries a style signal
            if rect.get("y", 0) >= d["viewport"]["h"] and (
                n.get("mixBlendMode") or n.get("backgroundImage")
            ):
                out.append(n)
        return out

    assert offscreen_blend_nodes(full), (
        "full scan should rank the off-screen style-bearing node"
    )
    assert not offscreen_blend_nodes(vp), (
        "viewport_only must drop nodes that don't intersect the viewport"
    )
    # And it must not be an empty result — on-screen signals still present.
    assert any(n.get("mixBlendMode") for n in vp["nodes"])


def test_snapshot_returns_interactive_nodes_with_center_xy(injected):
    """snapshot() returns interactive nodes; each carries a numeric center
    (x, y) suitable for click_at_xy. Shape-only — no name-string matching."""
    from browser_skill import snapshot

    s = snapshot()
    assert isinstance(s, dict)
    assert s["viewport"]["w"] > 0 and s["viewport"]["h"] > 0
    nodes = s["nodes"]
    assert nodes, "expected interactive nodes (a button + a link were injected)"

    for n in nodes:
        assert isinstance(n.get("x"), (int, float))
        assert isinstance(n.get("y"), (int, float))
        assert "role" in n

    roles = {n["role"] for n in nodes}
    assert "button" in roles
    assert "link" in roles
