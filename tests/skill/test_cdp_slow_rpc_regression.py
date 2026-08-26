"""Regression for GH #18: a slow control-plane RPC must NOT tear down the
control-surface transport.

Root cause: `cdp._open_unix_websocket` set `raw.settimeout(connect_timeout)` to
bound `connect()` but never cleared it, so the connect deadline leaked into
steady state as a per-recv read timeout. Any RPC whose reply took longer than
`connect_timeout` (e.g. the extension `BrowserwrightDaemon.ensureExecutor`
blocking while it waits for an extension to connect) made the socket read time
out, and websockets surfaced it as `ConnectionClosedError: no close frame
received or sent` — the confusing `ws closed` the issue reported.

ADR-0011 deleted that unix transport and the hand-rolled socket it was built on,
which removes the mechanism of the bug. The guard is still worth keeping, and is
now written against the transport that replaced it: `connect_timeout` reaches
`websockets` as `open_timeout`, which by contract bounds only the handshake. The
test proves that contract holds rather than trusting it — the previous transport
also *intended* the deadline to be connect-only.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from browserwright.cdp import CDPSession


@pytest.fixture
def slow_ws_server():
    """Factory for a real loopback ws server that replies after a delay.

    Yields ``start(reply_delay) -> url``; the server is shut down on teardown.
    """
    servers = []

    def start(reply_delay: float) -> str:
        from websockets.sync.server import serve

        def handler(conn) -> None:
            for raw in conn:
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                time.sleep(reply_delay)
                conn.send(json.dumps(
                    {"id": frame.get("id"), "result": {"ok": True}}))

        server = serve(handler, "127.0.0.1", 0)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        return f"ws://127.0.0.1:{port}/control?client=test"

    yield start
    for server in servers:
        server.shutdown()


def test_slow_rpc_does_not_drop_the_transport(slow_ws_server):
    """A reply that arrives AFTER `connect_timeout` must still be delivered —
    the connect deadline must not leak into the steady-state read."""
    # Reply takes 1.5s; connect timeout is a tiny 0.4s. Pre-fix this dropped the
    # connection at ~0.4s with "no close frame received or sent".
    url = slow_ws_server(1.5)
    sess = CDPSession(url, connect_timeout=0.4)
    try:
        t0 = time.monotonic()
        res = sess.send("BrowserwrightDaemon.ensureExecutor", bsSession="1")
        elapsed = time.monotonic() - t0
        assert res == {"ok": True}
        # The reply genuinely arrived after the (tiny) connect timeout.
        assert elapsed >= 1.4, f"reply came back too fast ({elapsed:.2f}s)"
    finally:
        sess.close()


def test_idle_transport_survives_past_connect_timeout(slow_ws_server):
    """An idle connection (no frames) must not self-close at `connect_timeout`.

    Regression guard for the same leak observed via a different lens: the
    transport stays usable after sitting idle longer than the connect deadline,
    proving liveness is governed by ws keepalive, not a stray read deadline.
    """
    url = slow_ws_server(0.0)
    sess = CDPSession(url, connect_timeout=0.3)
    try:
        # Idle well past the 0.3s connect timeout, then issue an RPC.
        time.sleep(1.0)
        assert sess.send(
            "BrowserwrightDaemon.ensureExecutor", bsSession="1") == {"ok": True}
    finally:
        sess.close()
