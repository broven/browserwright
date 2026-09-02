"""Issue #86: a session whose tab dies must rebind, not fail forever.

The field shape: one navigation ends with the session's tab gone
(``context.pages == []``). From that moment EVERY later call in that session —
including ``page.goto("https://example.com")`` — failed in 1-4ms with
``TargetClosedError``, for the life of the session. Only a brand-new session
recovered.

Two independent defects produced "forever", and both are pinned here:

  1. the executor holds ``page`` for its whole life and never re-checked it, so
     a dead binding was never revisited (``_reconcile_page_binding`` only
     compared the LEDGER target, which still names the dead tab);
  2. the one path that DID self-heal — ``_is_target_closed_family`` in the
     raw-exception branch — was unreachable for navigation, because
     ``repl._smart_goto`` translates the underlying ``TargetClosedError`` into
     ``PageLoadFailed(reason="target-closed")``, a ``BrowserwrightError``,
     which ``_execute`` catches in an EARLIER branch.

The recovery itself is bounded: one attempt, routed through
``resolve_current_target`` (never ``context.new_page()``, which would open an
un-grouped tab the agent path cannot track), and a failed rebind answers with
the DISTINCT ``TabRebindFailed`` plus a terminal recycle rather than arming
another attempt.
"""
from __future__ import annotations

import pytest

from browserwright._executor import protocol
from browserwright._executor.process import _Worker, _is_target_closed_family
from browserwright.errors import PageLoadFailed, TabRebindFailed
from browserwright.repl.playwright_handle import page_is_dead


class _FakePage:
    def __init__(self, url: str = "about:blank", closed: bool = False):
        self.url = url
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed

    def aria_snapshot(self, *, mode):
        return "- root [ref=e1]"


class _FakeContext:
    def __init__(self, pages=None):
        self.pages = list(pages or [])

    def new_page(self):  # pragma: no cover - the assertion is that it is unused
        raise AssertionError(
            "rebind must go through resolve_current_target, never new_page(): "
            "an un-grouped tab is ledger drift (playwright_handle.py:250)")


class _FakeSession:
    def __init__(self, current_target_id: str | None = "ext-tab-A"):
        self.current_target_id = current_target_id
        self.session_record = {"id": "sess-86"}


def _worker(page: _FakePage) -> _Worker:
    w = _Worker("sess-86")
    w._connected = True
    w._context = _FakeContext([page])
    w._page = page
    w._page_target_id = "ext-tab-A"
    w._call_warnings = []
    return w


def _patch_bind(monkeypatch, result, *, counter: list | None = None):
    """Patch the bind free function the rebind path resolves through."""
    import browserwright.repl.playwright_handle as ph_mod
    import browserwright.repl.snapshot as snap_mod
    import browserwright.session as sess_mod

    monkeypatch.setattr(sess_mod, "current_session",
                        lambda: _FakeSession("ext-tab-B"))
    monkeypatch.setattr(snap_mod, "make_snapshot",
                        lambda holder, warn=None: "snap")

    def bind(context, sess):
        if counter is not None:
            counter.append((context, sess))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(ph_mod, "bind_current_page", bind)


# ---- the dead-page probe ---------------------------------------------------


def test_page_is_dead_reads_is_closed_and_nothing_else():
    assert page_is_dead(None) is True
    assert page_is_dead(_FakePage(closed=True)) is True
    assert page_is_dead(_FakePage(closed=False)) is False

    class _NoProbe:
        pass

    # Unrecognised objects are ALIVE: a false "dead" would abandon a working
    # tab and open a replacement in the user's browser.
    assert page_is_dead(_NoProbe()) is False

    class _Raises:
        def is_closed(self):
            raise RuntimeError("transport gone")

    assert page_is_dead(_Raises()) is True


# ---- defect 2: the goto translation hid the failure from the self-heal -----


def test_page_load_failed_target_closed_is_recognised_by_its_bucket():
    """`_smart_goto` renames the failure; the classifier must still know it.

    The rendered message is bounded to 300 chars and stripped of Playwright's
    call log, so matching on marker text alone is not a contract. The
    classifier's own `reason` bucket is."""
    exc = PageLoadFailed("https://example.com/", "target-closed", detail="")
    assert _is_target_closed_family(exc) is True
    # ...and an unrelated bucket must NOT be treated as a dead tab.
    assert _is_target_closed_family(
        PageLoadFailed("https://example.com/", "network", detail="")) is False


