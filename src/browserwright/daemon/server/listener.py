"""Daemon lifecycle orchestrator + the endpoint's control-surface handler.

This module wires together:
  - `_ipc` (runtime files / ping)
  - `state` (DaemonState)
  - `upstream` (the Upstream protocol and its adapters)
  - `proxy` (Router)

Spec §8.5: the listener task accepts clients, the upstream-lifecycle task
opens/closes the upstream ws lazily, and the keepalive task is built into
CdpUpstream (heartbeat) + websockets server (ws-level pings).

ADR-0011: this module no longer *binds* anything client-facing. The one TCP
endpoint lives in `facade.py`; `run_serve` builds it and hands it
`_ClientHandler.serve_one` as the `/control` handler.
"""
from __future__ import annotations

from collections.abc import Callable

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection

from .. import _ipc
from .. import __version__
from ..config import Config
from ..errors import Unavailable
from ..resolver import resolve
from ..observability import (
    install_json_logging_if_requested,
    install_sigusr1_traceback,
)
from .state import CloseReason, DaemonState, UpstreamPhase
from .proxy import Router
from .daemon import Daemon, UnknownSessionError, UpstreamContext
from .upstream import CdpUpstream, Upstream
from .relay import RelayServer
from .extension_upstream import ExtensionUpstream
from .facade import PlaywrightFacade

logger = logging.getLogger(__name__)

_SESSION_PRUNE_INTERVAL_S = 3600.0

# A (auto-recovery): wait this long after an extension hello before
# re-attaching sessions, so a version-drift reload (which kills the SW right
# after this hello) has time to land first; and throttle consecutive recovery
# sweeps so a reconnect burst (maintainLoop backoff) does not hammer the
# extension with attach round-trips.
_AUTO_RECOVER_DELAY_S = 3.0
_AUTO_RECOVER_THROTTLE_S = 10.0


def _executor_ready_budget_s() -> float:
    """Bound extension reconnect grace below the control-plane deadline."""
    try:
        return float(os.environ.get("BW_EXT_READY_BUDGET_S", "") or 10.0)
    except (TypeError, ValueError):
        return 10.0


_EXECUTOR_READY_BUDGET_S = _executor_ready_budget_s()

_NO_EXTENSION_CONNECTED_MSG = (
    "no browserwright extension is connected to the daemon (session {sid}). "
    "Open the browser where the extension is installed and ensure it is "
    "enabled; if you just installed or upgraded browserwright, reload the "
    "extension at chrome://extensions so its service worker reconnects to the "
    "daemon relay. Then retry. (Use --backend=cdp --create for an isolated "
    "Chrome that needs no extension.)"
)


# ---- per-upstream context factory ------------------------------------------


def make_context(*, backend: str, cfg: Config,
                 session_id: str | None = None) -> UpstreamContext:
    """Build one `UpstreamContext` — the `(state, router, holder)` triple for a
    single upstream, wired exactly like `run_serve` wired the single triple
    before Phase 2. Lives here (not in daemon.py) because it constructs the
    `_UpstreamHolder`, which is a listener-module concern.

    The relay is NOT started here (only the extension *shared* context gets a
    relay, started eagerly in `run_serve`); for everything else the holder's
    lazy-open path opens the upstream on first client frame.
    """
    state = DaemonState(backend_name=backend)
    router = Router(state)
    holder = _UpstreamHolder(state, router, cfg, session_id=session_id)
    return UpstreamContext(
        backend=backend, state=state, router=router, holder=holder,
        session_id=session_id,
    )


# ---- top-level entry -------------------------------------------------------


def _reclaim_stale_daemon_ports(cfg: Config, *, probe=None) -> None:
    """Reclaim the relay/facade ports from a *confirmed* stale browserwright
    daemon (issue #15, 2.2). No-op when the ports are free, held by an
    unconfirmed process, or held by a browserwright daemon from a DIFFERENT
    runtime dir (issue #44 B — that is someone else's live daemon, e.g. the
    machine-global one; we never SIGTERM a stranger).

    The write-side twin of `cli status`: both read the world through the same
    `probe.DaemonProbe` so they can never disagree about which ports matter or
    who counts as a confirmed holder. Only the reclaim itself is local, because
    only this side mutates. `probe` is injectable for tests; production passes
    nothing.
    """
    from .. import _stale
    from ..probe import DaemonProbe

    p = probe if probe is not None else DaemonProbe(cfg)
    held = p.listening_ports(p.daemon_ports())
    if not held:
        return
    pid = p.confirmed_stale_holder(held)
    if pid is None:
        logger.warning(
            "ports %s are in use but no confirmed browserwright daemon holds "
            "them; leaving them alone (bind will surface a clear error)", held)
        return
    # issue #44 B: a *stale* daemon is one that crashed on the SAME control
    # socket we are about to bind — i.e. the same runtime dir. A confirmed
    # browserwright daemon from a DIFFERENT runtime dir is someone else's live
    # daemon: the machine-global daemon (default runtime dir /tmp) or a sibling
    # worktree's isolated e2e daemon. SIGTERMing it would kill the user's daily
    # daemon — never do that; refuse loudly and let the bind (if we actually
    # need the port) fail with an actionable message instead.
    if not _stale.same_runtime_dir_as_us(pid):
        logger.warning(
            "ports %s are held by browserwright daemon pid %d from a DIFFERENT "
            "runtime dir (%s) — not a stale daemon of ours, so refusing to "
            "signal it (issue #44 B). That is likely the machine-global daemon "
            "or another worktree's e2e daemon; if these ports are needed, stop "
            "that daemon yourself.",
            held, pid, _stale.pid_runtime_dir(pid))
        return
    logger.warning(
        "reclaiming ports %s from stale browserwright daemon pid %d (issue #15)",
        held, pid)
    if _stale.reclaim_ports(pid, held):
        logger.info("reclaimed ports %s from pid %d", held, pid)
    else:
        logger.warning("could not free ports %s from pid %d; bind may fail",
                       held, pid)


def _local_probe_host(facade_host: str) -> str:
    """The address a client ON THIS MACHINE uses for a daemon bound to
    ``facade_host``: loopback whenever the facade co-binds it (specific
    non-loopback host, or a wildcard), else the host itself."""
    from ..config import (LOOPBACK_HOST, _LOOPBACK_COVERING_HOSTS,
                          needs_loopback_cobind)
    if needs_loopback_cobind(facade_host) or facade_host in _LOOPBACK_COVERING_HOSTS:
        return LOOPBACK_HOST
    return facade_host


