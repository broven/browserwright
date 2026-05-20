"""Smoke test: start_session brings up daemon+Chrome, stop_session tears down.

Marked real_chrome — skipped unless explicitly selected.
"""
from __future__ import annotations

import socket
from contextlib import closing

import pytest

from hooks import EXT_PORT, start_session, stop_session

pytestmark = pytest.mark.real_chrome


def _port_in_use(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def test_session_lifecycle():
    """start_session -> daemon up + extension connected -> stop_session -> port free."""
    try:
        start_session()

        # Daemon is up and has at least one extension connected
        import json
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{EXT_PORT}/__status__", timeout=2
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert int(body.get("extensions", 0)) >= 1, f"no extensions: {body}"
    finally:
        stop_session()

    # Daemon process should be terminated (port may linger in TIME_WAIT).
    import urllib.error as _ue
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{EXT_PORT}/__status__", timeout=1
        )
        raise AssertionError("daemon still responding after stop_session")
    except (_ue.URLError, OSError):
        pass  # expected — daemon is gone
