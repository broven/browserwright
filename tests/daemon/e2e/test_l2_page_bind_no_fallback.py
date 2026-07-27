"""Real-extension regression coverage for delayed Playwright page binding.

The agent path creates and persists the session target before Playwright has
necessarily materialized its ``Page``.  A transient miss at that boundary must
wait for the authoritative target; it must never manufacture a replacement
tab with ``context.new_page()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from browserwright.session import Session, with_session
from browserwright.session_ctx import resolve_session

from .conftest import TEST_EXT_PORT
from .test_l2_heredoc_playwright_page import (
    _assert_one_session_group,
    _bound_target,
    _chrome_close_tabs,
    _cleanup_session,
    _extension_id_from_path,
    _seed_session,
)
from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)
from .test_l2_multisession import _extension_worker_target_id

_BS_HOME_EXT = Path(__file__).resolve().parent / "_bs_home" / "extension"


def _chrome_all_tab_ids(chrome, extension_id: str) -> list[int]:
    """Query every Chrome tab so failure cleanup also catches ungrouped tabs."""
    from browserwright.cdp import CDPSession

    cdp = CDPSession(chrome.ws_url)
    try:
        worker = _extension_worker_target_id(cdp, extension_id)
        session_id = cdp.attach(worker)
        result = cdp.send(
            "Runtime.evaluate",
            session=session_id,
            expression=(
                "(async () => {"
                "const tabs = await chrome.tabs.query({});"
                "return tabs.map(t => t.id);"
                "})()"
            ),
            returnByValue=True,
            awaitPromise=True,
        )
    finally:
        cdp.close()
    if "exceptionDetails" in result:
        raise AssertionError(f"all-tab query failed: {result!r}")
    return list(result.get("result", {}).get("value", []))


def test_delayed_page_materialization_does_not_create_replacement_tab_extension(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    e2e_chrome,
    patched_ext_dir,
    monkeypatch,
):
    """A delayed target→Page match still binds the one session-group tab."""
    pytest.importorskip("playwright.sync_api")
    import browserwright.repl.playwright_handle as ph

    runtime_dir, _facade_ws = _ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    baseline_tab_ids = set(_chrome_all_tab_ids(e2e_chrome, extension_id))
    sid = _seed_session(runtime_dir, "extension")
    handle = ph.PlaywrightHandle()
    sess: Session | None = None

    # Point this in-process Playwright client at the isolated E2E daemon and
    # ledger, exactly as the browserwright subprocess helpers do.
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)
    monkeypatch.setenv("TMPDIR", runtime_dir)
    monkeypatch.setenv("BS_HOME", str(_BS_HOME_EXT))
    monkeypatch.setenv("BD_EXTENSION_PORT", str(TEST_EXT_PORT))
    monkeypatch.setenv("BD_CONFIG", "")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    real_page_for_target = ph.page_for_target
    calls = 0
    matched_pages: list[object] = []

    def delayed_page_for_target(*args, **kwargs):
        nonlocal calls
        calls += 1
        # Deterministically model the short facade→Playwright materialization
        # gap.  The fourth lookup sees the real browser state.
        if calls <= 3:
            return None
        page = real_page_for_target(*args, **kwargs)
        if page is not None:
            matched_pages.append(page)
        return page

    monkeypatch.setattr(ph, "page_for_target", delayed_page_for_target)
    # Keep the test tolerant of a loaded CI host; the artificial delay itself
    # is poll-count based, so no wall-clock lower-bound assertion is needed.
    monkeypatch.setattr(ph, "_PAGE_BIND_TIMEOUT_S", 5.0)

    try:
        sess = Session(record=resolve_session(sid))
        with with_session(sess):
            bound_page = handle.page
            context = handle.context

        target_id = _bound_target("extension", sid)
        assert target_id, "binding did not persist the authoritative target"
        assert calls >= 4, "the delayed target mapping was not exercised"
        assert matched_pages and bound_page is matched_pages[-1], (
            "binding returned a replacement page instead of the delayed "
            "authoritative target"
        )

        group_tabs = _assert_one_session_group(e2e_chrome, extension_id, sid)
        assert len(group_tabs) == 1, (
            f"delayed binding created extra session-group tabs: {group_tabs}"
        )
        assert len(context.pages) == 1, (
            "delayed binding implicitly created a second Playwright page"
        )
    finally:
        handle.close()
        if sess is not None:
            sess.close()
        try:
            # Close every tab created after the baseline, including a buggy
            # ungrouped replacement tab if this regression ever returns.
            tab_ids = list(
                set(_chrome_all_tab_ids(e2e_chrome, extension_id)) - baseline_tab_ids
            )
            _chrome_close_tabs(e2e_chrome, extension_id, tab_ids)
        finally:
            _cleanup_session("extension", sid)
