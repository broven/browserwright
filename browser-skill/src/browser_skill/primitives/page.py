"""Navigation / tab primitives."""
from __future__ import annotations

import time
from typing import Any

from ..errors import CDPError, NeedsUserConfirm, PageLoadFailed
from ..session import current_session


_INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://",
             "chrome-extension://", "about:")


def attach_active() -> dict:
    """v0.5.4: extension-backend-only. Attach the user's currently-focused-
    window active tab in Chrome without requiring a popup click.

    Returns ``{targetId, tabId, url, title}``. The Session's current target
    is set so subsequent primitives (``js``, ``goto_url``, ``capture_screenshot``)
    operate on this tab. Raises ``CDPError`` if the daemon backend isn't
    extension or no extension is connected.

    The returned ``targetId`` stays valid across heredocs as long as the tab
    is open and the daemon is alive — print it, capture it in the agent's
    conversation, and use ``switch_tab(targetId)`` in subsequent heredocs to
    re-bind without another ``attach_active()`` call. That's more
    deterministic than re-attaching the focused tab on every heredoc,
    which can drift if the user clicks another window between calls.
    See SKILL.md "Persisting a tab handle across heredocs".

    For non-extension backends (rdp/env) the existing ``current_page()``
    helper already resolves the active tab via Target.getTargets — use
    that path instead.
    """
    from collections import deque as _deque
    sess = current_session()
    # Route through the long-lived ws (sess.cdp), NOT a CLI subprocess. The
    # subprocess opens its own short-lived client connection — daemon binds
    # the local sessionId to that ephemeral client and discards it on
    # disconnect, so by the time the caller hands the sid to a primitive on
    # its own connection the proxy answers "unknown sessionId".
    try:
        result = sess.cdp.send("BrowserDaemon.attachActiveTab")
    except CDPError as e:
        raise CDPError(
            method="BrowserDaemon.attachActiveTab",
            params={},
            cdp_message=(e.cdp_message or
                         "attach_active() requires the extension backend; "
                         "start the daemon with `browser-daemon serve "
                         "--backend extension` and load the chrome-extension/."),
        ) from e
    if not result:
        raise CDPError(
            method="BrowserDaemon.attachActiveTab",
            params={},
            cdp_message="empty response from daemon",
        )
    target_id = result.get("targetId")
    sid = result.get("sessionId")
    if not isinstance(target_id, str) or not isinstance(sid, str):
        raise CDPError(
            method="BrowserDaemon.attachActiveTab",
            params={},
            cdp_message=f"malformed daemon response: {result!r}",
        )
    sess.current_target_id = target_id
    from ..session_runtime import persist_target
    persist_target(target_id, sess=sess)
    # Register the daemon-minted session in CDPSession's tables so subsequent
    # send(method, session=sid) calls work, mirroring CDPSession.attach()'s
    # post-attach state. We skip the regular Page/Runtime/DOM/Network.enable
    # bootstrap because attach_active routes through the extension backend
    # which auto-enables those via the relay's session setup.
    cdp_session = sess.cdp
    from ..cdp import _EVENT_RING_LIMIT
    cdp_session._sessions[target_id] = sid  # type: ignore[attr-defined]
    cdp_session._events.setdefault(sid, _deque(maxlen=_EVENT_RING_LIMIT))  # type: ignore[attr-defined]
    # Best-effort domain enables (matching CDPSession.attach()). Errors are
    # tolerated since some Chrome builds noop on certain domains.
    for domain in ("Page", "Runtime", "DOM", "Network"):
        try:
            cdp_session.send(f"{domain}.enable", session=sid)
        except CDPError:
            pass
    return {
        "targetId": target_id,
        "tabId": result.get("tabId"),
        "url": result.get("url", ""),
        "title": result.get("title", ""),
    }


def attach_readonly(target_id: str) -> str:
    """Open a read-only secondary session on ``target_id`` (daemon v0.3 H7).

    Returns the daemon-assigned sessionId. The caller can ``drain_events()``
    or call ``cdp(..., session_id=sid)`` for read-only queries (e.g.
    ``Runtime.evaluate`` will be rejected by the daemon with ``-32602`` per
    the H7 contract; ``DOM.getDocument`` etc. likewise).

    Practical pattern: a monitoring task tails the page another agent is
    operating on. Use this primitive instead of ``switch_tab`` so you don't
    yank the foreground from under them.
    """
    sess = current_session()
    return sess.cdp.attach_readonly(target_id)


