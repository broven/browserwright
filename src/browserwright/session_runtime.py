"""Transparent reconnect-recovery for a session's tab binding.

A session's durable anchor is its **name == Chrome tab-group title**. The
ledger record carries a ``runtime`` cache (``current_target_id``, ``group_id``,
``owned_tab_ids``, ``updated_at``) as a *fast path* only — the source of truth
is the tab group, recoverable via the daemon verb ``BrowserwrightDaemon.recoverSession``.

These helpers let primitives re-attach to a session's tab across daemon
restarts / extension reconnects / new ``bs run`` processes without the caller
doing anything: ``ensure_session_target`` runs a 3-step fallback (in-process →
ledger.runtime fast path → group-anchor recovery).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional

from . import session_registry as reg
from .errors import CDPError


def _resolve_sid(sess) -> Optional[str]:
    """Best-effort current session id: from the bound record, else BD_SESSION."""
    rec = getattr(sess, "session_record", None)
    if isinstance(rec, dict) and rec.get("id"):
        return rec["id"]
    try:
        from .session_ctx import resolve_session
        rec = resolve_session()
    except Exception:
        return None
    return rec.get("id") if isinstance(rec, dict) else None


def _resolve_record(sess) -> Optional[dict]:
    """Best-effort current session record (fresh read from the ledger)."""
    sid = _resolve_sid(sess)
    if not sid:
        return None
    return reg.get(sid)


def persist_target(target_id: str, *, group_id: Optional[int] = None,
                   sess=None) -> None:
    """Cache the current tab binding in the ledger record's ``runtime`` field.

    Called wherever a primitive sets ``current_target_id`` so a later process
    can fast-path re-attach without querying the tab group."""
    if sess is None:
        from .session import current_session
        sess = current_session()
    sid = _resolve_sid(sess)
    if not sid:
        return
    runtime = {
        "current_target_id": target_id,
        "group_id": group_id,
        "owned_tab_ids": [],
        "updated_at": time.time(),
    }
    try:
        reg.update(sid, runtime=runtime)
    except Exception:
        # Caching is best-effort; never let a ledger write break a primitive.
        pass


def register_recovered(sess, payload: dict) -> Optional[str]:
    """Register a daemon-minted session/target in the local CDPSession tables.

    Factored out of ``open_background``'s post-open registration so recovery
    can reuse it. Returns the targetId (or None on a malformed payload)."""
    target_id = payload.get("targetId")
    session_id = payload.get("sessionId")
    if not target_id or not session_id:
        return None
    cdp = sess.cdp
    cdp._sessions[target_id] = session_id
    cdp._events.setdefault(session_id, deque(maxlen=1024))
    sess.current_target_id = target_id
    return target_id


def ensure_session_target(sess) -> Optional[str]:
    """Transparent 3-step recovery of the session's attached tab.

    1. ``sess.current_target_id`` already set → return it (in-process).
    2. ledger ``runtime.current_target_id`` → try ``cdp.attach(tid)`` (FAST
       PATH, no group query). The daemon auto-reattaches the debugger; a
       stale/closed tab raises → fall through.
    3. group anchor: ``BrowserwrightDaemon.recoverSession`` by name → register +
       persist the new binding. On CDPError / empty group return None.

    Returns the targetId, or None when nothing could be recovered (brand-new
    session with no tabs yet)."""
    if sess.current_target_id:
        return sess.current_target_id

    rec = _resolve_record(sess)

    # Step 2: ledger.runtime fast path.
    if isinstance(rec, dict):
        runtime = rec.get("runtime") or {}
        tid = runtime.get("current_target_id")
        if tid:
            try:
                sess.cdp.attach(tid)
                sess.current_target_id = tid
                return tid
            except CDPError:
                pass  # stale/closed tab — fall through to group recovery

    # Step 3: authoritative group-anchor recovery.
    name = rec.get("name") if isinstance(rec, dict) else None
    sid = rec.get("id") if isinstance(rec, dict) else _resolve_sid(sess)
    if not name:
        return None
    try:
        payload = sess.cdp.send(
            "BrowserwrightDaemon.recoverSession", groupName=name, bsSession=sid,
        )
    except CDPError:
        return None
    if not payload:
        return None
    target_id = register_recovered(sess, payload)
    if target_id is None:
        return None
    persist_target(target_id, group_id=payload.get("groupId"), sess=sess)
    return target_id
