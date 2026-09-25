"""The daemon's single TCP **endpoint** (ADR-0011).

One ws+HTTP server on one port (default 19990) is the only way any downstream
reaches the daemon, local or remote. The path on the ws upgrade selects one of
three sub-surfaces:

  - **`/cdp` — the cdp surface.** What this module used to be *called* the
    facade: the Playwright/puppeteer-compatible browser-level CDP face a client
    reaches with `chromium.connect_over_cdp("ws://127.0.0.1:19990/cdp")`.
    Byte-identical semantics to before, including `?session=` scoping and the
    ADR-0010 sessionless auto-group.
  - **`/control` — the control surface.** The CLI/skill control plane
    (`?session=&client=` + `BrowserwrightDaemon.*` verbs), which replaced the
    unix-socket listener. Handled by `listener._ClientHandler.serve_one`,
    injected here as `control_handler`.
  - **`/exec` — the exec-relay surface.** The executor data plane
    (`exec_relay.py`). Clients no longer dial an executor's socket.

Plus the HTTP routes: `/json/version`, `/json`, `/json/list` (CDP bootstrap),
`/__ping__` (liveness + version), and the deliberately minimal `/__status__`
(daemon version plus public per-session recovery diagnoses). The operator-only
full snapshot remains on the control RPC. Any other path is a 4xx.

Security (ADR-0011, deliberate): no application-layer auth. The boundary is the
network layer — loopback by default, a tunnel/tailnet for remote — plus Origin
validation on every ws upgrade, since nothing that legitimately dials this
endpoint is a browser page.

Two backends behind the cdp surface (the consumer is always a real Playwright
client):

  - **cdp** (PR1): the daemon owns the cdp Chrome, which already speaks real
    browser-level CDP — so the facade is a transparent byte-for-byte
    passthrough: on each ws client connect we resolve the cdp Chrome's real CDP
    ws (via the daemon resolver / `backends/cdp.py`) and pump frames in both
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

The cdp surface NEVER touches the `DaemonState` / `Router` translation tables
the control surface uses: they are two protocols sharing one port, not one
protocol.
"""
from __future__ import annotations

import asyncio
import contextlib
import http
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .. import _ipc
from ..config import (DEFAULT_FACADE_PORT, LOOPBACK_HOST, Config,
                      needs_loopback_cobind)
from .. import __version__
from ..errors import Unavailable
from ..resolver import resolve as resolve_upstream
from ..._executor.protocol import _MAX_FRAME
from .daemon import Daemon, UnknownSessionError, UpstreamContext
from .exec_relay import serve_exec_relay
from .facade_extension import ExtensionFacadeBridge
from .relay import RelayServer
from .upstream import _localhost_bypass_proxy

logger = logging.getLogger(__name__)


# DEFAULT_FACADE_PORT now lives in ``config`` (no import cycle there) and is
# re-exported here for the existing call sites / tests that import it from this
# module.
__all__ = ["DEFAULT_FACADE_PORT", "PlaywrightFacade", "EndpointServer",
           "FACADE_WS_PATH", "CDP_PATH", "CONTROL_PATH", "EXEC_PATH",
           "PING_PATH"]

#: The three ws sub-surfaces of the one endpoint (ADR-0011). The path is the
#: dispatch key — nothing else distinguishes them on the wire.
CDP_PATH = "/cdp"
CONTROL_PATH = "/control"
EXEC_PATH = "/exec"
#: HTTP liveness probe. Replaces the unix socket-file ping.
PING_PATH = "/__ping__"

#: Back-compat alias for the cdp surface's path, which the advertised
#: `webSocketDebuggerUrl` must keep agreeing with.
FACADE_WS_PATH = CDP_PATH

_WS_PATHS = (CDP_PATH, CONTROL_PATH, EXEC_PATH)

#: `/exec` carries whole executor frames, whose own cap is 256 MiB
#: (`_executor/protocol._MAX_FRAME`). The ws server's `max_size` must be at
#: least that or a legal executor response would be dropped as oversized —
#: which is why this is the endpoint-wide limit and not the old 100 MiB.
_MAX_WS_SIZE = _MAX_FRAME


