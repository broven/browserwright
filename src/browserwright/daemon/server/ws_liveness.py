"""Keepalive for the daemon's upstream CDP websockets.

websockets' built-in keepalive declares a connection dead when one pong is late
by ``ping_timeout``. A pong travels in the same TCP stream as the data, behind
it. On a slow link — an attached browser across a tailnet relay at 0.3-1 s RTT
and a few hundred KB/s — one large CDP reply (an aria snapshot of a big page, a
screenshot) can hold the pong back for longer than that, although bytes are
arriving the whole time. The built-in keepalive then closes a working
connection with 1011. On the cdp surface that is the Playwright client's whole
browser: every Page reports "Target page, context or browser has been closed"
while the tab itself is fine.

So the verdict here is "nothing at all has arrived": any inbound byte counts as
proof of life, and the ping is only how a quiet connection gets something to
arrive. Bytes, not messages: that one big reply is a single message that takes
the whole transfer to complete. Pings still go out on idle connections, so NAT
and proxy idle timers stay fed.

Open a connection with :func:`connect_kwargs` and run :func:`keep_alive` on it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

#: Ping a connection that has been quiet this long.
KEEPALIVE_INTERVAL = 20.0
#: Close it once it has been silent — not one byte in — this long after a ping.
KEEPALIVE_TIMEOUT = 20.0


class TrackedConnection(ClientConnection):
    """A client connection that records when bytes last arrived."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_inbound = time.monotonic()

    def data_received(self, data: bytes) -> None:
        self.last_inbound = time.monotonic()
        super().data_received(data)


def connect_kwargs() -> dict[str, Any]:
    """`websockets.connect` options that hand liveness to :func:`keep_alive`."""
    return {"ping_interval": None, "create_connection": TrackedConnection}


async def keep_alive(ws: TrackedConnection, *, label: str,
                     interval: float = KEEPALIVE_INTERVAL,
                     timeout: float = KEEPALIVE_TIMEOUT) -> None:
    """Run until ``ws`` closes, as its only keepalive."""
    try:
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() - ws.last_inbound < interval:
                continue
            sent = time.monotonic()
            pong = await ws.ping()
            while not pong.done():
                remaining = max(sent, ws.last_inbound) + timeout - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "%s: nothing received for %.0fs after a ping; "
                        "closing the connection", label, timeout)
                    await ws.close(code=1011, reason="keepalive timeout")
                    return
                await asyncio.wait({pong}, timeout=remaining)
    except (ConnectionClosed, asyncio.CancelledError):
        return
