"""L0 smoke -- daemon and Chrome reachability."""
from __future__ import annotations

import urllib.request


def test_e2e_daemon_status_ok(e2e_daemon):
    """Daemon `/__status__` returns 200; no extension yet, so
    extensions should be 0."""
    with urllib.request.urlopen(
        f"http://127.0.0.1:{e2e_daemon.ext_port}/__status__", timeout=2
    ) as resp:
        assert resp.status == 200
