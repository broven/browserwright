"""Lazy Playwright ``page`` / ``context`` for the inline execution namespace.

The agent writes real Playwright Python in ``browserwright -s <id> -e <code>``
against an injected ``page`` (and ``context``). The handle:

  - **connects lazily**: nothing happens until the first attribute access on
    ``page`` / ``context``. A pure ``memory()`` / site-skill call never opens
    a browser connection (see :class:`_LazyHandle`).
  - **connects through the daemon's cdp surface**: it resolves the daemon
    endpoint (``browserwright.daemon_url``), confirms a daemon is there with the
    cheap ``/__ping__`` probe, and ``chromium.connect_over_cdp``s to
    ``<endpoint>/cdp``. That surface drives both the cdp and extension backends.
  - **binds ``page`` to the session's current tab**: it resolves the session's
    ``current_target_id`` (ledger fast-path via ``ensure_session_target``) and
    selects the Playwright ``Page`` whose CDP ``targetId`` matches it. If the
    session has no current tab it opens one (``about:blank``) and binds it —
    ``session_runtime.resolve_current_target``'s "auto-open, NOT adopt"
    rule. The bound target is persisted back to the ledger so the NEXT call
    resolves the SAME tab (cross-call tab reuse — the whole point of Phase
    C). ``context.new_page()`` is the explicit "new tab" escape hatch.

Lifecycle: the inline runner calls :meth:`PlaywrightHandle.close` in a
``finally`` so the Playwright connection is torn down cleanly at call end.
``connect_over_cdp``'s ``browser.close()`` only DISCONNECTS the CDP transport —
it does NOT close the user's real tabs/browser — so closing is safe. We never
call ``page.close()`` / ``context.close()``.

Sync API only: inline execution is a standalone process with no running asyncio
loop, so we use ``playwright.sync_api``. The session's daemon client
(``mode_b_client``) uses the synchronous websockets client, not asyncio — so
there is no loop conflict with Playwright's sync driver.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from ..errors import BrowserwrightError, PageBindTimeout, TabRebindFailed

logger = logging.getLogger(__name__)


def _page_bind_timeout_s() -> float:
    """How long to wait for Playwright to expose the agent-resolved tab.

    BUG B: this was a hardcoded 2.0s, which is shorter than an extension MV3
    service worker takes to wake up. When the SW is cold — or has just
    reconnected to the relay, which happens routinely between commands — the
    daemon's announce lands well after the budget, the bind fails, and the
    caller is told to retry a command whose only real problem was being asked
    too early. A wait measured in seconds costs nothing on the happy path (the
    announce normally lands in milliseconds and the loop returns immediately),
    while the old budget turned a slow wake-up into a hard failure.

    Override with ``BW_PAGE_BIND_TIMEOUT`` (seconds) for a very slow machine.
    """
    import os

    raw = (os.environ.get("BW_PAGE_BIND_TIMEOUT") or "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return _PAGE_BIND_TIMEOUT_S


#: The bind budget when nothing overrides it. Tests monkeypatch this name.
_PAGE_BIND_TIMEOUT_S = 10.0
_PAGE_BIND_POLL_INTERVAL_S = 0.05


class FacadeUnavailable(BrowserwrightError):
    """The daemon's cdp surface could not be reached.

    ADR-0011 collapsed this to one cause. The cdp surface used to be a separate
    listener that could be disabled or fail to bind while the daemon kept
    serving, so this error carried the daemon's own reason for its absence.
    There is one port now: if a daemon answers, the surface is there; if none
    answers, that is the whole story."""

    default_fix = ("ensure the daemon is running and reachable at the endpoint "
                   "(`browserwright-daemon status --json` should show `alive`; "
                   "set BW_DAEMON_URL if it lives on another machine).")


def _current_browserwright_session_id() -> str | None:
    """Best-effort Browserwright session id bound to the current process."""
    try:
        from ..session import current_session
        rec = getattr(current_session(), "session_record", None)
    except Exception:
        return None
    if isinstance(rec, dict) and rec.get("id"):
        return str(rec["id"])
    return None


def _with_session_query(ws_url: str, session_id: str | None) -> str:
    """Append the Browserwright session id to the facade URL query."""
    return _session_scoped_ws_url(ws_url, session_id)


def _facade_ws_url(*, session_id: str | None = None) -> str:
    """The cdp-surface ws URL for this session.

    Derived from the resolved endpoint rather than asked for: with one port
    there is nothing to discover. We still ping, because "the URL is well-formed"
    and "a daemon is listening on it" are different facts and only the second
    one produces a usable connection — reporting the difference here is what
    keeps the failure legible instead of surfacing as a Playwright timeout.
    """
    from ..daemon import _ipc
    from ..daemon_url import daemon_endpoint, unreachable_message

    if session_id is None:
        session_id = _current_browserwright_session_id()
    ep = daemon_endpoint()
    pong = _ipc.ping_status_sync()
    if pong.pid is None:
        if ep.explicit:
            raise FacadeUnavailable(unreachable_message(ep))
        raise FacadeUnavailable(
            f"no daemon is answering at {ep.url}, so there is no browser to "
            "connect to (start it with `browserwright-daemon start`)")
    return _with_session_query(ep.ws("/cdp"), session_id)


def _session_scoped_ws_url(ws_url: str, session_id: str | None) -> str:
    """Attach the browserwright session id to a facade ws URL."""
    if not session_id:
        return ws_url
    parts = urlsplit(ws_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["session"] = session_id
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urlencode(query), parts.fragment,
    ))


def _session_id_from(sess: Any) -> str | None:
    rec = getattr(sess, "session_record", None)
    sid = rec.get("id") if isinstance(rec, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _agent_page_targets(sess: Any) -> list[dict]:
    """All page-type targets `{targetId, url, title}` of the session, via the
    AGENT CDP path (`sess.cdp` → daemon `Target.getTargets`).

    Extension: the daemon already scopes the enumeration to the session's tab
    group (`scoped_target_infos` — two sessions sharing one Chrome stay
    mutually invisible). cdp: the session's browser instance. This is also
    what the agent-facing ``tabs()`` / ``switch_tab()`` primitives
    (``tab_surface``) use, so the ids always line up with the ledger's
    ``current_target_id``.

    Why the agent path and not a Playwright CDP session? Two Playwright CDP
    sessions for target enumeration are FATAL over the extension facade:
    `context.new_cdp_session(page)` collides with the page's primary session,
    and even `browser.new_browser_cdp_session()` reuses the facade's single
    synthetic browser sessionId — both trip a Playwright-driver assert that
    kills the connection. The agent path is the daemon's own, fully-tested
    channel; its targetIds are exactly the daemon/ledger ids (extension
    `ext-tab-N`, cdp real ids), so they line up with the ledger's
    `current_target_id` and with `connect_over_cdp`'s synthesized targetIds."""
    try:
        res = sess.cdp.send("Target.getTargets")
    except Exception:
        return []
    out: list[dict] = []
    for ti in (res.get("targetInfos") or []):
        if ti.get("type") != "page":
            continue
        tid = ti.get("targetId")
        if isinstance(tid, str):
            out.append({
                "targetId": tid,
                "url": ti.get("url", ""),
                "title": ti.get("title", ""),
            })
    return out


