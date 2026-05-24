"""Playwright-facing CDP facade (Task #tab-handle-model, phase A1).

A **separate, additive** ws+HTTP server that lets a real Playwright client speak
browser-level CDP to the daemon-resolved Chrome via
`chromium.connect_over_cdp("ws://127.0.0.1:<facade_port>/cdp")`.

Why a new endpoint (not the existing unix-socket client path)?

  - The agent client path (listener.py) is a unix socket on POSIX speaking the
    `?session=<id>` + `BrowserwrightDaemon.*` translation protocol. Playwright
    can neither connect to a unix socket nor go through the per-session
    sessionId rewriting — it drives **raw** browser-level CDP
    (`Target.setAutoAttach` / `Browser.getVersion` / flat sessions).
  - playwriter exposes exactly this shape: a Hono ws on a TCP port plus a
    `/json/version` route returning `webSocketDebuggerUrl` so the CDP client
    can bootstrap (`research/playwright-over-extension-bridge.md`). We mirror
    that bootstrap shape.

Two backends, two transports (the consumer is always a real Playwright client):

  - **rdp** (PR1): the daemon owns the rdp Chrome, which already speaks real
    browser-level CDP — so the facade is a transparent byte-for-byte
    passthrough: on each ws client connect we resolve the rdp Chrome's real CDP
    ws (via the daemon resolver / `backends/rdp.py`) and pump frames in both
    directions. No `Target.*`/`Browser.*` synthesis is needed because the real
    Chrome answers them natively.

  - **extension** (PR2): there is NO resolvable upstream ws — the daemon IS the
    relay. We hand the client to `ExtensionFacadeBridge`
    (`facade_extension.py`), which reuses the existing `ExtensionUpstream`
    emulation over the shared `RelayServer` and ADDS the
    `Target.attachedToTarget`/`targetCreated` event synthesis,
    `Target.createTarget`→background-tab mapping, and `Runtime.enable` barrier
    that Playwright's `connect_over_cdp` discovery needs. The bridge needs the
    daemon's shared relay, so the facade is constructed with a `relay_getter`.

The facade NEVER touches the existing `DaemonState` / `Router` translation
tables: it is a parallel transport. The unix-socket agent path is untouched.
"""
from __future__ import annotations

import asyncio
import contextlib
import http
import json
import logging
from typing import Any, Callable

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .. import __version__
from ..config import DEFAULT_FACADE_PORT, Config
from ..errors import Unavailable
from ..resolver import resolve as resolve_upstream
from .facade_extension import ExtensionFacadeBridge
from .relay import RelayServer
from .upstream import _localhost_bypass_proxy

logger = logging.getLogger(__name__)


# DEFAULT_FACADE_PORT now lives in ``config`` (no import cycle there) and is
# re-exported here for the existing call sites / tests that import it from this
# module.
__all__ = ["DEFAULT_FACADE_PORT", "PlaywrightFacade", "FACADE_WS_PATH"]

# The ws path a CDP client connects to once it has read /json/version. The
# value is cosmetic (we passthrough regardless of path) but kept stable so the
# advertised webSocketDebuggerUrl and the served endpoint agree.
FACADE_WS_PATH = "/cdp"


