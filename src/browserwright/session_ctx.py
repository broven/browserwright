"""Resolve an explicitly requested session record (P1)."""
from __future__ import annotations

from typing import Optional

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