# ---- reusable connect + bind (shared by PlaywrightHandle and the executor) --
#
# Phase B: the persistent per-session executor (``browserwright._executor``)
# runs the SAME connect+bind dance, just ONCE at cold-start instead of per
# heredoc. These free functions are the single source of truth so the executor
# never re-implements (and never drifts from) the FATAL "no Playwright CDP
# session over the extension facade" constraint. ``PlaywrightHandle`` below is
# the per-heredoc Phase C consumer; the executor is the Phase B consumer.


def connect_over_cdp(pw: Any, *, session_id: str | None = None,
                     attempts: int = 1,
                     backoff_s: float = 0.5) -> Any:
    """``chromium.connect_over_cdp`` to the daemon facade. Returns the Browser.

    Raises :class:`FacadeUnavailable` when the facade ws can't be discovered or
    the connect fails — the actionable error the agent should see.

    ``attempts`` / ``backoff_s`` (defense-in-depth for the Phase B executor
    cold-start, Failure #4): a freshly-restarted daemon launches the cdp Chrome
    lazily, so the executor can race a Chrome that is still binding its CDP port
    — the facade then 404s/403s for a brief window. Retrying the connect a few
    times over a few seconds absorbs that startup race. The per-heredoc Phase C
    consumer keeps ``attempts=1`` (the daemon is already warm there); only the
    executor cold-start passes a higher count. Discovery (`_facade_ws_url`) is
    re-asked of the daemon each attempt, so a facade that binds a moment after
    the listener does is picked up rather than cached as absent."""
    attempts = max(1, attempts)
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            ws_url = _facade_ws_url()
        except FacadeUnavailable as e:
            last_exc = e
            ws_url = None
        if ws_url is not None:
            try:
                ws_url = _session_scoped_ws_url(ws_url, session_id)
                return pw.chromium.connect_over_cdp(ws_url, timeout=20000)
            except Exception as e:  # noqa: BLE001
                last_exc = FacadeUnavailable(
                    f"connect_over_cdp({ws_url!r}) failed: {e}")
        if i < attempts - 1:
            import time as _time
            _time.sleep(backoff_s)
    if isinstance(last_exc, FacadeUnavailable):
        raise last_exc
    raise FacadeUnavailable(
        "connect_over_cdp failed: facade unavailable after "
        f"{attempts} attempt(s)")


