"""Agent-facing tab management: ``tabs()`` and ``switch_tab()``.

A session's browser can hold several tabs at once (extension backend: the
session's tab group; cdp backend: the session's browser instance). The
injected ``page`` is bound to exactly ONE of them — the session's *current*
tab (ledger ``runtime.current_target_id``). These two primitives give the
agent the rest of the surface:

  - ``tabs()`` — enumerate the session's tabs. On extension this is already
    group-scoped by the daemon (``scoped_target_infos``: "two sessions sharing
    one Chrome stay mutually invisible at enumeration"); on cdp the session's
    browser instance is the workspace, so its pages are the list.
  - ``switch_tab(url_or_page)`` — make another tab the session's current tab.
    The resident executor's target-changed hook rebinds the live ``page`` to
    it (same call and across calls), so the agent can keep several pages open
    and move between them without ``page.goto`` ping-pong.

Both go through the AGENT CDP path (``sess.cdp``) — never Playwright-created
CDP sessions (the fatal-over-the-facade constraint, see
``repl/playwright_handle``). ``switch_tab`` delegates to
``session_runtime.bind_target`` — the existing internal switch_tab — which
attaches, persists the ledger binding, and fires the target-changed hook; it
does NOT steal the user's focus (its best-effort ``Target.activateTarget`` is
silently a no-op on the extension backend, and on cdp the browser belongs to
the session anyway).
"""
from __future__ import annotations

from typing import Any

from .errors import BrowserwrightError, CDPError
from .session import current_session
from .session_runtime import bind_target


class TabMatchError(BrowserwrightError):
    """``switch_tab`` could not resolve a unique tab to switch to.

    The message lists the session's open tabs so the agent can recover in the
    next call (``tabs()`` shows the same list)."""

    default_fix = (
        "call tabs() to see the session's open tabs, then switch_tab() with a "
        "more specific URL substring (or a Page from context.pages() / "
        "context.new_page())"
    )


def _agent_target_infos(sess: Any) -> list[dict]:
    """The session's page targets, internal URLs (chrome://, about:) filtered.

    Reuses ``session_runtime.session_tabs`` (the single unified enumeration)
    so the surface and the ledger can never disagree on ids. Extension:
    already scoped to the session's tab group by the daemon; cdp: the
    session's browser instance."""
    from .session_runtime import session_tabs

    return session_tabs(sess, include_internal=False)


def tabs() -> list[dict]:
    """List the session's open tabs: ``[{targetId, url, title, current}]``.

    Only the session's own tabs are listed (extension: the session's tab
    group; cdp: the session's browser instance). ``current`` marks the tab
    the injected ``page`` is bound to."""
    sess = current_session()
    current = getattr(sess, "current_target_id", None)
    return [
        {
            "targetId": t["targetId"],
            "url": t.get("url", ""),
            "title": t.get("title", ""),
            "current": t["targetId"] == current,
        }
        for t in _agent_target_infos(sess)
    ]


def _is_page_object(obj: Any) -> bool:
    """Duck-type a Playwright ``Page`` (has a live URL and can evaluate)."""
    return hasattr(obj, "url") and callable(getattr(obj, "evaluate", None))


def _resolve_target_id(sess: Any, infos: list[dict], url_or_page: Any) -> str:
    """Resolve ``url_or_page`` to exactly one targetId, or raise."""
    if _is_page_object(url_or_page):
        return _match_page_object(sess, infos, url_or_page)
    if not isinstance(url_or_page, str):
        raise TabMatchError(
            "switch_tab expects a URL substring or a live Playwright Page "
            f"object, got {type(url_or_page).__name__}"
        )
    needle = url_or_page.strip().lower()
    if not needle:
        raise TabMatchError("switch_tab: empty match string")
    hits = [
        t for t in infos
        if needle in (t.get("url") or "").lower()
    ]
    if len(hits) == 1:
        return hits[0]["targetId"]
    urls = [t.get("url") or "<no url>" for t in infos]
    if not hits:
        raise TabMatchError(
            f"switch_tab: no open tab matches {url_or_page!r}",
            fix=f"open it first (context.new_page(url)) — open tabs: {urls}",
        )
    raise TabMatchError(
        f"switch_tab: {url_or_page!r} matches {len(hits)} tabs",
        fix=f"be more specific — candidates: "
            f"{[t.get('url') or '<no url>' for t in hits]}",
    )


def _match_page_object(sess: Any, infos: list[dict], page: Any) -> str:
    """Find which session target a Playwright Page is, via the same short-lived
    marker the binding glue uses (``repl/playwright_handle``). No guessing
    from URL — duplicate URLs are legal."""
    from .repl.playwright_handle import (
        _clear_target_marker,
        _install_target_marker,
        _page_has_target_marker,
    )

    for t in infos:
        marker_attempted, marker = _install_target_marker(sess, t["targetId"])
        if not marker_attempted or marker is None:
            continue
        key, value, cdp, session_id = marker
        try:
            if _page_has_target_marker(page, key, value):
                return t["targetId"]
        finally:
            _clear_target_marker(cdp, session_id, key)
    # A Page can legitimately sit on an internal URL (a fresh about:blank
    # tab) that the filtered display list hides — probe everything.
    from .session_runtime import session_tabs

    for t in session_tabs(sess, include_internal=True):
        marker_attempted, marker = _install_target_marker(sess, t["targetId"])
        if not marker_attempted or marker is None:
            continue
        key, value, cdp, session_id = marker
        try:
            if _page_has_target_marker(page, key, value):
                return t["targetId"]
        finally:
            _clear_target_marker(cdp, session_id, key)
    raise TabMatchError(
        "switch_tab: the given Page does not belong to this session's tabs",
        fix="pass a Page from context.pages() / context.new_page() "
            "of THIS session, or a URL substring",
    )


def switch_tab(url_or_page: Any) -> dict:
    """Make a tab the session's current tab; the injected ``page`` follows.

    Match by URL substring (case-insensitive, must be unique) or pass a live
    Playwright ``Page`` from ``context.pages()`` / ``context.new_page()``.
    After switching, ``page`` is bound to the new tab — in the same call and
    across calls — and the browser does NOT steal the user's focus. Returns
    ``{"targetId": ...}``.

    Example: keep a form open in one tab while checking docs in another::

        p2 = context.new_page()
        p2.goto("https://docs.example.com/api-keys")
        switch_tab("app.example.com/signup")      # back to the form
        page.locator("...").fill("...")
        switch_tab("docs.example.com/api-keys")   # re-read the docs
    """
    sess = current_session()
    infos = _agent_target_infos(sess)
    if not infos:
        raise TabMatchError(
            "switch_tab: the session has no open tabs to switch to",
            fix="open one first: context.new_page(url) or page.goto(url)",
        )
    target_id = _resolve_target_id(sess, infos, url_or_page)
    try:
        return bind_target(sess, target_id)
    except CDPError as e:
        raise TabMatchError(
            f"switch_tab: could not attach to {target_id!r}: "
            f"{getattr(e, 'cdp_message', e)}",
            fix="the tab may have closed — call tabs() to see what "
                "is still open, then retry",
        ) from e