async def run_serve(cfg: Config) -> int:
    """Run a Mode B daemon until SIGTERM / Ctrl-C / shutdown. Returns exit code.

    There is exactly one global daemon on a fixed socket — no instance name.
    """
    # Stale-detect: ping the endpoint before binding. If something answers,
    # refuse to start a second copy of ourselves (enforces the "at most one
    # global daemon" invariant). ADR-0011 re-keyed this from the control socket
    # file onto TCP: `/__ping__` on the configured port is the liveness probe,
    # and the port bind below is the mutual-exclusion primitive — a socket file
    # could go stale behind our back, an EADDRINUSE cannot.
    # ADR-0012 rule 6: probe the port THIS daemon is about to bind, on the
    # address local clients use for it — not whatever endpoint this shell
    # resolves (which, in an isolated dev/test environment without
    # `BW_DAEMON_URL`, is the machine-global daemon).
    own_port = cfg.resolved_facade_port()
    if own_port:
        existing = await _ipc.ping_status_async(
            timeout=1.0, host=_local_probe_host(cfg.facade_host), port=own_port)
    else:
        existing = _ipc.NO_PONG  # port 0: nobody can be holding "our" port
    existing_pid, existing_version = existing.pid, existing.version
    if existing_pid is not None:
        # ADR-0012 rule 5: launchd KeepAlive respawns us into this branch
        # every few seconds for as long as another daemon holds the endpoint
        # (87k lines of it on the maintainer's machine). Collapse repeats to
        # one summary per minute, and say who asked for this start.
        version_hint = ""
        if existing_version and existing_version != __version__:
            version_hint = (
                f" (running {existing_version}, installed {__version__}; "
                "use `browserwright-daemon stop` or `browserwright-daemon restart`)"
            )
        line = _ipc.note_already_running(existing_pid)
        if line is not None:
            _ipc.stderr_line(
                f"{line}; this start was initiated by "
                f"{_ipc.initiator_from_env()}; try `browserwright-daemon "
                f"status` or `browserwright-daemon restart`{version_hint}")
        return 1
    _ipc.cleanup_endpoint()
    # issue #15 (2.2): the control-socket ping above was negative, but a
    # half-alive prior daemon can still hold the relay/facade TCP ports and
    # crash-loop us on EADDRINUSE. Reclaim them before binding — but only from a
    # process lsof confirms holds the port AND whose cmdline is a browserwright
    # daemon (never a stranger).
    _reclaim_stale_daemon_ports(cfg)

    # Phase 3 (C2 ephemeral): cdp Chrome processes are daemon children and die
    # with us — but a hard crash / SIGKILL can leave orphan Chrome processes
    # holding their `bs-s{id}` profile dirs. Sweep them before serving so
    # ephemeral cdp sessions start clean (and so a relaunch on the same profile
    # isn't blocked by a stale SingletonLock).
    _cleanup_orphan_cdp_chrome()
    # #38: clear ledger rows naming a retired backend. Left alone they are
    # immortal — this daemon would refuse to route them (UnknownSessionError),
    # which is also what stops `session end` and auto-prune from clearing them.
    try:
        from ... import session_registry

        swept = session_registry.migrate_legacy_backends()
        if swept["migrated"]:
            logger.info("ledger: migrated %d session(s) from backend 'rdp' to "
                        "'cdp': %s", len(swept["migrated"]),
                        ", ".join(swept["migrated"]))
        for rec in swept["evicted"]:
            logger.warning(
                "ledger: evicted session %s (%r) — backend 'env' no longer "
                "exists and its endpoint lived in a daemon's environment, not "
                "in the record, so there is nothing to migrate it to. Recreate "
                "it with `session new --backend=cdp --attach=<url>`.",
                rec.get("id"), rec.get("name"))
    except Exception as e:  # noqa: BLE001 — never block startup on the ledger
        logger.warning("ledger: legacy-backend sweep failed: %r", e)
    # Phase B (PR2): the executor is "cdp Chrome v2" — sweep orphan executor
    # subprocesses + their stale `bw-exec-*` sockets/discovery files left by a
    # prior daemon SIGKILL, same rationale as the cdp sweep above.
    from .executor_registry import cleanup_orphan_executors
    # ADR-0013 rule 1: live, fingerprint-verified executors are KEPT here and
    # adopted into the registry once the Daemon exists (below), so a daemon
    # swap no longer severs every session's executor.
    adoptable_executors = cleanup_orphan_executors()

    # Log file is best-effort — we route Python logging to it but never crash
    # the daemon over a write failure.
    _wire_logging()
    # v0.5: opt-in JSON log formatter. After _wire_logging adds the
    # file/console handlers, swap formatters in place if BD_LOG_JSON=1.
    install_json_logging_if_requested()
    # `kill -USR1 <daemon pid>` dumps every thread's stack into the daemon log.
    # `ps` answers "who is waiting"; this answers "on what line". Armed before
    # anything can hang, and a no-op until someone signals.
    install_sigusr1_traceback("daemon")
    logger.info("browserwright-daemon %s starting (backend=%s pid=%d "
                "initiator=%s)", __version__, cfg.backend or "extension",
                os.getpid(), _ipc.initiator_from_env())
    _ipc.log_lifecycle("start", pid=os.getpid(), version=__version__,
                       initiator=_ipc.initiator_from_env())

    # Phase 2: one global daemon holding many upstream contexts. The shared
    # context is the real-browser upstream (cfg.backend, default extension);
    # cdp sessions get their own context lazily (Daemon.context_for). The
    # routing engine (Router/DaemonState/_UpstreamHolder) is unchanged — we
    # just instantiate it per context and dispatch in `_ClientHandler`.
    shared_backend = cfg.backend or "extension"
    # Pin the shared context's holder cfg to the resolved backend: serve now
    # defaults a missing backend to extension (cli._cmd_serve), so the holder
    # must see backend="extension" — not None — to take its extension-upstream
    # open path. dataclasses.replace keeps the rest of cfg intact.
    import dataclasses as _dc
    shared_cfg = _dc.replace(cfg, backend=shared_backend)
    shared_context = make_context(backend=shared_backend, cfg=shared_cfg)
    daemon = Daemon(cfg=cfg, shared_context=shared_context,
                    make_context=make_context)
    # ADR-0013 rule 1: rebuild every session's recovery state from disk plus
    # what this daemon can observe right now, instead of assuming an empty
    # world. The shared holder reports relay events into the same machine.
    try:
        adoptable_sessions = {
            str(rec.get("session") or "") for rec in adoptable_executors}
        daemon.recovery.load(
            session_registry.list_all(), extension_connected=False,
            executor_alive=lambda sid: sid in adoptable_sessions)
    except Exception as e:  # noqa: BLE001 - a damaged ledger must not stop serve
        logger.warning("recovery: could not load session states: %r", e)
    if adoptable_executors:
        adopted = daemon.executors.adopt(adoptable_executors)
        logger.info("adopted %d live executor(s) from the previous daemon: %s",
                    len(adopted), ", ".join(adopted) or "-")
    holder = getattr(shared_context, "holder", None)
    if holder is not None:
        holder.recovery = daemon.recovery
        holder.executor_alive = daemon.executor_alive

    # SIGTERM / SIGINT → set the stop event. We don't tear down inline because
    # we still need to run the graceful shutdown sequence (close clients with
    # 1011 + emit upstreamClosed + close upstream).
    stop = asyncio.Event()

    def _on_signal():
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # e.g. inside pytest event loop

    # PID file (best-effort).
    _ipc.write_pid(os.getpid())

    handler = _ClientHandler(daemon, cfg)

    # ADR-0011: the daemon's ONE client-facing door. Everything downstream —
    # the CLI, the skill client, the executor data plane and a raw Playwright
    # `connect_over_cdp` — arrives here, on `/control`, `/exec` and `/cdp`
    # respectively. It is bound FIRST because its bind is now the mutual
    # exclusion between daemons (the control socket file that used to play that
    # role is gone), and a failure to bind it is fatal: unlike the old facade
    # there is no other path left to keep serving.
    endpoint_port = cfg.resolved_facade_port()
    try:
        # For the extension backend the cdp surface bridges through the
        # daemon's shared relay (started just below). Pass a getter so it
        # resolves the LIVE relay per client connection — the relay may be
        # (re)bound across the daemon's lifetime, and is not up yet here.
        def _shared_relay() -> RelayServer | None:
            return shared_context.holder.relay

        endpoint = PlaywrightFacade(cfg=cfg, port=endpoint_port,
                                    host=cfg.facade_host,
                                    relay_getter=_shared_relay,
                                    daemon=daemon,
                                    control_handler=handler.serve_one)
        bound = await endpoint.start()
    except Exception as e:  # noqa: BLE001 - a bind failure of any shape is fatal
        hint = ""
        if isinstance(e, OSError) and endpoint_port:
            hint = (
                f" — port {endpoint_port} is held by another process. Run "
                f"`lsof -nP -iTCP:{endpoint_port} -sTCP:LISTEN` to find it, "
                f"then `browserwright-daemon restart` (reclaims a stale "
                f"browserwright daemon) or kill that pid."
            )
        _ipc.stderr_line(
            f"browserwright-daemon failed to bind endpoint "
            f"{cfg.facade_host}:{endpoint_port}: {e}{hint}")
        _ipc.cleanup_endpoint()
        return 2
    endpoint_url = f"http://{cfg.facade_host}:{bound}"
    # Publish the URL we actually bound. This is the ONLY way a client can find
    # a daemon told to bind port 0 (the per-test isolation scheme), and it is
    # ranked below every configured source in `daemon_url` precedence, so a
    # stale file can cost at most one failed ping.
    #
    # ADR-0012 rule 3: when the bind host is a specific non-loopback address
    # (the tailnet remote-use setup) the facade co-binds loopback, and what we
    # publish for LOCAL clients is the loopback address — otherwise every
    # local client resolves the tailnet IP and dies with the VPN. Remote
    # clients configure `BW_DAEMON_URL` explicitly and never read this file.
    local_client_host = getattr(
        endpoint, "local_client_host", _local_probe_host(cfg.facade_host))
    published_url = f"http://{local_client_host}:{bound}"
    _ipc.write_endpoint_state(published_url)
    if published_url != endpoint_url:
        logger.info("endpoint started at %s (control/cdp/exec); published %s "
                    "for local clients", endpoint_url, published_url)
    else:
        logger.info("endpoint started at %s (control/cdp/exec)", endpoint_url)

    # v0.4: for the extension shared context, start the relay ws server eagerly
    # so `browserwright-daemon doctor` can probe `__status__` even before any
    # Skill client connects. The relay belongs to the shared context's holder
    # (it is the always-on, real-browser upstream).
    if shared_backend == "extension":
        try:
            # v0.5.3 F-5 / Task #24: bind at the configured host+port.
            # Precedence (CLI > env > toml port > toml relay_url > default)
            # is centralized in cfg.backends.extension.resolved_host_port().
            host, port = cfg.backends.extension.resolved_host_port()
            relay = RelayServer(host=host, port=port)
            shared_context.holder.relay = relay
            # A replacement daemon starts with no open ExtensionUpstream, but
            # Chrome reconnects to this eager relay immediately.  Install the
            # lifecycle callbacks after constructing the relay and BEFORE it
            # starts accepting connections; wiring them above this block was
            # a no-op because holder.relay was still None there, losing the
            # first hello and leaving adopted executors unable to rebind.
            relay._on_extension_hello = (  # noqa: SLF001
                shared_context.holder._on_extension_hello  # noqa: SLF001
            )
            relay._on_extension_closed = (  # noqa: SLF001
                shared_context.holder._on_extension_closed  # noqa: SLF001
            )
            add_listener = getattr(relay, "add_event_listener", None)
            if callable(add_listener):
                add_listener(shared_context.holder._on_target_event)  # noqa: SLF001
            port = await relay.start()
            logger.info("extension relay started on port %d", port)
        except OSError as e:
            # issue #15 (2.2): if we still can't bind after the reclaim pass, the
            # port is held by something we couldn't confirm as our daemon (lsof
            # missing, or a genuine stranger). Point the user at the exact port +
            # how to find the holder rather than a bare errno.
            hint = ""
            with contextlib.suppress(Exception):
                _, rport = cfg.backends.extension.resolved_host_port()
                hint = (
                    f" — port {rport} is held by another process. Run "
                    f"`lsof -nP -iTCP:{rport} -sTCP:LISTEN` to find it, then "
                    f"`browserwright-daemon restart` (reclaims a stale "
                    f"browserwright daemon) or kill that pid."
                )
            _ipc.stderr_line(
                f"browserwright-daemon failed to bind extension relay: {e}{hint}")
            with contextlib.suppress(Exception):
                await endpoint.stop()
            _ipc.cleanup_endpoint()
            return 2


    # The watchdog runs unconditionally: even when upstream idle-close is off
    # (cfg.idle_close_after None), it must still crash-reap dead executors
    # (Fork 4 self-exit / segfault) so the registry never accumulates corpses.
    # Upstream idle-close + executor idle-reap are gated on cfg.idle_close_after
    # inside the loop.
    await _auto_prune_sessions(daemon, reason="startup")
    idle_task: asyncio.Task | None = asyncio.create_task(
        _idle_watchdog(daemon, cfg.idle_close_after,
                       session_idle_prune=cfg.session_idle_prune))
    try:
        await stop.wait()
        logger.info("browserwright-daemon shutdown requested")
        await _graceful_shutdown(daemon)
    finally:
        if idle_task is not None:
            idle_task.cancel()
            with contextlib.suppress(Exception):
                await idle_task
        # Stop every context's relay (only the extension shared context has
        # one today, but iterate so a future cdp-with-relay can't leak).
        for ctx in daemon.all_contexts():
            if ctx.holder.relay is not None:
                with contextlib.suppress(Exception):
                    await ctx.holder.relay.stop()
        # The endpoint goes last: it is the only client-facing transport, so
        # closing it earlier would drop in-flight teardown replies.
        with contextlib.suppress(Exception):
            await endpoint.stop()
        _ipc.cleanup_endpoint()
    return 0


