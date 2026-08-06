"""ZER-123 regression coverage.

Bug: with chrome.debugger attached by the extension backend, Chromium can pause
new child targets (notably OOPIFs) until the debugger participates in
auto-attach and resumes them. Without that protocol, a page with a cross-site
iframe renders its main document but never fires load, so Playwright
`page.goto(..., wait_until="load")` stays pending and Chrome keeps spinning.

The regression intentionally does NOT navigate a same-site iframe first. That
would warm Chrome's first debugger/page path and hide the cold-start bug. The
first Playwright page in a fresh Chrome fixture must go directly to OOPIF.

Run:
    bash tests/daemon/e2e/run.sh tests/daemon/e2e/test_repro_zer123.py -v -s
"""
from __future__ import annotations

import http.server
import socketserver
import threading
import time

import pytest

# Reuse the facade-over-extension fixtures (session daemon on the per-worktree
# relay port + facade
# on conftest.TEST_EXT_FACADE_PORT, CfT chrome with the patched extension).
from .test_l1_playwright_facade_extension import (  # noqa: F401
    e2e_ext_facade_daemon,
    ext_facade_ready,
)

GOTO_TIMEOUT_MS = 20_000


class _PageHandler(http.server.BaseHTTPRequestHandler):
    """Serves parent/child pages; records every request path as evidence."""

    server_port: int = 0
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        port = type(self).server_port
        if self.path.startswith("/parent-oopif"):
            # Parent fetched via localhost, child fetched via a real cross-site
            # origin. Loopback host variations are not reliable OOPIF triggers
            # on current Chrome.
            body = ("<title>parent-oopif</title><h1>parent</h1>"
                    '<iframe src="https://example.com/favicon.ico"></iframe>')
        elif self.path.startswith("/parent-samesite"):
            # Child also via localhost => same site => in-process iframe.
            body = ("<title>parent-samesite</title><h1>parent</h1>"
                    f'<iframe src="http://localhost:{port}/child-samesite">'
                    "</iframe>")
        elif self.path.startswith("/child"):
            body = "<title>child</title><p>child content</p>"
        elif self.path.startswith("/plain"):
            body = f"<title>plain-{self.path}</title><h1>plain page</h1>"
        else:
            body = "<title>ok</title>"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence per-request stderr noise
        pass


@pytest.fixture
def page_server():
    """Local parent-page server; the OOPIF child uses a real cross-site URL."""
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", 0), _PageHandler) as server:
        port = server.server_address[1]
        _PageHandler.server_port = port
        _PageHandler.hits = []
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            yield port
        finally:
            server.shutdown()


def _connect(p, facade_port: int):
    return p.chromium.connect_over_cdp(
        f"ws://127.0.0.1:{facade_port}/cdp", timeout=20_000)


def test_first_page_oopif_iframe_goto_load_returns(ext_facade_ready, page_server):
    """Fresh Chrome first Playwright page with cross-site OOPIF fires load."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_api.sync_playwright
    PWTimeout = playwright_api.TimeoutError

    _ext_port, facade_port, _rt = ext_facade_ready
    port = page_server

    with sync_playwright() as p:
        browser = _connect(p, facade_port)
        try:
            ctx = (browser.contexts[0] if browser.contexts
                   else browser.new_context())

            page = ctx.new_page()
            t0 = time.monotonic()
            timed_out = False
            try:
                page.goto(f"http://localhost:{port}/parent-oopif",
                          wait_until="load", timeout=GOTO_TIMEOUT_MS)
            except PWTimeout:
                timed_out = True
            oopif_s = time.monotonic() - t0

            ready_state = page.evaluate("document.readyState")
            h1 = page.evaluate("document.querySelector('h1')?.textContent")
            iframe_src = page.evaluate("document.querySelector('iframe')?.src")

            print(f"[repro]  cross-site iframe: goto "
                  f"returned after {oopif_s:.1f}s; "
                  f"readyState={ready_state!r}, h1={h1!r}, "
                  f"iframe={iframe_src!r}")
            print(f"[server] request log: {_PageHandler.hits}")

            assert not timed_out, (
                "OOPIF page rendered but load did not complete; "
                f"readyState={ready_state!r}, iframe={iframe_src!r}, "
                f"hits={_PageHandler.hits}")
            assert ready_state == "complete"
            assert h1 == "parent"
            assert iframe_src == "https://example.com/favicon.ico"
        finally:
            browser.close()
