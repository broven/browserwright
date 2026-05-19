"""Per-process Skill session.

Holds:
  - one ``DaemonClient`` (Mode A subprocess or Mode B socket — duck-typed)
  - one ``CDPSession`` (lazy: opened on first primitive that touches the browser)
  - the currently-attached target id (the "current tab")
  - a REPL history list (used by ``propose_solidify``)

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
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from .cdp import CDPSession
from .errors import DaemonUnavailable


class Session:
    def __init__(self, daemon=None):
        # ``daemon`` is duck-typed: either a ``ModeAClient`` (subprocess-based)
        # or a ``ModeBClient`` (long-lived socket). Both expose
        # ``resolve_ws_url`` / ``ws_url`` semantics through the matching
        # methods we use below.
        if daemon is None:
            from .mode_b_client import auto_client
            daemon = auto_client()
        self.daemon = daemon
        self._cdp: Optional[CDPSession] = None
        self._cdp_lock = threading.Lock()
        self.current_target_id: Optional[str] = None
        self.history: list[dict] = []  # {"code", "ok", "stdout", "result", "exception", "ts"}
        # Last-seen accuracy from getActiveTab for warn-on-stale UX.
        self.last_active_tab: Optional[dict] = None
        # Caches keyed by host name → memory dict for performance.
        self._site_mem_cache: dict[str, dict] = {}
        # ``BS_HOME`` resolved once.
        self.home = os.path.expanduser(os.environ.get("BS_HOME", "~/.browser-skill"))
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

        Both Mode A (``ModeAClient.resolve_ws_url``) and Mode B
        (``ModeBClient.resolve_ws_url`` aliasing ``ws_url()``) implement the
        same one-method protocol. On failure, retry once after dropping the
        cached URL — spec §D.7.
        """
        try:
            return self.daemon.resolve_ws_url()
        except DaemonUnavailable:
            self.daemon.invalidate()
            return self.daemon.resolve_ws_url()

    def record(self, code: str, *, ok: bool, stdout: str = "", result=None, exception=None) -> None:
        self.history.append({
            "code": code,
            "ok": ok,
            "stdout": stdout,
            "result": result,
            "exception": exception,
            "ts": time.time(),
        })

    def close(self) -> None:
        if self._cdp is not None and self._owns_cdp:
            self._cdp.close()
        self._cdp = None

    @property
    def backend_name(self) -> str:
        """Lazy-resolved daemon backend name (``"rdp"``, ``"extension"``, …).

        Primitives use this to branch on backend-specific quirks — most
        notably extension's "you have to attach tabs explicitly" model —
        without a round-trip on every call. Falls back to ``""`` when the
        daemon doesn't surface backend info (older daemon / Mode A path
        that never wired it).
        """
        cached = getattr(self, "_backend_name_cache", None)
        if cached is not None:
            return cached
        info = None
        getter = getattr(self.daemon, "get_backend_info", None)
        if callable(getter):
            try:
                info = getter()
            except Exception:
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
    "browser_skill_session", default=None
)


def current_session() -> Session:
    """Return the Session bound to the current execution context.

    Resolution order:
      1. The ``ContextVar`` push (most recent ``with_session(...)`` block).
      2. The process-wide default singleton (lazily created).

    Calling code never has to plumb a session argument through every
    primitive — the same way ``logging`` keeps a default logger.
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
    """Replace the default singleton outright. Used by tests to install a
    mock session. Pushing via ``with_session()`` is preferred in production
    code because it scopes the override."""
    global _singleton
    _singleton = sess


@contextmanager
def with_session(sess: Session) -> Iterator[Session]:
    """Run a block with ``sess`` as the active session.

    Pattern (per-task isolation)::

        from browser_skill.session import Session, with_session
        with with_session(Session()) as sess:
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
