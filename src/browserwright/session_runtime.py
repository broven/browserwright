"""Internal session tab runtime: open / bind / resolve / reconnect-recover.

A session's durable extension anchor is its Chrome tab-group id. The ledger
record carries a ``runtime`` cache (``current_target_id``, ``group_id``,
``owned_tab_ids``, ``updated_at``) as a fast path — the source of truth is the
live tab group keyed by that numeric id, recoverable via the daemon verb
``BrowserwrightDaemon.recoverSession``.

``ensure_session_target`` runs a 3-step fallback (in-process → ledger.runtime
fast path → group-id recovery) so callers re-attach to a session's tab across
daemon restarts / extension reconnects / new processes without doing anything.

This module is also the INTERNAL home of the agent-path tab lifecycle that
survived the Phase C primitives retirement (the agent surface is real
Playwright now — ``page`` / ``context`` / ``snapshot()``):

  - ``session_tabs``           — enumerate the session's page targets
  - ``bind_target``            — attach + persist a tab as the session's current
  - ``open_session_tab``       — daemon ``openBackgroundTab`` (session group)
  - ``close_session_tab``      — daemon ``closeTab``
  - ``resolve_current_target`` — the "reuse → recover → adopt-own → open"
    current-tab discipline the Playwright binding glue relies on
  - ``eval_js`` / ``wait_for_ready`` — minimal agent-path evaluate helpers
    (used by browserwright's own verify paths and the daemon-capability e2e
    tests; NOT an agent surface)

All helpers take the ``Session`` explicitly — no ContextVar reads here.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from . import session_registry as reg
from .errors import CDPError

#: URL prefixes of Chrome-internal surfaces the debugger can't (or shouldn't)
#: drive. Used to filter "real" pages out of target enumeration.
INTERNAL_URL_PREFIXES = ("chrome://", "chrome-untrusted://", "devtools://",
                         "chrome-extension://", "about:")


def _resolve_sid(sess) -> Optional[str]:
    """Best-effort current session id from the bound record."""
    rec = getattr(sess, "session_record", None)
    if isinstance(rec, dict) and rec.get("id"):
        return rec["id"]
    return None


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
    sess.current_target_id = target_id
    return target_id


def ensure_session_target(sess) -> Optional[str]:
    """Transparent 3-step recovery of the session's attached tab.

    1. ``sess.current_target_id`` already set → return it (in-process).
    2. ledger ``runtime.current_target_id`` → try ``cdp.attach(tid)`` (FAST
       PATH, no group query). The daemon auto-reattaches the debugger; a
       stale/closed tab raises → fall through.
    3. group id: ``BrowserwrightDaemon.recoverSession`` by persisted group id
       → register + persist the new binding. On CDPError / empty group return
       None.

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

    # Step 3: durable group recovery by the persisted numeric groupId — NOT the
    # title (names aren't unique; the session = the tab group, keyed by id). The
    # groupId is cached in ledger.runtime.group_id on every open; without it
    # there's nothing to recover (a brand-new session, or Chrome itself
    # restarted and reassigned group ids — which needs no recovery).
    runtime = (rec.get("runtime") or {}) if isinstance(rec, dict) else {}
    gid = runtime.get("group_id")
    sid = rec.get("id") if isinstance(rec, dict) else _resolve_sid(sess)
    if not isinstance(gid, int) or gid < 0:
        return None
    try:
        payload = sess.cdp.send(
            "BrowserwrightDaemon.recoverSession", groupId=gid, bsSession=sid,
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


# ---------------------------------------------------------------------------
# Internal agent-path tab lifecycle (post-Phase-C: not an agent surface).
# ---------------------------------------------------------------------------


def session_tabs(sess, *, include_internal: bool = True) -> list[dict]:
    """The session's page targets ``[{targetId, url, title, attached}]``.

    Unified across backends: the daemon scopes ``Target.getTargets`` to the
    session's browser (extension = the session's tab group; rdp/env = the
    daemon-owned Chrome). ``[]`` when the session has no tabs — a legitimate
    state, not an error. ``include_internal=False`` filters chrome:// et al.
    """
    res = sess.cdp.send("Target.getTargets")
    out: list[dict] = []
    for t in res.get("targetInfos", []):
        if t.get("type") != "page":
            continue
        if (not include_internal
                and t.get("url", "").startswith(INTERNAL_URL_PREFIXES)):
            continue
        out.append({
            "targetId": t.get("targetId"),
            "url": t.get("url", ""),
            "title": t.get("title", ""),
            "attached": t.get("attached", False),
        })
    return out


def bind_target(sess, target_id: str) -> dict:
    """Attach ``target_id``, make it the session's current tab, persist it.

    Raises ``CDPError`` when the target no longer exists (tab closed, or the
    daemon restarted since the id was issued)."""
    sess.cdp.attach(target_id)
    sess.current_target_id = target_id
    persist_target(target_id, sess=sess)
    try:
        sess.cdp.send("Target.activateTarget", targetId=target_id)
    except CDPError:
        pass
    return {"targetId": target_id}


def open_session_tab(
    sess,
    url: str = "about:blank",
    *,
    background: bool = True,
    skip_post_attach_commands: bool = False,
) -> dict:
    """Open a new working tab in the session's browser, attach, bind current.

    The daemon dispatches ``BrowserwrightDaemon.openBackgroundTab`` by the
    session's (immutable) backend: extension opens inside the session's tab
    group honoring ``background=`` (don't steal the user's focus); rdp/env use
    ``Target.createTarget``. Returns ``{targetId, tabId, url, title, groupId}``
    (``groupId`` is -1 on non-extension backends)."""
    rec = getattr(sess, "session_record", None)
    sid = rec.get("id") if isinstance(rec, dict) else None
    try:
        payload = sess.cdp.send(
            "BrowserwrightDaemon.openBackgroundTab",
            url=url,
            bsSession=sid,
            background=background,
            skipPostAttachCommands=skip_post_attach_commands,
        )
    except CDPError as e:
        raise CDPError(
            method="BrowserwrightDaemon.openBackgroundTab",
            params={"url": url},
            cdp_message=(
                f"open failed: {e.cdp_message}. Requires a running daemon."
            ),
        ) from e
    if not payload:
        raise CDPError(
            method="BrowserwrightDaemon.openBackgroundTab",
            params={"url": url},
            cdp_message="daemon returned an empty payload",
        )
    target_id = payload.get("targetId")
    session_id = payload.get("sessionId")
    if not target_id or not session_id:
        raise CDPError(
            method="BrowserwrightDaemon.openBackgroundTab",
            params={"url": url},
            cdp_message=f"daemon returned incomplete payload: {payload!r}",
        )
    register_recovered(sess, payload)
    persist_target(target_id, group_id=payload.get("groupId"), sess=sess)
    return {
        "targetId": target_id,
        "tabId": payload.get("tabId"),
        "url": payload.get("url", url),
        "title": payload.get("title", ""),
        # groupId is the session's tab-group id on extension (the durable
        # reconnect anchor), -1 on rdp/env (tab groups are an extension concept).
        "groupId": payload.get("groupId", -1),
    }


def close_session_tab(
    sess, *, target_id: Optional[str] = None, session_id: Optional[str] = None,
) -> dict:
    """Close a tab via the daemon (``chrome.tabs.remove`` on extension,
    ``Target.closeTarget`` on rdp/env). Defaults to the session's current tab.

    Returns ``{"ok": True, "tabId": N}``; clears any cached CDP state for the
    closed target."""
    if target_id is None and session_id is None:
        target_id = sess.current_target_id
        if not target_id:
            raise CDPError(
                method="BrowserwrightDaemon.closeTab",
                params={"sessionId": None, "targetId": None},
                cdp_message="close_session_tab: no current attached tab to close",
            )
    if session_id is None and target_id is not None:
        session_id = sess.cdp._sessions.get(target_id)
    try:
        payload = sess.cdp.send(
            "BrowserwrightDaemon.closeTab",
            sessionId=session_id,
            targetId=target_id,
        )
    except CDPError as e:
        raise CDPError(
            method="BrowserwrightDaemon.closeTab",
            params={"sessionId": session_id, "targetId": target_id},
            cdp_message=f"close tab failed: {e.cdp_message}.",
        ) from e
    if not payload:
        raise CDPError(
            method="BrowserwrightDaemon.closeTab",
            params={"sessionId": session_id},
            cdp_message="daemon returned an empty close-tab payload",
        )
    cdp = sess.cdp
    stale_targets = [tid for tid, sid in cdp._sessions.items()
                     if sid == session_id]
    for tid in stale_targets:
        cdp._sessions.pop(tid, None)
        if sess.current_target_id == tid:
            sess.current_target_id = None
    return {"ok": bool(payload.get("ok", True)),
            "tabId": payload.get("tabId")}


def resolve_current_target(sess) -> dict:
    """The session's current working tab; auto-opens one if none.

    This is the reuse/recovery/auto-open discipline the Playwright binding
    glue (``repl/playwright_handle.bind_current_page``) relies on. Resolution
    order:
      1. the cached current target, if it's still a live tab of this session;
      2. transparent reconnect-recovery (ledger.runtime → group id);
      3. an existing tab of the session (first real page);
      4. else ``open_session_tab()`` a fresh working tab.

    The empty fallback OPENS a new tab — it never adopts the user's focused
    tab (too invasive for an implicit call)."""
    # 1. Cached current target still valid?
    if sess.current_target_id:
        for t in session_tabs(sess, include_internal=False):
            if t["targetId"] == sess.current_target_id:
                return {**t, "accuracy": "exact"}
        # Cached target gone (tab closed). Drop it and fall through.
        sess.current_target_id = None
    # 2. Transparent reconnect-recovery before opening anything new.
    recovered = ensure_session_target(sess)
    if recovered:
        for t in session_tabs(sess, include_internal=False):
            if t["targetId"] == recovered:
                return {**t, "accuracy": "exact"}
        return {"targetId": recovered, "accuracy": "exact"}
    # 3. Any existing real tab of the session.
    tabs = session_tabs(sess, include_internal=False)
    if tabs:
        bind_target(sess, tabs[0]["targetId"])
        return {"targetId": tabs[0]["targetId"], "url": tabs[0]["url"],
                "title": tabs[0]["title"], "accuracy": "unknown"}
    # 4. Empty session — open a fresh working tab (NOT adopt).
    opened = open_session_tab(sess, "about:blank",
                              skip_post_attach_commands=True)
    return opened | {"accuracy": "unknown"}


def eval_js(sess, expression: str, *, await_promise: bool = False) -> Any:
    """Evaluate a raw JS *expression* on the session's current tab and return
    its JSON value (``Runtime.evaluate`` with ``returnByValue``).

    Transparently recovers the tab binding first (``ensure_session_target``).
    Internal helper for browserwright's own verify paths and the
    daemon-capability e2e tests — the agent surface is ``page.evaluate``."""
    target_id = sess.current_target_id or ensure_session_target(sess)
    if not target_id:
        raise CDPError(
            method="Runtime.evaluate",
            params={"expression": expression},
            cdp_message="eval_js: session has no current tab",
        )
    sid = sess.cdp.attach(target_id)
    res = sess.cdp.send(
        "Runtime.evaluate", session=sid, expression=expression,
        returnByValue=True, awaitPromise=await_promise,
    )
    exc = res.get("exceptionDetails")
    if exc:
        detail = ((exc.get("exception") or {}).get("description")
                  or exc.get("text") or "evaluate failed")
        raise CDPError(
            method="Runtime.evaluate",
            params={"expression": expression},
            cdp_message=detail,
        )
    return (res.get("result") or {}).get("value")


def wait_for_ready(sess, timeout: float = 15.0) -> bool:
    """Block until the tab's REAL document is loaded, or timeout.

    ``readyState === 'complete'`` alone is not enough: a freshly-opened tab
    (``tabs.create`` / ``Target.createTarget``) starts on the initial
    ``about:blank`` document, which reports ``complete`` immediately while the
    real navigation is still in flight — the caller would then eval against an
    empty document and see missing DOM (e2e: parity/multisession textContent
    null under load). Wait for a complete document on a non-initial URL
    instead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = eval_js(sess, "document.readyState")
            href = eval_js(sess, "location.href")
        except Exception:
            state = href = None
        if state == "complete" and href not in (None, "", "about:blank"):
            return True
        time.sleep(0.3)
    return False