def context_for_browser(browser: Any) -> Any:
    """The first existing BrowserContext, or a fresh one."""
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    from ._smart_goto import patch_context_pages
    patch_context_pages(context)
    return context


def bind_current_page(context: Any, sess: Any) -> Any:
    """Bind the Playwright ``Page`` to the session's current tab.

    The tab itself is resolved/created via the AGENT path
    (``session_runtime.resolve_current_target``) — NOT ``context.new_page()``.
    This is deliberate:

      - ``resolve_current_target()`` owns the reuse/recovery/auto-open
        discipline (reuse the ledger target, recover via the tab group, else
        open a fresh tab in THIS session's group, NOT adopt) and PERSISTS the
        chosen target to the ledger, so the next cold-start resolves the same
        tab — the cross-call reuse acceptance.
      - It also creates the tab inside the session's tab group (extension
        backend), keeping the agent ledger and the Playwright view on ONE
        tab. ``context.new_page()`` over the facade would open an un-grouped
        tab the agent path can't track → ledger drift → tab explosion.

    We then attach Playwright to that exact tab by writing a short-lived random
    marker through the already-attached AGENT target session and accepting only
    the ``context.pages`` entry that reads it. The facade replays ``attached``
    events for every open tab, so a session-group tab is enumerable once
    materialized. Mapping creates NO Playwright CDP session — a per-page
    (``context.new_cdp_session``) or even a second browser-level
    (``new_browser_cdp_session``) session is fatal over the extension facade
    because it reuses one synthetic sessionId and trips a Playwright-driver
    assertion.
    """
    from ..session_runtime import resolve_current_target
    from ._smart_goto import patch_context_pages, patch_page_goto

    patch_context_pages(context)

    # Resolve/create + persist the session's current tab via the agent path.
    info = resolve_current_target(sess)
    target_id = info.get("targetId") if isinstance(info, dict) else None
    # Did THIS call open the tab? Only step 4 of `resolve_current_target` sets
    # it, so a reused tab is never mistaken for one we may discard.
    we_opened_it = bool(info.get("opened")) if isinstance(info, dict) else False

    budget = _page_bind_timeout_s()
    if target_id:
        page = _wait_for_target_page(
            context,
            sess,
            target_id,
            info.get("url"),
            timeout=budget,
        )
        if page is not None:
            return patch_page_goto(page)

    # The agent path has already resolved (and, for an empty workspace,
    # created) the session's one authoritative target. Never manufacture a
    # second target merely because the facade has not exposed the first one to
    # Playwright yet: that splits the ledger and Playwright views and is the
    # source of duplicate user-visible tabs.
    #
    # BUG B: a tab THIS call opened and then failed to bind has no owner —
    # `PageBindTimeout` is advertised as retryable, so the caller retries,
    # `resolve_current_target` finds no usable tab again and opens another.
    # Whether the previous one survives depends on `session end` later finding
    # it in the session's tab group, which is precisely what is unreliable when
    # the announce failed. No user-visible leak has been observed; this closes
    # the window rather than fixing a confirmed one. Roll our own creation back
    # before raising.
    if we_opened_it and target_id:
        _discard_unbindable_tab(sess, target_id)
    raise PageBindTimeout(
        target_id=target_id or "",
        timeout=budget,
    )


