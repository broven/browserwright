"""Single global daemon: multi-upstream, session-keyed routing.

`docs/refactor-single-daemon.md` §"Key insight: the engine already exists":
`Router` + `DaemonState` + `_UpstreamHolder` are already a complete
single-upstream / multi-client engine — they only touch `self.state.*` and one
`self._upstream_send`. So this module does NOT rewrite any routing/translation
logic. It:

  - bundles one `(state, router, holder)` triple per upstream into an
    `UpstreamContext` (the per-upstream unit of `docs §Decomposition`), and
  - adds the thin global `Daemon` that owns the shared (extension/real-browser)
    context plus one lazily-created context per rdp session, and dispatches a
    connecting client to the right context by reading the session's *immutable*
    backend from the ledger.

Cross-talk is structurally impossible: a client is bound to exactly one context
for its whole connection, so each context's `Router._broadcast` only ever
reaches that context's own clients (browser-level events stay scoped to the
upstream that produced them).

Phase boundaries (this file is Phase 2):
  - The actual per-session rdp Chrome *launch* is Phase 3. Here we only create
    the context object + its `(state, router, holder)` triple, wiring the
    holder with a cfg whose rdp port comes from the session's workspace; the
    holder's existing resolve/connect path is left untouched.
"""
from __future__ import annotations

import dataclasses
import itertools
import logging

from ... import session_registry
from ..config import Config
from .state import DaemonState
from .proxy import Router

logger = logging.getLogger(__name__)


class UnknownSessionError(KeyError):
    """Raised when an explicit session-bound client names no ledger session."""


# ---- per-upstream context --------------------------------------------------


class UpstreamContext:
    """One live upstream: its `(state, router, holder)` triple.

    Mirrors exactly what `run_serve` used to build for the single upstream —
    just bundled so the `Daemon` can hold N of them. The holder is created by
    `make_context` (it lives in listener.py to avoid an import cycle), so this
    class is a passive bundle: it owns no construction logic, only the handle
    each dispatch path needs (`router` to register a client, `state` to release
    it, `holder` for lifecycle).
    """

    def __init__(self, *, backend: str, state: DaemonState, router: Router,
                 holder: object, session_id: str | None = None):
        self.backend = backend
        self.state = state
        self.router = router
        # Typed as object to keep this module free of a listener import cycle
        # (_UpstreamHolder lives in listener.py and imports nothing from here).
        self.holder = holder
        # None for the shared context; the rdp session id otherwise.
        self.session_id = session_id


# ---- the thin global daemon ------------------------------------------------