# ---- cdp orphan cleanup (Phase 3 / C2 ephemeral) ---------------------------


def _cleanup_orphan_cdp_chrome() -> None:
    """Best-effort: on daemon startup, kill stray Chrome processes + remove
    leftover `bs-s{id}` profile dirs from a prior daemon crash (C2 ephemeral —
    docs/refactor-single-daemon.md §Notes "cdp orphan cleanup").

    Conservative by design:
      - We ONLY touch profile dirs we own: `<cache>/profiles/bs-s*`. We never
        scan the system process table for "chrome" (would catch the user's real
        Chrome) — we only signal a pid we can prove belongs to one of our
        profiles via that profile's own `SingletonLock`.
      - Chrome writes `SingletonLock` as a symlink whose target is
        `<hostname>-<pid>`. We parse the pid, SIGTERM it (if it still exists),
        then remove the whole profile dir. A profile with no SingletonLock is
        already-dead — we just remove the dir.
      - Every step is wrapped so a permission error / race never crashes serve.
    """
    import os as _os
    import shutil as _shutil
    import signal as _signal
    from ..platforms import cache_dir

    profiles_root = cache_dir() / "profiles"
    if not profiles_root.is_dir():
        return
    for entry in profiles_root.iterdir():
        if not entry.name.startswith("bs-s") or not entry.is_dir():
            continue
        # Try to identify + kill the Chrome that owns this profile via its
        # SingletonLock symlink (target == "<hostname>-<pid>").
        lock = entry / "SingletonLock"
        try:
            target = _os.readlink(lock)
            pid = int(target.rsplit("-", 1)[-1])
        except (OSError, ValueError):
            pid = None
        if pid is not None:
            try:
                _os.kill(pid, _signal.SIGTERM)
                logger.info("orphan-cleanup: SIGTERM stray cdp Chrome pid %d "
                            "(profile %s)", pid, entry.name)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        # Remove the leftover profile dir so the next launch of this session id
        # starts from a clean, lock-free profile.
        try:
            _shutil.rmtree(entry, ignore_errors=True)
            logger.info("orphan-cleanup: removed stale profile dir %s", entry.name)
        except OSError as e:
            logger.debug("orphan-cleanup: could not remove %s: %r", entry, e)


# ---- log wiring ------------------------------------------------------------