class PlaywrightFacade:
    """The daemon's one TCP endpoint: `/cdp`, `/control`, `/exec` + HTTP routes.

    Lifecycle mirrors `RelayServer`: ``start()`` binds (returns the bound port,
    useful with ``port=0``, which is how per-test daemons get an isolated
    endpoint); ``stop()`` closes everything cleanly.

    ``control_handler`` is `listener._ClientHandler.serve_one` — injected rather
    than imported so this module keeps knowing nothing about the Router.
    """

    def __init__(self, *, cfg: Config, port: int = DEFAULT_FACADE_PORT,
                 host: str = "127.0.0.1",
                 relay_getter: Callable[[], RelayServer | None] | None = None,
                 daemon: Daemon | None = None,
                 control_handler: Callable[[ServerConnection],
                                           Awaitable[None]] | None = None):
        self._cfg = cfg
        self._control_handler = control_handler
        self._port = port
        self._host = host
        self._server: Any = None
        # A bind to a *specific* non-loopback IP (the documented remote-access
        # setup, `--facade-host <tailnet-ip>`) does not listen on 127.0.0.1 at
        # all, which silently breaks every LOCAL client — they resolve the
        # loopback default when the endpoint state file is not visible to them
        # (different XDG_RUNTIME_DIR, sandboxed /tmp, another user). Remote
        # access must not cost local access, so we additionally bind loopback
        # on the same port and serve both from one handler.
        self._loopback_server: Any = None
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
        # ADR-0010: live auto-group sids for sessionless extension clients.
        # The reaper closes orphaned ``*-BWauto-<sid>`` groups (daemon crash /
        # extension SW death) whose sid is NOT in this set.
        self._auto_sessions: set[str] = set()
        self._reaper_task: asyncio.Task | None = None
        self._reaper_interval: float = 15 * 60.0

    # ---- lifecycle -------------------------------------------------------

    async def _serve_on(self, host: str, port: int) -> Any:
        """Bind one ws+HTTP listener for this facade on ``host:port``."""
        return await serve(
            self._handle_client,
            host,
            port,
            process_request=self._process_request,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            # CDP `Page.captureScreenshot` returns base64 blobs far above the
            # websockets 1 MiB default, and `/exec` carries whole executor
            # frames — see `_MAX_WS_SIZE`.
            max_size=_MAX_WS_SIZE,
        )

    async def start(self) -> int:
        """Bind the facade ws+HTTP server. Returns the actually-bound port.

        When ``host`` names a specific non-loopback address, loopback is bound
        as a SECOND listener on the same port so local clients keep working
        (see ``_loopback_server``). That co-bind is best-effort: it must never
        turn a working remote bind into a fatal startup failure.
        """
        self._server = await self._serve_on(self._host, self._port)
        for sock in self._server.sockets:
            sa = sock.getsockname()
            if isinstance(sa, tuple) and len(sa) >= 2:
                self._port = sa[1]
                break
        logger.info("endpoint listening on http://%s:%d (%s)",
                    self._host, self._port, ", ".join(_WS_PATHS))
        if needs_loopback_cobind(self._host):
            try:
                self._loopback_server = await self._serve_on(
                    LOOPBACK_HOST, self._port)
                logger.info(
                    "endpoint also listening on http://%s:%d "
                    "(loopback co-bind so local clients keep working)",
                    LOOPBACK_HOST, self._port)
            except OSError as e:
                # Someone else holds loopback:port, or the ephemeral port the
                # primary bind won is taken on loopback. Remote clients still
                # work; local ones need an explicit endpoint. Say so loudly —
                # this is exactly the failure mode that reads as "the daemon
                # is running but nothing can reach it".
                self._loopback_server = None
                logger.warning(
                    "endpoint could NOT also bind %s:%d (%s) — LOCAL clients "
                    "that resolve the loopback default will fail to connect. "
                    "Point them at http://%s:%d via $BW_DAEMON_URL.",
                    LOOPBACK_HOST, self._port, e, self._host, self._port)
            logger.warning(
                "endpoint is bound to non-loopback %s — it is reachable from "
                "every host that can route there, and it drives a real "
                "browser with no application-layer auth (ADR-0011). Keep it "
                "on a trusted tunnel/tailnet only.",
                self._host)
        if self._relay_getter is not None:
            self._reaper_task = asyncio.create_task(self._auto_reaper_loop())
        return self._port

    async def stop(self) -> None:
        if self._server is None:
            return
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper_task
            self._reaper_task = None
        for task in list(self._sessions):
            task.cancel()
            # `await`ing a cancelled task re-raises CancelledError, which is a
            # BaseException (not Exception) since py3.8 — suppress it explicitly
            # so one in-flight client can't abort the rest of shutdown and leak
            # the listening socket below.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._sessions.clear()
        for srv in (self._server, self._loopback_server):
            if srv is None:
                continue
            srv.close()
            with contextlib.suppress(Exception):
                await srv.wait_closed()
        self._server = None
        self._loopback_server = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def local_client_host(self) -> str:
        """Address this instance actually made reachable to local clients.

        A specific remote bind normally has a loopback co-listener.  If that
        best-effort co-bind failed, publishing loopback would point clients at
        the unrelated process that won the port, so publish the primary host
        instead and let diagnosis explain the explicit override required.
        """
        if not needs_loopback_cobind(self._host) or self._loopback_server is not None:
            return LOOPBACK_HOST
        return self._host

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
        if path == PING_PATH:
            # Liveness + version, answered before any upgrade. This is what
            # `serve` cold-start, `status`, `stop` and every client's
            # reachability check probe — it replaced the unix socket file, and
            # a successful bind of this port is now the mutual-exclusion
            # primitive that the socket file used to be.
            body = _ipc.make_pong_body(os.getpid()).decode("utf-8")
            resp = conn.respond(http.HTTPStatus.OK, body)
            resp.headers["Content-Type"] = "application/json"
            return resp
        if path == "/__status__":
            from .status import public_recovery_snapshot

            return self._http_json(conn, public_recovery_snapshot(self._daemon))
        session_id = self._session_for_request(request)
        authority = self._authority_from_request(request)
        if path == "/json/version":
            return self._http_json(conn, self._version_payload(session_id, authority))
        if path in ("/json", "/json/list"):
            return self._http_json(conn, self._list_payload(session_id, authority))
        if path in _WS_PATHS:
            denial = self._origin_denial(conn, request)
            if denial is not None:
                return denial
            return None  # allow the upgrade; `_handle_client` dispatches on path
        return conn.respond(
            http.HTTPStatus.NOT_FOUND,
            f"unknown browserwright endpoint path {path!r}; "
            f"expected one of {', '.join(_WS_PATHS)}, {PING_PATH}, "
            "/__status__, /json/version, /json, /json/list\n")

    def _origin_denial(self, conn: ServerConnection, request):
        """Anti-CSRF: refuse any ws upgrade that carries an `Origin` header.

        ADR-0011 chose the network layer as the whole security boundary, which
        makes this check the one thing standing between a page the user happens
        to have open and full code execution. It can be absolute here, unlike
        the relay's (`relay.py` §A.4, the model for this): the relay must admit
        `chrome-extension://` because that is exactly who dials it, whereas
        NOTHING that legitimately reaches this endpoint runs in a browser — the
        CLI, the skill client and Playwright's CDP transport all send no Origin.
        So any non-empty Origin is a browser, and a browser here is an attack.
        """
        try:
            origin = (request.headers.get("Origin", "")
                      or request.headers.get("origin", ""))
        except (AttributeError, KeyError):
            origin = ""
        if not origin:
            return None
        logger.warning("endpoint: refusing ws upgrade with Origin %r "
                       "(anti-CSRF)", origin)
        return conn.respond(
            http.HTTPStatus.FORBIDDEN,
            "browserwright endpoint refuses browser-originated connections "
            "(anti-CSRF): no Origin header is allowed on a ws upgrade\n")

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

    def _label_for_connection(self, conn: ServerConnection) -> str | None:
        """`?label=` query param → the auto-group's human title prefix.

        Sanitized: stripped, truncated to 40 chars, and ``-BWauto-<hex>``
        (which the title matching keys on) may not appear inside it. None →
        the bridge falls back to ``anon``.
        """
        parsed = urlparse(conn.request.path or "/")
        qs = parse_qs(parsed.query)
        label = (qs.get("label") or [None])[0]
        if label is None:
            return None
        label = " ".join(label.split())[:40]
        if not label or "-BWauto-" in label:
            return None
        return label

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

    @staticmethod
    def _path_for_connection(conn: ServerConnection) -> str:
        path = urlparse(conn.request.path or "/").path or "/"
        return path.rstrip("/") if len(path) > 1 else path

    async def _handle_client(self, conn: ServerConnection) -> None:
        """Dispatch one accepted ws upgrade to its sub-surface (ADR-0011)."""
        path = self._path_for_connection(conn)
        if path == CONTROL_PATH:
            if self._control_handler is None:
                with contextlib.suppress(Exception):
                    await conn.close(
                        code=1011, reason="control surface not wired")
                return
            await self._control_handler(conn)
            return
        if path == EXEC_PATH:
            await serve_exec_relay(
                conn, daemon=self._daemon,
                session_id=self._session_for_connection(conn))
            return
        await self._handle_cdp_surface(conn)

    async def _handle_cdp_surface(self, conn: ServerConnection) -> None:
        """One Playwright client connected. For the extension backend, bridge
        through the shared relay with target-event synthesis (PR2); otherwise
        resolve the cdp Chrome's real CDP ws and pump frames byte-for-byte."""
        task = asyncio.current_task()
        lease_token: object | None = None
        if task is not None:
            self._sessions.add(task)

        async def revoke_connection() -> None:
            with contextlib.suppress(Exception):
                await conn.close(code=1008, reason="browserwright session ended")
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        try:
            try:
                # A sessionless client used to be refused when the shared
                # context was `env`: that context had no session identity and
                # the ledger allowed only one env session per daemon, so "whose
                # browser is this?" had no answer. With the endpoint carried per
                # session (#38) a sessionless client cannot reach anyone's
                # session browser at all — it gets the operator-configured
                # default port — so the ambiguity, and the refusal, are gone.
                session_id = self._session_for_connection(conn)
                acquire = getattr(self._daemon, "acquire_session_lease", None)
                if session_id and callable(acquire):
                    lease_token = object()
                    ctx = acquire(
                        session_id, lease_token, revoke_connection,
                        kind="facade")
                else:
                    ctx = self._context_for_connection(conn)
            except UnknownSessionError:
                with contextlib.suppress(Exception):
                    await conn.close(code=1008, reason="unknown browserwright session")
                return
            backend = ctx.backend if ctx is not None else (self._cfg.backend or "extension")
            if backend == "extension":
                await self._handle_extension_client(conn, ctx)
                return
            await self._handle_cdp_client(conn, ctx)
        finally:
            release = getattr(self._daemon, "release_session_lease", None)
            if lease_token is not None and callable(release):
                release(lease_token)
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
            relay = ctx.upstream.relay
        if relay is None and self._relay_getter is not None:
            relay = self._relay_getter()
        if relay is None:
            logger.warning("facade(ext): no relay available; refusing client")
            with contextlib.suppress(Exception):
                await conn.close(code=1011, reason="extension relay unavailable")
            return
        session_id = self._session_for_connection(conn)
        binding_owner = None
        if ctx is not None:
            try:
                await ctx.holder.ensure_open()
            except Exception as e:  # noqa: BLE001
                logger.warning("facade(ext): upstream unavailable: %r", e)
                with contextlib.suppress(Exception):
                    await conn.close(code=1011, reason="extension upstream unavailable")
                return
            from .extension_upstream import ExtensionUpstream
            candidate = ctx.router.upstream
            if isinstance(candidate, ExtensionUpstream):
                binding_owner = candidate
        bridge = ExtensionFacadeBridge(
            client=conn, relay=relay,
            session_id=session_id, binding_owner=binding_owner,
            label=self._label_for_connection(conn),
            auto_registry=self._auto_sessions)
        try:
            await bridge.run()
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("facade(ext): bridge crashed: %r", e)
            with contextlib.suppress(Exception):
                await bridge.aclose()

    # ---- auto-group reaper (ADR-0010) -----------------------------------

    _AUTO_TITLE_RE = re.compile(r"-BW(auto-[0-9a-f]{8})$")

    @classmethod
    def _auto_sid_from_title(cls, title: str) -> str | None:
        m = cls._AUTO_TITLE_RE.search(title or "")
        return m.group(1) if m else None

    async def _auto_reaper_loop(self) -> None:
        """Sweep orphaned auto groups: once shortly after startup (a previous
        daemon crash / extension SW death can strand groups), then every
        ``_reaper_interval``. Best-effort; failures only log."""
        try:
            await asyncio.sleep(1.0)  # let the extension reconnect first
            await self._sweep_auto_groups()
            while True:
                await asyncio.sleep(self._reaper_interval)
                await self._sweep_auto_groups()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("facade(ext): auto-group reaper crashed")

    async def _sweep_auto_groups(self) -> None:
        """Close ``*-BWauto-<sid>`` groups whose sid has no live bridge.

        Live bridges register their sid in ``self._auto_sessions`` at connect
        and unregister at teardown; a group whose owner is gone is an orphan.
        """
        relay = self._relay_getter() if self._relay_getter is not None else None
        if relay is None:
            return
        try:
            groups = await relay.list_groups(timeout=10.0)
        except Exception:  # noqa: BLE001 - best-effort sweep
            return
        for g in groups:
            title = g.get("title") if isinstance(g, dict) else ""
            sid = self._auto_sid_from_title(str(title or ""))
            if sid is None or sid in self._auto_sessions:
                continue
            logger.info("facade(ext): reaping orphaned auto group %r", title)
            try:
                await relay.close_group_tabs(str(title), timeout=10.0)
            except Exception as e:  # noqa: BLE001 - best-effort
                logger.warning(
                    "facade(ext): reaper close failed for %r: %r", title, e)

    async def _handle_cdp_client(
        self, conn: ServerConnection, ctx: UpstreamContext | None = None,
    ) -> None:
        """cdp backend (PR1): transparent byte-for-byte passthrough."""
        # Open the session's upstream first, exactly as the extension path does.
        # For a `--create` session the browser is launched lazily by
        # `ensure_open`, so resolving before it ran meant probing a port nothing
        # was listening on yet and closing 1011. That was survivable while this
        # path was the exception; per-session endpoints make it the common case.
        if ctx is not None:
            try:
                await ctx.holder.ensure_open()
            except Exception as e:  # noqa: BLE001
                logger.warning("facade: upstream unavailable: %r", e)
                with contextlib.suppress(Exception):
                    await conn.close(code=1011, reason="upstream unavailable")
                return
        try:
            ws_url = await self._resolve_cdp_ws(ctx)
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

    async def _resolve_cdp_ws(self, ctx: UpstreamContext | None = None) -> str:
        """Resolve the upstream Chrome CDP ws URL via the daemon resolver.

        Reads the *adapter's* cfg, not the daemon-wide one. That is the whole
        channel by which a per-session endpoint reaches the facade: the port or
        URL from the session's ledger record was pinned into that Config by
        `upstream_context.cdp_cfg_for`, and a lazily allocated `--create` port
        is pinned back into it by the adapter's launch. Anything that moves
        the endpoint out of the Config has to teach this function a second way
        to find it."""
        cfg = ctx.upstream.cfg if ctx is not None else self._cfg
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


#: ADR-0011 name for what this class now is. `PlaywrightFacade` stays as the
#: class's own name (it is spelled in a lot of call sites and tests), but new
#: code should read `EndpointServer` — the object serves three surfaces, only
#: one of which is Playwright's.
EndpointServer = PlaywrightFacade