def test_goto_target_closed_rebinds_and_does_not_recycle(monkeypatch):
    """The issue's exact shape: `page.goto` fails target-closed.

    Before the fix this returned a plain error and left `page` bound to the
    dead tab, so the NEXT call failed identically — forever. Now the executor
    rebinds in place and says "retry", with no terminal recycle (the session's
    `state` survives)."""
    # The tab dies DURING the call, so the pre-call probe sees a live page and
    # the failure path is the only thing that can recover.
    w = _worker(_FakePage(closed=False))
    live = _FakePage("https://example.com/")
    calls: list = []
    _patch_bind(monkeypatch, live, counter=calls)

    code = (
        "from browserwright.errors import PageLoadFailed\n"
        "raise PageLoadFailed('https://example.com/', 'target-closed',\n"
        "                     detail='TargetClosedError: Page.goto')\n"
    )
    r = w._execute(protocol.ExecuteRequest(code, 1000))

    assert r.error is not None
    assert r.error["type"] == "PageLoadFailed"
    # Recovered in place: no executor recycle, `page` now points at a live tab.
    assert r.terminal_reason is None
    assert w._page is live
    assert "RETRY" in r.error["fix"]
    assert "session does not need to be recreated" in r.error["fix"]
    # A call spends at most ONE rebind, and this one spent it here.
    assert len(calls) == 1, f"rebind attempts: {len(calls)}"


def test_raw_target_closed_error_also_rebinds_in_place(monkeypatch):
    """The pre-existing raw-exception path keeps working — but now it recovers
    instead of only recycling."""
    w = _worker(_FakePage(closed=False))
    live = _FakePage("https://example.com/")
    _patch_bind(monkeypatch, live)

    code = (
        "class TargetClosedError(Exception):\n    pass\n"
        "raise TargetClosedError('Target page, context or browser has been closed')\n"
    )
    r = w._execute(protocol.ExecuteRequest(code, 1000))

    assert r.error is not None and r.terminal_reason is None
    assert w._page is live


# ---- defect 1: the binding was never revisited between calls ---------------


def test_pre_call_reconcile_rebinds_a_dead_page(monkeypatch):
    """A tab that died with nobody telling browserwright leaves the LEDGER
    still naming it, so the ledger comparison matches and used to return early.
    Liveness is what makes the binding recoverable."""
    dead = _FakePage(closed=True)
    w = _worker(dead)
    live = _FakePage("https://fresh.test/")
    calls: list = []
    _patch_bind(monkeypatch, live, counter=calls)

    r = w._execute(protocol.ExecuteRequest("print('after')", 1000))

    assert r.exit_code == 0 and "after" in r.console
    assert w._page is live, "dead page was not rebound before the call"
    assert w._live_page_holder.page is live, "views still observe the corpse"
    assert len(calls) == 1


def test_live_page_is_not_rebound(monkeypatch):
    """The probe must not churn a healthy tab."""
    live = _FakePage("https://example.com/")
    w = _worker(live)
    calls: list = []
    _patch_bind(monkeypatch, _FakePage("https://other.test/"), counter=calls)

    r = w._execute(protocol.ExecuteRequest("print('ok')", 1000))

    assert r.exit_code == 0
    assert w._page is live
    assert calls == [], "a healthy page must never be rebound"


# ---- the bound: one attempt, a distinct error, no loop ---------------------


def test_failed_rebind_is_terminal_and_distinct(monkeypatch):
    """A browser that cannot give us a tab must not look like a closed tab.

    It answers `TabRebindFailed` (a different error, a different next action)
    and escalates to the pre-existing cold-restart — never another rebind."""
    w = _worker(_FakePage(closed=True))
    calls: list = []
    _patch_bind(monkeypatch, RuntimeError("relay is gone"), counter=calls)

    r = w._execute(protocol.ExecuteRequest("print('never runs')", 1000))

    assert r.error is not None
    assert r.error["type"] == "TabRebindFailed"
    assert r.terminal_reason == protocol.TERMINAL_TARGET_CLOSED
    assert "will NOT help" in r.error["fix"]
    assert len(calls) == 1, f"rebind must be attempted once, got {len(calls)}"
    assert "never runs" not in (r.console or ""), (
        "user code must not run against an unbindable page")


def test_rebind_that_returns_a_dead_page_is_also_a_failure(monkeypatch):
    """The loop bound. Handing back a page that is dead on arrival would
    re-arm the condition on every call; it is reported as a rebind failure."""
    w = _worker(_FakePage(closed=True))
    calls: list = []
    _patch_bind(monkeypatch, _FakePage(closed=True), counter=calls)

    r = w._execute(protocol.ExecuteRequest("print('never runs')", 1000))

    assert r.error is not None and r.error["type"] == "TabRebindFailed"
    assert r.terminal_reason == protocol.TERMINAL_TARGET_CLOSED
    assert len(calls) == 1


