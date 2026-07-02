"""Upstream ws connection — minimal CDP transport without cdp-use framing.

Why hand-rolled? We need raw frame-in/frame-out because the daemon is a
transparent proxy: a client's outbound text frame gets forwarded byte-for-byte
to upstream, and upstream's response/event frames get forwarded back without
re-parsing or rewriting (§6.3). cdp-use parses + re-emits + tracks ids on its
own; that's two layers of conflict we don't want.

websockets.connect gives us the right primitive: a raw async iterator of text
frames, with `.send(str|bytes)` for the other direction. We also handle the
localhost-proxy-bypass dance from active_tab here.

Spec §6.5 invariant: upstream never auto-reconnects. When the connection
drops, we mark CLOSING and signal up; the caller decides what comes next.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# 30s upstream heartbeat — spec §10 open question "Browser.getVersion 心跳频率"
# resolved to 30s.
HEARTBEAT_INTERVAL = 30.0
# Number of synthetic command ids reserved for daemon-internal use (heartbeat,
# Target subscriptions). Client ids passthrough unchanged; daemon uses big
# negatives to avoid colliding with anything a CDP client might send.
_DAEMON_ID_BASE = -2_000_000_000


class UpstreamConnection:
    """Wraps a single ws to Chrome's browser-level CDP endpoint.

    Lifecycle:
      open(ws_url) → forward() pumps frames → close() ends it cleanly.

    `on_frame(text)` is called for every frame *from* upstream. It is the
    caller's job to forward it downstream (modulo BrowserwrightDaemon.* answers
    which never enter here).
    """

    def __init__(
        self,
        on_frame: Callable[[str], Awaitable[None]],
        on_close: Callable[[str], Awaitable[None]],
    ):
        self._on_frame = on_frame
        self._on_close = on_close
        self._ws: websockets.ClientConnection | None = None  # type: ignore[name-defined]
        self._reader_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._next_internal_id = _DAEMON_ID_BASE
        self._pending_internal: dict[int, asyncio.Future] = {}
        self._ws_url: str | None = None

    # ---- public API -------------------------------------------------------

    @property
    def ws_url(self) -> str | None:
        return self._ws_url

    @property
    def is_open(self) -> bool:
        return self._ws is not None

    async def open(self, ws_url: str, *, timeout: float = 30.0) -> None:
        """Connect to upstream. Raises on failure; caller transitions state."""
        if self._ws is not None:
            raise RuntimeError("upstream already open")
        with _localhost_bypass_proxy(ws_url):
            connect_kwargs: dict[str, Any] = {
                # Big max_size: CDP `Page.captureScreenshot` returns base64
                # blobs that comfortably exceed the websockets default 1MiB.
                "max_size": 100 * 1024 * 1024,
                # Disable per-message-deflate — Chrome's browser-level CDP
                # doesn't speak it, and websockets v15 sometimes negotiates
                # extensions that break the handshake.
                "compression": None,
                # Never route the daemon→browser CDP control channel through the
                # user's ambient web proxy. websockets v15 honors
                # http_proxy/all_proxy by default, which breaks any non-loopback
                # upstream (LAN / Tailscale / an env-backed CloakBrowser profile)
                # that the loopback-only NO_PROXY bypass above can't cover. Same
                # fix as the Playwright facade bridge. (issue #20)
                "proxy": None,
                # Keep the upstream alive with ws-level pings; CDP-level
                # Browser.getVersion heartbeat is layered on top for protocol
                # liveness.
                "ping_interval": 20,
                "ping_timeout": 20,
            }
            self._ws = await asyncio.wait_for(
                websockets.connect(ws_url, **connect_kwargs),
                timeout=timeout,
            )
        self._ws_url = ws_url
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def send_text(self, frame: str) -> None:
        """Forward a downstream frame to upstream verbatim."""
        if self._ws is None:
            raise RuntimeError("upstream not open")
        await self._ws.send(frame)

    async def send_command(self, method: str, params: dict | None = None,
                           session_id: str | None = None,
                           timeout: float = 10.0) -> dict:
        """Daemon-internal command — distinct id space from client ids so
        results never collide with downstream traffic.

        Used for: initial Target.setDiscoverTargets to populate the target
        table, the periodic Browser.getVersion heartbeat, and the close-time
        Target.detachFromTarget.
        """
        if self._ws is None:
            raise RuntimeError("upstream not open")
        cmd_id = self._alloc_id()
        msg: dict[str, Any] = {"id": cmd_id, "method": method}
        if params is not None:
            msg["params"] = params
        if session_id is not None:
            msg["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_internal[cmd_id] = fut
        try:
            await self._ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_internal.pop(cmd_id, None)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        """Close the upstream cleanly. Idempotent."""
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        ws = self._ws
        self._ws = None
        for fut in self._pending_internal.values():
            if not fut.done():
                fut.set_exception(ConnectionError("upstream closing"))
        self._pending_internal.clear()
        if ws is not None:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass
        self._ws_url = None

    # ---- internal ---------------------------------------------------------

    def _alloc_id(self) -> int:
        v = self._next_internal_id
        self._next_internal_id += 1
        return v

    async def _reader_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                # Intercept responses to *our* internal ids (heartbeat etc).
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    cid = parsed.get("id")
                    if isinstance(cid, int) and cid in self._pending_internal:
                        fut = self._pending_internal.pop(cid)
                        if not fut.done():
                            fut.set_result(parsed)
                        continue
                # Forward to downstream.
                try:
                    await self._on_frame(text)
                except Exception as e:
                    logger.warning("on_frame raised: %r", e)
        except ConnectionClosed as e:
            logger.info("upstream closed: code=%s reason=%s", e.code, e.reason)
        except Exception as e:
            logger.warning("upstream reader crashed: %r", e)
        finally:
            # Always notify close — this is the canonical signal for the
            # state machine to enter CLOSING (caller decides reason).
            try:
                await self._on_close("upstream-eof")
            except Exception:
                pass

    async def _heartbeat_loop(self) -> None:
        """Keep CDP alive by pinging `Browser.getVersion` every 30s.

        Spec §10 open question: 30s is the chosen cadence. Too fast = wasted
        CDP traffic; too slow = stale-Chrome detection latency. Tunable later.
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws is None:
                    return
                try:
                    await self.send_command("Browser.getVersion", timeout=10)
                except (asyncio.TimeoutError, ConnectionError, ConnectionClosed):
                    logger.warning("heartbeat failed, closing upstream")
                    return
        except asyncio.CancelledError:
            return


# ---- localhost proxy bypass (same trick as active_tab) --------------------


@contextlib.contextmanager
def _localhost_bypass_proxy(ws_url: str):
    """When the upstream URL is loopback, ensure NO_PROXY covers it. Same
    rationale as `active_tab._localhost_bypass_proxy`. Spec doesn't mention
    this — but Chrome runs on the user's machine, and the user often has
    HTTPS_PROXY / ALL_PROXY set."""
    host = (urlparse(ws_url).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        yield
        return
    prev = os.environ.get("NO_PROXY", "")
    augmented = prev
    for h in ("127.0.0.1", "localhost", "::1"):
        if h not in augmented:
            augmented = f"{augmented},{h}" if augmented else h
    os.environ["NO_PROXY"] = augmented
    try:
        yield
    finally:
        if prev:
            os.environ["NO_PROXY"] = prev
        else:
            os.environ.pop("NO_PROXY", None)
