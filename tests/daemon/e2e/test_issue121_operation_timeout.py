"""Issue #121 / ADR-0014 (extension backend, real Chrome): one Playwright call
running out of its own timeout is catchable in code as Playwright's
`TimeoutError`, and when it escapes the code the call reports it as
`OperationTimeout`, exit 8 — the executor survives. It is never confused with
the call deadline.

- `page.click("#does-not-exist", timeout=1000)` raises after ~1s and
  `except TimeoutError` catches it; uncaught it exits 8 with a `[fix]` naming
  `timeout=` and `snapshot()`; `state` survives.
- An unset `timeout=` is Playwright's 30s even under `--timeout 300`.
- A smart `goto` that cannot commit in time reports `OperationTimeout` too
  (`PageLoadFailed(reason="timeout")` is gone), and it honours the agent's
  `set_default_navigation_timeout()`.
- When the call deadline is the binding cap — including a Playwright timeout
  that lands right at it — the answer is `DeadlineExceeded`, exit 7.

The slow page is served locally (see `test_issue119_call_deadline`).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from .test_issue119_call_deadline import (
    _assert_deadline_exceeded,
    _cli,
    _warm,
    local_site,  # noqa: F401 - fixture
)
from .test_l2_heredoc_playwright_page import (
    _cleanup_session,
    _grep,
    _seed_session,
)
from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)

#: One JSON line per measured timeout: the repeatable record of "exit 8 at
#: about N seconds" (gitignored, like all artifacts).
_EVIDENCE = (Path(__file__).resolve().parent / "_artifacts"
             / "issue121_operation_timeout.jsonl")

#: CLI overhead on top of the measured wait: process start, session lookup,
#: `ensureExecutor`, the response round trip.
_OVERHEAD_S = 8.0


def _record(case: str, **fields) -> None:
    _EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    with _EVIDENCE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"case": case, "at": time.time(), **fields}) + "\n")


def _detail(proc, elapsed: float) -> str:
    return (f"rc={proc.returncode} elapsed={elapsed:.1f}s\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr}")


def _assert_operation_timeout(proc, elapsed: float) -> None:
    detail = _detail(proc, elapsed)
    assert proc.returncode == 8, detail
    assert "OperationTimeout" in proc.stderr, detail
    fix_lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("[fix]")]
    assert fix_lines, detail
    assert "timeout=" in fix_lines[-1] and "snapshot()" in fix_lines[-1], detail


def _state_survived(sid: str, runtime_dir: str) -> None:
    """The executor was not recycled: `_warm`'s `state` entry is still there."""
    after, _ = _cli(
        ["-s", sid, "-e", "print('STATE=' + repr(state.get('sentinel')))\n"],
        runtime_dir)
    assert after.returncode == 0, f"next call failed: {after.stdout!r} {after.stderr!r}"
    assert _grep(after.stdout, "STATE") == "'alive'"


def test_click_timeout_is_a_catchable_operation_timeout_and_exits_8(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,  # noqa: F811 - imported pytest fixture
):
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")

        # Caught in code, as Playwright's own TimeoutError.
        caught, _ = _cli(["-s", sid, "-e", (
            "import time\n"
            "from playwright.sync_api import TimeoutError as PWTimeout\n"
            "t = time.monotonic()\n"
            "try:\n"
            "    page.click('#does-not-exist', timeout=1000)\n"
            "except PWTimeout:\n"
            "    print('CAUGHT=yes')\n"
            "print('ELAPSED=%.2f' % (time.monotonic() - t))\n"
        )], runtime_dir)
        assert caught.returncode == 0, _detail(caught, 0)
        assert _grep(caught.stdout, "CAUGHT") == "yes"
        in_code = float(_grep(caught.stdout, "ELAPSED"))
        _record("click-1s-caught", elapsed_in_code_s=in_code)
        assert 0.9 <= in_code <= 3.0, in_code

        # Uncaught: exit 8, the [fix] names both knobs.
        proc, elapsed = _cli(
            ["-s", sid, "-e", "page.click('#does-not-exist', timeout=1000)\n"],
            runtime_dir)
        _record("click-1s-uncaught", elapsed_s=round(elapsed, 2),
                returncode=proc.returncode, stderr_tail=proc.stderr[-400:])
        _assert_operation_timeout(proc, elapsed)
        assert 1.0 <= elapsed <= 1.0 + _OVERHEAD_S, _detail(proc, elapsed)

        _state_survived(sid, runtime_dir)
    finally:
        _cleanup_session("extension", sid)


