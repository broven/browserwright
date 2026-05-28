"""Lazy Playwright ``page`` / ``context`` for the inline execution namespace.

The agent writes real Playwright Python in ``browserwright -s <id> -e <code>``
against an injected ``page`` (and ``context``). The handle:

  - **connects lazily**: nothing happens until the first attribute access on
    ``page`` / ``context``. A pure ``memory()`` / site-skill call never opens
    a browser connection (see :class:`_LazyHandle`).
  - **connects through the daemon facade**: it reads the facade ws URL the
    daemon advertised (``browserwright-daemon status``'s ``facade.ws`` →
    ``_ipc.read_facade_file``) and ``chromium.connect_over_cdp`` to it. The
    facade drives both the rdp and extension backends (see
    ``.trellis/spec/backend/playwright-cdp-facade.md``).
  - **binds ``page`` to the session's current tab**: it resolves the session's
    ``current_target_id`` (ledger fast-path via ``ensure_session_target``) and
    selects the Playwright ``Page`` whose CDP ``targetId`` matches it. If the
    session has no current tab it opens one (``about:blank``) and binds it —
    mirroring ``primitives/page.py:current_page()``'s "auto-open, NOT adopt"
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
(``mode_b_client`` over a unix socket) is plain sockets, not asyncio — so there
is no loop conflict with Playwright's sync driver.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from ..errors import BrowserwrightError


class FacadeUnavailable(BrowserwrightError):
    """The Playwright facade ws could not be discovered/connected.

    Carried fix: ensure the daemon is running (it auto-enables the facade); a
    daemon predating Phase C, or one started with ``--facade-port 0``, won't
    advertise one."""

    default_fix = ("ensure the daemon is running and the Playwright facade is "
                   "enabled (it is on by default; `browserwright-daemon "
                   "status --json` should show a non-null `facade.ws`). Do not "
                   "pass `--facade-port 0`.")


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
    if not session_id:
        return ws_url
    parts = urlsplit(ws_url)
    query = parts.query
    sep = "&" if query else ""
    query = f"{query}{sep}session={quote(session_id, safe='')}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _facade_ws_url(*, session_id: str | None = None) -> str:
    """Discover the running daemon's facade ws URL.

    Prefers an explicit ``BD_FACADE_WS`` override (tests / advanced setups),
    else reads the daemon's ``_ipc`` facade discovery file. Raises
    :class:`FacadeUnavailable` when nothing is found."""
    override = os.environ.get("BD_FACADE_WS")
    if session_id is None:
        session_id = _current_browserwright_session_id()
    if override:
        return _with_session_query(override, session_id)
    from ..daemon import _ipc
    ws, _port = _ipc.read_facade_file()
    if not ws:
        raise FacadeUnavailable(
            "no Playwright facade advertised by the daemon "
            "(facade discovery file absent)")
    return _with_session_query(ws, session_id)


def _agent_page_targets(sess: Any) -> list[dict]:
    """All page-type targets `{targetId, url}` of the session, via the AGENT
    CDP path (`sess.cdp` → daemon `Target.getTargets`).

    Why the agent path and not a Playwright CDP session? Two Playwright CDP
    sessions for target enumeration are FATAL over the extension facade:
    `context.new_cdp_session(page)` collides with the page's primary session,
    and even `browser.new_browser_cdp_session()` reuses the facade's single
    synthetic browser sessionId — both trip a Playwright-driver assert that
    kills the connection. The agent path is the daemon's own, fully-tested
    channel; its targetIds are exactly the daemon/ledger ids (extension
    `ext-tab-N`, rdp real ids), so they line up with the ledger's
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
            out.append({"targetId": tid, "url": ti.get("url", "")})
    return out


# ---- reusable connect + bind (shared by PlaywrightHandle and the executor) --
#
# Phase B: the persistent per-session executor (``browserwright._executor``)
# runs the SAME connect+bind dance, just ONCE at cold-start instead of per
# heredoc. These free functions are the single source of truth so the executor
# never re-implements (and never drifts from) the FATAL "no Playwright CDP
# session over the extension facade" constraint. ``PlaywrightHandle`` below is
# the per-heredoc Phase C consumer; the executor is the Phase B consumer.