def list_tabs(include_chrome: bool = True) -> list[dict]:
    sess = current_session()
    res = sess.cdp.send("Target.getTargets")
    # Page-type targets, unfiltered — used for the "any attached at all?" check.
    raw_pages = [t for t in res.get("targetInfos", []) if t.get("type") == "page"]
    out: list[dict] = []
    for t in raw_pages:
        if not include_chrome and t.get("url", "").startswith(_INTERNAL):
            continue
        out.append({
            "targetId": t.get("targetId"),
            "url": t.get("url", ""),
            "title": t.get("title", ""),
            "attached": t.get("attached", False),
        })
    # Extension backend returns only ghost targets — tabs the user has
    # explicitly attached. Zero ghosts isn't "Chrome has no tabs", it's
    # "you haven't attached one yet"; the agent's next move is
    # `attach_active()` (drive the user's focused tab) or
    # `open_background(url)` (open a fresh tab in the agent group). Decide
    # based on the unfiltered page-target count — a ghost on chrome://newtab/
    # IS attached, just hidden by include_chrome=False; falsely raising
    # there would silently clear the cached attachment in current_page().
    if not raw_pages and sess.backend_name == "extension":
        raise NeedsUserConfirm(
            what="extension backend has zero attached tabs",
            proposal=(
                "call `open_background(url, group='Agent')` to spawn a new "
                "background tab in the Agent group (does not steal user focus), "
                "or `attach_active()` to drive the focused-window tab if the "
                "task is explicitly 'use my current tab'"
            ),
        )
    return out


def current_tab() -> dict | None:
    """The tab Skill is currently attached to (may be stale / chrome:// page).

    Mode A backends: returns ``None`` when no tab has been attached yet —
    a legitimate "Chrome has tabs but Skill hasn't picked one" state.

    Extension backend: ``None`` is never legitimate here because the
    daemon only knows about explicitly-attached ghost targets. Raise
    instead so the agent learns to call ``attach_active()`` /
    ``open_background()`` first.
    """
    sess = current_session()
    if not sess.current_target_id:
        # Transparent reconnect-recovery before declaring "no tab".
        from ..session_runtime import ensure_session_target
        ensure_session_target(sess)
    if sess.current_target_id:
        for t in list_tabs():
            if t["targetId"] == sess.current_target_id:
                return t
    if sess.backend_name == "extension":
        raise NeedsUserConfirm(
            what="no tab attached on extension backend",
            proposal=(
                "call `open_background(url, group='Agent')` to spawn a "
                "background tab (does not steal user focus), or "
                "`attach_active()` to attach the focused-window tab if "
                "the task is explicitly 'use my current tab'"
            ),
        )
    return None


def switch_tab(target) -> dict:
    """Bind the current Session to ``target``.

    ``target`` = targetId string or a dict carrying ``targetId``.

    Primary use case is heredoc-to-heredoc continuity: capture the
    ``targetId`` from ``attach_active()`` / ``new_tab()`` /
    ``open_background()``, then call ``switch_tab(targetId)`` at the top
    of subsequent heredocs to re-bind to the same tab without going
    through another attach. Cheaper and more deterministic than
    ``attach_active()`` (which always grabs the currently-focused tab
    and can drift if the user clicks another window between heredocs).

    Raises ``CDPError`` with an actionable message when the target no
    longer exists (tab closed since the handle was issued).
    """
    if isinstance(target, dict):
        target_id = target.get("targetId")
    else:
        target_id = target
    if not target_id:
        raise ValueError("switch_tab: missing targetId")
    sess = current_session()
    try:
        sess.cdp.attach(target_id)
    except CDPError as e:
        raise CDPError(
            method="Target.attachToTarget",
            params={"targetId": target_id},
            cdp_message=(
                f"switch_tab: target {target_id!r} no longer exists "
                f"(tab likely closed, or daemon restarted since the "
                f"handle was issued). Call `attach_active()` "
                f"(extension backend) or `new_tab(url)` to get a fresh "
                f"handle. Original CDP error: {e.cdp_message}"
            ),
        ) from e
    sess.current_target_id = target_id
    from ..session_runtime import persist_target
    persist_target(target_id, sess=sess)
    try:
        sess.cdp.send("Target.activateTarget", targetId=target_id)
    except CDPError:
        pass
    return {"targetId": target_id}