class PlaywrightFacade:
    """A TCP ws server that bridges a Playwright `connect_over_cdp` client to
    the daemon-resolved (rdp) Chrome's real browser-level CDP.

    Lifecycle mirrors `RelayServer`: ``start()`` binds (returns the bound port,
    useful with ``port=0`` in tests); ``stop()`` closes everything cleanly.
    """

    def __init__(self, *, cfg: Config, port: int = DEFAULT_FACADE_PORT,
                 host: str = "127.0.0.1",
                 relay_getter: Callable[[], RelayServer | None] | None = None):
        self._cfg = cfg
        self._port = port
        self._host = host
        self._server: Any = None
        # PR2: for the extension backend the facade has no resolvable upstream
        # ws — it bridges through the daemon's shared RelayServer. The listener
        # passes a getter (the relay is created during run_serve startup, and
        # may be (re)bound across reconnects, so we resolve it lazily per
        # client connection rather than capturing the instance now).
        self._relay_getter = relay_getter
        # Track live passthrough/bridge tasks so stop() can cancel them.
        self._sessions: set[asyncio.Task] = set()

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> int:
        """Bind the facade ws+HTTP server. Returns the actually-bound port."""
        self._server = await serve(
            self._handle_client,
            self._host,
            self._port,
            process_request=self._process_request,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            # CDP `Page.captureScreenshot` returns base64 blobs far above the
            # websockets 1 MiB default — match the listener/relay limits.
            max_size=100 * 1024 * 1024,
        )
        for sock in self._server.sockets:
            sa = sock.getsockname()
            if isinstance(sa, tuple) and len(sa) >= 2:
                self._port = sa[1]
                break
        logger.info("playwright facade listening on ws://%s:%d%s",
                    self._host, self._port, FACADE_WS_PATH)
        return self._port

    async def stop(self) -> None:
        if self._server is None:
            return
        for task in list(self._sessions):
            task.cancel()
            # `await`ing a cancelled task re-raises CancelledError, which is a
            # BaseException (not Exception) since py3.8 — suppress it explicitly
            # so one in-flight client can't abort the rest of shutdown and leak
            # the listening socket below.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._sessions.clear()
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    @property
    def port(self) -> int:
        return self._port

    # ---- HTTP discovery (CDP bootstrap) ----------------------------------

    def _process_request(self, conn: ServerConnection, request) -> Any:
        """Serve the CDP HTTP discovery routes so a Playwright client can
        bootstrap, then allow the ws upgrade for the CDP path.

        playwriter's relay implements `/json/version` returning
        `webSocketDebuggerUrl: ws://host/cdp`; `connect_over_cdp("ws://...")`
        also accepts an `http://` URL it resolves via this route. We answer
        `/json/version`, `/json`, and `/json/list` (the latter two are cheap
        and some CDP clients probe them)."""
        path = (request.path or "/").split("?", 1)[0]
        if path == "/json/version":
            return self._http_json(conn, self._version_payload())
        if path in ("/json", "/json/list"):
            return self._http_json(conn, self._list_payload())
        # Anything else (e.g. the /cdp ws upgrade) falls through to the ws
        # handler. Return None to allow the upgrade.
        return None

    def _http_json(self, conn: ServerConnection, payload: Any):
        body = json.dumps(payload)
        resp = conn.respond(http.HTTPStatus.OK, body)
        # Replace the default text/plain Content-Type (mirrors relay.__status__).
        resp.headers["Content-Type"] = "application/json"
        return resp

    def _ws_url(self) -> str:
        return f"ws://{self._host}:{self._port}{FACADE_WS_PATH}"

    def _version_payload(self) -> dict:
        return {
            "Browser": f"Browserwright/{__version__}",
            "Protocol-Version": "1.3",
            "User-Agent": f"Browserwright facade {__version__}",
            "webSocketDebuggerUrl": self._ws_url(),
        }

    def _list_payload(self) -> list:
        # The browser-level endpoint is what Playwright wants; per-page targets
        # are discovered via Target.* over the ws once connected. We advertise a
        # single synthetic "browser" entry pointing at our ws.
        return [{
            "type": "browser",
            "title": "Browserwright",
            "url": "",
            "webSocketDebuggerUrl": self._ws_url(),
        }]

    # ---- ws passthrough --------------------------------------------------

    def _backend_name(self) -> str:
        """The effective shared backend. `run_serve` defaults a missing backend
        to extension, so mirror that here."""
        return self._cfg.backend or "extension"

    async def _handle_client(self, conn: ServerConnection) -> None:
        """One Playwright client connected. For the extension backend, bridge
        through the shared relay with target-event synthesis (PR2); otherwise
        resolve the rdp Chrome's real CDP ws and pump frames byte-for-byte."""
        task = asyncio.current_task()
        if task is not None:
            self._sessions.add(task)
        try:
            if self._backend_name() == "extension":
                await self._handle_extension_client(conn)
                return
            await self._handle_rdp_client(conn)
        finally:
            if task is not None:
                self._sessions.discard(task)

    async def _handle_extension_client(self, conn: ServerConnection) -> None:
        """Bridge a Playwright client to the extension backend via the shared
        relay. Requires the relay to be up (it is started eagerly in run_serve
        for the extension backend)."""
        relay = self._relay_getter() if self._relay_getter is not None else None
        if relay is None:
            logger.warning("facade(ext): no relay available; refusing client")
            with contextlib.suppress(Exception):
                await conn.close(code=1011, reason="extension relay unavailable")
            return
        bridge = ExtensionFacadeBridge(client=conn, relay=relay)
        try:
            await bridge.run()
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("facade(ext): bridge crashed: %r", e)
            with contextlib.suppress(Exception):
                await bridge.aclose()

    async def _handle_rdp_client(self, conn: ServerConnection) -> None:
        """rdp backend (PR1): transparent byte-for-byte passthrough."""
        try:
            ws_url = await self._resolve_rdp_ws()
        except Unavailable as e:
            logger.warning("facade: cannot resolve upstream Chrome: %s", e)
            with contextlib.suppress(Exception):
                await conn.close(code=1011, reason="upstream unavailable")
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("facade: upstream resolve crashed: %r", e)
            with contextlib.suppress(Exception):
                await conn.close(code=1011, reason="upstream error")
            return

        try:
            await self._bridge(conn, ws_url)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("facade: bridge crashed: %r", e)

    async def _resolve_rdp_ws(self) -> str:
        """Resolve the upstream Chrome CDP ws URL via the daemon resolver.

        Phase A1 is rdp-only; the resolver's rdp backend reads
        `/json/version` (or the DevToolsActivePort fallback) and returns the
        browser-level ws the daemon-owned Chrome is listening on."""
        rr = await resolve_upstream(self._cfg)
        return rr.ws_url

    async def _bridge(self, client: ServerConnection, upstream_url: str) -> None:
        """Open a raw ws to the upstream Chrome and shuttle frames both ways.

        Transparent: no id/sessionId rewriting (unlike the agent Router) — a
        Playwright client owns the whole browser-level CDP namespace on its own
        dedicated upstream connection, so Target.*/Browser.* responses and
        events flow back unmodified."""
        with _localhost_bypass_proxy(upstream_url):
            upstream = await websockets.connect(
                upstream_url,
                max_size=100 * 1024 * 1024,
                compression=None,
                ping_interval=20,
                ping_timeout=20,
            )
        c2u = asyncio.create_task(self._pump(client, upstream, "c->u"))
        u2c = asyncio.create_task(self._pump(upstream, client, "u->c"))
        try:
            await asyncio.wait(
                {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancel + await BOTH pumps unconditionally. This runs on the normal
            # FIRST_COMPLETED path AND when stop() cancels the handler task while
            # it's suspended in asyncio.wait above (where the `for pending`
            # cleanup would otherwise be skipped, orphaning the pump tasks).
            for t in (c2u, u2c):
                t.cancel()
                # CancelledError is a BaseException; suppress it explicitly so a
                # cancelled pump doesn't escape this cleanup.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            with contextlib.suppress(Exception):
                await upstream.close()
            with contextlib.suppress(Exception):
                await client.close()

    @staticmethod
    async def _pump(src, dst, label: str) -> None:
        """Forward every frame from src to dst until either side closes."""
        try:
            async for raw in src:
                await dst.send(raw)
        except websockets.exceptions.ConnectionClosed:
            return
        except Exception as e:  # noqa: BLE001
            logger.debug("facade pump %s ended: %r", label, e)
            return