def page_is_dead(page: Any) -> bool:
    """Is this bound ``Page`` a handle to a tab that no longer exists?

    Issue #86. The bind used to be a one-shot: once a handle had a ``page``, it
    never asked again, so a tab that died mid-session left every later call
    failing in 1-4ms with ``TargetClosedError`` — forever, for the life of that
    session. Detecting the dead handle is what lets the (already correct)
    ``resolve_current_target`` recovery path be re-entered.

    The probe is ``page.is_closed()`` — the flag Playwright sets when it
    processes the target's destruction. It is LOCAL (no CDP round-trip), so it
    is cheap enough to run before every call, and it is authoritative: it does
    not fire for a page that is merely slow, busy, or in another context.

    It is deliberately the ONLY probe. Inspecting ``context.pages`` instead
    (empty, or missing this page) reads on the same condition — the field repro
    left ``context.pages == []`` behind — but both spellings have false
    positives a bare flag does not: a page can legitimately belong to a second
    context, and a context can legitimately be observed before its pages
    materialize. A false "dead" is expensive (it opens a replacement tab in the
    user's browser and abandons a working one); a false "alive" costs nothing,
    because the call then fails with the real ``TargetClosedError`` and the
    reactive path rebinds from there. So anything unrecognised — a unit-test
    double without the method, a probe that raises differently — is reported
    ALIVE, and the reactive path is the backstop.
    """
    if page is None:
        return True
    is_closed = getattr(page, "is_closed", None)
    if not callable(is_closed):
        return False
    try:
        return bool(is_closed())
    except Exception:  # noqa: BLE001 - a probe that raises means gone
        return True


def drain_page_events(context: Any, *, timeout: float = 0.001) -> None:
    """Let Playwright process target events that queued while we were idle.

    Issue #86's second half. ``page.is_closed()`` is fed by a channel event,
    and the sync API only dispatches channel events while a sync call is in
    flight — between two ``browserwright -s <id> -e ...`` invocations the
    resident executor is parked in ``queue.get`` and nothing pumps. So a tab
    that died while the session was idle leaves ``is_closed()`` reading False,
    the pre-call probe sees a healthy page, and the caller eats one
    ``TargetClosedError`` before the reactive path can rebind.

    Draining first turns that lost call into a clean one. The budget is
    deliberately ~1ms: this runs before EVERY executed call, and the queued
    events are already sitting in the driver — the wait is only there to yield
    to the dispatcher, not to wait for anything to arrive. Best-effort by
    construction (``_pump_page_events`` swallows the timeout it is guaranteed
    to hit on a quiet session).
    """
    _pump_page_events(context, timeout=timeout)


