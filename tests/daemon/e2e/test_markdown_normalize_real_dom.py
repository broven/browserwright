"""The in-page normalizer, against a real browser (ADR-0007 rule 1).

This is the only place the injected JS is actually executed, and it has to be:
every property it exists for — shadow-root flattening, per-frame URL
resolution, CSP survival — is a *browser behaviour*, not something a fake page
can assert. A unit test here would only be testing the fake.

Lives under `tests/daemon/e2e/` per TESTING.md: it needs a real browser, so it
is auto-marked `real_chrome` and stays out of the default gate. It needs neither
the daemon nor the extension — plain Playwright chromium plus a local server.
"""
from __future__ import annotations

import http.server
import threading

import pytest

from browserwright.repl._md_convert import convert_html
from browserwright.repl._md_normalize import build_script

_MAIN = b"""<!doctype html><html><head><title>Doc Title</title>
<style>b{color:red}</style></head><body>
<nav><a href="/nav">NAVLINK</a></nav>
<div id="host"><template shadowrootmode="open">
  <h2>SHADOWHEADING</h2><a href="/in-shadow">SHADOWLINK</a><slot></slot>
</template><p>SLOTTEDTEXT</p></div>
<article><h1>Real Article</h1>
<p><a href="rel/page?x=1">RELLINK</a> and <a href="https://example.com/abs">ABSLINK</a></p>
<pre><code class="language-python">print("hi")</code></pre></article>
<iframe src="/frame"></iframe>
<iframe src="http://127.0.0.1:1/cross"></iframe>
<p hidden>HIDDENTEXT</p><p aria-hidden="true">ARIAHIDDENTEXT</p>
<script>window.__ran = true;</script></body></html>"""

_FRAME = b"""<!doctype html><html><body><p>FRAMEBODY</p>
<a href="deep/inner">FRAMELINK</a></body></html>"""

# `script-src 'none'` blocks the page's own inline script while leaving
# `page.evaluate` exempt — the asymmetry the whole design rests on.
_CSP = "default-src 'none'; script-src 'none'; style-src 'none'"


class _Handler(http.server.BaseHTTPRequestHandler):
    csp = False

    def do_GET(self):
        body = _FRAME if self.path.startswith("/frame") else _MAIN
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if type(self).csp:
            self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def serve():
    def _serve(*, csp: bool = False) -> str:
        handler = type("H", (_Handler,), {"csp": csp})
        srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}/"

    servers: list[http.server.HTTPServer] = []
    yield _serve
    for s in servers:
        s.shutdown()


@pytest.fixture
def render(serve):
    """(markdown, raw, base_url) for a freshly loaded page."""
    from playwright.sync_api import sync_playwright

    def _render(*, csp: bool = False, extract: bool = False):
        base = serve(csp=csp)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                # bypass_csp stays OFF on purpose: turning it on would let the
                # page's blocked scripts run and stop Trusted Types being
                # enforced, i.e. change the DOM we are here to capture.
                page = browser.new_context().new_page()
                page.goto(base)
                before = page.evaluate("document.body.innerHTML")
                raw = page.evaluate(build_script(extract=extract),
                                    {"extract": extract})
                after = page.evaluate("document.body.innerHTML")
                assert before == after, "normalization mutated the live page"
            finally:
                browser.close()
        return convert_html(raw["fullHtml"]), raw, base

    return _render


def test_full_page_recovers_everything_page_content_loses(render):
    md, raw, base = render()

    # Shadow DOM, in composed-tree order: the slot's assigned light content has
    # to come through where the <slot> sits, not before or after the shadow.
    assert "SHADOWHEADING" in md
    assert "SHADOWLINK" in md
    assert "SLOTTEDTEXT" in md
    assert md.index("SHADOWHEADING") < md.index("SLOTTEDTEXT")
    assert raw["stats"]["shadowRoots"] == 1

    # Same-origin iframe inlined; its relative link resolved against the
    # FRAME's base URI, which is the part no Python-side converter can do.
    assert "FRAMEBODY" in md
    assert f"{base}deep/inner" in md
    assert raw["stats"]["sameOriginFrames"] == 1

    # Cross-origin iframe excluded, and counted so it can be announced.
    assert raw["stats"]["crossOriginFrames"] == 1

    # URLs absolute; author-absolute URLs untouched.
    assert f"{base}rel/page?x=1" in md
    assert "https://example.com/abs" in md

    # Noise gone.
    assert "window.__ran" not in md
    assert "color:red" not in md
    assert "HIDDENTEXT" not in md
    assert "ARIAHIDDENTEXT" not in md

    # Verbatim really is verbatim: nav survives (the converter's own default
    # would have deleted it).
    assert "NAVLINK" in md

    # Code fence keeps its language.
    assert "```python" in md


def test_strict_csp_does_not_block_the_normalizer(render):
    """`page.evaluate` is exempt from page CSP (CDP passes
    `allowUnsafeEvalBlockedByCSP`); `add_script_tag` is not, which is why the
    normalizer never uses it. If this ever fails, the exemption changed and the
    whole in-page design needs revisiting — not a `bypass_csp=True` patch."""
    md, raw, base = render(csp=True)
    assert "SHADOWHEADING" in md
    assert f"{base}rel/page?x=1" in md
    assert raw["stats"]["shadowRoots"] == 1


def test_extraction_runs_without_damaging_the_full_rendering(render):
    """Readability rewrites whatever document it is handed, so it must only see
    a copy — twice over: the live page (asserted inside `render`) and the
    scratch document the full rendering is read from.

    Note what is deliberately NOT asserted: that extraction dropped the nav. On
    a page this small Readability falls back to returning almost everything
    ("Ruthless and lenient parsing did not work. Returning raw html" in its own
    source), which is real behaviour, not a bug — and is exactly why the
    collapse gate in `render_page_markdown` judges the *result* rather than
    trusting the extractor. Extraction quality is judged there, on fixtures
    sized for it; this test is about wiring.
    """
    md, raw, base = render(extract=True)
    assert raw["articleHtml"], "extraction produced nothing on an article page"
    assert "Real Article" in convert_html(raw["articleHtml"])
    # The full rendering, read from the same scratch document, is untouched.
    assert "NAVLINK" in md
    assert "SHADOWHEADING" in md
    assert f"{base}deep/inner" in md
