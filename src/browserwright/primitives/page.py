"""Navigation / tab primitives."""
from __future__ import annotations

import time
from typing import Any

from ..errors import CDPError, PageLoadFailed
from ..session import current_session


_INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://",
             "chrome-extension://", "about:")


def _is_nonattachable_internal_url_error(msg: str | None) -> bool:
    """Does this CDP/daemon error mean 'the active tab is an internal page the
    debugger can't attach to'?

    Chrome's ``chrome.debugger.attach`` rejects internal surfaces with messages
    like ``Cannot access a chrome-extension:// URL`` /
    ``Cannot access contents of url "chrome://..."`` /
    ``Cannot attach to this target (devtools://...)``. We match on the *internal
    scheme* appearing alongside an access/attach refusal, so the detection
    generalizes across schemes (chrome://, chrome-extension://, devtools://,
    chrome-untrusted://) and Chrome's slightly-varying wording — not one
    hardcoded string.
    """
    if not msg:
        return False
    low = msg.lower()
    mentions_internal = any(scheme in low for scheme in _INTERNAL)
    refuses = ("cannot access" in low or "cannot attach" in low
               or "cannot be attached" in low)
    return mentions_internal and refuses


def attach_active() -> dict:
    """First-class verb: bind the session to its "active" tab.

    Unified across backends — the daemon dispatches by the session's
    (immutable) backend, so this primitive never branches:
      - extension: adopt the user's currently-focused-window active tab into
        this session's tab group (the analogue of rdp's owned Chrome).
      - rdp: the daemon owns the Chrome, so "active" = the session's current
        front target (most-recently-fronted), created+attached if none.

    Returns ``{targetId, tabId, url, title}``. The Session's current target
    is set so subsequent primitives (``js``, ``goto_url``, ``capture_screenshot``)
    operate on this tab.

    The returned ``targetId`` stays valid across heredocs as long as the tab
    is open and the daemon is alive — print it, capture it in the agent's
    conversation, and use ``switch_tab(targetId)`` in subsequent heredocs to
    re-bind without another ``attach_active()`` call. That's more
    deterministic than re-attaching the focused tab on every heredoc,
    which can drift if the user clicks another window between calls.
    See SKILL.md "Persisting a tab handle across heredocs".
    """
    from collections import deque as _deque
    sess = current_session()
    # Route through the long-lived ws (sess.cdp), NOT a CLI subprocess. The
    # subprocess opens its own short-lived client connection — daemon binds
    # the local sessionId to that ephemeral client and discards it on
    # disconnect, so by the time the caller hands the sid to a primitive on
    # its own connection the proxy answers "unknown sessionId".
    try:
        result = sess.cdp.send("BrowserwrightDaemon.attachActiveTab")
    except CDPError as e:
        # The user's *focused* tab may be an internal page Chrome's debugger
        # refuses to attach to (chrome://, chrome-extension://, devtools://, a
        # New Tab Page, …). That's not fatal — it just means "the active tab
        # isn't drivable". Fall back to open(): a fresh attachable working tab
        # in this session's browser the agent can drive. Generic — any
        # non-attachable internal URL triggers it, no scheme hardcoded.
        if _is_nonattachable_internal_url_error(e.cdp_message):
            return open("about:blank")
        raise
    if not result:
        raise CDPError(
            method="BrowserwrightDaemon.attachActiveTab",
            params={},
            cdp_message="empty response from daemon",
        )
    target_id = result.get("targetId")
    sid = result.get("sessionId")
    if not isinstance(target_id, str) or not isinstance(sid, str):
        raise CDPError(
            method="BrowserwrightDaemon.attachActiveTab",
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
    """The session's tabs ``[{targetId, url, title, attached}]``.

    Unified across backends (docs §Tier B): the daemon scopes enumeration to
    the session's browser (extension = the session's tab group; rdp = the
    daemon-owned Chrome's targets). Returns ``[]`` when the session has no
    tabs — an empty session is a legitimate state, not an error.
    """
    sess = current_session()
    res = sess.cdp.send("Target.getTargets")
    out: list[dict] = []
    for t in res.get("targetInfos", []):
        if t.get("type") != "page":
            continue
        if not include_chrome and t.get("url", "").startswith(_INTERNAL):
            continue
        out.append({
            "targetId": t.get("targetId"),
            "url": t.get("url", ""),
            "title": t.get("title", ""),
            "attached": t.get("attached", False),
        })
    return out


def current_tab() -> dict | None:
    """The tab Skill is currently bound to, or ``None`` if none yet.

    Unified across backends (docs §Tier B): returns the current binding (may
    be stale / a chrome:// page) or ``None`` when the session hasn't picked a
    tab. ``None`` is a legitimate "no tab bound yet" state — call
    ``current_page()`` (auto-opens) or ``attach_active()`` to get one.
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


def open(url: str = "about:blank", *, background: bool = True) -> dict:
    """Open a new working tab in this session's browser, attach, bind as
    current. The unified tab-opening primitive (docs §Tier B) — replaces
    both ``new_tab`` and ``open_background``.

    Returns ``{targetId, tabId, url, title}``. The daemon dispatches by the
    session's (immutable) backend, so this primitive never branches:
      - extension: opens the tab in this session's tab group; ``background=``
        is honored (``True`` = don't steal the user's focus).
      - rdp: ``Target.createTarget``; ``background=`` is a no-op (no human
        focus to protect — every tab is "background").

    The ``targetId`` stays valid across heredocs as long as the tab is open
    and the daemon is alive — print it, capture it in the agent's
    conversation, and use ``switch_tab(targetId)`` in later heredocs to
    re-bind. See SKILL.md "Persisting a tab handle across heredocs".
    """
    sess = current_session()
    _name, sid = _session_name_and_id(sess)
    try:
        payload = sess.cdp.send(
            "BrowserwrightDaemon.openBackgroundTab",
            url=url, bsSession=sid, background=background,
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
    from ..session_runtime import persist_target, register_recovered
    register_recovered(sess, payload)
    persist_target(target_id, group_id=payload.get("groupId"), sess=sess)
    return {
        "targetId": target_id,
        "tabId": payload.get("tabId"),
        "url": payload.get("url", url),
        "title": payload.get("title", ""),
    }


def new_tab(url: str = "about:blank") -> dict:
    """DEPRECATED alias for :func:`open`. Kept for one release so existing
    callers / solidified tasks don't break. Use ``open(url)`` instead."""
    return open(url)


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


def reload(*, hard: bool = False) -> dict:
    """Reload the currently attached tab, then wait for it to finish loading.

    First-class refresh primitive: issues ``Page.reload`` (``ignoreCache=hard``
    bypasses the HTTP cache, the equivalent of Ctrl/Cmd-Shift-R) and blocks on
    ``wait_for_load()``. Use this instead of ``goto_url(current_url)`` to get the
    page to re-fetch — it's what you reach for when a tab is stale or an action
    didn't take. Returns the post-reload ``page_info()`` dict.

    Requires a tab to be attached; auto-attaches via ``current_page()`` if the
    session has no current target yet.
    """
    from .inspect import cdp, page_info

    sess = current_session()
    if not sess.current_target_id:
        current_page()
    sid = sess.cdp.attach(sess.current_target_id)
    try:
        cdp("Page.reload", session_id=sid, ignoreCache=hard)
    except CDPError as e:
        raise PageLoadFailed(url="(reload)", reason=e.cdp_message) from e
    wait_for_load()
    return page_info()


def current_page() -> dict:
    """The session's current working tab; auto-opens one if none (docs §Tier B).

    Unified across backends — no ``backend_name`` branch. Resolution order:
      1. the cached current target, if it's still a live tab of this session;
      2. transparent reconnect-recovery (ledger.runtime → group anchor);
      3. an existing tab of the session (first real page);
      4. else ``open()`` a fresh working tab.

    The empty fallback is ``open()`` (a NEW tab), NOT ``attach_active()`` —
    adopt moves the user's focused tab, too invasive for an implicit call
    (docs: "current_page() empty fallback = open() NOT adopt").
    """
    sess = current_session()
    # 1. Cached current target still valid?
    if sess.current_target_id:
        for t in list_tabs(include_chrome=False):
            if t["targetId"] == sess.current_target_id:
                return {**t, "accuracy": "exact"}
        # Cached target gone (tab closed). Drop it and fall through.
        sess.current_target_id = None
    # 2. Transparent reconnect-recovery before opening anything new.
    from ..session_runtime import ensure_session_target
    recovered = ensure_session_target(sess)
    if recovered:
        for t in list_tabs(include_chrome=False):
            if t["targetId"] == recovered:
                return {**t, "accuracy": "exact"}
        return {"targetId": recovered, "accuracy": "exact"}
    # 3. Any existing real tab of the session.
    tabs = list_tabs(include_chrome=False)
    if tabs:
        switch_tab(tabs[0])
        return {"targetId": tabs[0]["targetId"], "url": tabs[0]["url"],
                "title": tabs[0]["title"], "accuracy": "unknown"}
    # 4. Empty session — open a fresh working tab (NOT adopt).
    return open("about:blank") | {"accuracy": "unknown"}


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
    available (e.g. the daemon's active-tab probe is unreachable) and
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


def open_background(url: str, *, group: str | None = None) -> dict:
    """DEPRECATED alias for :func:`open` (``background=True``). Kept for one
    release so existing callers / solidified tasks don't break.

    The ``group=`` kwarg is now an internal daemon detail (the group is
    derived from the session) — it is accepted but ignored. Use
    ``open(url, background=True)`` instead.
    """
    return open(url, background=True)


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
                method="BrowserwrightDaemon.closeTab",
                params={"sessionId": None, "targetId": None},
                cdp_message="close_tab: no current attached tab to close",
            )
        resolved_session_id = sess.cdp._sessions.get(resolved_target_id)
    elif resolved_session_id is None and resolved_target_id is not None:
        # Caller passed target_id; fill in session_id from local cache if we have it.
        resolved_session_id = sess.cdp._sessions.get(resolved_target_id)
    try:
        payload = sess.cdp.send(
            "BrowserwrightDaemon.closeTab",
            sessionId=resolved_session_id,
            targetId=resolved_target_id,
        )
    except CDPError as e:
        raise CDPError(
            method="BrowserwrightDaemon.closeTab",
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
            method="BrowserwrightDaemon.closeTab",
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
