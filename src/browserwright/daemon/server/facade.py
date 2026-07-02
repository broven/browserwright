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
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .. import __version__
from ..config import DEFAULT_FACADE_PORT, Config
from ..errors import Unavailable
from ..resolver import resolve as resolve_upstream
from .daemon import Daemon, UnknownSessionError, UpstreamContext
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
                 relay_getter: Callable[[], RelayServer | None] | None = None,
                 daemon: Daemon | None = None):
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
        # Single-daemon model: raw Playwright facade clients may carry
        # `?session=<id>`. When present, route them to that session's
        # UpstreamContext instead of the shared daemon backend.
        self._daemon = daemon
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
        and some CDP clients probe them).

        The advertised `webSocketDebuggerUrl` is derived from the incoming
        request's `Host` header so a client that reached us over Tailscale/LAN
        gets a ws URL that points back at the same authority it just used —
        exactly what CloakBrowser does. We only fall back to the configured
        `host:port` when no Host header is present (e.g. a hand-rolled probe)."""
        path = (request.path or "/").split("?", 1)[0]
        # Playwright's `connect_over_cdp("http://host:port")` probes
        # `.../json/version/` WITH a trailing slash; normalize it away so the
        # http bootstrap form works, not just the direct `ws://.../cdp` form.
        if len(path) > 1:
            path = path.rstrip("/")
        session_id = self._session_for_request(request)
        authority = self._authority_from_request(request)
        if path == "/json/version":
            return self._http_json(conn, self._version_payload(session_id, authority))
        if path in ("/json", "/json/list"):
            return self._http_json(conn, self._list_payload(session_id, authority))
        # Anything else (e.g. the /cdp ws upgrade) falls through to the ws
        # handler. Return None to allow the upgrade.
        return None

    def _http_json(self, conn: ServerConnection, payload: Any):
        body = json.dumps(payload)
        resp = conn.respond(http.HTTPStatus.OK, body)
        # Replace the default text/plain Content-Type (mirrors relay.__status__).
        resp.headers["Content-Type"] = "application/json"
        return resp

    def _session_for_request(self, request) -> str | None:
        qs = parse_qs(urlparse(request.path or "/").query)
        return (qs.get("session") or [None])[0]

    def _authority_from_request(self, request) -> str | None:
        """Return the `Host` header (``host[:port]``) the client used to reach
        us, or None when absent. Drives the advertised ws so a remote client
        that connected over Tailscale/LAN gets a ws URL that points back at the
        same authority (not a hardcoded loopback it can't reach)."""
        try:
            host = request.headers.get("Host")
        except (AttributeError, KeyError):
            return None
        host = (host or "").strip()
        return host or None

    def _ws_url(self, session_id: str | None = None,
                authority: str | None = None) -> str:
        # Prefer the incoming request's Host (already `host[:port]`); fall back
        # to the configured bind host:port when no Host header was sent.
        netloc = authority or f"{self._host}:{self._port}"
        suffix = ""
        if session_id:
            from urllib.parse import quote
            suffix = f"?session={quote(session_id, safe='')}"
        return f"ws://{netloc}{FACADE_WS_PATH}{suffix}"

    def _version_payload(self, session_id: str | None = None,
                         authority: str | None = None) -> dict:
        return {
            "Browser": f"Browserwright/{__version__}",
            "Protocol-Version": "1.3",
            "User-Agent": f"Browserwright facade {__version__}",
            "webSocketDebuggerUrl": self._ws_url(session_id, authority),
        }

    def _list_payload(self, session_id: str | None = None,
                      authority: str | None = None) -> list:
        # The browser-level endpoint is what Playwright wants; per-page targets
        # are discovered via Target.* over the ws once connected. We advertise a
        # single synthetic "browser" entry pointing at our ws.
        return [{
            "type": "browser",
            "title": "Browserwright",
            "url": "",
            "webSocketDebuggerUrl": self._ws_url(session_id, authority),
        }]

    # ---- ws passthrough --------------------------------------------------

    def _session_for_connection(self, conn: ServerConnection) -> str | None:
        parsed = urlparse(conn.request.path or "/")
        qs = parse_qs(parsed.query)
        return (qs.get("session") or [None])[0]

    def _context_for_connection(self, conn: ServerConnection) -> UpstreamContext | None:
        """Resolve the session-bound upstream context for this facade client.

        A missing session keeps the historical shared-backend facade behavior
        used by generic `connect_over_cdp` callers. A present session id must
        use the same ledger-backed `Daemon.context_for()` routing as the agent
        websocket path.
        """
        if self._daemon is None:
            return None
        session_id = self._session_for_connection(conn)
        if not session_id:
            return None
        return self._daemon.context_for_required(session_id)

    async def _handle_client(self, conn: ServerConnection) -> None:
        """One Playwright client connected. For the extension backend, bridge
        through the shared relay with target-event synthesis (PR2); otherwise
        resolve the rdp Chrome's real CDP ws and pump frames byte-for-byte."""
        task = asyncio.current_task()
        if task is not None:
            self._sessions.add(task)
        try:
            try:
                ctx = self._context_for_connection(conn)
            except UnknownSessionError:
                with contextlib.suppress(Exception):
                    await conn.close(code=1008, reason="unknown browserwright session")
                return
            backend = ctx.backend if ctx is not None else (self._cfg.backend or "extension")
            if backend == "extension":
                await self._handle_extension_client(conn, ctx)
                return
            await self._handle_rdp_client(conn, ctx)
        finally:
            if task is not None:
                self._sessions.discard(task)

    async def _handle_extension_client(
        self, conn: ServerConnection, ctx: UpstreamContext | None = None,
    ) -> None:
        """Bridge a Playwright client to the extension backend via the shared
        relay. Requires the relay to be up (it is started eagerly in run_serve
        for the extension backend)."""
        relay = None
        if ctx is not None:
            relay = getattr(ctx.holder, "relay", None)
        if relay is None and self._relay_getter is not None:
            relay = self._relay_getter()
        if relay is None:
            logger.warning("facade(ext): no relay available; refusing client")
            with contextlib.suppress(Exception):
                await conn.close(code=1011, reason="extension relay unavailable")
            return
        session_id = self._session_for_connection(conn)
        session_name = None
        session_group_id = None
        if session_id:
            from ... import session_registry
            rec = session_registry.get(session_id)
            if isinstance(rec, dict):
                session_name = rec.get("name")
                runtime = rec.get("runtime") or {}
                gid = runtime.get("group_id") if isinstance(runtime, dict) else None
                if isinstance(gid, int) and gid >= 0:
                    session_group_id = gid
        bridge = ExtensionFacadeBridge(
            client=conn, relay=relay,
            session_id=session_id, session_name=session_name,
            session_group_id=session_group_id)
        try:
            await bridge.run()
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("facade(ext): bridge crashed: %r", e)
            with contextlib.suppress(Exception):
                await bridge.aclose()

    async def _handle_rdp_client(
        self, conn: ServerConnection, ctx: UpstreamContext | None = None,
    ) -> None:
        """rdp backend (PR1): transparent byte-for-byte passthrough."""
        try:
            ws_url = await self._resolve_rdp_ws(ctx)
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

    async def _resolve_rdp_ws(self, ctx: UpstreamContext | None = None) -> str:
        """Resolve the upstream Chrome CDP ws URL via the daemon resolver.

        Phase A1 is rdp-only; the resolver's rdp backend reads
        `/json/version` (or the DevToolsActivePort fallback) and returns the
        browser-level ws the daemon-owned Chrome is listening on."""
        cfg = getattr(ctx.holder, "_cfg", self._cfg) if ctx is not None else self._cfg
        rr = await resolve_upstream(cfg)
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
                # The daemon→browser CDP control channel must never traverse the
                # user's ambient web proxy (http_proxy/all_proxy). `websockets`
                # 15.x honors those env vars by default, which breaks any
                # non-loopback upstream (LAN / Tailscale / CloakBrowser) — the
                # loopback-only NO_PROXY augmentation above can't cover it.
                # proxy=None disables proxying entirely; per-page proxying is
                # applied downstream by Chrome/CloakBrowser itself. (issue #20)
                proxy=None,
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