def rebind_dead_page(context: Any, sess: Any) -> Any:
    """Re-enter the bind discipline for a session whose tab died. ONE attempt.

    Goes through :func:`bind_current_page` — i.e. ``resolve_current_target`` —
    and NOT ``context.new_page()``, for exactly the reason the first bind does:
    ``new_page`` opens a tab outside the session's tab group, which the agent
    path cannot track (ledger drift → tab explosion). See
    :func:`bind_current_page`'s docstring.

    Raises :class:`~browserwright.errors.TabRebindFailed` when the rebind
    itself fails, or when it hands back a page that is *also* already dead.
    That second check is the loop bound: a browser that cannot hold a tab open
    reports a distinct, terminal error instead of inviting another rebind.

    The dead binding is DROPPED first — in memory and in the ledger — because
    ``resolve_current_target`` would otherwise hand it straight back. Its step 2
    (``ensure_session_target``'s ledger fast path) trusts ``cdp.attach(tid)`` to
    fail for a closed tab; over the extension backend it does not, so recovery
    returned the very target we are here to replace and the bind then timed out
    against a tab that no longer exists. Measured, not reasoned: the e2e repro
    resolved the same ``ext-tab-<id>`` that had just been removed. We know this
    target is gone — that is the precondition of this function — so forgetting
    it is not a guess. Everything below step 2 then does the right thing:
    another live tab of the session if there is one, else a fresh tab opened in
    the session's own group.
    """
    _forget_dead_binding(sess)
    try:
        page = bind_current_page(context, sess)
    except Exception as e:  # noqa: BLE001 - re-raised as the distinct error
        raise TabRebindFailed(f"{type(e).__name__}: {e}") from e
    if page_is_dead(page):
        raise TabRebindFailed(
            "the replacement tab was already gone when it was bound")
    return page


def _forget_dead_binding(sess: Any) -> None:
    """Drop the session's current-tab binding, in memory and in the ledger.

    Best-effort by design: this runs on a recovery path, and a ledger write
    that fails must not replace the rebind's own outcome with a bookkeeping
    error. The worst case of a failed clear is the pre-existing behaviour —
    recovery re-proposes the dead target and the bind times out.
    """
    try:
        from ..session_runtime import persist_target

        sess.current_target_id = None
        persist_target(None, sess=sess)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("could not clear the dead tab binding before rebinding",
                       exc_info=True)


def _discard_unbindable_tab(sess: Any, target_id: str) -> None:
    """Close a tab we opened but could not bind, and clear its ledger binding.

    Strictly best-effort: the bind already failed, and a failure to clean up
    must not replace ``PageBindTimeout`` (which names the real problem) with a
    teardown error. Worst case we are back to the old leak.

    Clearing the durable binding matters as much as closing the tab: leaving
    the ledger pointed at a tab that is gone makes the NEXT call take the
    recovery path against a dead target instead of opening a clean one.
    """
    from ..session_runtime import close_session_tab, persist_target

    try:
        close_session_tab(sess, target_id=target_id)
    except Exception:  # noqa: BLE001 - see docstring: never mask the bind error
        logger.warning("could not close unbindable tab %s", target_id,
                       exc_info=True)
    try:
        if getattr(sess, "current_target_id", None) == target_id:
            sess.current_target_id = None
        persist_target(None, sess=sess)
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("could not clear binding for unbindable tab %s",
                       target_id, exc_info=True)