class Daemon:
    """Global daemon: one shared context + one context per rdp session.

    `shared_context` is the always-on, real-browser upstream (backend ==
    `cfg.backend`, default `extension`); for the extension backend its holder
    owns the always-on `RelayServer` started eagerly in `run_serve`. Every
    extension/env session routes here.

    `contexts` holds one `UpstreamContext` per rdp session, created lazily on
    first reference (Phase 3 makes the holder actually launch the per-session
    Chrome — here it is only wired with a port-pinned cfg).
    """

    def __init__(self, *, cfg: Config, shared_context: UpstreamContext,
                 make_context):
        self.cfg = cfg
        self.shared_context = shared_context
        self.contexts: dict[str, UpstreamContext] = {}
        # Phase B: per-session persistent executor subprocesses, keyed by
        # session id (mirrors `contexts`). Lazily spawned by the
        # `ensureExecutor` verb (PR1). Supervised by the daemon (PR2): idle/crash
        # reap via the idle-watchdog, endSession kill in the endSession handler,
        # kill-all on graceful shutdown, orphan-sweep on startup.
        from .executor_registry import ExecutorRegistry
        self.executors = ExecutorRegistry()
        # Injected factory `(backend, cfg, session_id) -> UpstreamContext`.
        # Lives in listener.py (it builds the _UpstreamHolder); injected to
        # avoid an import cycle.
        self._make_context = make_context
        # Global, unique-across-contexts client id source — purely for
        # log-friendliness so two contexts never print the same client #.
        self._next_client_id: "itertools.count[int]" = itertools.count(1)
        # Back-reference set on each context's router so RPC handlers
        # (ensureSession / endSession) can reach the daemon to create/drop
        # an rdp context.
        shared_context.router.daemon = self  # type: ignore[attr-defined]

    def all_contexts(self) -> list[UpstreamContext]:
        """Shared context first, then every rdp context — used by shutdown /
        idle / signal paths that must iterate every live upstream."""
        return [self.shared_context, *self.contexts.values()]

    def context_for(self, session_id: str | None, *, require_known: bool = False) -> UpstreamContext:
        """Resolve the `UpstreamContext` that should serve `session_id`.

        - None / empty session       → the shared (real-browser) context.
        - ledger backend == "rdp"     → a per-session context (created lazily).
        - extension/env         → the shared context.
        - unknown explicit session    → raises when `require_known=True`.

        The backend is the ledger's immutable `backend` field — never a client
        param (docs §RPCs). Sessionless clients keep the historical shared
        context; explicit session-bound clients can require the ledger record so
        a typo or stale id never silently falls into the extension backend.
        """
        if not session_id:
            return self.shared_context
        record = session_registry.get(session_id)
        if record is None:
            if require_known:
                raise UnknownSessionError(session_id)
            return self.shared_context
        backend = record.get("backend")
        if backend == "rdp":
            return self._ensure_rdp_context(session_id, record)
        # extension / env → shared upstream.
        return self.shared_context

    def context_for_required(self, session_id: str) -> UpstreamContext:
        """Resolve an explicitly session-bound context, failing closed."""
        return self.context_for(session_id, require_known=True)

    def _ensure_rdp_context(self, session_id: str, record: dict) -> UpstreamContext:
        """Get or create the per-session rdp context.

        Phase 2 scope: build the context object (its own state/router/holder)
        and wire the holder with a cfg whose rdp port comes from the session's
        `workspace["port"]` when present (else the existing resolve path is
        left intact). The actual Chrome *launch* is Phase 3 — the holder's
        lazy-open will then resolve+connect to that port.
        """
        ctx = self.contexts.get(session_id)
        if ctx is not None:
            return ctx
        cfg = self._rdp_cfg_for(record)
        ctx = self._make_context(backend="rdp", cfg=cfg, session_id=session_id)
        # Preserve ownership semantics past the ledger→context boundary:
        # create-owned sessions launch/kill daemon-owned Chrome; attach
        # sessions only connect to the caller-provided port.
        try:
            ctx.holder.rdp_owns_browser = record.get("owner") == "create"  # type: ignore[attr-defined]
        except Exception:
            pass
        # Same daemon back-reference the shared context got, so the rdp
        # context's RPC handlers can drop themselves on endSession.
        ctx.router.daemon = self  # type: ignore[attr-defined]
        self.contexts[session_id] = ctx
        logger.info("created rdp upstream context for session %s (port=%s)",
                    session_id, cfg.backends.rdp.port)
        return ctx

    def _rdp_cfg_for(self, record: dict) -> Config:
        """Derive a per-session rdp Config from the ledger record.

        Pins `backend="rdp"` and, when the session's workspace carries a port,
        `backends.rdp.port` so the holder's resolve path targets the right
        per-session Chrome. Leaves the resolve path otherwise unchanged.
        """
        workspace = record.get("workspace")
        port = None
        if isinstance(workspace, dict):
            raw = workspace.get("port")
            if isinstance(raw, int):
                port = raw
        cfg = dataclasses.replace(self.cfg, backend="rdp")
        if port is not None:
            # `replace` shares the nested BackendsConfig instance; copy the rdp
            # sub-config so per-session port pinning never mutates the shared
            # cfg (or another session's context).
            cfg.backends = dataclasses.replace(
                cfg.backends,
                rdp=dataclasses.replace(cfg.backends.rdp, port=port),
            )
        return cfg

    def drop_rdp_context(self, session_id: str) -> UpstreamContext | None:
        """Remove an rdp context from the registry. Returns the dropped
        context, or None if absent.

        This only de-registers the context (sync, callable from `_on_upstream_
        closed` after teardown already ran). To also close the upstream + kill
        the owned Chrome, use the async `teardown_rdp_context` instead — it runs
        the holder's `trigger_close` (which SIGTERMs the Chrome) before
        dropping."""
        return self.contexts.pop(session_id, None)

    async def teardown_rdp_context(self, session_id: str) -> bool:
        """Phase 3 endSession teardown: close the per-session upstream, kill the
        daemon-owned Chrome (the holder's `trigger_close` SIGTERMs `rdp_pid`),
        and drop the context. Returns True if a context was found + torn down.

        Idempotent-ish: a missing context returns False so the caller can still
        answer the wire RPC with a uniform success shape."""
        ctx = self.contexts.get(session_id)
        if ctx is None:
            return False
        try:
            # "skill_disconnect" is the closest honest CloseReason — the client
            # explicitly asked to end this session (vs chrome_exit / idle).
            await ctx.holder.trigger_close("skill_disconnect")  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning("teardown rdp context %s close failed: %r",
                           session_id, e)
        self.contexts.pop(session_id, None)
        logger.info("tore down rdp context for session %s", session_id)
        return True