def new_tab(url: str = "about:blank") -> dict:
    """Create a new tab and bind the Session to it.

    Returns ``{targetId, url}``. The ``targetId`` stays valid across
    heredocs as long as the tab is open and the daemon is alive — print
    it, capture it in the agent's conversation, and use
    ``switch_tab(targetId)`` in later heredocs to re-bind. See SKILL.md
    "Persisting a tab handle across heredocs".
    """
    sess = current_session()
    res = sess.cdp.send("Target.createTarget", url=url)
    target_id = res["targetId"]
    sess.cdp.attach(target_id)
    sess.current_target_id = target_id
    from ..session_runtime import persist_target
    persist_target(target_id, sess=sess)
    if url not in ("", "about:blank"):
        # ``Target.createTarget`` returns before the URL is actually loading,
        # and an empty about:blank may pass readyState=='complete' the first
        # time we poll. Wait until location.href has actually moved off
        # about:blank *and* readyState is complete — bounded by the same
        # timeout.
        try:
            _wait_for_real_load(url, timeout=15.0)
        except PageLoadFailed:
            pass
    return {"targetId": target_id, "url": url}


def _wait_for_real_load(target_url: str, *, timeout: float) -> bool:
    """Wait until the page has actually navigated AND finished loading.

    This is the two-condition wait we need after ``Target.createTarget`` —
    plain ``wait_for_load`` only checks ``document.readyState`` which is
    "complete" on the empty placeholder before the URL kicks in.
    """
    from .interact import js

    deadline = time.monotonic() + timeout
    moved = False
    while time.monotonic() < deadline:
        try:
            state = js(
                "return {h: location.href, r: document.readyState}"
            )
        except Exception:
            state = None
        if state:
            href = state.get("h") or ""
            ready = state.get("r")
            if not moved and href and href != "about:blank":
                moved = True
            if moved and ready == "complete":
                return True
        time.sleep(0.2)
    return False


def goto_url(url: str) -> dict:
    """Navigate the currently attached tab to ``url``. If no tab is attached
    yet, attach the first real page (or create one)."""
    sess = current_session()
    if not sess.current_target_id:
        # Attach to first non-chrome page, or open a new one.
        tabs = [t for t in list_tabs(include_chrome=False) if t["url"] != ""]
        if tabs:
            switch_tab(tabs[0])
        else:
            return new_tab(url)
    sid = sess.cdp.attach(sess.current_target_id)
    try:
        sess.cdp.send("Page.navigate", session=sid, url=url)
    except CDPError as e:
        raise PageLoadFailed(url=url, reason=e.cdp_message) from e
    return {"url": url}


def current_page() -> dict:
    """User's visually-foreground tab (US1). Auto-attaches.

    Mode A backends use ``browser-daemon active-tab --json`` to find the
    most-recently-activated target and switch to it.

    Extension backend has no notion of "active tab" outside of attached
    ghosts, so we route through ``attach_active()`` instead — the daemon
    asks the extension to attach Chrome's focused-window active tab right
    now. The first call in a session triggers Chrome's yellow banner;
    subsequent calls in the same session reuse the cached target.

    When ``BS_CDP_WS`` / ``BU_CDP_WS`` is set the daemon CLI may be
    querying a different Chrome than the one we're attached to. Trust
    ``list_tabs`` over the daemon's hint in that case.
    """
    import os as _os
    sess = current_session()
    if sess.backend_name == "extension":
        if sess.current_target_id:
            try:
                for t in list_tabs(include_chrome=False):
                    if t["targetId"] == sess.current_target_id:
                        return {**t, "accuracy": "exact"}
            except NeedsUserConfirm:
                # Cached target got reaped (tab closed). Fall through to a
                # fresh attach instead of bubbling the raise.
                sess.current_target_id = None
        # Transparent reconnect-recovery: re-attach to this session's tab
        # (ledger.runtime fast path → group anchor) before grabbing the
        # user's focused tab via attach_active().
        from ..session_runtime import ensure_session_target
        recovered = ensure_session_target(sess)
        if recovered:
            for t in list_tabs(include_chrome=False):
                if t["targetId"] == recovered:
                    return {**t, "accuracy": "exact"}
            return {"targetId": recovered, "accuracy": "exact"}
        info = attach_active()
        return {**info, "accuracy": "exact"}
    explicit_ws = bool(_os.environ.get("BS_CDP_WS") or _os.environ.get("BU_CDP_WS"))
    if not explicit_ws:
        info = sess.daemon.active_tab()
        if info and info.get("targetId"):
            sess.last_active_tab = info
            # Confirm the targetId is actually reachable on our ws before
            # blindly switching to it (cross-Chrome confusion guard).
            if any(t["targetId"] == info["targetId"]
                   for t in list_tabs(include_chrome=False)):
                switch_tab(info["targetId"])
                return {**info, "accuracy": info.get("accuracy", "unknown")}
    # Degrade: pick first real tab, or open a fresh one.
    tabs = [t for t in list_tabs(include_chrome=False)]
    if tabs:
        switch_tab(tabs[0])
        return {"targetId": tabs[0]["targetId"], "url": tabs[0]["url"],
                "title": tabs[0]["title"], "accuracy": "unknown"}
    return new_tab("about:blank") | {"accuracy": "unknown"}


