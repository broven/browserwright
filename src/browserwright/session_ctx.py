"""Resolve an explicitly requested session record (P1)."""
from __future__ import annotations

from typing import Optional
import os

from . import session_registry as reg
from .errors import NoSession


def resolve_session(session_id: Optional[str] = None) -> dict:
    """Return the ledger record for the current session.

    Raises :class:`NoSession` when no id is provided or the id is unknown.
    On success, bumps the record's ``last_seen`` and returns it.
    """
    raw = session_id
    sid = str(raw) if raw not in (None, "") else ""
    if not sid:
        raise NoSession()
    rec = reg.get(sid)
    if rec is None:
        raise NoSession(f"unknown session id {sid!r} (not in ledger).")
    reg.touch(sid)
    return rec


def resolve_session_or_env(session_id: Optional[str] = None) -> dict:
    """Resolve an explicit session id, falling back to ``BD_SESSION``.

    Entry points that are themselves session-scoped commands (``task``,
    ``userscript push --verify``) use this convenience. The lower-level
    ``resolve_session()`` remains strict so internal call sites cannot
    accidentally inherit environment state unless they opted into it.
    """
    raw = session_id
    if raw in (None, ""):
        raw = os.environ.get("BD_SESSION")
    return resolve_session(raw)