def connect_over_cdp(pw: Any, *, attempts: int = 1,
                     backoff_s: float = 0.5) -> Any:
    """``chromium.connect_over_cdp`` to the daemon facade. Returns the Browser.

    Raises :class:`FacadeUnavailable` when the facade ws can't be discovered or
    the connect fails — the actionable error the agent should see.

    ``attempts`` / ``backoff_s`` (defense-in-depth for the Phase B executor
    cold-start, Failure #4): a freshly-restarted daemon launches the rdp Chrome
    lazily, so the executor can race a Chrome that is still binding its CDP port
    — the facade then 404s/403s for a brief window. Retrying the connect a few
    times over a few seconds absorbs that startup race. The per-heredoc Phase C
    consumer keeps ``attempts=1`` (the daemon is already warm there); only the
    executor cold-start passes a higher count. Discovery (`_facade_ws_url`) is
    re-read each attempt so a freshly-(re)written facade file is picked up."""
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
    return browser.contexts[0] if browser.contexts else browser.new_context()


def bind_current_page(context: Any, sess: Any) -> Any:
    """Bind the Playwright ``Page`` to the session's current tab.

    The tab itself is resolved/created via the AGENT primitive
    ``current_page()`` — NOT ``context.new_page()``. This is deliberate:

      - ``current_page()`` owns the reuse/recovery/auto-open discipline
        (reuse the ledger target, recover via the tab group, else open a
        fresh tab in THIS session's group, NOT adopt) and PERSISTS the
        chosen target to the ledger, so the next cold-start resolves the same
        tab — the cross-call reuse acceptance.
      - It also creates the tab inside the session's tab group (extension
        backend), keeping the agent ledger and the Playwright view on ONE
        tab. ``context.new_page()`` over the facade would open an un-grouped
        tab the agent path can't track → ledger drift → tab explosion.

    We then attach Playwright to that exact tab by matching the agent's
    targetId against ``context.pages`` (the facade replays ``attached`` events
    for every open tab, so a session-group tab IS enumerable). Mapping is done
    WITHOUT any Playwright CDP session — a per-page (``context.new_cdp_session``)
    or even a second browser-level (``new_browser_cdp_session``) session is
    FATAL over the extension facade (the facade reuses one synthetic sessionId
    → a Playwright-driver assert kills the connection). We correlate by URL via
    the AGENT path instead.
    """
    from ..primitives.page import current_page

    # Resolve/create + persist the session's current tab via the agent path.
    info = current_page()
    target_id = info.get("targetId") if isinstance(info, dict) else None

    if target_id:
        page = page_for_target(context, sess, target_id, info.get("url"))
        if page is not None:
            return page
        if _wait_for_session_announce(sess, timeout=2.0):
            page = page_for_target(context, sess, target_id, info.get("url"))
            if page is not None:
                return page

    # Could not correlate a Playwright Page to the agent tab (e.g. the facade
    # hasn't replayed it yet). Fall back to a Playwright-created page so the
    # agent still gets a usable handle; the agent ledger already points at the
    # current tab for the next cold-start.
    if context.pages:
        return context.pages[0]
    return context.new_page()


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

    Mapping uses NO Playwright CDP session (fatal over the extension facade —
    see ``_agent_page_targets``). Steady state — the session owns exactly one
    tab — binds that page directly (one tab per session is the whole point of
    the reuse discipline, and also resolves the ``about:blank`` ambiguity URLs
    can't). Otherwise correlate the target's URL (agent-path
    ``Target.getTargets``, or the caller's hint) to the matching page."""
    pages = list(context.pages)
    if not pages:
        return None
    if len(pages) == 1:
        return pages[0]
    url = hint_url
    if url is None:
        targets = _agent_page_targets(sess)
        url = next((t["url"] for t in targets
                    if t["targetId"] == target_id), None)
    if not url:
        return None
    matches = [p for p in pages if p.url == url]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Ambiguous (e.g. several `about:blank` tabs incl. a non-session one):
    # prefer the MOST-RECENTLY-announced match. The facade replays targets in
    # creation order, so the session's just-opened tab is announced last —
    # `context.pages` preserves that order. This disambiguates the fresh-blank
    # first bind; once the agent tab carries real content its url is unique and
    # the tie never arises.
    return matches[-1]


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

    # ---- lazy connect + bind --------------------------------------------

    def _ensure_connected(self) -> None:
        if self._connected:
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
        try:
            self._browser = connect_over_cdp(self._pw)
        except Exception as e:
            # Tear the driver back down so a failed connect doesn't leak it.
            with _suppress():
                self._pw_cm.__exit__(type(e), e, e.__traceback__)
            self._pw_cm = None
            self._pw = None
            raise
        self._context = context_for_browser(self._browser)
        self._page = bind_current_page(self._context, current_session())
        self._connected = True

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