def wait(seconds: float = 1.0) -> None:
    time.sleep(seconds)


def wait_for_load(timeout: float = 15.0) -> bool:
    """Block until ``document.readyState === 'complete'`` or timeout."""
    from .interact import js  # local import to avoid cycle at module init

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = js("return document.readyState")
        except Exception:
            state = None
        if state == "complete":
            return True
        time.sleep(0.3)
    return False


def ensure_real_tab() -> dict | None:
    """Switch to a real user-facing tab if the current attachment is on a
    chrome:// / devtools:// / about: / extension page.

    Returns the dict of the tab we landed on, or ``None`` if no real
    tabs exist. Falls back to ``current_tab()`` when the current
    attachment is already a real page.

    Use this when ``current_page()``'s active-tab heuristic isn't
    available (e.g. ``BS_CDP_WS`` set, daemon Mode A unreachable) and
    you just want "some real page" to operate on.
    """
    tabs = list_tabs(include_chrome=False)
    if not tabs:
        return None
    cur = current_tab()
    if cur and cur.get("url") and not cur["url"].startswith(_INTERNAL):
        return cur
    switch_tab(tabs[0])
    return tabs[0]


def _session_name_and_id(sess) -> tuple[Any, Any]:
    """Resolve the current session's (name, id) from the bound record or
    BD_SESSION→ledger. Either may be None when no session is in scope."""
    rec = getattr(sess, "session_record", None)
    if not isinstance(rec, dict):
        try:
            from ..session_ctx import resolve_session
            rec = resolve_session()
        except Exception:
            rec = None
    if isinstance(rec, dict):
        return rec.get("name"), rec.get("id")
    return None, None


def open_background(url: str, *, group: Optional[str] = None) -> dict:
    """Phase B Feature 1 — open a new Chrome tab in the background.

    The tab is created with ``active: false`` so the user's currently-focused
    tab keeps focus; the new tab is placed in a Chrome tab group and
    ``chrome.debugger`` is attached. The group title defaults to the current
    session's ``name`` (the durable reconnect-recovery anchor); pass an
    explicit ``group=`` to override. Returns
    ``{"targetId","tabId","url","title","groupId"}``. After this call,
    ``sess.current_target_id`` points at the new tab and subsequent primitives
    (``js``, ``capture_screenshot``, etc.) operate on it.

    Extension backend only. On other backends (rdp, cloud) the daemon
    answers ``-32601`` which we translate to ``CDPError``.
    """
    sess = current_session()
    name, sid = _session_name_and_id(sess)
    if group is None:
        group = name  # may stay None if no session/name resolvable
    try:
        payload = sess.cdp.send(
            "BrowserDaemon.openBackgroundTab",
            url=url, groupName=group, bsSession=sid,
        )
    except CDPError as e:
        raise CDPError(
            method="BrowserDaemon.openBackgroundTab",
            params={"url": url, "groupName": group},
            cdp_message=(
                f"open_background failed: {e.cdp_message}. "
                "Requires the extension backend with a running daemon."
            ),
        ) from e
    if not payload:
        raise CDPError(
            method="BrowserDaemon.openBackgroundTab",
            params={"url": url, "groupName": group},
            cdp_message="daemon returned an empty payload",
        )
    target_id = payload.get("targetId")
    session_id = payload.get("sessionId")
    if not target_id or not session_id:
        raise CDPError(
            method="BrowserDaemon.openBackgroundTab",
            params={"url": url, "groupName": group},
            cdp_message=f"daemon returned incomplete payload: {payload!r}",
        )
    # Pre-register the daemon-minted sessionId in the CDPSession's session
    # map (reused by recovery) and persist the binding to the ledger cache.
    from ..session_runtime import persist_target, register_recovered
    register_recovered(sess, payload)
    persist_target(target_id, group_id=payload.get("groupId"), sess=sess)
    return {
        "targetId": target_id,
        "tabId": payload.get("tabId"),
        "url": payload.get("url", url),
        "title": payload.get("title", ""),
        "groupId": payload.get("groupId", -1),
    }