def _wait_for_target_page(
    context: Any,
    sess: Any,
    target_id: str,
    hint_url: str | None,
    *,
    timeout: float,
) -> Any | None:
    """Wait for Playwright to expose the agent-resolved target.

    The daemon announce is the efficient extension-backend wake-up. Polling
    ``context.pages`` after it returns covers the short interval between the
    facade emitting the target event and Playwright materializing its ``Page``.
    This function only correlates existing pages; it never creates a target.
    """
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    page = page_for_target(context, sess, target_id, hint_url)
    if page is not None:
        return page

    remaining = deadline - time.monotonic()
    if remaining > 0:
        _wait_for_session_announce(sess, timeout=remaining)

    while True:
        page = page_for_target(context, sess, target_id, hint_url)
        if page is not None:
            return page
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Pump the sync event loop: ``context.pages`` is a Python-side cache
        # fed by the "page" channel event, which playwright's sync API only
        # delivers while a sync call is in flight (issue #30). Without the
        # pump, an announce that lands mid-wait leaves the event queued, the
        # cache empty, and this loop polling nothing until PageBindTimeout.
        _pump_page_events(context, timeout=remaining)
        time.sleep(min(_PAGE_BIND_POLL_INTERVAL_S, remaining))


def _pump_page_events(context: Any, *, timeout: float) -> None:
    """Pump the sync API's event queue so ``context.pages`` sees new pages.

    ``context.pages`` is populated by the "page" channel event, and the
    playwright sync API only processes channel events while a sync call is in
    flight — between calls they queue on the driver loop. A bind wait that
    only polls ``context.pages`` therefore never sees the page the facade
    announced: the driver completes CRPage init and dispatches the event, but
    nothing pumps it, so the cache stays empty no matter how long the wait.

    ``expect_page`` is the idiomatic pump: arming it registers an event
    future, and reading its ``value`` switches the dispatcher fiber until the
    event (or the budget) lands — draining every queued channel event on the
    way. Unit-test doubles without ``expect_page`` fall back to plain polling
    (the exception is swallowed; the caller re-checks ``context.pages`` and
    retries).
    """
    try:
        with context.expect_page(timeout=max(1.0, timeout * 1000)) as info:
            pass
        info.value
    except Exception:  # noqa: BLE001 - see docstring: pump is best-effort
        pass


def _wait_for_session_announce(sess: Any, *, timeout: float) -> bool:
    """Wait for the daemon facade to announce the agent-created tab.

    This is a daemon RPC because the Playwright binding code runs in the
    skill/executor process, while the announce event is produced inside the
    daemon's extension facade bridge.
    """
    try:
        rec = getattr(sess, "session_record", None)
        sid = rec.get("id") if isinstance(rec, dict) else None
        if not sid:
            return False
        res = sess.cdp.send(
            "BrowserwrightDaemon.waitForSessionAnnounce",
            bsSession=sid,
            timeout=timeout,
        )
        return bool(res.get("announced"))
    except Exception:
        return False


def page_for_target(context: Any, sess: Any, target_id: str,
                     hint_url: str | None = None) -> Any | None:
    """Find the live Playwright Page for the session's ``target_id``.

    Mapping uses NO *Playwright-created* CDP session (fatal over the extension
    facade — see ``_agent_page_targets``). Instead, the already-attached agent
    path writes a short-lived random marker into the exact target's main world;
    only the Playwright Page that can read that marker is accepted. This avoids
    guessing from page count or URL, both of which are ambiguous for launcher
    tabs, duplicate URLs, and several ``about:blank`` pages.

    Lightweight unit-test doubles without an agent CDP surface retain a strict
    unique-URL fallback. A real Session whose marker command fails returns no
    match and lets the outer wait retry; it never degrades to a guess."""
    pages = list(context.pages)
    if not pages:
        return None

    marker_attempted, marker = _install_target_marker(sess, target_id)
    if marker_attempted:
        if marker is None:
            return None
        key, value, cdp, session_id = marker
        try:
            matches = [
                page for page in pages
                if _page_has_target_marker(page, key, value)
            ]
            return matches[0] if len(matches) == 1 else None
        finally:
            _clear_target_marker(cdp, session_id, key)

    # Test-double compatibility only: without an agent CDP path, require a
    # unique URL match. Never use the old singleton/last-match heuristics.
    url = hint_url
    if url is None:
        targets = _agent_page_targets(sess)
        url = next((t["url"] for t in targets
                    if t["targetId"] == target_id), None)
    if not url:
        return None
    matches = [p for p in pages if p.url == url]
    return matches[0] if len(matches) == 1 else None


