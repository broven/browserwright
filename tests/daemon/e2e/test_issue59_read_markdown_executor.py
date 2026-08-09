"""Issue #59: `read_markdown()` inside the RESIDENT EXECUTOR, end to end.

The sibling file `test_markdown_command_extension.py` covers the one-shot
`browserwright markdown <url>` command — and it stayed green for the whole life
of the bug, because that surface never goes through `ExecutorProcess`. The
executor is a *different composition*: it owns a live Playwright driver on its
worker thread, so a namespace entry left bound to `_namespace`'s lazy
`PlaywrightHandle` re-enters `sync_playwright()` inside a running asyncio loop
and every call raises "Playwright Sync API inside the asyncio loop".

So this is the one place the actual reported repro exists: a real
`browserwright -s <sid>` heredoc, a real resident executor, a real page.

Uses the isolated harness — test daemon on its own socket + Chrome for Testing
with the patched unpacked extension — so it never touches the daily Chrome.
"""
from __future__ import annotations

import http.server
import threading

import pytest

from .helpers import run_skill

_PAGE = b"""<!doctype html><html><head><title>Issue 59</title></head><body>
<nav><a href="/nav">NAVLINK</a></nav>
<article><h1>Executor Content View</h1>
<p>Body paragraph with <a href="rel/deep?q=1">RELLINK</a> inside it.</p>
</article></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # A real served response, not a `data:` URL — the extension backend
        # cannot navigate to `data:` at all.
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *a):
        pass


@pytest.fixture
def page_url():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()


def test_read_markdown_works_inside_the_resident_executor(
    ext_ready, e2e_daemon, page_url,
):
    """The issue #59 repro verbatim: goto, then read the page as Markdown."""
    result = run_skill(
        f'page.goto("{page_url}")\nprint(read_markdown(mode="full"))',
        backend="extension",
        runtime_dir=e2e_daemon.runtime_dir,
        timeout=120.0,
    )

    assert "asyncio loop" not in result.stderr, (
        "issue #59 regression — read_markdown resolved the lazy "
        f"PlaywrightHandle inside the executor:\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    md = result.stdout
    assert "# Executor Content View" in md
    assert "NAVLINK" in md, "mode='full' must keep the nav"
    # Absolute links — the property that makes this view worth having over
    # `inner_text()`, and proof the render ran against the navigated page.
    assert f"{page_url}rel/deep?q=1" in md


def test_read_markdown_notes_ride_the_executor_warning_channel(
    ext_ready, e2e_daemon, page_url,
):
    """ADR-0007's out-of-band notes must reach the client, not the body.

    This is the half of the fix a live-page rebinding alone would drop: the
    view is handed the executor's PER-CALL globals so it can find the `_bw_warn`
    channel injected into that same dict. That the note lands on the *client's*
    stderr is the proof — `_emit`'s fallback prints to the stderr of the
    executor PROCESS, which goes to the daemon log and never reaches here. Only
    a note that rode `_bw_warn` into the response comes out as a `[WARNING]`
    line (`cli.py`). Bind the live page but forget the namespace and this test
    fails while the one above passes.
    """
    # Below the page's own markdown length, so the truncation note fires too.
    result = run_skill(
        f'page.goto("{page_url}")\nprint(read_markdown(max_chars=40))',
        backend="extension",
        runtime_dir=e2e_daemon.runtime_dir,
        timeout=120.0,
    )

    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert "[WARNING] read_markdown:" in result.stderr, (
        f"notes never reached the executor warning channel:\n{result.stderr}")
    assert "output truncated" in result.stderr
    # ...and never into the markdown itself.
    assert "output truncated" not in result.stdout
