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

    For non-extension backends (rdp/autoconnect/env) the existing
    ``current_page()`` helper already resolves the active tab via
    Target.getTargets — use that path instead.
    """
    from collections import deque as _deque
    sess = current_session()
    result = sess.daemon.attach_active() if hasattr(sess.daemon, "attach_active") else None
    if not result:
        raise CDPError(
            method="BrowserDaemon.attachActiveTab",
            params={},
            cdp_message=("attach_active() requires the extension backend; "
                         "start the daemon with `browser-daemon serve "
                         "--backend extension` and load the chrome-extension/."),
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
    # Register the daemon-minted session in CDPSession's tables so subsequent
    # send(method, session=sid) calls work, mirroring CDPSession.attach()'s
    # post-attach state. We skip the regular Page/Runtime/DOM/Network.enable
    # bootstrap because attach_active routes through the extension backend
    # which auto-enables those via the relay's session setup.
    cdp_session = sess.cdp
    from .. import cdp as _cdp_mod
    cdp_session._sessions[target_id] = sid  # type: ignore[attr-defined]
    cdp_session._events.setdefault(sid, _deque(maxlen=_cdp_mod._EVENT_RING_LIMIT))  # type: ignore[attr-defined]
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
    # Extension backend returns only ghost targets — tabs the user has
    # explicitly attached. An empty list there isn't "Chrome has no tabs",
    # it's "you haven't attached one yet"; the agent's natural next move
    # is `attach_active()` (drive the user's focused tab) or
    # `open_background(url)` (open a fresh tab in the agent group). Make
    # that path discoverable instead of returning a silently-empty list.
    if not out and sess.backend_name == "extension":
        raise NeedsUserConfirm(
            what="extension backend has zero attached tabs",
            proposal=(
                "call `attach_active()` to drive the focused-window tab, "
                "or `open_background(url, group='Agent')` to spawn a new "
                "background tab in the Agent group"
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
    if sess.current_target_id:
        for t in list_tabs():
            if t["targetId"] == sess.current_target_id:
                return t
    if sess.backend_name == "extension":
        raise NeedsUserConfirm(
            what="no tab attached on extension backend",
            proposal=(
                "call `attach_active()` to attach the focused-window tab, "
                "or `open_background(url)` to spawn a background tab"
            ),
        )
    return None


def switch_tab(target) -> dict:
    """``target`` = targetId string or a dict carrying ``targetId``."""
    if isinstance(target, dict):
        target_id = target.get("targetId")
    else:
        target_id = target
    if not target_id:
        raise ValueError("switch_tab: missing targetId")
    sess = current_session()
    sess.cdp.attach(target_id)
    sess.current_target_id = target_id
    try:
        sess.cdp.send("Target.activateTarget", targetId=target_id)
    except CDPError:
        pass
    return {"targetId": target_id}


def new_tab(url: str = "about:blank") -> dict:
    sess = current_session()
    res = sess.cdp.send("Target.createTarget", url=url)
    target_id = res["targetId"]
    sess.cdp.attach(target_id)
    sess.current_target_id = target_id
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


def open_background(url: str, *, group: str = "Agent") -> dict:
    """Phase B Feature 1 — open a new Chrome tab in the background.

    The tab is created with ``active: false`` so the user's currently-focused
    tab keeps focus; the new tab is placed in a Chrome tab group titled
    ``group`` (default "Agent") and ``chrome.debugger`` is attached. Returns
    ``{"targetId","tabId","url","title","groupId"}``. After this call,
    ``sess.current_target_id`` points at the new tab and subsequent primitives
    (``js``, ``capture_screenshot``, etc.) operate on it.

    Extension backend only. On other backends (rdp, autoconnect, cloud) the
    daemon answers ``-32601`` which we translate to ``CDPError``.
    """
    sess = current_session()
    daemon = sess.daemon
    if not hasattr(daemon, "open_background"):
        raise CDPError(
            method="BrowserDaemon.openBackgroundTab",
            params={"url": url, "groupName": group},
            cdp_message="open_background requires the Mode B daemon client "
                        "(ModeBClient)",
        )
    payload = daemon.open_background(url, group=group)
    if not payload:
        raise CDPError(
            method="BrowserDaemon.openBackgroundTab",
            params={"url": url, "groupName": group},
            cdp_message="daemon did not return a valid open-background payload "
                        "(requires the extension backend, with a running daemon)",
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
    # map so subsequent primitives that ask for the active session reuse it
    # without a duplicate Target.attachToTarget roundtrip. We also ensure
    # the per-session event ring exists.
    cdp = sess.cdp
    cdp._sessions[target_id] = session_id
    from collections import deque  # local import: avoid module-level cost
    cdp._events.setdefault(session_id, deque(maxlen=1024))
    sess.current_target_id = target_id
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
    daemon = sess.daemon
    if not hasattr(daemon, "close_tab"):
        raise CDPError(
            method="BrowserDaemon.closeTab",
            params={"sessionId": session_id, "targetId": target_id},
            cdp_message="close_tab requires the Mode B daemon client (ModeBClient)",
        )
    # If neither was provided, default to the current attached tab. Resolve
    # both the targetId and the sessionId so daemon-side lookup has the best
    # chance regardless of which client carries the binding.
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
    payload = daemon.close_tab(
        resolved_session_id, target_id=resolved_target_id,
    )
    # Backfill below the rest of the function with the resolved id for state cleanup.
    session_id = resolved_session_id
    if not payload:
        raise CDPError(
            method="BrowserDaemon.closeTab",
            params={"sessionId": session_id},
            cdp_message="daemon did not return a valid close-tab payload "
                        "(requires the extension backend, with a running daemon)",
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
