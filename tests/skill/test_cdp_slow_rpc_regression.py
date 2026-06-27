"""Regression for GH #18: a slow control-plane RPC must NOT tear down the
mode-B unix-socket transport.

Root cause: `cdp._open_unix_websocket` set `raw.settimeout(connect_timeout)` to
bound `connect()` but never cleared it, so the connect deadline leaked into
steady state as a per-recv read timeout. Any RPC whose reply took longer than
`connect_timeout` (e.g. the extension `BrowserwrightDaemon.ensureExecutor`
blocking while it waits for an extension to connect) made the socket read time
out, and websockets surfaced it as `ConnectionClosedError: no close frame
received or sent` — the confusing `ws closed` the issue reported.

This test stands up a REAL unix websocket server that delays its reply well
beyond a deliberately tiny `connect_timeout`, and asserts the `CDPSession` still
receives the reply instead of dropping the connection.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
import threading
import time

import pytest

from browserwright.cdp import CDPSession


@pytest.fixture
def short_sock_path():
    """A SHORT unix-socket path (macOS caps AF_UNIX paths at ~104 bytes, which
    pytest's deep `tmp_path` blows past). Lives directly under the system temp
    dir and is unlinked on teardown."""
    fd, path = tempfile.mkstemp(prefix="bwt", suffix=".sock")
    os.close(fd)
    os.unlink(path)  # the server binds it; it must not pre-exist
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _start_slow_unix_ws(sock_path: str, *, reply_delay: float) -> "object":
    """Run a unix-socket ws server that echoes a result after `reply_delay`s.

    Returns the running `Server`; the caller is responsible for `shutdown()`.
    """
    from websockets.sync.server import unix_serve

    def handler(conn) -> None:
        for raw in conn:
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                continue
            time.sleep(reply_delay)
            conn.send(json.dumps({"id": frame.get("id"), "result": {"ok": True}}))

    server = unix_serve(handler, path=sock_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_slow_rpc_does_not_drop_unix_transport(short_sock_path):
    """A reply that arrives AFTER `connect_timeout` must still be delivered —
    the connect deadline must not leak into the steady-state read."""
    sock_path = short_sock_path
    # Reply takes 1.5s; connect timeout is a tiny 0.4s. Pre-fix this dropped the
    # connection at ~0.4s with "no close frame received or sent".
    server = _start_slow_unix_ws(sock_path, reply_delay=1.5)
    try:
        sess = CDPSession(f"ws+unix://{sock_path}?client=test", connect_timeout=0.4)
        t0 = time.monotonic()
        res = sess.send("BrowserwrightDaemon.ensureExecutor", bsSession="1")
        elapsed = time.monotonic() - t0
        assert res == {"ok": True}
        # The reply genuinely arrived after the (tiny) connect timeout.
        assert elapsed >= 1.4, f"reply came back too fast ({elapsed:.2f}s)"
    finally:
        server.shutdown()


def test_idle_unix_transport_survives_past_connect_timeout(short_sock_path):
    """An idle connection (no frames) must not self-close at `connect_timeout`.

    Regression guard for the same leak observed via a different lens: the
    transport stays usable after sitting idle longer than the connect deadline,
    proving liveness is governed by ws keepalive, not the stray socket timeout.
    """
    sock_path = short_sock_path
    server = _start_slow_unix_ws(sock_path, reply_delay=0.0)
    try:
        sess = CDPSession(f"ws+unix://{sock_path}?client=test", connect_timeout=0.3)
        # Idle well past the 0.3s connect timeout, then issue an RPC.
        time.sleep(1.0)
        res = sess.send("BrowserwrightDaemon.ensureExecutor", bsSession="1")
        assert res == {"ok": True}
    finally:
        server.shutdown()
