"""WebSocket listener + lifecycle orchestrator.

This module wires together:
  - `_ipc` (socket file / token / ping)
  - `state` (DaemonState)
  - `upstream` (UpstreamConnection)
  - `proxy` (Router)

Spec §8.5: the listener task accepts clients, the upstream-lifecycle task
opens/closes the upstream ws lazily, and the keepalive task is built into
UpstreamConnection (heartbeat) + websockets server (ws-level pings).

v0.2 single-client model: the second ws upgrade is rejected with HTTP 503
+ a clear body. spec §9.2.
"""
from __future__ import annotations

import asyncio
import contextlib
import http
import json
import logging
import os
import signal
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection, serve, unix_serve

from .. import _ipc
from .. import __version__
from ..config import Config
from ..errors import Unavailable
from ..resolver import resolve
from ..observability import metrics, install_json_logging_if_requested
from .state import CloseReason, DaemonState, UpstreamPhase
from .proxy import Router
from .upstream import UpstreamConnection
from .relay import RelayServer
from .extension_upstream import ExtensionUpstream

logger = logging.getLogger(__name__)


# ---- top-level entry -------------------------------------------------------


async def run_serve(cfg: Config) -> int:
    """Run a Mode B daemon until SIGTERM / Ctrl-C / shutdown. Returns exit code."""
    name = cfg.name
    _ipc.check_name(name)

    # Stale-detect: ping any existing endpoint before binding. If something
    # answers, refuse to start a second copy of ourselves — but if the ping
    # comes back negative, we cleanup the dead socket file and proceed.
    existing_pid = await _ipc.ping_async(name, timeout=1.0)
    if existing_pid is not None:
        print(
            f"browser-daemon[{name}] already running (pid {existing_pid}); "
            f"use `browser-daemon stop --name {name}` to shut it down",
            file=sys.stderr,
        )
        return 1
    _ipc.cleanup_endpoint(name)

    # Log file is best-effort — we route Python logging to it but never crash
    # the daemon over a write failure.
    _wire_logging(name)
    # v0.5: opt-in JSON log formatter. After _wire_logging adds the
    # file/console handlers, swap formatters in place if BD_LOG_JSON=1.
    install_json_logging_if_requested()
    logger.info("browser-daemon %s starting (name=%s, backend=%s)",
                __version__, name, cfg.backend or "auto")

    state = DaemonState(name=name, backend_name=cfg.backend or "auto")
    router = Router(state)
    upstream = _UpstreamHolder(state, router, cfg)

    # SIGTERM / SIGINT → set the stop event. We don't tear down inline because
    # we still need to run the graceful shutdown sequence (close clients with
    # 1011 + emit upstreamClosed + close upstream).
    stop = asyncio.Event()

    def _on_signal():
        stop.set()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(s, _on_signal)
            except NotImplementedError:
                pass  # e.g. inside pytest event loop

    # PID file (best-effort).
    _ipc.write_pid(name, os.getpid())

    handler = _ClientHandler(state, router, upstream, cfg)
    server = await _open_server(name, handler)

    # v0.4: for `--backend extension`, start the relay ws server eagerly so
    # `browser-daemon doctor` can probe `__status__` even before any Skill
    # client connects.
    if cfg.backend == "extension":
        try:
            # v0.5.3 F-5 / Task #24: bind at the configured host+port.
            # Precedence (CLI > env > toml port > toml relay_url > default)
            # is centralized in cfg.backends.extension.resolved_host_port().
            host, port = cfg.backends.extension.resolved_host_port()
            upstream.relay = RelayServer(host=host, port=port)
            port = await upstream.relay.start()
            logger.info("extension relay started on port %d", port)
        except OSError as e:
            print(
                f"browser-daemon[{name}] failed to bind extension relay: {e}",
                file=sys.stderr,
            )
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
            _ipc.cleanup_endpoint(name)
            return 2

    idle_task: asyncio.Task | None = None
    if cfg.idle_close_after is not None and cfg.idle_close_after > 0:
        idle_task = asyncio.create_task(_idle_watchdog(state, upstream, cfg.idle_close_after))
    try:
        await stop.wait()
        logger.info("browser-daemon[%s] shutdown requested", name)
        await _graceful_shutdown(state, router, upstream)
    finally:
        if idle_task is not None:
            idle_task.cancel()
            with contextlib.suppress(Exception):
                await idle_task
        if upstream.relay is not None:
            with contextlib.suppress(Exception):
                await upstream.relay.stop()
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
        _ipc.cleanup_endpoint(name)
    return 0