def _install_target_marker(
    sess: Any,
    target_id: str,
) -> tuple[bool, tuple[str, str, Any, str] | None]:
    """Mark ``target_id`` through the existing agent CDP attachment.

    Returns ``(False, None)`` only for lightweight objects with no ``cdp``
    attribute (unit-test doubles). For a real Session, ``True`` means exact
    matching was attempted; a failed attach/evaluate returns ``(True, None)``
    so callers retry rather than fall back to an inexact URL guess.
    """
    try:
        cdp = sess.cdp
    except AttributeError:
        return False, None
    except Exception:
        return True, None

    key = f"__browserwright_bind_{uuid4().hex}"
    value = uuid4().hex
    try:
        session_id = cdp.attach(target_id)
        import json

        expression = (
            "(() => {"
            f"Object.defineProperty(globalThis, {json.dumps(key)}, "
            f"{{value: {json.dumps(value)}, configurable: true}});"
            "return true;"
            "})()"
        )
        result = cdp.send(
            "Runtime.evaluate",
            session=session_id,
            expression=expression,
            returnByValue=True,
        )
        if result.get("exceptionDetails"):
            return True, None
    except Exception:
        return True, None
    return True, (key, value, cdp, session_id)


def _page_has_target_marker(page: Any, key: str, value: str) -> bool:
    try:
        return bool(page.evaluate(
            "([key, value]) => globalThis[key] === value",
            [key, value],
        ))
    except Exception:
        return False


def _clear_target_marker(
    cdp: Any,
    session_id: str,
    key: str,
) -> None:
    """Best-effort cleanup of the temporary page-global marker."""
    try:
        import json

        cdp.send(
            "Runtime.evaluate",
            session=session_id,
            expression=f"delete globalThis[{json.dumps(key)}]",
            returnByValue=True,
        )
    except Exception:
        pass


