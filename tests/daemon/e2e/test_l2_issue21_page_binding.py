"""Issue #21 regression (extension backend): the live ``page`` must follow
``bind_target`` (the internal switch_tab) even when the previously-bound tab
is closed in the same call — and the rebind must survive a fresh interpreter
on the same session, plus a terminal ``reset()``.

Repro shape (mirrors the issue): the executor cold-start binds ``page`` to a
fresh ``about:blank`` tab (A). One script then opens two more tabs (B, C),
``bind_target(C)`` (switch_tab), closes B, then closes A (the OLD binding).
``session_tabs`` correctly reports only C attached, but ``page.url`` must be
C's URL — before the fix it stayed on the closed A tab and printed
``about:blank``.
"""
from __future__ import annotations

import pytest

from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)

# Runs in the resident executor (touches `page`). Uses the INTERNAL tab
# lifecycle — the direct descendants of the deleted switch_tab / close_tab /
# list_tabs primitives the issue was filed against.
_REPRO_SCRIPT = '''
from browserwright.session import current_session
from browserwright.session_runtime import (
    bind_target, close_session_tab, open_session_tab, session_tabs,
)
sess = current_session()
# A: the session's cold-start tab (about:blank) — what `page` is bound to.
a_id = sess.current_target_id
b = open_session_tab(sess, "data:text/html,<title>B-incidental</title>")
c = open_session_tab(sess, "data:text/html,<title>C-target</title>")
b_id, c_id = b["targetId"], c["targetId"]
c_url = c["url"]
bind_target(sess, c_id)                  # switch_tab("ext-tab-C")
close_session_tab(sess, target_id=b_id)  # close_tab("ext-tab-B")
close_session_tab(sess, target_id=a_id)  # close_tab("ext-tab-A") -- old binding
remaining = [(t["targetId"], t["attached"]) for t in session_tabs(sess)]
print("A_ID=" + str(a_id))
print("C_ID=" + c_id)
print("C_URL=" + c_url)
print("REMAINING=" + repr(remaining))
try:
    print("PAGE_URL=" + page.url)
except Exception as e:
    print("PAGE_URL=ERR:" + type(e).__name__)
try:
    print("PAGE_TITLE=" + page.title())
except Exception as e:
    print("PAGE_TITLE=ERR:" + type(e).__name__)
'''

_FRESH_INTERPRETER_SCRIPT = '''
try:
    print("PAGE_TITLE2=" + page.title())
except Exception as e:
    print("PAGE_TITLE2=ERR:" + type(e).__name__)
'''

_AFTER_RESET_SCRIPT = '''
try:
    print("PAGE_TITLE3=" + page.title())
except Exception as e:
    print("PAGE_TITLE3=ERR:" + type(e).__name__)
'''


def _grep(out: str, key: str) -> str:
    for line in out.splitlines():
        if line.startswith(key + "="):
            return line[len(key) + 1:]
    raise AssertionError(f"{key}= not found in output:\n{out}")


def test_page_binding_follows_switch_tab_across_close_of_old_tab(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    e2e_chrome,
    patched_ext_dir,
):
    """Issue #21: switch_tab(C) + close old tab in one call must leave `page`
    bound to C; a fresh interpreter and reset() must both keep it there."""
    pytest.importorskip("playwright.sync_api")
    from .test_l2_heredoc_playwright_page import (
        _chrome_group_tab_ids,
        _cleanup_session,
        _run_execute,
        _seed_session,
        _session_group_id,
    )
    from .test_l2_multisession import (
        _chrome_close_tabs,
        _extension_id_from_path,
    )

    runtime_dir, _facade_ws = _ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    try:
        # Warm-up: executor cold-start binds page to A (about:blank). The
        # cold-start announce race (pre-existing, unrelated to #21) can make
        # this first bind flaky, so retry it once like the doc suggests.
        w = _run_execute(
            "print('WARM=' + page.url)\n",
            sid=sid, runtime_dir=runtime_dir, timeout=60)
        if w.returncode != 0:
            w2 = _run_execute("reset()\n", sid=sid,
                              runtime_dir=runtime_dir, timeout=60)
            assert w2.returncode == 0, f"warmup reset failed: {w2.stderr!r}"
            w = _run_execute(
                "print('WARM=' + page.url)\n",
                sid=sid, runtime_dir=runtime_dir, timeout=60)
        assert w.returncode == 0, (
            f"warmup heredoc failed: {w.stdout!r} {w.stderr!r}")
        # The repro script: same resident executor, switch+close.
        r = _run_execute(_REPRO_SCRIPT, sid=sid, runtime_dir=runtime_dir,
                         timeout=90)
        assert r.returncode == 0, (
            f"repro heredoc failed; stdout={r.stdout!r} stderr={r.stderr!r}")
        a_id = _grep(r.stdout, "A_ID")
        c_id = _grep(r.stdout, "C_ID")
        assert a_id and a_id != c_id, f"tab setup wrong: A={a_id!r} C={c_id!r}"

        # The daemon-side tab set is correct (only C remains, attached)...
        remaining = eval(_grep(r.stdout, "REMAINING"))  # noqa: S307 - test repr
        assert remaining == [(c_id, True)], (
            f"session_tabs wrong after switch+close: {remaining}")

        # ...and `page` must have followed the switch: it is C's page, NOT the
        # stale about:blank the old binding left behind.
        # `page.title` may or may not carry the extension's "👀 " attach
        # prefix depending on injection timing (pre-existing race, unrelated
        # to the switch binding under test) — containment tolerates both.
        page_title = _grep(r.stdout, "PAGE_TITLE")
        assert "C-target" in page_title, (
            f"page did not follow switch_tab: page.title={page_title!r} "
            f"(url={_grep(r.stdout, 'PAGE_URL')!r})")

        # Fresh interpreter, same session, same resident executor: still C.
        r2 = _run_execute(_FRESH_INTERPRETER_SCRIPT, sid=sid,
                          runtime_dir=runtime_dir, timeout=60)
        assert r2.returncode == 0, (
            f"fresh-interpreter heredoc failed: {r2.stdout!r} {r2.stderr!r}")
        assert "C-target" in _grep(r2.stdout, "PAGE_TITLE2"), (
            f"stale binding survived a fresh interpreter: {r2.stdout!r}")

        # Terminal reset(): the next command cold-starts and re-resolves the
        # page against the attached tab (ledger current = C).
        r3 = _run_execute("reset()\n", sid=sid, runtime_dir=runtime_dir,
                          timeout=60)
        assert r3.returncode == 0, f"reset heredoc failed: {r3.stderr!r}"
        r4 = _run_execute(_AFTER_RESET_SCRIPT, sid=sid,
                          runtime_dir=runtime_dir, timeout=90)
        assert r4.returncode == 0, (
            f"post-reset heredoc failed: {r4.stdout!r} {r4.stderr!r}")
        assert "C-target" in _grep(r4.stdout, "PAGE_TITLE3"), (
            f"reset() did not re-resolve page to the attached tab: "
            f"{r4.stdout!r}")
    finally:
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        if gid is not None:
            tab_ids = _chrome_group_tab_ids(e2e_chrome, extension_id, gid)
            _chrome_close_tabs(e2e_chrome, extension_id, tab_ids)
        _cleanup_session("extension", sid)
