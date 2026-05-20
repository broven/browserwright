"""Resolve the current session record (P1).

A call's identity comes from an explicit ``--session <id>`` argument or the
``BD_SESSION`` env var — never an import-time default. If neither names a
known session, we refuse loudly (:class:`NoSession`) rather than silently
sharing a browser.
"""
from __future__ import annotations

import os
from typing import Optional

from . import session_registry as reg
from .errors import NoSession


def resolve_session(session_id: Optional[str] = None) -> dict:
    """Return the ledger record for the current session.

    Resolution order: explicit ``session_id`` arg → ``$BD_SESSION``. Raises
    :class:`NoSession` when nothing is provided or the id is unknown. On
    success, bumps the record's ``last_seen`` and returns it.
    """
    raw = session_id if session_id is not None else os.environ.get("BD_SESSION")
    sid = str(raw) if raw not in (None, "") else ""
    if not sid:
        raise NoSession()
    rec = reg.get(sid)
    if rec is None:
        raise NoSession(f"unknown session id {sid!r} (not in ledger).")
    reg.touch(sid)
    return rec
