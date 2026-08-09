"""The in-page half of the markdown pipeline: normalize the live DOM to HTML.

This exists because ``page.content()`` silently loses two things Markdown needs,
and neither can be recovered on the Python side (ADR-0007):

- **open shadow roots are not serialized at all.** Verified in this repo: a page
  with a declarative ``<template shadowrootmode="open">`` reports ``shadowRoot``
  present, in-page JS reads its text fine, and the string from
  ``page.content()`` does not contain it.
- **relative URLs stay relative.** No Python converter resolves ``<base>``. The
  browser resolves per document — including separately inside each iframe.

So this script runs where the DOM is, rebuilds a detached copy with those two
problems fixed, and hands back plain HTML strings.

Why ``page.evaluate`` and not ``add_script_tag``
------------------------------------------------
Verified in this repo against ``default-src 'none'; script-src 'none'`` with
``bypass_csp=False``: the page's own inline ``<script>`` does NOT run (CSP is
being enforced), while ``page.evaluate("new Function('return 41+1')()")``
returns ``42`` and ``add_script_tag`` raises. CDP's ``Runtime.evaluate`` carries
``allowUnsafeEvalBlockedByCSP``, so protocol-driven evaluation is exempt where
page-driven script injection is not.

Three consequences the script below obeys:

- **Stay synchronous.** The CSP exemption is a toggle held for the duration of
  the protocol call; a continuation scheduled from inside it (``setTimeout``,
  ``await``) loses it. Everything here is one synchronous pass.
- **Never assign ``innerHTML``, never use ``DOMParser``.** Those are gated by
  Trusted Types, which ``page.evaluate`` does NOT bypass — it runs in the main
  world. *Reading* ``innerHTML``/``outerHTML`` and calling ``cloneNode`` /
  ``importNode`` are unaffected, which is why the copy is built with node APIs
  and HTML is only ever read out.
- **Never mutate the live page.** Everything is built detached, and Readability
  (which rewrites whatever document it is handed) only ever sees a scratch
  document.

And do not "fix" any of this by turning on ``bypass_csp``: that lets the page's
previously-blocked inline scripts run and stops Trusted Types being enforced,
i.e. it changes the very DOM we are here to capture.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent / "vendor"


@lru_cache(maxsize=1)
def _readability_source() -> str:
    """Mozilla Readability 0.6.0, vendored. See ``vendor/README.md``."""
    return (_VENDOR / "readability.js").read_text(encoding="utf-8")


# The body of the injected function. Assembled with the Readability source in
# `build_script()` below, because the extraction pass needs it in scope.
_NORMALIZE_BODY = r"""
  // Elements dropped wholesale: no readable content, pure token cost.
  // `svg`/`canvas` are decorative here - when they carry an accessible name it
  // lives on an ancestor, which survives. `template` is author-declared inert
  // content; declarative shadow DOM is consumed by the parser long before this
  // runs and surfaces as a real shadowRoot, handled below.
  const DROP = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS",
    "LINK", "META", "BASE",
  ]);
  // Attributes whose value is a URL we want absolute in the output.
  const URL_ATTRS = { A: "href", AREA: "href", IMG: "src", SOURCE: "src" };
  const MAX_DEPTH = 128;

  const stats = {
    shadowRoots: 0,
    sameOriginFrames: 0,
    crossOriginFrames: 0,
    linksTotal: 0,
  };

  const isHidden = (el) => {
    if (el.hasAttribute("hidden")) return true;
    if (el.getAttribute("aria-hidden") === "true") return true;
    return false;
  };

  // Resolve against the *owning* document's base URI, which differs per frame.
  // Reading the IDL property (`a.href`) would resolve nodes imported from an
  // iframe against the top document instead, so resolve explicitly.
  const absolutize = (src, clone, baseURI) => {
    const attr = URL_ATTRS[src.nodeName.toUpperCase()];
    if (!attr) return;
    const raw = src.getAttribute(attr);
    if (!raw) return;
    if (attr === "href") stats.linksTotal += 1;
    try {
      clone.setAttribute(attr, new URL(raw, baseURI).href);
    } catch (e) {
      /* mailto:, javascript:, malformed - keep the author's value */
    }
  };

  // Build a detached copy of `node`. Returns null when the node is dropped.
  const build = (node, baseURI, depth) => {
    if (depth > MAX_DEPTH) return null;

    if (node.nodeType === Node.TEXT_NODE) {
      return node.data.trim() ? node.cloneNode(false) : null;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;

    const name = node.nodeName.toUpperCase();
    if (DROP.has(name)) return null;
    if (isHidden(node)) return null;

    if (name === "IFRAME") {
      // A null contentDocument IS the same-origin test. Sandboxed frames can
      // throw rather than return null, hence the guard.
      let doc = null;
      try { doc = node.contentDocument; } catch (e) { doc = null; }
      if (!doc || !doc.body) {
        stats.crossOriginFrames += 1;
        return null;
      }
      stats.sameOriginFrames += 1;
      // Inline the frame body at the iframe's position, wrapped so the
      // converter sees a block boundary instead of splicing the frame's
      // paragraphs into the parent's.
      const holder = document.createElement("div");
      for (const child of Array.from(doc.body.childNodes)) {
        const built = build(child, doc.baseURI, depth + 1);
        if (built) holder.appendChild(built);
      }
      return holder.childNodes.length ? holder : null;
    }

    // Shallow clone keeps attributes; children are rebuilt so we control
    // shadow/slot/iframe expansion.
    const clone = node.cloneNode(false);
    absolutize(node, clone, baseURI);

    // A SLOT renders its assigned nodes. Expanding it here is what makes a
    // flattened shadow tree come out in composed-tree order rather than
    // shadow-then-light.
    if (name === "SLOT" && typeof node.assignedNodes === "function") {
      const holder = document.createElement("div");
      for (const child of node.assignedNodes({ flatten: true })) {
        const built = build(child, baseURI, depth + 1);
        if (built) holder.appendChild(built);
      }
      return holder.childNodes.length ? holder : null;
    }

    // Open shadow root: its content is what the user actually sees, and the
    // light children are reached through the slots inside it. A CLOSED root is
    // indistinguishable from no root here - `shadowRoot` is null either way -
    // so closed roots are invisible to every approach, ours included, and we
    // cannot even report their number. See ADR-0007.
    const root = node.shadowRoot;
    if (root) {
      stats.shadowRoots += 1;
      for (const child of Array.from(root.childNodes)) {
        const built = build(child, baseURI, depth + 1);
        if (built) clone.appendChild(built);
      }
      return clone;
    }

    for (const child of Array.from(node.childNodes)) {
      const built = build(child, baseURI, depth + 1);
      if (built) clone.appendChild(built);
    }
    return clone;
  };

  // A scratch document, so Readability has something it is allowed to destroy
  // and so the normalized tree is a real document rather than a loose node.
  const scratch = document.implementation.createHTMLDocument(document.title || "");
  const normalized = document.body ? build(document.body, document.baseURI, 0) : null;
  if (normalized) {
    for (const child of Array.from(normalized.childNodes)) {
      scratch.body.appendChild(scratch.importNode(child, true));
    }
  }

  const result = {
    fullHtml: scratch.body.innerHTML,
    articleHtml: null,
    articleLinks: 0,
    title: document.title || "",
    url: location.href,
    contentType: document.contentType || "",
    stats: stats,
  };

  if (opts && opts.extract) {
    // Readability rewrites the document it is given, so hand it a copy of the
    // scratch document - never `scratch` itself, which we still need above.
    try {
      const forReader = scratch.cloneNode(true);
      const article = new Readability(forReader).parse();
      if (article && article.content) {
        result.articleHtml = article.content;
        // Counted here rather than in Python: the caller's collapse test is
        // about the DOM that came out, and re-parsing markdown to count links
        // would measure the converter instead.
        const probe = document.implementation.createHTMLDocument("");
        probe.body.appendChild(probe.importNode(forReader.body, true));
        result.articleLinks = probe.querySelectorAll("a[href]").length;
      }
    } catch (e) {
      result.articleHtml = null;   // treated as a collapse by the caller
    }
  }

  return result;
"""


def build_script(*, extract: bool) -> str:
    """Assemble the full ``page.evaluate`` payload.

    Returns the source of a single arrow function taking one options object.
    The Readability source is inlined only when extraction is requested — it is
    ~91 KB, and shipping it across the CDP boundary on every full-page read
    would be pure overhead.
    """
    prelude = ""
    if extract:
        # Readability ends with `if (typeof module === "object") module.exports
        # = Readability;`. In a page that leaked a bundler's `module` global
        # (webpack/browserify do), that assignment would mutate the page. A
        # local binding shadows it so the branch is never taken.
        prelude = "  var module = void 0;\n" + _readability_source() + "\n"
    return "(opts) => {\n" + prelude + _NORMALIZE_BODY + "\n}"