# ---- log wiring ------------------------------------------------------------


def _wire_logging(name: str) -> None:
    """Route the daemon's logger to a file under TMPDIR. Best-effort."""
    try:
        log_p = _ipc.log_path(name)
        log_p.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_p), encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # Also keep a console echo when stderr is a TTY (foreground serve).
        if sys.stderr.isatty():
            root.addHandler(logging.StreamHandler(sys.stderr))
        root.addHandler(handler)
    except OSError:
        pass


# ---- websockets server with single-client gate ----------------------------


async def _open_server(name: str, handler: "_ClientHandler"):
    """Bind the listener with correct umask / file perms for POSIX, or the
    token file for Windows. The HTTP /__ping__ path is intercepted here so
    stale-detect works without a ws upgrade."""
    process_request = _make_process_request(name, handler)

    if _ipc.IS_WINDOWS:
        sock, port, token = _ipc.make_tcp_socket()
        _ipc.write_port_file(name, port, token)
        handler.token = token
        server = await serve(
            handler.serve_one,
            sock=sock,
            process_request=process_request,
            max_size=100 * 1024 * 1024,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info("listening on 127.0.0.1:%d (token=%s...)", port, token[:8])
        return server

    sock = _ipc.make_unix_socket(name)
    # Verify the 0600 perms — spec §6.2 promises it; failing loudly here is
    # better than silently exposing the socket.
    st = os.stat(_ipc.sock_path(name))
    if (st.st_mode & 0o777) != 0o600:
        logger.warning("unexpected sock perms %o", st.st_mode & 0o777)
    server = await unix_serve(
        handler.serve_one,
        sock=sock,
        process_request=process_request,
        max_size=100 * 1024 * 1024,
        compression=None,
        ping_interval=20,
        ping_timeout=20,
    )
    logger.info("listening on %s", _ipc.sock_path(name))
    return server


def _make_process_request(name: str, handler: "_ClientHandler"):
    """Intercept the HTTP handshake.

    Two responsibilities:
    1. `/__ping__` GET → return a 200 with {"pong":true,"pid":N} so the
       stale-detect probe works *before* a ws upgrade.
    2. On Windows, verify the `?token=` query matches the server token.

    v0.3: the single-client gate from v0.2 is **gone**. Multiple clients
    connect concurrently; the router's sessionId/id translation keeps them
    cleanly separated.
    """
    def process_request(conn: ServerConnection, request) -> Any:
        path = request.path or "/"
        if path.startswith("/__ping__"):
            body = _ipc.make_pong_body(os.getpid())
            resp = conn.respond(http.HTTPStatus.OK, body.decode("utf-8"))
            resp.headers["Content-Type"] = "application/json"
            return resp
        if _ipc.IS_WINDOWS:
            query = _parse_query(path)
            got = query.get("token")
            if got != handler.token:
                resp = conn.respond(
                    http.HTTPStatus.UNAUTHORIZED,
                    "missing or wrong ?token=\n",
                )
                return resp
        return None  # allow upgrade
    return process_request


def _parse_query(path: str) -> dict[str, str]:
    """Pull single-valued query params from the request path."""
    parsed = urlparse(path)
    q = parse_qs(parsed.query)
    return {k: v[0] for k, v in q.items() if v}


# ---- per-client handler ----------------------------------------------------


class _ClientHandler:
    """Stateless adapter object — websockets gives us a ServerConnection per
    incoming client; we wire it through the Router."""

    def __init__(self, state: DaemonState, router: Router,
                 upstream: "_UpstreamHolder", cfg: Config):
        self.state = state
        self.router = router
        self.upstream = upstream
        self.cfg = cfg
        self.token: str | None = None

    async def serve_one(self, conn: ServerConnection) -> None:
        """v0.3: handler instance per client connection — many run concurrently.

        Each gets its own ClientState in DaemonState.clients and its own
        send-to-client closure registered with the router. On disconnect we
        release the client's sessions (which also pops attachers it owned)
        but leave upstream warm for surviving clients.
        """
        query = _parse_query(conn.request.path or "/")
        label = query.get("client", "anonymous")
        client = self.state.allocate_client(label)

        async def send_to_client(text: str) -> None:
            try:
                await conn.send(text)
            except Exception as e:
                logger.warning("client %d send failed: %r", client.client_id, e)

        self.router.register_client(client.client_id, send_to_client)
        self.router.bind_lifecycle(
            ensure_upstream=self.upstream.ensure_open,
            trigger_disconnect=self.upstream.trigger_close,
        )

        # If upstream is already open (warm from another client), make sure the
        # router has the send fn wired. (The first ensure_open call wires it
        # internally; subsequent client sessions inherit it.)
        if self.upstream.is_open:
            self.router.update_upstream_send(self.upstream.send_text)

        metrics().client_connected_total += 1
        logger.info("client %d connected (label=%s, total=%d)",
                    client.client_id, label, len(self.state.clients))
        try:
            async for raw in conn:
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                metrics().client_frame_received_total += 1
                await self.router.route_from_client(client, text)
        except websockets.exceptions.ConnectionClosed:
            logger.info("client %d disconnected", client.client_id)
        except Exception as e:
            logger.warning("client %d crashed: %r", client.client_id, e)
        finally:
            metrics().client_disconnected_total += 1
            self.state.release_client(client.client_id)
            self.router.unregister_client(client.client_id)
            # Upstream stays warm so other clients (or the next reconnect)
            # don't pay banner-flash for our churn.


# ---- upstream lifecycle ----------------------------------------------------


class _UpstreamHolder:
    """Owns the single UpstreamConnection. Provides lazy-open + graceful-close
    primitives the Router can call.

    v0.4: when `cfg.backend == "extension"` we replace the conventional ws
    upstream with an ExtensionUpstream wrapping a RelayServer. The relay is
    started eagerly (so doctor probe answers `available=true` as soon as
    the daemon is up), and `ensure_open` blocks the first client until the
    extension has connected.
    """

    def __init__(self, state: DaemonState, router: Router, cfg: Config):
        self.state = state
        self.router = router
        self.upstream: UpstreamConnection | ExtensionUpstream | None = None
        self._open_lock = asyncio.Lock()
        self._cfg: Config = cfg
        # v0.4: only populated when backend=extension. Owned by the holder
        # for the daemon's full lifetime; we don't tear down on idle-close
        # so the extension's persistent ws to us stays warm.
        self.relay: RelayServer | None = None

    @property
    def is_open(self) -> bool:
        return self.upstream is not None and self.upstream.is_open

    async def send_text(self, frame: str) -> None:
        """Proxy to the live UpstreamConnection.send_text.

        Exposed on the holder so callers in this module can pass
        `holder.send_text` as the router's `upstream_send` callable without
        needing to drill through `holder.upstream.send_text` (which races
        with close: holder.upstream may become None mid-call).
        """
        conn = self.upstream
        if conn is None:
            raise RuntimeError("upstream not open")
        await conn.send_text(frame)

    async def _broadcast_event(self, method: str, params: dict) -> None:
        """Fan a `{method, params}` envelope to every connected client.
        Same shape as the existing `upstreamClosed` broadcast (listener
        spec §6.5). Used by v0.5.3 F-3: surface `upstreamConnecting` and
        `upstreamReady` lifecycle events so Skill code subscribing per
        design-v2.md:550-551 actually sees something."""
        envelope = json.dumps({"method": method, "params": params})
        for cid in list(self.state.clients.keys()):
            try:
                await self.router._send_to_client(cid, envelope)
            except Exception:
                pass

    async def ensure_open(self) -> None:
        """Open upstream if not already. Idempotent + reentrant-safe.

        v0.4 branches on `cfg.backend == "extension"`:
          - extension → wait for the relay's first extension to send hello,
            wrap in ExtensionUpstream, mark CONNECTED
          - everything else → resolve a CDP ws URL and connect a real
            UpstreamConnection

        v0.5.3 F-3: emits two lifecycle events to subscribed clients:
          - `BrowserDaemon.upstreamConnecting {backend}` at the start of
            the open attempt (after we've taken the lock and bumped state
            to CONNECTING)
          - `BrowserDaemon.upstreamReady {backend, ws_url}` on successful
            open (after `state.set_connected`)
        Failed-open paths emit `upstreamClosed {reason}` via the
        `trigger_close` path the resolver/connect call site already runs.
        """
        if self.is_open:
            return
        async with self._open_lock:
            if self.is_open:
                return
            cfg = self._cfg
            await self.state.begin_connecting(cfg.backend or "auto")
            metrics().upstream_open_attempts_total += 1
            # F-3: emit BrowserDaemon.upstreamConnecting to all clients.
            await self._broadcast_event(
                "BrowserDaemon.upstreamConnecting",
                {"backend": cfg.backend or "auto"},
            )

            try:
                if cfg.backend == "extension":
                    await self._open_extension_upstream(cfg)
                else:
                    await self._open_chrome_upstream(cfg)
            except Exception:
                metrics().upstream_open_failed_total += 1
                raise
            else:
                metrics().upstream_open_succeeded_total += 1
                # F-3: emit BrowserDaemon.upstreamReady. `state.upstream_ws_url`
                # is set by both open paths via `state.set_connected(...)`.
                await self._broadcast_event(
                    "BrowserDaemon.upstreamReady",
                    {
                        "backend": cfg.backend or "auto",
                        "ws_url": self.state.upstream_ws_url,
                    },
                )

            # Task #76: any client frame that arrived during the lazy-open
            # window was buffered per-client. Replay them now that the
            # upstream is live and `_upstream_send` is wired.
            try:
                await self.router.drain_pre_open_buffers()
            except Exception as e:
                logger.warning("drain pre-open buffers failed: %r", e)

    async def _open_chrome_upstream(self, cfg: Config) -> None:
        # Mark this resolve as Mode-B-originated. The autoconnect backend
        # uses this to bypass its rate-limit defense — Mode B holds one
        # upstream ws across many client connections, so the popup-storm
        # rationale doesn't apply.
        from .. import resolver as _resolver_mod
        ctx_token = _resolver_mod.caller_context.set("mode_b_serve")
        try:
            rr = await resolve(cfg)
        except Unavailable as e:
            logger.warning("upstream resolve failed: %s", e)
            self.state.last_close_reason = "backend_lost"
            await self.state.set_disconnected()
            _resolver_mod.caller_context.reset(ctx_token)
            raise
        _resolver_mod.caller_context.reset(ctx_token)

        # v0.5: when backend=cloud, ask the cloud config's AuthProvider to
        # produce headers + ssl_context for the upstream ws handshake. For
        # every other backend (env/rdp/autoconnect) the provider is None
        # and connect runs unchanged.
        additional_headers: dict[str, str] = {}
        ssl_context = None
        if cfg.backend == "cloud":
            additional_headers, ssl_context = await self._build_cloud_auth(cfg)

        try:
            conn = UpstreamConnection(
                on_frame=self.router.forward_from_upstream,
                on_close=self._on_upstream_closed,
            )
            await conn.open(
                rr.ws_url,
                timeout=cfg.timeout,
                additional_headers=additional_headers or None,
                ssl_context=ssl_context,
            )
        except Exception as e:
            logger.warning("upstream open failed: %r", e)
            self.state.last_close_reason = "backend_lost"
            await self.state.set_disconnected()
            raise
        self.upstream = conn
        self.router.update_upstream_send(conn.send_text)
        # Tell Chrome to gossip about all targets so we can maintain the
        # last_activated table without needing the client to enable it.
        # `waitForDebuggerOnStart=False` keeps target creation immediate.
        try:
            await conn.send_command(
                "Target.setDiscoverTargets", {"discover": True})
        except Exception as e:
            logger.warning("setDiscoverTargets failed: %r", e)
        await self.state.set_connected(rr.ws_url, was_popup=False)

    async def _build_cloud_auth(self, cfg: Config) -> tuple[dict[str, str], Any]:
        """Build (headers, ssl_context) for the cloud backend's upstream
        ws handshake. Pulls the AuthProvider from `cfg.backends.cloud`.

        Errors at this layer are logged and converted to "no auth"
        gracefully — the connect itself will then 401, which surfaces a
        clear `backend_lost` close reason to clients.
        """
        from ..auth import build_auth_provider
        from ..errors import UserError
        cc = cfg.backends.cloud
        if not cc.auth_kind:
            return {}, None
        try:
            provider = build_auth_provider(cc.auth_kind, cc.auth)
            headers = await provider.headers()
            ssl_ctx = provider.ssl_context()
            return headers, ssl_ctx
        except UserError as e:
            logger.warning("cloud auth misconfigured: %s", e)
            return {}, None

    async def _open_extension_upstream(self, cfg: Config) -> None:
        """v0.4 extension backend: the daemon IS the upstream.

        The relay was already started at daemon launch (run_serve). All we
        do here is wait for an extension to connect (with timeout) and wrap
        the relay in an ExtensionUpstream. The relay stays alive across
        idle-close / reconnect cycles.
        """
        if self.relay is None:
            # Bug: holder wasn't bootstrapped with a relay. Fall back to
            # raising — surface the misconfig instead of hanging silently.
            self.state.last_close_reason = "backend_lost"
            await self.state.set_disconnected()
            raise Unavailable(
                "extension backend selected but relay was never started — "
                "internal bug, please report")
        try:
            ext = ExtensionUpstream(
                relay=self.relay,
                on_frame=self.router.forward_from_upstream,
                on_close=self._on_upstream_closed,
            )
            # Use the daemon's open timeout (default 5s in tests) but allow
            # the user a generous window (60s) to load the extension. Spec
            # §8.4 'extension-permission' ux_cost — user has to click the
            # popup; that takes seconds.
            timeout = max(cfg.timeout, 60.0)
            await ext.open(timeout=timeout)
        except asyncio.TimeoutError:
            self.state.last_close_reason = "backend_lost"
            await self.state.set_disconnected()
            raise Unavailable(
                "no extension connected within timeout — load the daemon's "
                "Chrome extension from `chrome-extension/`")
        except Exception as e:
            logger.warning("extension upstream open failed: %r", e)
            self.state.last_close_reason = "backend_lost"
            await self.state.set_disconnected()
            raise
        self.upstream = ext
        self.router.update_upstream_send(ext.send_text)
        # IMPORTANT: wire all extension-only verb callbacks BEFORE
        # state.set_connected — concurrent BrowserDaemon.* handlers in the
        # proxy gate on state.upstream_phase == CONNECTED to skip the lazy-
        # open call, so if we flip the phase first they'd see callback=None
        # and respond -32601 incorrectly. Tear-down in trigger_close runs
        # the opposite order (clear callbacks AFTER set_disconnected) for
        # the symmetric reason.
        # v0.5.4: wire the daemon-driven attach-active path. Only the
        # extension backend has an out-of-band attach verb; other backends
        # leave the callback as None so the proxy errors -32601.
        self.router._attach_active_tab = ext.attach_active_tab
        # Phase B: open_background + close_tab — same extension-only contract.
        self.router._open_background_tab = ext.open_background_tab
        self.router._close_tab = ext.close_tab
        self.router._close_tab_by_target_id = ext.close_tab_by_target_id
        await self.state.set_connected(ext.ws_url or "ext://relay",
                                       was_popup=False)

    async def trigger_close(self, reason: CloseReason) -> None:
        """Run the spec §6.5 close etiquette + tear down upstream.

        Sequence per spec §6.5:
          1. send Target.detachedFromTarget for each owned sessionId
          2. send BrowserDaemon.upstreamClosed
          3. close client ws with 1011
        We do (1)+(2) here. The actual ws close (3) is the client handler's
        job; we set state so the handler's outer `async for` returns.
        """
        if self.state.upstream_phase in (UpstreamPhase.DISCONNECTED, UpstreamPhase.CLOSING):
            # Already closing / closed — idempotent.
            return
        await self.state.begin_closing(reason)

        # Spec §6.5 step 1: per-session synthetic Target.detachedFromTarget
        # events. v0.3 sends them to EACH client that owns a session, with
        # that client's local sessionId AND the real targetId (the v0.2
        # "<unknown>" placeholder upgrade).
        for cid, client in list(self.state.clients.items()):
            for local_sid, binding in list(client.sessions.items()):
                try:
                    await self.router._send_to_client(cid, json.dumps({
                        "method": "Target.detachedFromTarget",
                        "params": {
                            "sessionId": local_sid,
                            "targetId": binding.target_id,
                        },
                    }))
                except Exception:
                    pass
            # We don't clear client.sessions here — set_disconnected() below
            # wipes everyone's sessions atomically.

        # Spec §6.5 step 2: BrowserDaemon.upstreamClosed event broadcast.
        for cid in list(self.state.clients.keys()):
            try:
                await self.router._send_to_client(cid, json.dumps({
                    "method": "BrowserDaemon.upstreamClosed",
                    "params": {"reason": reason},
                }))
            except Exception:
                pass

        # Tear down upstream ws.
        up = self.upstream
        self.upstream = None
        self.router.update_upstream_send(None)
        # v0.5.4: drop the extension-backend attach-active callback so
        # post-close BrowserDaemon.attachActiveTab returns -32601 instead
        # of racing against a torn-down upstream.
        self.router._attach_active_tab = None
        self.router._open_background_tab = None
        self.router._close_tab = None
        self.router._close_tab_by_target_id = None
        if up is not None:
            try:
                await up.close(code=1000, reason=reason)
            except Exception:
                pass

        # Spec §6.5 step 3: close client ws. The handler's `async for` will
        # exit naturally on the next read once we set state DISCONNECTED;
        # for prompt teardown we'd need to plumb each ServerConnection in
        # — left as a follow-up since the natural-exit path is reliable.
        await self.state.set_disconnected()

    async def _on_upstream_closed(self, reason: str) -> None:
        """Called by UpstreamConnection's reader when upstream drops on its
        own (Chrome exited, etc.). We translate to a CloseReason and run
        the close-etiquette path."""
        metrics().upstream_closed_total += 1
        if self.state.upstream_phase in (UpstreamPhase.DISCONNECTED, UpstreamPhase.CLOSING):
            return
        await self.trigger_close("chrome_exit")


# ---- graceful shutdown -----------------------------------------------------


async def _idle_watchdog(state: DaemonState, upstream: "_UpstreamHolder",
                         idle_after: float) -> None:
    """Spec §6.5/§6.6: when configured, close upstream after `idle_after`
    seconds with no activity. The next client command lazy-opens it again.

    Default is None (never) — the listener only schedules this task when
    `idle_close_after` is set. We poll at half the threshold so the wakeup
    granularity matches the policy.
    """
    poll = max(1.0, idle_after / 2.0)
    try:
        while True:
            await asyncio.sleep(poll)
            if state.upstream_phase != UpstreamPhase.CONNECTED:
                continue
            idle_for = time.time() - state.last_activity_at
            if idle_for >= idle_after:
                logger.info("idle-watchdog: closing upstream after %.1fs", idle_for)
                try:
                    await upstream.trigger_close("idle_close")
                except Exception as e:
                    logger.warning("idle close failed: %r", e)
    except asyncio.CancelledError:
        return


async def _graceful_shutdown(state: DaemonState, router: Router,
                             upstream: "_UpstreamHolder") -> None:
    """Called on SIGTERM. Run close etiquette then close listener."""
    try:
        await upstream.trigger_close("daemon_shutdown")
    except Exception as e:
        logger.warning("shutdown close failed: %r", e)


# ---- helper for the cli serve dispatcher ----------------------------------


def make_holder(state: DaemonState, router: Router, cfg: Config) -> _UpstreamHolder:
    """Test seam: build an _UpstreamHolder pre-bound to cfg."""
    return _UpstreamHolder(state, router, cfg)