def test_unset_operation_timeout_is_playwrights_30s_not_the_call_deadline(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,  # noqa: F811 - imported pytest fixture
):
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")
        proc, elapsed = _cli(["-s", sid, "--timeout", "300", "-e", (
            "import time\n"
            "t = time.monotonic()\n"
            "try:\n"
            "    page.click('#does-not-exist')\n"
            "finally:\n"
            "    print('ELAPSED=%.2f' % (time.monotonic() - t))\n"
        )], runtime_dir, timeout=200)
        in_code = float(_grep(proc.stdout, "ELAPSED"))
        _record("click-unset-under-300", elapsed_s=round(elapsed, 2),
                elapsed_in_code_s=in_code, returncode=proc.returncode)
        _assert_operation_timeout(proc, elapsed)
        assert 29.0 <= in_code <= 35.0, _detail(proc, elapsed)
        _state_survived(sid, runtime_dir)
    finally:
        _cleanup_session("extension", sid)


def test_goto_that_cannot_commit_in_time_is_an_operation_timeout(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,  # noqa: F811 - imported pytest fixture
):
    """The smart-goto `timeout` bucket folds into OperationTimeout: same exit
    code, same knobs, plus #123's rule — check whether it landed, never a
    fresh tab."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")
        slow = local_site + "/slow"
        proc, elapsed = _cli(
            ["-s", sid, "-e", f"page.goto({slow!r}, timeout=2000)\n"],
            runtime_dir)
        _record("goto-2s-uncaught", elapsed_s=round(elapsed, 2),
                returncode=proc.returncode, stderr_tail=proc.stderr[-400:])
        _assert_operation_timeout(proc, elapsed)
        assert "PageLoadFailed" not in proc.stderr, _detail(proc, elapsed)
        assert slow in proc.stderr
        fix = [ln for ln in proc.stderr.splitlines() if ln.startswith("[fix]")][-1]
        assert "page.url" in fix and "new_page" not in fix
        assert 2.0 <= elapsed <= 2.0 + _OVERHEAD_S, _detail(proc, elapsed)
        _state_survived(sid, runtime_dir)
    finally:
        _cleanup_session("extension", sid)


def test_goto_honours_set_default_navigation_timeout(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,  # noqa: F811 - imported pytest fixture
):
    """An unset goto `timeout=` is the agent's own default when it set one
    (ADR-0014), not smart goto's 60s."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")
        slow = local_site + "/slow"
        proc, elapsed = _cli(["-s", sid, "-e", (
            "page.set_default_navigation_timeout(2000)\n"
            f"page.goto({slow!r})\n"
        )], runtime_dir)
        _record("goto-default-nav-2s", elapsed_s=round(elapsed, 2),
                returncode=proc.returncode, stderr_tail=proc.stderr[-400:])
        _assert_operation_timeout(proc, elapsed)
        assert 2.0 <= elapsed <= 2.0 + _OVERHEAD_S, _detail(proc, elapsed)
        _state_survived(sid, runtime_dir)
    finally:
        _cleanup_session("extension", sid)


@pytest.mark.parametrize("code", [
    # The Playwright timeout equals the call deadline: it lands right at it.
    "page.click('#does-not-exist', timeout=3000)\n",
    # A longer goto timeout capped by the deadline (#119's AC4, from the
    # operation-timeout side): the deadline is the binding cap.
    "page.goto(SLOW, timeout=60_000)\n",
], ids=["click-timeout-equals-deadline", "goto-timeout-above-deadline"])
def test_a_timeout_at_or_past_the_call_deadline_is_exit_7_never_8(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,  # noqa: F811 - imported pytest fixture
    code,
):
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")
        code = code.replace("SLOW", repr(local_site + "/slow"))
        proc, elapsed = _cli(["-s", sid, "--timeout", "3", "-e", code],
                             runtime_dir)
        _record(os.environ.get("PYTEST_CURRENT_TEST", "?").split(" ")[0],
                elapsed_s=round(elapsed, 2), returncode=proc.returncode)
        assert "OperationTimeout" not in proc.stderr, _detail(proc, elapsed)
        _assert_deadline_exceeded(proc, elapsed, 3.0)
    finally:
        _cleanup_session("extension", sid)