class PlaywrightHandle:
    """Owns the lazy Playwright connection + the bound ``page`` / ``context``.

    Construct one per heredoc; access ``.page`` / ``.context`` to trigger the
    lazy connect+bind; call ``.close()`` in a ``finally`` to tear down.
    """

    def __init__(self) -> None:
        self._pw: Any = None          # sync_playwright() context manager
        self._pw_cm: Any = None       # the entered manager (for __exit__)
        self._browser: Any = None     # connect_over_cdp Browser
        self._context: Any = None     # bound BrowserContext
        self._page: Any = None        # bound Page
        self._connected = False
        # Issue #86 re-entrancy guard: `rebind_dead_page` resolves the tab
        # through the agent path, which can touch `page`/`context` again.
        self._rebinding = False

    # ---- lazy connect + bind --------------------------------------------

    def _ensure_connected(self) -> None:
        if self._connected:
            self._rebind_if_dead()
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - dep is a hard requirement
            raise FacadeUnavailable(
                "playwright is not importable; it is a runtime dependency of "
                "the skill heredoc `page`/`context` surface") from e

        # Enter sync_playwright() and connect_over_cdp. Keep the context manager
        # so close() can __exit__ it (stops the bundled driver process). The
        # connect + bind logic is the shared free functions (also used by the
        # Phase B executor), so the FATAL "no Playwright CDP session over the
        # extension facade" constraint lives in exactly one place.
        from ..session import current_session

        self._pw_cm = sync_playwright()
        self._pw = self._pw_cm.__enter__()
        sess = current_session()
        try:
            self._browser = connect_over_cdp(
                self._pw, session_id=_session_id_from(sess))
        except Exception as e:
            # Tear the driver back down so a failed connect doesn't leak it.
            with _suppress():
                self._pw_cm.__exit__(type(e), e, e.__traceback__)
            self._pw_cm = None
            self._pw = None
            raise
        self._context = context_for_browser(self._browser)
        self._page = bind_current_page(self._context, sess)
        self._connected = True

    def _rebind_if_dead(self) -> None:
        """Issue #86: revisit the bind when the bound tab has died.

        ``_connected`` used to be set once and never cleared, so ``_page`` was
        never re-resolved: a tab that went away left this handle answering
        every call with ``TargetClosedError`` in 1-4ms for the rest of its
        life. The recovery already existed one layer down
        (``resolve_current_target`` reuses / recovers via the tab group / opens
        a fresh tab in THIS session's group) — it was simply never re-entered.

        Bounded by construction rather than by a counter: a rebind only runs
        while the current page reads as dead, and `rebind_dead_page` refuses to
        hand back a page that is dead on arrival. So a successful rebind ends
        the condition, and an unsuccessful one raises ``TabRebindFailed``
        instead of arming another attempt.
        """
        if self._rebinding or not page_is_dead(self._page):
            return
        from ..session import current_session

        self._rebinding = True
        try:
            self._page = rebind_dead_page(self._context, current_session())
        finally:
            self._rebinding = False

    # ---- accessors (trigger the lazy connect) ---------------------------

    @property
    def page(self) -> Any:
        self._ensure_connected()
        return self._page

    @property
    def context(self) -> Any:
        self._ensure_connected()
        return self._context

    # ---- teardown -------------------------------------------------------

    def close(self) -> None:
        """Disconnect the Playwright connection WITHOUT closing the user's real
        tabs/browser.

        We deliberately do NOT call ``browser.close()`` /
        ``context.close()`` / ``page.close()``: over the daemon facade (esp. the
        extension backend, where teardown CDP frames aren't all answered)
        ``browser.close()`` round-trips a ``Browser.close`` that can hang the
        Playwright driver — and ``context``/``page`` close WOULD close the
        user's real tabs. Instead we just ``__exit__`` the ``sync_playwright()``
        manager, which stops the bundled driver subprocess and severs the CDP
        transport (a pure disconnect — the user's tabs stay open). Idempotent +
        fully suppressed: teardown of a partly-connected handle (e.g. connect
        failed) must never raise."""
        if not self._connected and self._pw_cm is None:
            return
        self._browser = None
        self._context = None
        self._page = None
        if self._pw_cm is not None:
            with _suppress():
                # Stops the driver process → disconnects the CDP transport.
                # Does NOT close the user's tabs/browser.
                self._pw_cm.__exit__(None, None, None)
        self._pw_cm = None
        self._pw = None
        self._connected = False


class _suppress:
    """Tiny ``contextlib.suppress(Exception)`` clone kept local so teardown has
    no import surface to fail on."""

    def __enter__(self) -> "_suppress":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


class _LazyHandleProxy:
    """A transparent proxy that defers ALL access to the underlying live object
    (``handle.page`` / ``handle.context``) until first use.

    This is what lands in the heredoc namespace as ``page`` / ``context``: a
    pure ``memory()`` heredoc that never touches them never triggers the
    connect. Any attribute access / call / item access / iteration forwards to
    the real object, which lazily connects on first resolution."""

    __slots__ = ("_handle", "_attr")

    def __init__(self, handle: PlaywrightHandle, attr: str) -> None:
        object.__setattr__(self, "_handle", handle)
        object.__setattr__(self, "_attr", attr)

    def _resolve(self) -> Any:
        return getattr(object.__getattribute__(self, "_handle"),
                       object.__getattribute__(self, "_attr"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._resolve(), name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._resolve()(*args, **kwargs)

    def __getitem__(self, key: Any) -> Any:
        return self._resolve()[key]

    def __iter__(self) -> Any:
        return iter(self._resolve())

    def __repr__(self) -> str:
        return f"<lazy {object.__getattribute__(self, '_attr')} (Playwright, " \
               f"connects on first use)>"
