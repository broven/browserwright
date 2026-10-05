"""Issue #116 / #120: a slow-committing `goto` must not die on an inner budget.

The extension used to cap every `chrome.debugger.sendCommand` at a fixed
9000ms, and the relay at 10.0s. `Page.navigate` does not answer until the
navigation commits, so a page whose first byte arrives after 12s failed at 9s
with `PageLoadFailed(extension-budget)` even though `goto` had 60s and the
call had 90s. ADR-0014: the caller's remaining call deadline is propagated
executor -> daemon relay -> extension -> chrome.debugger, so no inner layer
can expire first.

This drives the issue's reproduction verbatim on the isolated harness (test
daemon + Chrome for Testing with the patched extension), with a local server
whose `/slow` path answers after 12s — never a third-party site, never the
developer's daily Chrome.
"""
from __future__ import annotations

import http.server
import threading
import time

import pytest

from .helpers import run_skill

#: How long `/slow` holds its first byte. Above the old 9s/10s budgets, well
#: below the default 60s smart-goto timeout and the 90s call deadline.
SLOW_COMMIT_S = 12.0

_BODY = b"<!doctype html><title>slow</title><h1 id=v>ok</h1>"


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/slow"):
            time.sleep(SLOW_COMMIT_S)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_BODY)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(_BODY)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser gave up on this request; the test reports why

    def log_message(self, *_a):
        pass


@pytest.fixture
def slow_site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


_SCRIPT = r'''
import time
t0 = time.time()
page.goto("__URL__")
print("BWGOTO ms=%d url=%s text=%s" % (
    int((time.time() - t0) * 1000), page.url, page.inner_text("#v")))
'''


def test_goto_on_a_slow_commit_page_returns_under_the_default_deadline(
    ext_ready, e2e_daemon, slow_site,
):
    url = f"{slow_site}/slow"
    result = run_skill(
        _SCRIPT.replace("__URL__", url),
        backend="extension",
        runtime_dir=e2e_daemon.runtime_dir,
        timeout=120.0,
    )

    assert "extension-budget" not in result.stderr, (
        "issue #116 regression — an inner fixed budget killed an in-deadline "
        f"navigation:\n{result.stderr[-3000:]}")
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-3000:]}")
    line = next((ln for ln in result.stdout.splitlines()
                 if ln.startswith("BWGOTO ")), None)
    assert line is not None, f"no BWGOTO line:\n{result.stdout[-2000:]}"
    fields = dict(part.split("=", 1) for part in line.split()[1:])
    assert fields["url"] == url
    assert fields["text"] == "ok"
    # It really waited for the slow commit, rather than racing past it.
    assert int(fields["ms"]) >= SLOW_COMMIT_S * 1000 - 500, line