def test_rebind_forgets_the_dead_binding_before_resolving(monkeypatch):
    """Measured in the real-Chrome repro, not reasoned about.

    ``resolve_current_target`` step 2 (``ensure_session_target``'s ledger fast
    path) trusts ``cdp.attach(tid)`` to fail for a closed tab. Over the
    extension backend it does NOT: recovery handed back the very
    ``ext-tab-<id>`` that had just been removed, and the bind then spent its
    whole 10s budget waiting for a Playwright page that could never appear. The
    binding this function is replacing has to be forgotten first."""
    from browserwright.repl.playwright_handle import rebind_dead_page
    import browserwright.repl.playwright_handle as ph_mod
    import browserwright.session_runtime as rt_mod

    sess = _FakeSession("ext-tab-DEAD")
    persisted: list = []
    monkeypatch.setattr(rt_mod, "persist_target",
                        lambda tid, sess=None: persisted.append(tid))

    seen_target_at_bind: list = []

    def bind(ctx, s):
        seen_target_at_bind.append(s.current_target_id)
        return _FakePage("https://fresh.test/")

    monkeypatch.setattr(ph_mod, "bind_current_page", bind)

    rebind_dead_page(_FakeContext([]), sess)

    assert seen_target_at_bind == [None], (
        "the dead target was still bound when recovery ran; it will be handed "
        f"straight back: {seen_target_at_bind}")
    assert persisted == [None], f"ledger binding not cleared: {persisted}"


def test_rebind_goes_through_resolve_current_target_not_new_page(monkeypatch):
    """`context.new_page()` is the wrong tool: it opens a tab outside this
    session's tab group, which the agent path cannot track (ledger drift → tab
    explosion). `_FakeContext.new_page` asserts if anything reaches for it."""
    from browserwright.repl.playwright_handle import rebind_dead_page

    context = _FakeContext([])
    sess = _FakeSession()
    seen: list = []
    import browserwright.repl.playwright_handle as ph_mod

    live = _FakePage("https://fresh.test/")
    monkeypatch.setattr(
        ph_mod, "bind_current_page",
        lambda ctx, s: (seen.append((ctx, s)), live)[1])

    assert rebind_dead_page(context, sess) is live
    assert seen == [(context, sess)]

    monkeypatch.setattr(
        ph_mod, "bind_current_page",
        lambda ctx, s: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(TabRebindFailed) as ei:
        rebind_dead_page(context, sess)
    assert "boom" in str(ei.value)


# ---- the in-process handle (the surface the issue names) -------------------


def test_playwright_handle_revisits_a_dead_binding(monkeypatch):
    """`PlaywrightHandle._ensure_connected` returned immediately once
    `_connected` was True, so `_page` was never re-resolved."""
    from browserwright.repl.playwright_handle import PlaywrightHandle
    import browserwright.repl.playwright_handle as ph_mod
    import browserwright.session as sess_mod

    monkeypatch.setattr(sess_mod, "current_session", lambda: _FakeSession())

    h = PlaywrightHandle()
    dead = _FakePage(closed=True)
    h._connected = True
    h._context = _FakeContext([])
    h._page = dead

    live = _FakePage("https://fresh.test/")
    calls: list = []
    monkeypatch.setattr(
        ph_mod, "bind_current_page",
        lambda ctx, s: (calls.append(ctx), live)[1])

    assert h.page is live
    assert len(calls) == 1
    # A second access must NOT rebind again: the condition is gone.
    assert h.page is live
    assert len(calls) == 1

    # And a rebind that fails surfaces the distinct error rather than a
    # TargetClosedError the caller cannot act on differently.
    h._page = _FakePage(closed=True)
    monkeypatch.setattr(
        ph_mod, "bind_current_page",
        lambda ctx, s: (_ for _ in ()).throw(RuntimeError("no tab")))
    with pytest.raises(TabRebindFailed):
        _ = h.page


def test_second_dead_tab_in_one_call_escalates_instead_of_rebinding(monkeypatch):
    """One rebind per call, spent once.

    Here the pre-call reconcile spends it healing a dead binding, and the code
    then dies target-closed anyway — the fresh tab did not survive either. That
    is not a tab worth rebinding again; it escalates to the pre-existing
    cold-restart, which is a different (and terminating) recovery."""
    w = _worker(_FakePage(closed=True))
    calls: list = []
    _patch_bind(monkeypatch, _FakePage("https://example.com/"), counter=calls)

    code = (
        "from browserwright.errors import PageLoadFailed\n"
        "raise PageLoadFailed('https://example.com/', 'target-closed')\n"
    )
    r = w._execute(protocol.ExecuteRequest(code, 1000))

    assert len(calls) == 1, f"rebind budget exceeded: {len(calls)}"
    assert r.terminal_reason == protocol.TERMINAL_TARGET_CLOSED
    assert "already rebound once" in r.error["fix"]
