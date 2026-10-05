"""GH#116 (extension backend, real Chrome): `tabs()` must report live URLs.

The relay's per-tab record (`GhostTarget`) captures url/title only at
create/attach time and never updates them on navigation. `tabs()` enumerates
through the daemon's group-scoped `Target.getTargets`, which built each entry
from that stale cache — so a tab that attached on ``about:blank`` and then
navigated was reported as ``about:blank``. ``session_tabs(include_internal=
False)`` then dropped it as an internal page and `tabs()` returned ``[]`` while
`context.pages` held several real, grouped tabs.

This drives the real agent surface (`page` / `context.new_page()` / `tabs()`)
against the Chrome-for-Testing extension harness and asserts that every tab in
the session's tab group is listed with the URL it actually landed on — and
that nothing outside the group leaks in.

Run:
    bash tests/daemon/e2e/run.sh tests/daemon/e2e/test_l2_extension_tabs_live_url.py -v -s
"""
from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)
from .test_l2_multisession import (
    _chrome_close_tabs,
    _extension_id_from_path,
)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        name = self.path.strip("/").split("?")[0] or "root"
        body = (
            f"<!doctype html><title>bw116 {name}</title><main>{name}</main>"
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


def test_tabs_reports_live_urls_for_session_tabs(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    e2e_chrome,
    patched_ext_dir,
    local_site,
):
    pytest.importorskip("playwright.sync_api")
    from .test_l2_heredoc_playwright_page import (
        _chrome_group_tab_ids,
        _cleanup_session,
        _grep,
        _run_execute,
        _seed_session,
        _session_group_id,
    )

    runtime_dir, _facade_ws = _ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    try:
        # The bound page starts on about:blank (no group yet); navigating it
        # is what makes its cached ghost URL stale. Then three more tabs are
        # opened through the real Playwright surface and navigated.
        script = (
            "import json\n"
            f"base = {local_site!r}\n"
            "page.goto(base + '/one', wait_until='load')\n"
            "for name in ('two', 'three', 'four'):\n"
            "    p = context.new_page()\n"
            "    p.goto(base + '/' + name, wait_until='load')\n"
            "print('TABS=' + json.dumps(tabs()))\n"
        )
        result = _run_execute(script, sid=sid, runtime_dir=runtime_dir,
                              timeout=90)
        assert result.returncode == 0, (
            f"tabs() heredoc failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}")

        rows = json.loads(_grep(result.stdout, "TABS"))
        assert len(rows) == 4, f"tabs() should list every grouped tab: {rows}"

        urls = sorted(r["url"] for r in rows)
        assert urls == sorted(
            f"{local_site}/{name}" for name in ("one", "two", "three", "four")
        ), f"tabs() reported stale/blank urls: {urls}"
        assert all("about:blank" not in (r["url"] or "") for r in rows)
        assert any(r["current"] for r in rows), (
            "tabs() must still mark the session's current tab")

        # The live title comes from the same group query as the url. The
        # extension strips its 👀 marker, so the server's <title> is intact.
        titles = sorted(r["title"] for r in rows)
        assert titles == sorted(
            f"bw116 {name}" for name in ("one", "two", "three", "four")
        ), f"tabs() reported stale/blank titles: {titles}"

        # Scoping: tabs() is EXACTLY the Chrome tab group — no more, no less.
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        assert gid is not None, "session has no live tab group"
        grouped = set(_chrome_group_tab_ids(e2e_chrome, extension_id, gid))
        listed = {int(r["targetId"].rsplit("-", 1)[1]) for r in rows}
        assert listed == grouped, (
            f"tabs() must be scoped to the session group: listed={listed} "
            f"grouped={grouped}")
    finally:
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        if gid is not None:
            _chrome_close_tabs(
                e2e_chrome, extension_id,
                _chrome_group_tab_ids(e2e_chrome, extension_id, gid))
        _cleanup_session("extension", sid)