#: One timestamp shape for every line in the daemon log — logger output,
#: `LIFECYCLE` lines and startup stderr all agree (ADR-0012 rule 5).
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def _wire_logging() -> None:
    """Route the daemon's logger to a file under TMPDIR. Best-effort."""
    try:
        log_p = _ipc.log_path()
        log_p.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_p), encoding="utf-8")
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # Also echo to stderr. Under launchd that is the captured
        # StandardErrorPath file; without a handler here, warnings fell
        # through to logging's lastResort handler — undated, unlabelled,
        # which is what the launchd log looked like (ADR-0012 rule 5).
        echo = logging.StreamHandler(sys.stderr)
        echo.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        root.addHandler(echo)
        root.addHandler(handler)
    except OSError:
        pass


# ---- request-path helpers --------------------------------------------------


def _parse_query(path: str) -> dict[str, str]:
    """Pull single-valued query params from the request path."""
    parsed = urlparse(path)
    q = parse_qs(parsed.query)
    return {k: v[0] for k, v in q.items() if v}


# ---- per-client handler ----------------------------------------------------


class _ClientHandler:
    """Stateless adapter object — websockets gives us a ServerConnection per
    incoming client; we dispatch it to the right `UpstreamContext` and wire it
    through THAT context's Router.

    Phase 2: the handler holds the global `Daemon`, not a single triple. The
    client's `?session=<id>` query selects the context (via the ledger's
    immutable backend); `?client=<label>` is kept for log-friendly labels.
    """

    def __init__(self, daemon: "Daemon", cfg: Config):
        self.daemon = daemon
        self.cfg = cfg

    async def serve_one(self, conn: ServerConnection) -> None:
        """v0.3: handler instance per client connection — many run concurrently.

        Phase 2 dispatch: parse `?session=<id>` (and keep `?client=<label>`),
        resolve the `UpstreamContext` via `daemon.context_for(session_id)`, then
        register/route/release entirely against THAT context's state + router.
        Because a client is bound to one context for its whole life, each
        context's `Router._broadcast` only ever reaches its own clients —
        browser-level events cannot leak across contexts.
        """
        query = _parse_query(conn.request.path or "/")
        label = query.get("client", "anonymous")
        session_id = query.get("session") or None

        lease_token: object | None = None
        handler_task = asyncio.current_task()

        async def revoke_connection() -> None:
            with contextlib.suppress(Exception):
                await conn.close(code=1008, reason="browserwright session ended")
            if (handler_task is not None
                    and handler_task is not asyncio.current_task()):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await handler_task

        try:
            if session_id:
                acquire = getattr(self.daemon, "acquire_session_lease", None)
                if callable(acquire):
                    lease_token = object()
                    ctx = acquire(
                        session_id, lease_token, revoke_connection,
                        kind="control")
                else:
                    ctx = self.daemon.context_for_required(session_id)
            else:
                ctx = self.daemon.context_for(None)
        except UnknownSessionError:
            logger.warning("refusing client %s: unknown session %s",
                           label, session_id)
            with contextlib.suppress(Exception):
                await conn.close(code=1008, reason="unknown browserwright session")
            return
        state = ctx.state
        router = ctx.router
        holder = ctx.holder

        # Allocate with a globally-unique client id (unique across contexts)
        # but register it in this context's own client table. The session id +
        # name (from the ledger) ride on the client so the shared extension
        # context can scope Target.getTargets to this session's tab group.
        session_name: str | None = None
        if session_id:
            from ... import session_registry
            rec = session_registry.get(session_id)
            if isinstance(rec, dict):
                session_name = rec.get("name")
        client = state.allocate_client(
            label, client_id=next(self.daemon._next_client_id),
            session_id=session_id, session_name=session_name)
        client.connection_token = lease_token

        async def send_to_client(text: str) -> None:
            try:
                await conn.send(text)
            except Exception as e:
                logger.warning("client %d send failed: %r", client.client_id, e)

        router.register_client(client.client_id, send_to_client)
        router.bind_lifecycle(
            ensure_upstream=holder.ensure_open,
            trigger_disconnect=holder.trigger_close,
            prepare_executor=holder.prepare_executor,
        )

        logger.info("client %d connected (label=%s, session=%s, backend=%s, total=%d)",
                    client.client_id, label, session_id or "-", ctx.backend,
                    len(state.clients))
        try:
            async for raw in conn:
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                await router.route_from_client(client, text)
        except websockets.exceptions.ConnectionClosed:
            logger.info("client %d disconnected", client.client_id)
        except Exception as e:
            logger.warning("client %d crashed: %r", client.client_id, e)
        finally:
            await router.release_client(client.client_id)
            router.unregister_client(client.client_id)
            if lease_token is not None:
                release = getattr(self.daemon, "release_session_lease", None)
                if callable(release):
                    release(lease_token)
            # Upstream stays warm so other clients (or the next reconnect)
            # don't pay banner-flash for our churn.


# ---- upstream lifecycle ----------------------------------------------------