def close_tab(
    session_id: str | None = None, *, target_id: str | None = None,
) -> dict:
    """Phase B Feature 2 — close the tab via ``chrome.tabs.remove``.

    Identify the tab one of two ways:
      - ``target_id`` (e.g. ``ext-tab-N`` returned by ``open_background``) —
        globally addressable, works across daemon-client boundaries.
      - ``session_id`` — per-client; falls back to the currently-attached
        tab when omitted. Only works in contexts that share the original
        opener's persistent ws.

    This is NOT a debugger detach. After the call returns the sessionId is
    invalid; any cached CDPSession state for it is cleared.

    Returns ``{"ok": True, "tabId": N}``.

    Extension backend only — raises ``CDPError`` on other backends.
    """
    sess = current_session()
    # Resolve target_id and session_id from local state when not passed,
    # then forward to the daemon over the long-lived ws so the session
    # binding lookup runs against this client's bindings.
    resolved_target_id = target_id
    resolved_session_id = session_id
    if resolved_target_id is None and resolved_session_id is None:
        resolved_target_id = sess.current_target_id
        if not resolved_target_id:
            raise CDPError(
                method="BrowserDaemon.closeTab",
                params={"sessionId": None, "targetId": None},
                cdp_message="close_tab: no current attached tab to close",
            )
        resolved_session_id = sess.cdp._sessions.get(resolved_target_id)
    elif resolved_session_id is None and resolved_target_id is not None:
        # Caller passed target_id; fill in session_id from local cache if we have it.
        resolved_session_id = sess.cdp._sessions.get(resolved_target_id)
    try:
        payload = sess.cdp.send(
            "BrowserDaemon.closeTab",
            sessionId=resolved_session_id,
            targetId=resolved_target_id,
        )
    except CDPError as e:
        raise CDPError(
            method="BrowserDaemon.closeTab",
            params={"sessionId": resolved_session_id,
                    "targetId": resolved_target_id},
            cdp_message=(
                f"close_tab failed: {e.cdp_message}. "
                "Requires the extension backend with a running daemon."
            ),
        ) from e
    # Backfill the local session_id var for the state-cleanup block below.
    session_id = resolved_session_id
    if not payload:
        raise CDPError(
            method="BrowserDaemon.closeTab",
            params={"sessionId": session_id},
            cdp_message="daemon returned an empty close-tab payload",
        )
    # Clear local CDPSession state — locate any target whose stored sessionId
    # matches and drop it; clear the per-session event ring too.
    cdp = sess.cdp
    stale_targets = [tid for tid, sid in cdp._sessions.items()
                     if sid == session_id]
    for tid in stale_targets:
        cdp._sessions.pop(tid, None)
        if sess.current_target_id == tid:
            sess.current_target_id = None
    cdp._events.pop(session_id, None)
    return {"ok": bool(payload.get("ok", True)),
            "tabId": payload.get("tabId")}


def iframe_target(url_substr: str) -> str | None:
    """First iframe target whose URL contains ``url_substr``. Returns
    its ``targetId`` (for ``js(..., target_id=...)``), or ``None`` if no
    iframe matches. Useful when a site embeds a cross-origin form/widget
    that ``js`` can't reach via the main-page context."""
    sess = current_session()
    res = sess.cdp.send("Target.getTargets")
    for t in res.get("targetInfos", []):
        if t.get("type") == "iframe" and url_substr in t.get("url", ""):
            return t.get("targetId")
    return None
