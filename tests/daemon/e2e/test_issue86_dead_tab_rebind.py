"""Issue #86 (extension backend, real Chrome): a session whose tab dies rebinds.

The field shape was one navigation ending with the session's tab gone — after
which EVERY later call in that session, `example.com` included, failed in 1-4ms
with `TargetClosedError`, for good. Only a brand-new session recovered.

The field trigger (`v.douyin.com`) is not a test dependency and should never
become one: whether a particular site kills its tab is that site's business and
can change any day. What browserwright owes the caller is recovery from a dead
tab, whatever killed it — so this test kills the tab ITSELF, deterministically,
from a SEPARATE process that never touches `page`:

    sess.cdp.send("BrowserwrightDaemon.closeTab", ...)

The raw daemon RPC is used on purpose instead of `close_session_tab`. The
latter is the bookkeeping path: it clears the binding and fires the
target-changed hook, i.e. it TELLS browserwright the tab is gone — which is
precisely the case that already worked. The raw RPC reproduces the real one:
the tab dies, the ledger still names it, and the resident executor is still
holding a Playwright handle to a target that no longer exists.

Served from a local HTTP server: `data:` navigations are aborted over
chrome.debugger on the extension backend, and a real site would make the test
depend on the internet.
"""
from __future__ import annotations

import http.server
import socket
import threading

import pytest

from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        name = self.path.strip("/").split("?")[0] or "root"
        body = (
            f"<!doctype html><title>bw86 {name}</title><main>{name}</main>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


@pytest.fixture
def local_site():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _grep(out: str, key: str) -> str:
    for line in out.splitlines():
        if line.startswith(key + "="):
            return line[len(key) + 1:]
    raise AssertionError(f"{key}= not found in output:\n{out}")


def test_session_recovers_after_its_tab_dies(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    e2e_chrome,
    patched_ext_dir,
    local_site,
):
    pytest.importorskip("playwright.sync_api")
    from .test_l2_heredoc_playwright_page import (
        _chrome_group_tab_ids,
        _cleanup_session,
        _run_execute,
        _seed_session,
        _session_group_id,
    )
    from .test_l2_multisession import (
        _chrome_close_tabs,
        _extension_id_from_path,
    )

    runtime_dir, _facade_ws = _ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    try:
        # Round 0 — a normal navigation in a healthy session.
        r0 = _run_execute(
            f"page.goto({local_site + '/one'!r})\n"
            "print('TITLE0=' + page.title())\n",
            sid=sid, runtime_dir=runtime_dir, timeout=90)
        if r0.returncode != 0:
            # Absorb the known cold-start announce race the other e2e tests
            # retry through; it is unrelated to what is under test here.
            _run_execute("reset()\n", sid=sid, runtime_dir=runtime_dir,
                         timeout=60)
            r0 = _run_execute(
                f"page.goto({local_site + '/one'!r})\n"
                "print('TITLE0=' + page.title())\n",
                sid=sid, runtime_dir=runtime_dir, timeout=90)
        assert r0.returncode == 0, (
            f"round 0 failed: {r0.stdout!r} {r0.stderr!r}")
        assert "one" in _grep(r0.stdout, "TITLE0")

        # Kill the session's tab from OUTSIDE browserwright entirely:
        # `chrome.tabs.remove`, driven through Chrome's own CDP against the
        # extension's service worker. No browserwright code path is involved,
        # so nothing clears the binding, nothing fires the target-changed hook,
        # and the resident executor goes on holding a Playwright handle to a
        # target that no longer exists. That is the field condition, and it is
        # deterministic — unlike whichever real site happened to trigger it.
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        assert gid is not None, "session has no tab group to kill"
        doomed = _chrome_group_tab_ids(e2e_chrome, extension_id, gid)
        assert doomed, "session group has no tabs to kill"
        _chrome_close_tabs(e2e_chrome, extension_id, doomed)

        # Round 1 — the whole bug. Before the fix this failed in 1-4ms with
        # `TargetClosedError` (surfaced as PageLoadFailed "(target-closed)"),
        # and so did every round after it, forever.
        r1 = _run_execute(
            f"page.goto({local_site + '/two'!r})\n"
            "print('TITLE1=' + page.title())\n",
            sid=sid, runtime_dir=runtime_dir, timeout=90)
        assert r1.returncode == 0, (
            "a session whose tab died did not rebind (issue #86): "
            f"stdout={r1.stdout!r} stderr={r1.stderr!r}")
        assert "two" in _grep(r1.stdout, "TITLE1")

        # Round 2 — and it is durably healthy, not a one-off.
        r2 = _run_execute(
            f"page.goto({local_site + '/three'!r})\n"
            "print('TITLE2=' + page.title())\n",
            sid=sid, runtime_dir=runtime_dir, timeout=90)
        assert r2.returncode == 0, (
            f"round 2 failed after recovery: {r2.stdout!r} {r2.stderr!r}")
        assert "three" in _grep(r2.stdout, "TITLE2")

        # The replacement tab must live in THIS session's tab group — the
        # reason recovery goes through resolve_current_target and never
        # `context.new_page()` (an un-grouped tab is ledger drift).
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        assert gid is not None, "session lost its tab group after recovery"
        tab_ids = _chrome_group_tab_ids(e2e_chrome, extension_id, gid)
        assert len(tab_ids) == 1, (
            f"recovery should leave exactly one grouped tab, found {tab_ids}")
    finally:
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        if gid is not None:
            tab_ids = _chrome_group_tab_ids(e2e_chrome, extension_id, gid)
            _chrome_close_tabs(e2e_chrome, extension_id, tab_ids)
        _cleanup_session("extension", sid)
