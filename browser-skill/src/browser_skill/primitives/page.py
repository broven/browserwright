"""Navigation / tab primitives."""
from __future__ import annotations

import time
from typing import Any

from ..errors import CDPError, PageLoadFailed
from ..session import current_session


_INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://",
             "chrome-extension://", "about:")


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
    return out


def current_tab() -> dict | None:
    """The tab Skill is currently attached to (may be stale / chrome:// page)."""
    sess = current_session()
    if not sess.current_target_id:
        return None
    for t in list_tabs():
        if t["targetId"] == sess.current_target_id:
            return t
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

    Backed by ``browser-daemon active-tab --json`` (Mode A v0.1). If that
    fails or returns ``accuracy != "heuristic-recent-activate"`` we surface
    ``accuracy`` in the return value so the agent can decide whether to ask
    the user.

    When ``BS_CDP_WS`` / ``BU_CDP_WS`` is set, the daemon CLI may be querying
    a *different* Chrome than the one we're attached to. Trust list_tabs over
    the daemon's hint in that case — the daemon's targetId would be invalid
    on our ws.
    """
    import os as _os
    sess = current_session()
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
