"""Per-process Skill session.

Holds:
  - one ``ModeBClient`` (long-lived daemon socket)
  - one ``CDPSession`` (lazy: opened on first primitive that touches the browser)
  - the currently-attached target id (the "current tab")

Concurrency model (v0.3 prep)
-----------------------------

Primitives reach the session through ``current_session()``. Historically this
returned a process-wide singleton — fine for REPL / inline / single-task
flows because there's only one Chrome ws to multiplex.

Layer 3 wants to fan out multiple tasks concurrently against the same daemon.
If two tasks ran in the same process they'd race over ``current_target_id``
(one task's ``new_tab`` would yank the other's attached tab). So v0.3 adds:

* ``Session`` instances per task, isolated state, no shared mutable surface
  beyond the daemon CDP transport (which is already thread-safe).
* ``with_session(sess)`` context manager that pushes ``sess`` onto a
  ``ContextVar`` for the duration of the ``with`` block. Threads that enter
  the context see *that* session via ``current_session()``; outside the
  context they see the default singleton.

The default singleton stays — REPL / inline never need a fresh session and
overriding it would force callers to pass it through explicitly. The
``ContextVar`` is the override knob.
"""
from __future__ import annotations

import contextvars
import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from .cdp import CDPSession
from .errors import DaemonUnavailable


class Session:
    def __init__(self, daemon=None, *, record=None):
        # ``daemon`` is a ``ModeBClient`` (long-lived socket), exposing
        # ``resolve_ws_url`` / ``ws_url`` semantics through the methods we use
        # below. (Mode A — the subprocess resolver — was removed.)
        #
        # ``record`` is a resolved session ledger record. There is one global
        # daemon on a fixed socket; the session's ``id`` is carried as the ws
        # client label (``skill-s<id>``) so the daemon routes this client to the
        # session's UpstreamContext (per-session isolation lives daemon-side now).
        # Stored as ``session_record`` to avoid shadowing the ``record()`` method.
        self.session_record = record
        if daemon is None:
            if record is None:
                from .errors import NoSession
                raise NoSession(
                    "no session bound: a Session needs an explicit ledger "
                    "record (its backend/daemon comes from `session new`). "
                    "Pass record=/daemon= explicitly."
                )
            from .mode_b_client import client_for_session
            daemon = client_for_session(record)
        self.daemon = daemon
        self._cdp: Optional[CDPSession] = None
        self._cdp_lock = threading.Lock()
        self.current_target_id: Optional[str] = None
        # Last-seen accuracy from getActiveTab for warn-on-stale UX.
        self.last_active_tab: Optional[dict] = None
        # Caches keyed by host name → memory dict for performance.
        self._site_mem_cache: dict[str, dict] = {}
        # ``BS_HOME`` resolved once.
        self.home = os.path.expanduser(os.environ.get("BS_HOME", "~/.browserwright"))
        # Whether this Session was created for an isolated scope (with_session
        # / task_runner per-task). Affects close(): we leave shared CDP
        # transports alone, but isolated ones get their CDP closed too if it
        # was opened just for this scope.
        self._owns_cdp = True

    @property
    def cdp(self) -> CDPSession:
        if self._cdp is not None and not self._cdp._closed:
            return self._cdp
        with self._cdp_lock:
            if self._cdp is not None and not self._cdp._closed:
                return self._cdp
            url = self._resolve_ws_url()
            self._cdp = CDPSession(url)
            return self._cdp

    def _resolve_ws_url(self) -> str:
        """Ask the underlying daemon client for a CDP ws URL.

        ``ModeBClient.resolve_ws_url`` aliases ``ws_url()``. On failure, retry
        once after dropping the cached URL — spec §D.7.
        """
        try:
            return self.daemon.resolve_ws_url()
        except DaemonUnavailable:
            self.daemon.invalidate()
            return self.daemon.resolve_ws_url()

    def close(self) -> None:
        if self._cdp is not None and self._owns_cdp:
            self._cdp.close()
        self._cdp = None

    @property
    def backend_name(self) -> str:
        """Lazy-resolved daemon backend name (``"rdp"``, ``"extension"``, …).

        Diagnostics only — the downstream API is unified, so primitives no
        longer branch on the backend (backend divergence is absorbed daemon-side;
        see docs/refactor-single-daemon.md). Surfaced for doctor / debugging.
        Falls back to ``""`` when the daemon doesn't surface backend info.
        """
        cached = getattr(self, "_backend_name_cache", None)
        if cached is not None:
            return cached
        if isinstance(self.session_record, dict):
            name = self.session_record.get("backend") or ""
            if name:
                self._backend_name_cache = name  # type: ignore[attr-defined]
                return name
        info = None
        getter = getattr(self.daemon, "get_backend_info", None)
        if callable(getter):
            # Narrow the catch to the failure modes the underlying clients
            # actually surface: ModeBClient.get_backend_info wraps subprocess
            # plumbing (FileNotFoundError, TimeoutExpired) and JSON parsing.
            # OSError covers low-level I/O. AttributeError absorbs a missing
            # inner shim rather than letting an internal bug crash backend-name
            # resolution. Truly unexpected exceptions propagate.
            import json as _json
            import subprocess as _subprocess
            try:
                info = getter()
            except (AttributeError, OSError,
                    _subprocess.CalledProcessError, _subprocess.TimeoutExpired,
                    _json.JSONDecodeError):
                info = None
        name = ""
        if isinstance(info, dict):
            name = info.get("backend") or info.get("name") or ""
        self._backend_name_cache = name  # type: ignore[attr-defined]
        return name