class _UpstreamHolder:
    """Owns the single Upstream adapter. Provides lazy-open + graceful-close
    primitives the Router can call.

    v0.4: when `cfg.backend == "extension"` we replace the conventional ws
    upstream with an ExtensionUpstream wrapping a RelayServer. The relay is
    started eagerly (so doctor probe answers `available=true` as soon as
    the daemon is up), and `ensure_open` blocks the first client until the
    extension has connected.
    """

    def __init__(self, state: DaemonState, router: Router, cfg: Config,
                 *, session_id: str | None = None):
        self.state = state
        self.router = router
        self.upstream: Upstream | None = None
        # Keep the extension adapter (and its live session→group bindings)
        # across idle detach/reattach. The relay is transport only.
        self._extension_adapter: ExtensionUpstream | None = None
        self._last_auto_recover: float = 0.0
        # A sweep is queued and has not started yet; later hellos ride on it.
        self._auto_recover_queued: bool = False
        self._auto_recover_hello: tuple[str, bool] = ("", False)
        # ADR-0013: the daemon's recovery state machine and its executor
        # liveness oracle, wired by `run_serve` once the Daemon exists.
        self.recovery = None
        self.executor_alive: Callable[[str], bool] = lambda sid: False
        self._open_lock = asyncio.Lock()
        self._cfg: Config = cfg
        # v0.4: only populated when backend=extension. Owned by the holder
        # for the daemon's full lifetime; we don't tear down on idle-close
        # so the extension's persistent ws to us stays warm.
        self.relay: RelayServer | None = None
        # Phase 3 (docs/refactor-single-daemon.md §P3 + C2): for an cdp context
        # the daemon itself launches and owns a dedicated Chrome (own port +
        # profile `bs-s{id}`). We record the launched process's pid + profile
        # dir here so teardown can SIGTERM it and so orphan-cleanup can spot
        # leftover `bs-s*` profiles after a crash. None on every other backend
        # (the extension/env holders never own a Chrome process).
        self.session_id: str | None = session_id
        self.cdp_pid: int | None = None
        self.cdp_profile_dir: str | None = None
        self.cdp_port: int | None = None
        self.cdp_owns_browser: bool = False

    @property
    def is_open(self) -> bool:
        return self.upstream is not None and self.upstream.is_open

    async def send_text(self, frame: str) -> None:
        """Proxy to the live Upstream.send_cdp.

        Exposed for lifecycle callers that should not drill through
        ``holder.upstream`` while it may be replaced or cleared on reconnect.
        """
        conn = self.upstream
        if conn is None:
            raise RuntimeError("upstream not open")
        await conn.send_cdp(frame)

    async def prepare_executor(self, session_id: str) -> None:
        """Backend-owned cold-start preflight for ``ensureExecutor``.

        Raw-CDP holders need no separate readiness check: ``ensure_open`` below
        launches/resolves their browser. The extension holder gives its
        service worker a short reconnect grace and then fails with the useful
        diagnosis before the normal 60-second interactive open can outlive the
        control-plane response deadline. This probe never mutates the upstream
        state machine, so a later extension reconnect remains recoverable.
        """
        if self.relay is None:
            return
        if self.relay.is_ready:
            return
        try:
            await self.relay.wait_ready(timeout=_EXECUTOR_READY_BUDGET_S)
        except Exception:  # noqa: BLE001 - timeout + relay reconnect hiccups
            pass
        if not self.relay.is_ready:
            raise Unavailable(
                _NO_EXTENSION_CONNECTED_MSG.format(sid=session_id))

    async def converge_session_tab(self, session_id: str, *, force: bool = False) -> dict | None:
        """Make an extension session own one live tab, once and bounded.

        Called after ``prepare_executor`` and ``ensure_open`` on the ordinary
        command path, and forced by the explicit recovery verb.  Existing
        healthy sessions stay on the fast path.  If the prior group vanished,
        opening one blank tab is the deterministic replacement; returning
        ``healthy`` while merely promising that a later call might open it was
        the ambiguity ADR-0013 removes.
        """
        if self.relay is None:
            return None
        machine = self.recovery
        if (not force and machine is not None
                and machine.state_of(session_id) == "healthy"):
            return None
        ext = self._extension_adapter
        if ext is None:
            raise RuntimeError("extension adapter is not open")
        generation = getattr(self.relay, "connection_generation", None)
        try:
            result = await ext.recover_session(session_id)
            detail = "tab group re-attached"
        except Exception:
            result = await ext.open_background_tab(
                "about:blank", session_id=session_id, background=True)
            detail = "fresh tab opened in the session group"
        self._note(session_id, "tab_recovered", generation=generation,
                   reason=detail)
        logger.info("recovery: converged session %s (%s, target=%s)",
                    session_id, detail,
                    result.get("targetId") if isinstance(result, dict) else "-")
        return result

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
            CdpUpstream

        v0.5.3 F-3: emits two lifecycle events to subscribed clients:
          - `BrowserwrightDaemon.upstreamConnecting {backend}` at the start of
            the open attempt (after we've taken the lock and bumped state
            to CONNECTING)
          - `BrowserwrightDaemon.upstreamReady {backend, ws_url}` on successful
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
            # F-3: emit BrowserwrightDaemon.upstreamConnecting to all clients.
            await self._broadcast_event(
                "BrowserwrightDaemon.upstreamConnecting",
                {"backend": cfg.backend or "auto"},
            )

            try:
                if cfg.backend == "extension":
                    await self._open_extension_upstream(cfg)
                else:
                    # Phase 3: an cdp context owns its Chrome. Launch it (once)
                    # BEFORE the resolve/connect path runs, so the cfg's pinned
                    # cdp port is actually listening when `_open_chrome_upstream`
                    # → resolve() probes it. Other cdp callers (env shares
                    # `_open_chrome_upstream` too) skip this — only a holder with
                    # a session_id + cdp backend owns a Chrome.
                    if (cfg.backend == "cdp" and self.session_id is not None
                            and self.cdp_owns_browser):
                        await self._launch_cdp_chrome(cfg)
                    await self._open_chrome_upstream(cfg)
            except Exception:
                raise
            else:
                # F-3: emit BrowserwrightDaemon.upstreamReady. `state.upstream_ws_url`
                # is set by both open paths via `state.set_connected(...)`.
                await self._broadcast_event(
                    "BrowserwrightDaemon.upstreamReady",
                    {
                        "backend": cfg.backend or "auto",
                        "ws_url": self.state.upstream_ws_url,
                    },
                )

            # Task #76: any client frame that arrived during the lazy-open
            # window was buffered per-client. Replay them now that the
            # upstream is live and atomically attached to the router.
            try:
                await self.router.drain_pre_open_buffers()
            except Exception as e:
                logger.warning("drain pre-open buffers failed: %r", e)

    async def _open_chrome_upstream(self, cfg: Config) -> None:
        try:
            rr = await resolve(cfg)
        except Unavailable as e:
            logger.warning("upstream resolve failed: %s", e)
            self.state.last_close_reason = "backend_lost"
            await self.state.set_disconnected()
            raise

        try:
            conn = CdpUpstream(
                on_frame=self.router.forward_from_upstream,
                on_close=self._on_upstream_closed,
                state=self.state,
                on_end_session=self._end_raw_session,
            )
            await conn.open(rr.ws_url, timeout=cfg.timeout)
        except Exception as e:
            logger.warning("upstream open failed: %r", e)
            self.state.last_close_reason = "backend_lost"
            await self.state.set_disconnected()
            raise
        self.upstream = conn
        # Publish the complete adapter before CONNECTED becomes visible.
        conn.attach(self.router)
        # Tell Chrome to gossip about all targets so we can maintain the
        # last_activated table without needing the client to enable it.
        # `waitForDebuggerOnStart=False` keeps target creation immediate.
        try:
            await conn.send_command(
                "Target.setDiscoverTargets", {"discover": True})
        except Exception as e:
            logger.warning("setDiscoverTargets failed: %r", e)
        await self.state.set_connected(rr.ws_url)

    async def _launch_cdp_chrome(self, cfg: Config) -> None:
        """Phase 3 (C2 ephemeral): the daemon launches + owns this cdp session's
        Chrome — a dedicated process on its own port with profile `bs-s{id}`.

        Idempotent: if we already launched (cdp_pid set) we no-op so a
        reconnect after idle-close doesn't spawn a second Chrome.

        Port selection mirrors the old `session_create._launch_daemon`: reuse
        `cfg.backends.cdp.port` when the ledger pinned one (Daemon._cdp_cfg_for
        copies the session's `workspace["port"]` into the cfg), else allocate a
        free port and pin it onto `self._cfg` so the subsequent resolve probes
        the right port.

        We call `launch_chrome.launch_chrome` in-process (NOT the CLI) so the
        spawned Chrome's pid is visible to us for teardown. The function spawns
        a detached Chrome and waits for `DevToolsActivePort`; on failure it
        raises Unavailable, which propagates out of `ensure_open` and surfaces
        to the client as a normal upstream-open failure.
        """
        if self.cdp_pid is not None:
            return  # already launched (warm reconnect)
        from ..launch_chrome import launch_chrome as _launch_chrome

        port = cfg.backends.cdp.port
        if not port:
            # No port pinned by the ledger — pick a free one and pin it onto
            # the holder's cfg so `_open_chrome_upstream`'s resolve hits it.
            import socket as _socket
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            finally:
                s.close()
            import dataclasses as _dc
            self._cfg = _dc.replace(
                cfg,
                backends=_dc.replace(
                    cfg.backends,
                    cdp=_dc.replace(cfg.backends.cdp, port=port),
                ),
            )
            cfg = self._cfg

        profile = f"bs-s{self.session_id}"
        logger.info("launching cdp Chrome for session %s on port %d (profile %s)",
                    self.session_id, port, profile)
        out = await _launch_chrome(cfg, profile=profile, persistent=True,
                                   port=port, timeout=max(cfg.timeout, 30.0))
        extras = out.get("extras") or {}
        self.cdp_pid = extras.get("pid")
        self.cdp_profile_dir = extras.get("profile_path")
        self.cdp_port = port

    def _kill_cdp_chrome(self) -> bool:
        """Phase 3 teardown: SIGTERM the daemon-owned Chrome for this cdp
        session (best-effort; the process may already be gone). Clears the pid
        so a later relaunch starts fresh. Leaves the profile dir on disk — it's
        a persistent `bs-s{id}` dir that orphan-cleanup sweeps on next startup;
        removing it inline races Chrome's shutdown writeback."""
        pid = self.cdp_pid
        if pid is None:
            return True
        import os as _os
        import signal as _signal
        try:
            _os.kill(pid, _signal.SIGTERM)
            self.cdp_pid = None
            logger.info("killed cdp Chrome pid %d for session %s",
                        pid, self.session_id)
            return True
        except ProcessLookupError as e:
            self.cdp_pid = None
            logger.debug("cdp Chrome pid %s already gone: %r", pid, e)
            return True
        except (PermissionError, OSError) as e:
            logger.warning("could not terminate cdp Chrome pid %s: %r", pid, e)
            return False

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
            ext = self._extension_adapter
            if ext is None or ext._relay is not self.relay:  # noqa: SLF001
                ext = ExtensionUpstream(
                    relay=self.relay,
                    on_frame=self.router.forward_from_upstream,
                    on_close=self._on_upstream_closed,
                )
                self._extension_adapter = ext
            # A (auto-recovery): every extension (re)connect re-attaches the
            # extension sessions whose relay ghost table died with the old
            # connection. Guarded by hasattr: unit tests stub the relay with
            # a bare object().
            if hasattr(self.relay, "_on_extension_hello"):
                self.relay._on_extension_hello = (  # noqa: SLF001
                    self._on_extension_hello
                )
            if hasattr(self.relay, "_on_extension_closed"):
                self.relay._on_extension_closed = (  # noqa: SLF001
                    self._on_extension_closed
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
        # Atomic publication replaces the old twelve-field callback wiring.
        ext.attach(self.router)
        # Prefer the adapter's own pseudo-URL: it carries the relay port, which
        # is what `daemon ps` shows per context. "ext://relay" is only the
        # fallback for an adapter that reports nothing.
        await self.state.set_connected(ext.ws_url or "ext://relay")

    async def _on_extension_hello(
        self, *, install_id: str = "", first_seen: bool = False,
    ) -> None:
        """A (auto-recovery): extension (re)connected with a fresh SW.

        A reloaded/updated SW reconnects with an EMPTY ``attachedTabs`` set,
        so the relay's ghost table for every extension session's tabs is gone
        and nothing re-announces it. Re-attach each session's tab group by
        its title (ADR-0009) so the tabs become drivable again WITHOUT any
        client action. Idempotent: sessions whose ghost survived (same-SW ws
        reconnect, re-announce) short-circuit in ``attach_tab``.

        Runs fire-and-forget with a short delay: a version-drift reload may
        land right after this hello and would kill the SW mid-recovery; and
        a reconnect burst (maintainLoop backoff) is throttled so we don't
        hammer the extension with attach round-trips.
        """

        from .session_state import EXTENSION_HELLO
        generation = getattr(self.relay, "connection_generation", None)
        self._note_extension_sessions(EXTENSION_HELLO, generation=generation,
                                      reason="extension connected; re-attaching tabs")

        # GH#106: the throttle defers, it never drops. It used to `return` for
        # a hello inside the window, so a service worker that restarted within
        # 10s of the previous sweep (e.g. a drift reload right after the first
        # hello) lost its tabs for good: nothing else re-attaches them.
        self._auto_recover_hello = (install_id, first_seen)
        if self._auto_recover_queued:
            return
        self._auto_recover_queued = True

        async def _recover() -> None:
            try:
                await asyncio.sleep(_AUTO_RECOVER_DELAY_S)
                wait = (self._last_auto_recover + _AUTO_RECOVER_THROTTLE_S
                        - time.monotonic())
                if wait > 0:
                    await asyncio.sleep(wait)
            except asyncio.CancelledError:
                self._auto_recover_queued = False
                return
            # Unqueue before sweeping: a hello that lands mid-sweep may come
            # from a SW that lost the tabs this sweep is re-attaching.
            self._auto_recover_queued = False
            self._last_auto_recover = time.monotonic()
            install_id, first_seen = self._auto_recover_hello
            generation = getattr(self.relay, "connection_generation", None)
            ext = self._extension_adapter
            if ext is None:
                return
            try:
                from ... import session_registry as reg
                rows = reg.list_all()
            except Exception as e:  # noqa: BLE001 - recovery is best-effort
                logger.warning("auto-recover: ledger unreadable: %r", e)
                return
            for rec in rows:
                sid = str(rec.get("id") or "")
                if rec.get("backend") != "extension":
                    continue
                try:
                    await ext.recover_session(sid)
                    self._note(sid, "tab_recovered", generation=generation,
                               reason="tab group re-attached after extension hello")
                    # GH#79: say which of the two it was. This line used to
                    # read "after extension reconnect" unconditionally — it
                    # fires on EVERY hello, including the very first one from
                    # a brand-new browser profile, and reading it in a
                    # session-scoped e2e log is what made a fresh Chrome per
                    # test look like a service worker churning between
                    # commands.
                    logger.info(
                        "auto-recovered session %s after extension %s "
                        "(install_id=%s)",
                        sid,
                        "first connect" if first_seen else "reconnect",
                        install_id or "(unknown)")
                except Exception as e:  # noqa: BLE001 - no group / empty group /
                    # still reconnecting -- the next hello retries.
                    self._note(sid, "tab_recover_failed", generation=generation,
                               reason=str(e)[:200])

        asyncio.create_task(_recover())

    async def _on_extension_closed(self, *, install_id: str = "") -> None:
        """The last ready extension connection went away (ADR-0013)."""
        self._note_extension_sessions(
            "extension_lost",
            reason=f"extension disconnected (install_id={install_id or 'unknown'})")

    async def _on_target_event(self, msg: dict) -> None:
        """Validate Target lifecycle against the canonical tab group."""
        kind = msg.get("type")
        tab_id = msg.get("tabId")
        if kind not in ("attached", "detached") or not isinstance(tab_id, int):
            return
        generation = msg.get("_relay_generation")
        if not isinstance(generation, int):
            generation = getattr(self.relay, "connection_generation", None)
        ext = self._extension_adapter
        if ext is None:
            # Before the first upstream open there is no group-aware adapter;
            # the hello recovery sweep will establish and report the facts.
            return
        target_id = f"ext-tab-{tab_id}"
        try:
            from ... import session_registry as reg
            from .session_state import TAB_RECOVER_FAILED, TAB_RECOVERED

            for row in reg.list_all():
                if row.get("backend") != "extension":
                    continue
                runtime = row.get("runtime") or {}
                if runtime.get("current_target_id") != target_id:
                    continue
                sid = str(row.get("id") or "")
                current = self.recovery.get(sid) if self.recovery is not None else None
                if (isinstance(generation, int) and current is not None
                        and isinstance(current.get("generation"), int)
                        and generation < current["generation"]):
                    continue
                if kind == "detached":
                    # `chrome.debugger` detached can mean tab removal OR a
                    # DevTools takeover. Re-resolve the named group and attempt
                    # the normal bounded re-attach before deciding which.
                    try:
                        await ext.recover_session(sid)
                    except Exception as e:  # noqa: BLE001
                        self._note(sid, TAB_RECOVER_FAILED,
                                   generation=generation,
                                   reason=f"current target could not be re-attached: {e}")
                    else:
                        self._note(sid, TAB_RECOVERED, generation=generation,
                                   reason="current target re-attached after Target detach")
                else:
                    # An attached debugger says nothing about workspace
                    # ownership. Promote only after the live tab group proves
                    # this target belongs to the session.
                    if await ext.target_belongs_to_session(sid, target_id):
                        self._note(sid, TAB_RECOVERED, generation=generation,
                                   reason="current target attached in session group")
        except Exception as e:  # noqa: BLE001 - observation cannot break relay
            logger.debug("recovery: target event could not be recorded: %r", e)

    def _note(self, sid: str, event: str, *, generation=None, reason: str = "") -> None:
        machine = self.recovery
        if machine is None:
            return
        try:
            machine.note(sid, event, reason=reason, generation=generation,
                         executor_alive=self.executor_alive(sid))
        except Exception as e:  # noqa: BLE001 - never let bookkeeping break the relay path
            logger.debug("recovery note %s for %s failed: %r", event, sid, e)

    def _note_extension_sessions(self, event: str, *, generation=None,
                                 reason: str = "") -> None:
        if self.recovery is None:
            return
        try:
            from ... import session_registry as reg
            rows = reg.list_all()
        except Exception:  # noqa: BLE001
            return
        for rec in rows:
            if rec.get("backend") == "extension":
                self._note(str(rec.get("id") or ""), event,
                           generation=generation, reason=reason)

    async def _end_raw_session(
        self, session_id: str, *, deadline: float | None = None,
    ) -> bool | None:
        """Apply the raw adapter's ownership policy through its daemon context."""
        daemon = getattr(self.router, "daemon", None)
        contexts = getattr(daemon, "contexts", None)
        teardown = getattr(daemon, "teardown_cdp_context", None)
        if (isinstance(contexts, dict) and session_id in contexts
                and callable(teardown)):
            return bool(await teardown(session_id, deadline=deadline))
        # env and attach-owned raw browsers are external: ending a browserwright
        # session must not fabricate ownership or kill them.
        return None

    async def trigger_close(self, reason: CloseReason) -> None:
        """Run the spec §6.5 close etiquette + tear down upstream.

        Sequence per spec §6.5:
          1. send Target.detachedFromTarget for each owned sessionId
          2. send BrowserwrightDaemon.upstreamClosed
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

        # Spec §6.5 step 2: BrowserwrightDaemon.upstreamClosed event broadcast.
        for cid in list(self.state.clients.keys()):
            try:
                await self.router._send_to_client(cid, json.dumps({
                    "method": "BrowserwrightDaemon.upstreamClosed",
                    "params": {"reason": reason},
                }))
            except Exception:
                pass

        # Tear down upstream ws.
        up = self.upstream
        self.upstream = None
        if up is not None:
            try:
                await up.close(code=1000, reason=reason)
            except Exception:
                pass

        # Phase 3 (C2 ephemeral): an cdp context's Chrome is a daemon child —
        # it must die with the upstream. Kill it on every close path
        # (endSession, idle_close, daemon_shutdown, chrome_exit). Harmless on
        # non-cdp holders (cdp_pid is None there).
        if self.cdp_pid is not None:
            self._kill_cdp_chrome()

        # Spec §6.5 step 3: close client ws. The handler's `async for` will
        # exit naturally on the next read once we set state DISCONNECTED;
        # for prompt teardown we'd need to plumb each ServerConnection in
        # — left as a follow-up since the natural-exit path is reliable.
        await self.state.set_disconnected()
        # Inverse of open: publish DISCONNECTED before removing the one adapter
        # reference, so concurrent verbs lazy-open instead of seeing a connected
        # router with an absent implementation.
        if up is not None:
            up.detach(self.router)

    async def abort_cdp_teardown(self) -> None:
        """Restore a retryable, non-CLOSING context after bounded teardown."""
        self._kill_cdp_chrome()
        up = self.upstream
        self.upstream = None
        await self.state.set_disconnected()
        if up is not None:
            up.detach(self.router)
            # Detaching only unhooks it from the Router; the websocket, its
            # reader task and the heartbeat keep running. For an attach-owned
            # session _kill_cdp_chrome is deliberately a no-op, so nothing else
            # ends them — and a retry would open a second adapter while frames
            # from this abandoned one still arrive at the Router. Best-effort:
            # this path exists because teardown already ran out of budget, so a
            # failing close must not stop the context becoming retryable.
            with contextlib.suppress(Exception):
                await up.close(reason="teardown_aborted")

    async def _on_upstream_closed(self, reason: str) -> None:
        """Called by CdpUpstream's reader when upstream drops on its
        own (Chrome exited, etc.). We translate to a CloseReason and run
        the close-etiquette path.

        Phase 3 (docs/refactor-single-daemon.md §Notes): for an cdp context the
        Chrome IS the upstream — once it's gone the context is dead, so we drop
        it from the daemon's registry (not just mark disconnected). A later
        a later session connect then recreates a fresh context + relaunches
        Chrome."""
        if self.state.upstream_phase in (UpstreamPhase.DISCONNECTED, UpstreamPhase.CLOSING):
            return
        await self.trigger_close("chrome_exit")
        if self.session_id is not None:
            daemon = getattr(self.router, "daemon", None)
            if daemon is not None:
                try:
                    daemon.drop_cdp_context(self.session_id)
                except Exception as e:
                    logger.warning("drop cdp context %s failed: %r",
                                   self.session_id, e)


# ---- graceful shutdown -----------------------------------------------------


async def _auto_prune_sessions(daemon: "Daemon", *, reason: str) -> list[dict]:
    """Best-effort durable ledger prune.

    The session idle clock is `ledger.last_seen`, updated when a new
    user/agent instruction arrives. Executor liveness is deliberately ignored:
    a stuck executor can stay alive forever and must not keep a session from
    being cleaned after the instruction-idle threshold.
    """
    idle_seconds = daemon.cfg.session_idle_prune
    if not idle_seconds:
        return []
    try:
        from ... import session_registry
        stale = session_registry.stale(idle_seconds=idle_seconds)
    except Exception as e:  # noqa: BLE001 - cleanup must not kill the daemon
        logger.warning("auto session-prune failed (%s): %r", reason, e)
        return []
    pruned: list[dict] = []
    for rec in stale:
        sid = str(rec.get("id") or "")
        if not sid:
            continue
        context_for_required = getattr(daemon, "context_for_required", None)
        if callable(context_for_required):
            try:
                # Use the same ledger/backend/scope boundary as every live
                # client, and a legacy/unknown-backend row is skipped rather
                # than pruned blind.
                context_for_required(sid)
            except UnknownSessionError:
                continue

        async def teardown_workspace() -> dict:
            if rec.get("backend") == "cdp":
                # Every cdp context, not just create-owned. The scope check
                # above goes through context_for_required, which for cdp
                # *creates* the per-session context as a side effect of
                # validating it — so skipping teardown here would strand that
                # context, and any upstream socket it opened, for the rest of
                # the daemon's life once the ledger row is gone.
                #
                # Safe for attach: the holder only SIGTERMs a pid it launched
                # itself, and an attach-owned holder has none, so this closes
                # our websocket and drops the context without touching the
                # external browser — the ownership rule in
                # docs/session-workspaces.md is preserved.
                await daemon.teardown_cdp_context(sid)
                return {"ok": True, "backend": "cdp", "closed": [],
                        "failed": [], "kept": []}
            if rec.get("backend") == "extension":
                runtime = rec.get("runtime") or {}
                clean = {"ok": True, "backend": "extension", "closed": [],
                         "failed": [], "kept": []}
                if not runtime:
                    # This session never touched Chrome, so there is nothing to
                    # tear down and the record is already clean. Saying so is
                    # what lets it be pruned at all — this path runs from the
                    # idle watchdog, which fires when nobody has touched the
                    # session, i.e. exactly when the lazily-opened adapter is
                    # cold.
                    #
                    # ADR-0009: an empty `runtime` is the signal, not a missing
                    # `runtime.group_id`. That field is gone, and reading its
                    # absence as "never bound" would now be true of every
                    # session, pruning live ones.
                    return clean
                holder = daemon.shared_context.holder
                if holder.upstream is None:
                    relay = holder.relay
                    if relay is None or not relay.is_ready:
                        # No extension is connected, so we cannot prove the
                        # group is gone. Leave the record for a later sweep
                        # rather than deleting state we cannot verify or
                        # blocking the watchdog on a 60s cold open.
                        raise RuntimeError(
                            "extension not connected; deferring prune")
                    await holder.ensure_open()
                upstream = holder.upstream
                if upstream is None:
                    raise RuntimeError("extension upstream unavailable")
                return await upstream.end_session(sid)
            return {"ok": True, "backend": str(rec.get("backend") or "unknown"),
                    "closed": [], "failed": [], "kept": []}

        teardown_ok = True
        try:
            terminate = getattr(daemon, "terminate_session", None)
            if callable(terminate):
                reap, result = await terminate(sid, teardown_workspace)
            else:
                legacy_terminate = getattr(
                    daemon.executors, "terminate_session", None)
                if callable(legacy_terminate):
                    reap, result = await legacy_terminate(
                        sid, teardown_workspace)
                else:
                    reap = await daemon.executors.kill_current_and_wait(sid)
                    result = await teardown_workspace()
            if reap.get("reaped") is not True:
                teardown_ok = False
                logger.warning(
                    "auto session-prune could not confirm executor death "
                    "(session=%s): %r", sid, reap)
            if not isinstance(result, dict) or result.get("ok") is not True:
                teardown_ok = False
                logger.warning(
                    "auto session-prune workspace teardown incomplete "
                    "(session=%s): %r", sid, result)
        except Exception as e:  # noqa: BLE001
            logger.warning("auto session-prune teardown failed "
                           "(session=%s): %r", sid, e)
            teardown_ok = False
        if not teardown_ok:
            continue
        try:
            removed = session_registry.remove(sid)
        except Exception as e:  # noqa: BLE001
            logger.warning("auto session-prune ledger remove failed "
                           "(session=%s): %r", sid, e)
            removed = None
        if removed is not None:
            pruned.append(removed)
    if pruned:
        logger.info("auto-pruned %d idle session(s) on %s: %s",
                    len(pruned), reason,
                    [str(rec.get("id")) for rec in pruned])
    return pruned


async def _idle_watchdog(
    daemon: "Daemon",
    idle_after: float | None,
    *,
    session_idle_prune: float | None = None,
) -> None:
    """Spec §6.5/§6.6: when configured, close each upstream after `idle_after`
    seconds with no activity. The next client command lazy-opens it again.

    Phase 2: iterate every context (shared + cdp) so per-upstream idle is
    enforced independently — one busy upstream doesn't keep an idle one warm.

    Phase B (PR2): the same loop supervises the per-session executors —
      - crash-reap (ALWAYS, even when idle-close is off): drop executors whose
        child has exited on its own (Fork 4 facade-death self-exit / segfault)
        so the registry never holds corpses + the next ensure cold-starts fresh;
      - idle-reap (gated on idle_after, like upstream idle-close): SIGTERM
        executors idle past the threshold so a long-abandoned session doesn't
        leak a subprocess.

    Runs unconditionally; idle-close + idle-reap are no-ops when `idle_after`
    is None. Durable ledger prune is controlled independently by
    `session_idle_prune`. We poll at the smallest active supervision cadence,
    or every 5s when only crash-reap is active.
    """
    cadences = [5.0]
    if idle_after:
        cadences.append(max(1.0, idle_after / 2.0))
    if session_idle_prune:
        cadences.append(_SESSION_PRUNE_INTERVAL_S)
    poll = min(cadences)
    last_session_prune_at = time.time()
    try:
        while True:
            await asyncio.sleep(poll)
            now = time.time()
            # --- executor supervision (Phase B PR2) ---
            try:
                daemon.executors.reap_dead()
                if idle_after:
                    await daemon.executors.reap_idle(idle_after)
            except Exception as e:  # noqa: BLE001 - never let reap break the loop
                logger.warning("executor reap failed: %r", e)
            # --- upstream idle-close (gated) ---
            if session_idle_prune and (
                    now - last_session_prune_at >= _SESSION_PRUNE_INTERVAL_S):
                await _auto_prune_sessions(daemon, reason="watchdog")
                last_session_prune_at = now
            if not idle_after:
                continue
            for ctx in daemon.all_contexts():
                if ctx.state.upstream_phase != UpstreamPhase.CONNECTED:
                    continue
                idle_for = now - ctx.state.last_activity_at
                if idle_for >= idle_after:
                    logger.info("idle-watchdog: closing %s upstream after %.1fs",
                                ctx.backend, idle_for)
                    try:
                        await ctx.holder.trigger_close("idle_close")
                    except Exception as e:
                        logger.warning("idle close failed: %r", e)
                    # An idle-closed cdp context's Chrome is gone; drop the
                    # context so the dict doesn't accumulate dead per-session
                    # entries for the daemon's lifetime. (trigger_close flips
                    # the phase itself, so _on_upstream_closed — the usual drop
                    # path — never fires for the idle case.) A later client
                    # frame for the session re-creates + relaunches cleanly.
                    if ctx.backend == "cdp" and ctx.session_id is not None:
                        daemon.drop_cdp_context(ctx.session_id)
    except asyncio.CancelledError:
        return


#: How long shutdown waits for in-flight workspace teardowns. Must exceed the
#: teardown budget (`verbs._END_SESSION_BUDGET_S`) by enough to let a teardown
#: that started just before SIGTERM finish its last tab close.
_SHUTDOWN_TEARDOWN_DRAIN_S = 65.0


async def _graceful_shutdown(daemon: "Daemon") -> None:
    """Called on SIGTERM. Drain in-flight teardowns, run close etiquette on
    every context, then close the listener."""
    # Consume replacement intent immediately. Draining a teardown may take
    # longer than the marker's anti-staleness window; validation is about when
    # SIGTERM arrived, not how long graceful shutdown etiquette takes.
    from .. import _ipc as daemon_ipc
    preserve_executors = daemon_ipc.consume_executor_handoff(os.getpid())
    # ADR-0009: teardown may be mid-flight for up to the full teardown budget,
    # and `trigger_close` below pulls the relay out from under it — which can
    # land between a tab close and its ledger checkpoint. Wait for those tasks
    # FIRST; ordering this after the close is equivalent to not draining at all.
    registry = getattr(daemon, "executors", None)
    pending = getattr(registry, "_pending_teardowns", None)
    if isinstance(pending, dict) and pending:
        tasks = [t for t in pending.values() if t is not None]
        logger.info("shutdown: draining %d in-flight workspace teardown(s)",
                    len(tasks))
        with contextlib.suppress(Exception):
            await asyncio.wait(tasks, timeout=_SHUTDOWN_TEARDOWN_DRAIN_S)
        still = [t for t in tasks if not t.done()]
        if still:
            logger.warning(
                "shutdown: %d teardown(s) still running after %.0fs; "
                "the ledger row is kept and the retry resumes them",
                len(still), _SHUTDOWN_TEARDOWN_DRAIN_S)
    for ctx in daemon.all_contexts():
        try:
            await ctx.holder.trigger_close("daemon_shutdown")
        except Exception as e:
            logger.warning("shutdown close failed for %s: %r", ctx.backend, e)
    # A real stop still owns and reaps every executor.  A replacement writes a
    # fingerprinted, one-shot handoff marker before SIGTERM; in that one case
    # the new daemon adopts the still-live processes and their Python state.
    if preserve_executors:
        logger.info("shutdown: preserving %d executor(s) for replacement daemon",
                    len(getattr(daemon.executors, "_handles", {})))
        return
    try:
        await daemon.executors.kill_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("executor shutdown kill failed: %r", e)


# ---- helper for the cli serve dispatcher ----------------------------------


def make_holder(state: DaemonState, router: Router, cfg: Config) -> _UpstreamHolder:
    """Test seam: build an _UpstreamHolder pre-bound to cfg."""
    return _UpstreamHolder(state, router, cfg)
