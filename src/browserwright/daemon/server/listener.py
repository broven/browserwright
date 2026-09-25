"""Daemon lifecycle orchestrator + the endpoint's control-surface handler.

This module wires together:
  - `_ipc` (runtime files / ping)
  - `daemon` (the global Daemon and its per-upstream contexts)
  - `upstream_context` (building a context; each context's holder opens and
    closes its adapter lazily — backend lifecycle lives in the adapters)
  - `facade` (the one client-facing endpoint)

It is the daemon's process lifecycle — serve, supervise, prune, shut down —
plus the endpoint's `/control` client handler. It holds no backend knowledge.

ADR-0011: this module no longer *binds* anything client-facing. The one TCP
endpoint lives in `facade.py`; `run_serve` builds it and hands it
`_ClientHandler.serve_one` as the `/control` handler.
"""
from __future__ import annotations

import asyncio
import contextlib
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
from ..observability import (
    install_json_logging_if_requested,
    install_sigusr1_traceback,
)
from .state import UpstreamPhase
from .daemon import Daemon, UnknownSessionError
from .facade import PlaywrightFacade
from .upstream import OWNED_PROFILE_PREFIX
from .upstream_context import build_context

logger = logging.getLogger(__name__)

_SESSION_PRUNE_INTERVAL_S = 3600.0

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

    # One global daemon holding many upstream contexts. The shared context is
    # the real-browser upstream (cfg.backend, default extension); cdp sessions
    # get their own context lazily (Daemon.context_for).
    shared_backend = cfg.backend or "extension"
    # Pin the shared context's cfg to the resolved backend: serve defaults a
    # missing backend to extension (cli._cmd_serve). dataclasses.replace keeps
    # the rest of cfg intact.
    import dataclasses as _dc
    shared_cfg = _dc.replace(cfg, backend=shared_backend)
    shared_context = build_context(backend=shared_backend, cfg=shared_cfg)
    # The Daemon wires its recovery machine into every context it registers,
    # the shared one included, before anything can deliver an event.
    daemon = Daemon(cfg=cfg, shared_context=shared_context)
    # ADR-0013 rule 1: rebuild every session's recovery state from disk plus
    # what this daemon can observe right now, instead of assuming an empty
    # world. The extension adapter reports relay events into the same machine.
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
        def _shared_relay():
            return shared_context.upstream.relay

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

    # Start the shared context's daemon-lifetime resources eagerly — for the
    # extension backend that binds the relay, so `browserwright-daemon doctor`
    # can probe `__status__` even before any Skill client connects. The relay
    # stays up across idle close so the extension's ws to us stays warm.
    try:
        await shared_context.start()
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
        # Release every context's daemon-lifetime resources (the relay).
        for ctx in daemon.all_contexts():
            with contextlib.suppress(Exception):
                await ctx.stop()
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
        if not entry.name.startswith(OWNED_PROFILE_PREFIX) or not entry.is_dir():
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
        try:
            # Use the same ledger/backend/scope boundary as every live client,
            # and a legacy/unknown-backend row is skipped rather than pruned
            # blind.
            daemon.context_for_required(sid)
        except UnknownSessionError:
            continue

        async def teardown_workspace(sid: str = sid) -> dict:
            # No deadline: the unattended sweep never blocks the watchdog on a
            # disconnected browser. An adapter that cannot prove the workspace
            # is gone defers (raises) and the row waits for a later sweep.
            return await daemon.end_workspace(sid)

        teardown_ok = True
        try:
            reap, result = await daemon.terminate_session(
                sid, teardown_workspace)
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
                    # An idle-closed per-session context's connection (and
                    # any browser it owned) is gone; drop the context so the
                    # dict doesn't accumulate dead per-session entries for the
                    # daemon's lifetime. (trigger_close flips the phase itself,
                    # so on_upstream_lost — the usual drop path — never fires
                    # for the idle case.) A later client frame for the session
                    # re-creates + relaunches cleanly.
                    if ctx.session_id is not None:
                        daemon.drop_context(ctx.session_id)
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