# ---- singleton + context-var override ---------------------------------

# Module-level default. Lazily created on first ``current_session()`` call
# from a context that hasn't pushed an override. Lives for the process.
_singleton: Optional[Session] = None
_singleton_lock = threading.Lock()

# ContextVar holds the currently-active Session for this thread / task.
# ``None`` means "no override → use the default singleton".
_active: contextvars.ContextVar[Optional[Session]] = contextvars.ContextVar(
    "browserwright_session", default=None
)


def current_session() -> Session:
    """Return the Session bound to the current execution context.

    Resolution order:
      1. The ``ContextVar`` push (most recent ``with_session(...)`` block).
      2. The process-wide default singleton, bound by ``set_session()``.

    There is no env-guessed default: a Session's backend/daemon comes from an
    explicit ledger record. Entry points bind one via
    ``set_session(Session(record=...))`` before primitives run. Calling this
    with nothing bound raises ``NoSession`` from ``Session.__init__``.
    """
    override = _active.get()
    if override is not None:
        return override
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = Session()
    return _singleton


def set_session(sess: Optional[Session]) -> None:
    """Bind the process-wide default Session. The CLI entry point calls
    this with a record-bound Session; tests use it to install a mock. Pushing
    via ``with_session()`` is preferred when the override should be scoped."""
    global _singleton
    _singleton = sess


def isolated_session() -> Session:
    """A fresh Session for fan-out / isolated task runs.

    Inherits the current session's daemon binding (so it drives the *same*
    browser) but isolates target tracking (its own ``current_target_id``).
    Task runners must bind the returned session to a fresh target before using
    browser primitives; otherwise reconnect recovery may still see the parent
    ledger target. Prefers the ledger record (own client connection) and falls
    back to sharing the parent's daemon when the parent was constructed without
    a record (tests)."""
    parent = current_session()
    if parent.session_record is not None:
        return Session(record=parent.session_record)
    return Session(daemon=parent.daemon)


@contextmanager
def with_session(sess: Session) -> Iterator[Session]:
    """Run a block with ``sess`` as the active session.

    Pattern (per-task isolation)::

        from browserwright.session import isolated_session, with_session
        with with_session(isolated_session()) as sess:
            goto_url("https://example.com")
            # this block's primitives operate on `sess`, not the default
        # outside the `with`, primitives revert to the default singleton.

    The session's CDP transport is *not* closed automatically — callers that
    spin a Session for a one-shot task should call ``sess.close()`` after
    the ``with``. This split exists because most callers want to reuse one
    transport across many ``with_session`` blocks (Layer 3 task pool).
    """
    token = _active.set(sess)
    try:
        yield sess
    finally:
        _active.reset(token)
