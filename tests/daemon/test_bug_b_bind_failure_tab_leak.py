"""Regression: a failed Playwright bind must clean up the tab it just opened.

BUG B. `bind_current_page` resolves the session's working tab through the AGENT
path (`resolve_current_target`), which OPENS a fresh tab when the session has
none. If the subsequent Playwright bind then times out, the old code raised
`PageBindTimeout` and walked away without touching that tab.

NOTE ON SCOPE — no user-visible tab leak was ever observed. The climbing
synthetic ids in the original report (32700140 → 144 → 148 → …) only show the
allocator advancing; the user checked their Chrome and found no stray tabs.
What IS established is the code path: the failure path had no cleanup, and
`PageBindTimeout` advertises `retryable: true`, so each retry opens another tab
whose survival then depends on `session end` finding it in the session's tab
group — the very thing that is unreliable when the announce failed.

These tests assert that code path at the `close_session_tab` call. They pin the
rollback, and pin that it applies ONLY to a tab this bind attempt created —
closing a REUSED tab on a failed bind would destroy the agent's working tab.
They do not, and cannot, prove anything about tabs in a real browser.
"""
from __future__ import annotations

import pytest

import browserwright.repl.playwright_handle as ph
import browserwright.session_runtime as sr
from browserwright.errors import PageBindTimeout


class _FakeCDP:
    def __init__(self):
        self._sessions: dict[str, str] = {}

    def send(self, *_a, **_k):  # announce RPC / anything else
        raise RuntimeError("no daemon in this unit test")


class _FakeSession:
    def __init__(self):
        self.cdp = _FakeCDP()
        self.current_target_id = None
        self.session_record = {"id": "1172", "backend": "extension"}


class _FakeContext:
    """A context that never materializes a Page — the bind always times out."""
    pages: list = []

    def new_page(self):  # only so `_smart_goto.patch_context_pages` can wrap it
        raise AssertionError(
            "bind_current_page must never manufacture a replacement page")


@pytest.fixture
def never_binds(monkeypatch):
    """Wire the bind so it can only ever time out, fast."""
    monkeypatch.setattr(ph, "_PAGE_BIND_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ph, "page_for_target", lambda *a, **k: None)
    monkeypatch.setattr(ph, "_wait_for_session_announce",
                        lambda *a, **k: False)
    monkeypatch.setattr(ph, "_pump_page_events", lambda *a, **k: None)


@pytest.fixture
def spies(monkeypatch):
    """Record what the failure path does to the tab and the ledger."""
    closed: list[str] = []
    persisted: list = []

    def _close(sess, *, target_id=None, session_id=None):
        closed.append(target_id)
        return {"ok": True, "tabId": 1}

    monkeypatch.setattr(sr, "close_session_tab", _close)
    monkeypatch.setattr(sr, "persist_target",
                        lambda tid, **kw: persisted.append(tid))
    return closed, persisted


def _resolves_to(monkeypatch, info: dict):
    monkeypatch.setattr(sr, "resolve_current_target", lambda _sess: info)


def test_a_tab_opened_by_this_bind_is_closed_when_the_bind_times_out(
        monkeypatch, never_binds, spies):
    """The leak itself."""
    closed, persisted = spies
    _resolves_to(monkeypatch, {"targetId": "ext-tab-32700148",
                               "accuracy": "unknown", "opened": True})

    with pytest.raises(PageBindTimeout) as exc:
        ph.bind_current_page(_FakeContext(), _FakeSession())

    assert exc.value.target_id == "ext-tab-32700148"
    assert closed == ["ext-tab-32700148"], (
        "the tab this bind opened was left behind in the user's Chrome")
    # ...and the durable binding must not keep pointing at the closed tab, or
    # the next call recovers a dead target instead of opening a clean one.
    assert persisted == [None]


def test_a_reused_tab_is_never_closed_by_a_failed_bind(
        monkeypatch, never_binds, spies):
    """The tab was already the session's. Closing it would be data loss."""
    closed, _persisted = spies
    _resolves_to(monkeypatch, {"targetId": "ext-tab-32700100",
                               "accuracy": "exact"})

    with pytest.raises(PageBindTimeout):
        ph.bind_current_page(_FakeContext(), _FakeSession())

    assert closed == [], "a failed bind destroyed the agent's working tab"


def test_repeated_failures_do_not_accumulate_tabs(
        monkeypatch, never_binds, spies):
    """`retryable: true` means the agent WILL retry. Seven retries, seven closes.

    Mirrors the id shape from the original report (climbing by 4) purely to
    keep the case recognisable — the report's *conclusion* that this meant
    seven leaked tabs was an inference, and was not borne out.
    """
    closed, _persisted = spies
    opened: list[str] = []

    def _resolve(_sess):
        tid = f"ext-tab-{32700140 + 4 * len(opened)}"
        opened.append(tid)
        return {"targetId": tid, "accuracy": "unknown", "opened": True}

    monkeypatch.setattr(sr, "resolve_current_target", _resolve)

    for _ in range(7):
        with pytest.raises(PageBindTimeout):
            ph.bind_current_page(_FakeContext(), _FakeSession())

    assert len(opened) == 7
    assert closed == opened, f"not rolled back: {set(opened) - set(closed)}"


def test_cleanup_failure_does_not_mask_the_bind_error(
        monkeypatch, never_binds):
    """A teardown error must never replace the diagnosis with a worse one."""
    def _boom(*_a, **_k):
        raise RuntimeError("daemon went away mid-cleanup")

    monkeypatch.setattr(sr, "close_session_tab", _boom)
    monkeypatch.setattr(sr, "persist_target", _boom)
    _resolves_to(monkeypatch, {"targetId": "ext-tab-1", "opened": True})

    with pytest.raises(PageBindTimeout):
        ph.bind_current_page(_FakeContext(), _FakeSession())


def test_resolve_current_target_marks_only_the_tab_it_opened(monkeypatch):
    """The flag `bind_current_page` keys off must come from step 4 only.

    Steps 1-3 hand back a tab the session already owned; only step 4 creates
    one. Without this distinction the rollback above cannot be safe.
    """
    sess = _FakeSession()

    # Step 4: empty session -> opens.
    monkeypatch.setattr(sr, "session_tabs", lambda *a, **k: [])
    monkeypatch.setattr(sr, "ensure_session_target", lambda _s: None)
    monkeypatch.setattr(sr, "open_session_tab",
                        lambda *a, **k: {"targetId": "ext-tab-9", "url": "",
                                         "title": ""})
    assert sr.resolve_current_target(sess).get("opened") is True

    # Step 3: an existing tab -> reuse, no flag.
    monkeypatch.setattr(sr, "session_tabs", lambda *a, **k: [
        {"targetId": "ext-tab-7", "url": "u", "title": "t"}])
    monkeypatch.setattr(sr, "bind_target", lambda *a, **k: None)
    assert "opened" not in sr.resolve_current_target(sess)

    # Step 1: the cached current target -> reuse, no flag.
    sess.current_target_id = "ext-tab-7"
    assert "opened" not in sr.resolve_current_target(sess)
