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

Phase A1 scope (this module): **rdp backend only**. The daemon owns the rdp
Chrome, which already speaks real browser-level CDP — so the facade is a
transparent byte-for-byte passthrough: on each ws client connect we resolve the
rdp Chrome's real CDP ws (via the daemon resolver / `backends/rdp.py`) and pump
frames in both directions. No `Target.*`/`Browser.*` synthesis is needed
because the real Chrome answers them natively.

Deliberately out of scope here (left for PR2 = phase A2+):
  - extension backend (needs the `Target.attachedToTarget`/`targetCreated`
    event synthesis described in the research delta; the relay only speaks a
    restricted CDP subset).
  - `Target.createTarget` → `openBackgroundTab` mapping, `Runtime.enable`
    execution-context barrier — those harden the extension path, not rdp.

The facade NEVER touches the existing `DaemonState` / `Router` translation
tables: it is a parallel transport. The unix-socket agent path is untouched.
"""
from __future__ import annotations

import asyncio
import contextlib
import http
import json
import logging
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .. import __version__
from ..config import Config
from ..errors import Unavailable
from ..resolver import resolve as resolve_upstream
from .upstream import _localhost_bypass_proxy

logger = logging.getLogger(__name__)


# Default facade port. Distinct from the extension relay (19989) and
# playwriter's 19988 so all three can coexist on one machine.
DEFAULT_FACADE_PORT = 19990

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
                 host: str = "127.0.0.1"):
        self._cfg = cfg
        self._port = port
        self._host = host
        self._server: Any = None
        # Track live passthrough tasks so stop() can cancel them.
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

    async def _handle_client(self, conn: ServerConnection) -> None:
        """One Playwright client connected. Resolve the rdp Chrome's real CDP
        ws and pump frames byte-for-byte in both directions for the life of the
        connection."""
        task = asyncio.current_task()
        if task is not None:
            self._sessions.add(task)
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
        finally:
            if task is not None:
                self._sessions.discard(task)

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
