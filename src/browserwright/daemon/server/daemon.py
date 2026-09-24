"""Single global daemon: multi-upstream, session-keyed routing.

The daemon holds one shared `UpstreamContext` (the real-browser upstream every
extension session multiplexes onto) plus one lazily-created context per `cdp`
session, and dispatches a connecting client to the right one by reading the
session's *immutable* backend from the ledger. Which record gets which context
— and which adapter — is decided in exactly one place,
`upstream_context.context_for_record`.

Cross-talk is structurally impossible: a client is bound to exactly one context
for its whole connection, so each context's `Router._broadcast` only ever
reaches that context's own clients (browser-level events stay scoped to the
upstream that produced them).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable

from ... import session_registry
from ..config import Config
from .upstream_context import (
    UnroutableRecord,
    UpstreamContext,
    context_for_record,
)

__all__ = ["Daemon", "UnknownSessionError", "UpstreamContext"]

logger = logging.getLogger(__name__)

#: `(session_id, ledger record, daemon cfg) -> UpstreamContext | None`.
ContextFactory = Callable[[str, dict, Config], "UpstreamContext | None"]


class UnknownSessionError(KeyError):
    """Raised when an explicit session-bound client names no ledger session."""


# ---- the thin global daemon ------------------------------------------------


class Daemon:
    """Global daemon: one shared context + one context per cdp session.

    `shared_context` is the always-on, real-browser upstream (backend ==
    `cfg.backend`, default `extension`); for the extension backend its holder
    owns the always-on `RelayServer` started eagerly in `run_serve`. Every
    extension/env session routes here.

    `contexts` holds one `UpstreamContext` per cdp session, created lazily on
    first reference by the context factory (default
    `upstream_context.context_for_record`, injectable for tests).
    """

    def __init__(self, *, cfg: Config, shared_context: UpstreamContext,
                 context_factory: ContextFactory | None = None):
        self.cfg = cfg
        self.shared_context = shared_context
        # Exactly one context per cdp session, no exceptions. `daemon_scope`
        # lived here to authorize env records against the socket that allocated
        # them; with the endpoint carried per session there is nothing left to
        # arbitrate, and the uniformity is what makes teardown analyzable.
        self.contexts: dict[str, UpstreamContext] = {}
        # Phase B: per-session persistent executor subprocesses, keyed by
        # session id (mirrors `contexts`). Lazily spawned by the
        # `ensureExecutor` verb (PR1). Supervised by the daemon (PR2): idle/crash
        # reap via the idle-watchdog, endSession kill in the endSession handler,
        # kill-all on graceful shutdown, orphan-sweep on startup.
        from .executor_registry import ExecutorRegistry
        from .session_state import RecoveryStateMachine, ledger_persist
        self.executors = ExecutorRegistry()
        # ADR-0013 rule 1: one recovery state per session, persisted into the
        # ledger row. The registry, the relay and the recovery sweep report
        # into it; `status`, `doctor` and `recover` read it.
        self.recovery = RecoveryStateMachine(persist=ledger_persist)
        self.executors.on_event = self.recovery.note
        self._context_factory: ContextFactory = (
            context_factory or context_for_record)
        # Global, unique-across-contexts client id source — purely for
        # log-friendliness so two contexts never print the same client #.
        self._next_client_id: "itertools.count[int]" = itertools.count(1)
        # Every session-bound downstream connection holds a daemon lease.  The
        # registry is transport-neutral: control websocket and facade clients
        # are revoked by the same terminal lifecycle operation.
        self._session_leases: dict[
            str, dict[object, tuple[str, Callable[[], Awaitable[None]]]]
        ] = {}
        self._lease_sessions: dict[object, str] = {}
        self._session_phases: dict[str, str] = {}
        self._session_results: dict[str, dict] = {}
        self._termination_locks: dict[str, asyncio.Lock] = {}
        self._register(shared_context)

    def _register(self, ctx: UpstreamContext) -> None:
        """Wire one context into the daemon: the router's back-reference (verbs
        reach the executor registry through it) and the ADR-0013 recovery
        machine — the same for every context, shared or per-session."""
        ctx.router.daemon = self  # type: ignore[attr-defined]
        ctx.bind_recovery(self.recovery, self.executor_alive)

    def executor_alive(self, session_id: str) -> bool:
        handle = self.executors.get(str(session_id))
        return handle is not None and handle.is_alive()

    def all_contexts(self) -> list[UpstreamContext]:
        """Shared context first, then every cdp context — used by shutdown /
        idle / signal paths that must iterate every live upstream."""
        return [self.shared_context, *self.contexts.values()]

    def context_for(self, session_id: str | None, *, require_known: bool = False) -> UpstreamContext:
        """Resolve the `UpstreamContext` that should serve `session_id`.

        - None / empty session     → the shared (real-browser) context.
        - ledger backend == "cdp"  → a per-session context (created lazily).
        - extension                → the shared context, if it is the extension one.
        - unknown explicit session → raises when `require_known=True`.

        The backend is the ledger's immutable `backend` field — never a client
        param (docs §RPCs). Sessionless clients keep the historical shared
        context; explicit session-bound clients can require the ledger record so
        a typo or stale id never silently falls into the extension backend.

        Note what is NOT conditioned on the shared backend: an `cdp` session
        gets its own context whatever the daemon was started with. That is what
        lets one extension-backed daemon simultaneously host N sessions each
        attached to a different external browser — the thing that used to
        require running N isolated daemons (#38).
        """
        if not session_id:
            return self.shared_context
        record = session_registry.get(session_id)
        if record is None:
            if require_known:
                raise UnknownSessionError(session_id)
            return self.shared_context
        ctx = self.contexts.get(session_id)
        if ctx is not None:
            return ctx
        try:
            ctx = self._context_factory(session_id, record, self.cfg)
        except UnroutableRecord:
            raise UnknownSessionError(session_id) from None
        if ctx is None:
            # The record rides the shared context — only if that context
            # actually serves its backend.
            if record.get("backend") == self.shared_context.backend:
                return self.shared_context
            raise UnknownSessionError(session_id)
        self._register(ctx)
        # Dies with its browser: an upstream that closes on its own takes the
        # per-session context with it, and a later connect rebuilds it.
        ctx.holder.on_upstream_lost = (
            lambda: self.drop_context(session_id))
        self.contexts[session_id] = ctx
        logger.info("created %s upstream context for session %s",
                    ctx.backend, session_id)
        return ctx

    def context_for_required(self, session_id: str) -> UpstreamContext:
        """Resolve an explicitly session-bound context, failing closed."""
        return self.context_for(session_id, require_known=True)

    # ---- the drivable path (ADR-0013 rule 1) ------------------------------

    async def ensure_session_drivable(self, session_id: str, *,
                                      force: bool = False) -> dict | None:
        """Make ``session_id``'s browser side drivable: the one path.

        Three steps, each owned by the session's adapter, none branching on
        backend here: wait (bounded) for the browser side to be reachable,
        open the upstream, converge the session to one live tab. ``force``
        skips the adapter's healthy fast path — the explicit recovery verbs
        use it. Returns the adapter's representative tab when it converged,
        ``None`` when nothing needed doing.

        Callers that go on to spawn an executor use `ensure_executor`, which
        runs this inside the executor registry's per-session lifecycle lock.
        """
        ctx = self.context_for_required(session_id)
        try:
            await ctx.upstream.await_browser(session_id)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"upstream readiness: {e}") from e
        try:
            await ctx.holder.ensure_open()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"upstream open: {e!r}") from e
        return await ctx.upstream.converge(session_id, force=force)

    async def ensure_executor(self, session_id: str) -> str:
        """Make the session drivable and return its executor socket path,
        spawning the executor if absent.

        The drivable path runs **inside** the registry's per-session lifecycle
        lock: outside it a concurrent `endSession` could close the browser
        between readiness and spawn. Used by the `ensureExecutor` verb, the
        `recover` verb and the `/exec` relay alike.
        """
        return await self.executors.ensure_with_preflight(
            session_id, lambda: self.ensure_session_drivable(session_id))

    def acquire_session_lease(
        self,
        session_id: str,
        token: object,
        revoke: Callable[[], Awaitable[None]],
        *,
        kind: str,
    ) -> UpstreamContext:
        """Authorize and register one live session-bound connection.

        Resolution and lease publication contain no await, so a termination
        cannot begin between the authorization check and registration.  A
        terminal control connection is allowed solely so a lost endSession
        acknowledgement can be retried; Router gates every other command.
        Facade clients never have a legitimate post-terminal operation.
        """
        ctx = self.context_for_required(session_id)
        phase = self._session_phases.get(session_id, "active")
        if phase != "active" and kind != "control":
            raise UnknownSessionError(session_id)
        if token in self._lease_sessions:
            raise RuntimeError("session lease token is already registered")
        self._session_leases.setdefault(session_id, {})[token] = (kind, revoke)
        self._lease_sessions[token] = session_id
        return ctx

    def release_session_lease(self, token: object) -> None:
        """Forget a connection lease after its transport has exited."""
        session_id = self._lease_sessions.pop(token, None)
        if session_id is None:
            return
        leases = self._session_leases.get(session_id)
        if leases is None:
            return
        leases.pop(token, None)
        if not leases:
            self._session_leases.pop(session_id, None)

    async def revoke_session_lease(self, token: object) -> None:
        """Close one leased transport and forget it after closure confirms."""
        session_id = self._lease_sessions.get(token)
        if session_id is None:
            return
        lease = self._session_leases.get(session_id, {}).get(token)
        if lease is None:
            self.release_session_lease(token)
            return
        _kind, revoke = lease
        await revoke()
        self.release_session_lease(token)

    def session_is_terminal(self, session_id: str | None) -> bool:
        return bool(
            session_id
            and self._session_phases.get(session_id) == "ended")

    def session_is_restricted(self, session_id: str | None) -> bool:
        """Whether only a retry of endSession may use this control lease."""
        return bool(
            session_id
            and self._session_phases.get(session_id, "active") != "active")

    async def terminate_session(
        self,
        session_id: str,
        teardown: Callable[[], Awaitable[dict]],
        *,
        caller_token: object | None = None,
        budget: float | None = None,
        wait: bool = True,
    ) -> tuple[dict[str, object], dict]:
        """Atomically revoke clients, reap executor, and tear down workspace.

        Issue #32 contract: the verb handler passes ``wait=False`` and gets
        back at the initiate boundary — ``{initiated, phase: "terminating"}``
        — with the 60s-bounded workspace teardown continuing as a daemon-side
        task. Every other caller (auto-prune, embedders, tests) keeps the
        default ``wait=True`` blocking semantics: initiate, then join the
        in-flight teardown and return its FINAL result. A retry of
        ``endSession`` against an already-terminating session joins the
        in-flight teardown either way, so it gets the final result, never a
        second initiate.
        """
        lock = self._termination_locks.setdefault(session_id, asyncio.Lock())
        revocations: list[asyncio.Task[None]] = []
        try:
            async with lock:
                cached = self._session_results.get(session_id)
                if cached is not None:
                    return ({
                        "killed": False,
                        "reaped": True,
                        "matched": True,
                        "executor_id": None,
                    }, dict(cached))
                self._session_phases[session_id] = "terminating"
                try:
                    tokens = list(self._session_leases.get(session_id, {}))
                    for token in tokens:
                        if token is caller_token:
                            continue
                        # Closing starts promptly, but a control revoker may
                        # join its handler task.  A concurrent endSession
                        # handler is waiting for this same lock, so joining it
                        # here would form a lock cycle.  Await every revocation
                        # only after terminal state is published and the lock
                        # is released below.
                        revocations.append(asyncio.create_task(
                            self.revoke_session_lease(token)))
                    if revocations:
                        # Give every close coroutine one turn so transports
                        # begin shutting down before workspace teardown.  We
                        # deliberately do not join their handler tasks here.
                        await asyncio.sleep(0)
                    reap, result = await self.executors.terminate_session(
                        session_id, teardown, budget=budget)
                except BaseException:
                    self._session_phases[session_id] = "active"
                    raise
                if result.get("initiated") is True:
                    # The workspace teardown continues in the registry's
                    # background task. Watch it so `ps` reports the real
                    # terminal state even if no caller ever polls, and — for
                    # the blocking callers — join it and return the final
                    # result.
                    asyncio.create_task(
                        self._watch_termination(session_id))
                    if wait:
                        result = await self.executors.await_termination(
                            session_id)
                        if (isinstance(result, dict)
                                and result.get("ok") is True):
                            self._session_results[session_id] = dict(result)
                            self._session_phases[session_id] = "ended"
                            self._mark_session_ended(session_id)
                        else:
                            self._session_phases[session_id] = "active"
                elif (reap.get("reaped") is True
                        and isinstance(result, dict)
                        and result.get("ok") is True):
                    self._session_results[session_id] = dict(result)
                    self._session_phases[session_id] = "ended"
                    self._mark_session_ended(session_id)
                else:
                    self._session_phases[session_id] = "active"
                return reap, result
        finally:
            if revocations:
                await asyncio.gather(*revocations)

    async def _watch_termination(self, session_id: str) -> None:
        """Publish terminal state when an initiated background teardown
        completes, so ``ps``/leases reflect reality even if no caller ever
        polls or retries. Idempotent against the join path in
        ``terminate_session`` (both write the same values)."""
        try:
            result = await self.executors.await_termination(session_id)
        except BaseException:  # noqa: BLE001 - the watcher must not die loud
            self._session_phases[session_id] = "active"
            return
        if isinstance(result, dict) and result.get("ok") is True:
            self._session_results[session_id] = dict(result)
            self._session_phases[session_id] = "ended"
            self._mark_session_ended(session_id)
        else:
            self._session_phases[session_id] = "active"

    def _mark_session_ended(self, session_id: str) -> None:
        """Drop recovery diagnosis when durable teardown reaches terminal."""
        from .session_state import SESSION_ENDED

        self.recovery.note(session_id, SESSION_ENDED)

    def drop_context(self, session_id: str) -> UpstreamContext | None:
        """Forget a per-session context. Returns it, or None if absent.

        Only de-registers (sync): the connection is already closed when this
        runs from `on_upstream_lost` / the idle watchdog. Ending a session's
        workspace goes through `end_workspace`, which closes first."""
        return self.contexts.pop(session_id, None)

    async def end_workspace(self, session_id: str, *,
                            deadline: float | None = None) -> dict:
        """Tear down one session's workspace through its context.

        The one teardown entry point for `endSession` and auto-prune alike:
        the context's adapter applies the owner rule, a per-session context
        closes its own connection, and on success that context is dropped so
        the registry never keeps a dead session's entry.
        """
        ctx = self.context_for_required(session_id)
        result = await ctx.end_session(session_id, deadline=deadline)
        if (ctx is not self.shared_context and isinstance(result, dict)
                and result.get("ok") is True):
            self.contexts.pop(session_id, None)
            logger.info("tore down %s context for session %s",
                        ctx.backend, session_id)
        return result
