"""Issue #21 unit tests: the executor's live page follows the session's
current target (the switch_tab-equivalent path).

The session_runtime hook mechanics are covered in
tests/skill/test_issue21_page_binding.py; these tests pin the executor side:
  - ``_on_target_changed`` (the registered hook) rebinds the live page to the
    session's current target and swaps the RUNNING heredoc's ``page`` name;
  - ``_rebind_page`` keeps the old page + warns when the rebind fails;
  - ``_reconcile_page_binding`` rebinds when the LEDGER current target moved
    (a separate process / in-process heredoc changed the binding).
"""
from __future__ import annotations


from browserwright._executor.process import _Worker


class _FakeContext:
    pass


class _FakePage:
    def __init__(self, url: str = "https://c.test/"):
        self.url = url


class _FakeSession:
    def __init__(self, current_target_id: str | None):
        self.current_target_id = current_target_id
        self.session_record = {"id": "sess-1"}


def _patch_session(monkeypatch):
    import browserwright.session as sess_mod

    monkeypatch.setattr(sess_mod, "current_session",
                        lambda: _FakeSession("ext-tab-A"))


def _worker_with(*, page_url: str | None = None) -> _Worker:
    w = _Worker("sess-1")
    w._connected = True
    w._context = _FakeContext()
    w._page = _FakePage(page_url) if page_url else None
    w._page_target_id = "ext-tab-A" if w._page else None
    w._call_warnings = []
    w._active_globals = {}
    return w


def test_on_target_changed_rebinds_page_and_swaps_running_globals(monkeypatch):
    w = _worker_with(page_url="about:blank")
    w._active_globals = {"page": w._page, "state": {}}
    _patch_session(monkeypatch)

    # _rebind_page does `from ..repl import playwright_handle as ph` inside
    # the method; patch the module's functions so the import sees them.
    import browserwright.repl.playwright_handle as ph_mod
    import browserwright.repl.snapshot as snap_mod

    new_page = _FakePage("https://c.test/")
    monkeypatch.setattr(ph_mod, "bind_current_page",
                        lambda context, sess: new_page)
    monkeypatch.setattr(snap_mod, "make_snapshot", lambda holder, warn=None: "snap")

    sess = _FakeSession("ext-tab-C")
    w._on_target_changed(sess)

    assert w._page is new_page
    assert w._page_target_id == "ext-tab-C"
    # The running heredoc's `page` name now points at the rebound page.
    assert w._active_globals["page"] is new_page
    assert w._snapshot_holder.page is new_page
    assert w._call_warnings == []


def test_on_target_changed_ignores_same_target_and_guards_unconnected(monkeypatch):
    w = _worker_with(page_url="about:blank")
    w._page_target_id = "ext-tab-C"
    w._active_globals = {"page": w._page}
    _patch_session(monkeypatch)

    import browserwright.repl.playwright_handle as ph_mod

    called = False

    def _boom(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("bind_current_page must not be called")

    monkeypatch.setattr(ph_mod, "bind_current_page", _boom)
    w._on_target_changed(_FakeSession("ext-tab-C"))
    assert not called, "same-target change must be a no-op"

    # Unconnected executor: hook must not touch anything.
    w2 = _worker_with(page_url="about:blank")
    w2._connected = False
    w2._on_target_changed(_FakeSession("ext-tab-D"))
    assert not called


def test_rebind_page_failure_keeps_old_page_and_warns(monkeypatch):
    w = _worker_with(page_url="about:blank")
    w._active_globals = {"page": w._page}
    _patch_session(monkeypatch)

    import browserwright.repl.playwright_handle as ph_mod

    def _raise(*_a, **_k):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(ph_mod, "bind_current_page", _raise)
    w._rebind_page()
    # Old page retained, warning surfaced, target unchanged.
    assert w._page.url == "about:blank"
    assert w._page_target_id == "ext-tab-A"
    assert any("page rebind" in x for x in w._call_warnings)


def test_reconcile_page_binding_follows_ledger(monkeypatch):
    w = _worker_with(page_url="about:blank")
    w._active_globals = {"page": w._page}
    _patch_session(monkeypatch)

    import browserwright.repl.playwright_handle as ph_mod
    import browserwright.repl.snapshot as snap_mod

    new_page = _FakePage("https://ledger.test/")
    monkeypatch.setattr(ph_mod, "bind_current_page",
                        lambda context, sess: new_page)
    monkeypatch.setattr(snap_mod, "make_snapshot", lambda holder, warn=None: "snap")

    # Ledger says the current target moved to ext-tab-D (e.g. an in-process
    # heredoc ran bind_target without touching the page surface).
    import browserwright.session as sess_mod
    import browserwright.session_registry as reg_mod

    monkeypatch.setattr(sess_mod, "current_session",
                        lambda: _FakeSession("ext-tab-A"))

    monkeypatch.setattr(reg_mod, "get",
                        lambda sid: {"runtime": {
                            "current_target_id": "ext-tab-D", "group_id": 7}})
    w._reconcile_page_binding()
    assert w._page is new_page
    assert w._page_target_id == "ext-tab-D"
