"""L2 -- Phase B PR1: the persistent per-session executor.

Proves the Phase B core through the REAL `browserwright` heredoc CLI:

  1. cross-heredoc `state` survival: heredoc #1 sets `state.x = 1`; a SEPARATE
     heredoc #2 reads `state.x == 1` — the persistent per-session executor kept
     it (the whole Phase B win; phase C could not do this).
  2. same LIVE `page` across heredocs: heredoc #1 `page.goto(url)`; heredoc #2
     reads `page.url` and it is STILL `url` with NO re-navigation — the page is
     the same live object held by the resident executor, not a re-bound tab.
  3. pure-memory heredocs stay lightweight: a heredoc touching none of
     {page,context,snapshot,state,reset} runs in-process and never spawns an
     executor (no `bw-exec-*` discovery file appears).
  4. Terminal `reset()`: the old executor is confirmed dead before the command
     returns; tabs survive, the next call gets a new executor, and `state` is
     empty — `test_reset_clears_state_across_heredocs_rdp`.

Run on BOTH backends: rdp (cheapest) + the extension CfT harness. These reuse
the auto-facade daemon fixtures from `test_l2_heredoc_playwright_page.py`.

NOTE (honest gap): these tests need the CfT harness + a live daemon; they were
NOT executed in the implementing agent's sandbox. They are written to the SAME
green pattern as `test_l2_heredoc_playwright_page.py` (same fixtures, same
`run_skill`, same `_seed_session`/`_bound_target` helpers).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .test_l2_heredoc_playwright_page import (  # reuse the proven harness
    _bound_target,
    _cleanup_session,
    _grep,
    _seed_session,
    _status_facade_ws,
    ext_autofacade_ready,  # noqa: F401 - fixture
    rdp_autofacade_daemon,  # noqa: F401 - fixture
)
from .helpers import run_skill


_BS_HOME_RDP = Path(__file__).resolve().parent / "_bs_home" / "rdp"


def _executor_files(runtime_dir: str) -> list[str]:
    return glob.glob(os.path.join(runtime_dir, "bw-exec-*.json"))


def _wait_gone(runtime_dir: str, timeout: float = 8.0) -> bool:
    """Poll until no `bw-exec-*` discovery file remains (executor reaped)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _executor_files(runtime_dir):
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
#   rdp backend
# ---------------------------------------------------------------------------

# heredoc #1: navigate + stash state. Touches `page`/`state` → ships to executor.
_STATE_SCRIPT_1 = (
    "page.goto('data:text/html,<title>persist</title>', wait_until='load')\n"
    "state['x'] = 1\n"
    "state['url'] = page.url\n"
    "print('SET_OK')\n"
)

# heredoc #2: state survived (same executor) + page is the SAME live object
# (still on the URL #1 navigated to, with no re-goto in this heredoc).
_STATE_SCRIPT_2 = (
    "print('X=' + str(state.get('x')))\n"
    "print('SAMEURL=' + str(page.url == state.get('url')))\n"
    "print('TITLE=' + page.title())\n"
)


def test_state_and_page_persist_across_heredocs_rdp(rdp_autofacade_daemon):
    """ACCEPTANCE (rdp): `state.x` survives across heredocs and `page` is the
    SAME live object held by the resident per-session executor."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = rdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "rdp")
    extra = {"BD_SESSION": sid}
    try:
        r1 = run_skill(_STATE_SCRIPT_1, backend="rdp",
                       runtime_dir=runtime_dir, extra_env=extra)
        assert r1.returncode == 0, f"heredoc#1 failed: {r1.stderr}"
        assert "SET_OK" in r1.stdout
        # The executor was spawned (its discovery file exists).
        assert _executor_files(runtime_dir), "no executor discovery file"

        r2 = run_skill(_STATE_SCRIPT_2, backend="rdp",
                       runtime_dir=runtime_dir, extra_env=extra)
        assert r2.returncode == 0, f"heredoc#2 failed: {r2.stderr}"
        # state persisted by reference across calls.
        assert _grep(r2.stdout, "X") == "1", "state.x did not survive"
        # page is the same live object — still on the URL #1 navigated to.
        assert _grep(r2.stdout, "SAMEURL") == "True", "page is not the same live obj"
        assert _grep(r2.stdout, "TITLE") == "persist"
    finally:
        _cleanup_session("rdp", sid)


def test_single_executor_single_tab_rdp(rdp_autofacade_daemon):
    """ACCEPTANCE (rdp): N browser heredocs reuse ONE executor + ONE tab — the
    page count is STABLE across heredocs (no tab opened per call).

    We assert STABILITY, not an absolute count: a daemon-launched rdp Chrome
    starts with its own built-in `about:blank` launcher tab, and
    `bind_current_page`→`resolve_current_target()` opens the session's working
    tab WITHOUT adopting that launcher tab ("auto-open, NOT adopt",
    `session_runtime.py`).
    So `context.pages` is stably 2 (launcher blank + session tab), not 1. The
    proven phase-C sibling (`test_l2_heredoc_playwright_page.py`) likewise
    asserts stability + growth-by-exactly-1-only-on-`new_page()`, never an
    absolute count. A GROWING count here (2→3→4) would be a real reuse bug
    (the executor opening an extra tab per call); a stable count proves the
    resident executor reuses the same bound tab."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = rdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "rdp")
    extra = {"BD_SESSION": sid}
    try:
        baseline: str | None = None
        for i in range(3):
            r = run_skill(
                f"page.goto('data:text/html,<title>n{i}</title>', wait_until='load')\n"
                "print('PAGES=' + str(len(context.pages)))\n",
                backend="rdp", runtime_dir=runtime_dir, extra_env=extra)
            assert r.returncode == 0, f"heredoc#{i} failed: {r.stderr}"
            pages = _grep(r.stdout, "PAGES")
            if baseline is None:
                baseline = pages
            else:
                assert pages == baseline, (
                    f"page count grew across heredocs ({baseline} → {pages}): "
                    "the executor opened a new tab per call instead of reusing "
                    "the bound one")
        # Exactly one executor for the session (single resident process).
        assert len(_executor_files(runtime_dir)) == 1
        assert _bound_target("rdp", sid), "no persisted target"
    finally:
        _cleanup_session("rdp", sid)


def test_reset_clears_state_across_heredocs_rdp(rdp_autofacade_daemon):
    """Terminal reset reaps the old executor while preserving its browser tab."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = rdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "rdp")
    extra = {"BD_SESSION": sid}
    try:
        r1 = run_skill(
            "page.goto('data:text/html,<title>r</title>', wait_until='load')\n"
            "state['x'] = 99\n"
            "print('SET_OK')\n",
            backend="rdp", runtime_dir=runtime_dir, extra_env=extra)
        assert r1.returncode == 0, f"heredoc#1 failed: {r1.stderr}"
        assert "SET_OK" in r1.stdout
        files_before = _executor_files(runtime_dir)
        assert files_before, "no executor discovery file"
        old_record = json.loads(Path(files_before[0]).read_text())
        old_pid = old_record["pid"]
        old_executor_id = old_record["executor_id"]

        r2 = run_skill(
            "reset()\n"
            "print('AFTER_RESET_SHOULD_NOT_RUN')\n",
            backend="rdp", runtime_dir=runtime_dir, extra_env=extra)
        assert r2.returncode == 0, f"heredoc#2 (reset) failed: {r2.stderr}"
        assert "AFTER_RESET_SHOULD_NOT_RUN" not in r2.stdout
        assert _executor_files(runtime_dir) == [], (
            "reset command returned before executor discovery was removed")
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)

        r3 = run_skill(
            "print('X=' + repr(state.get('x')))\n"
            "print('TITLE=' + page.title())\n",
            backend="rdp", runtime_dir=runtime_dir, extra_env=extra)
        assert r3.returncode == 0, f"heredoc#3 (cold restart) failed: {r3.stderr}"
        assert _grep(r3.stdout, "X") == "None", "reset() did not clear state"
        assert _grep(r3.stdout, "TITLE") == "r", "reset() did not preserve the tab"
        files_after = _executor_files(runtime_dir)
        assert len(files_after) == 1
        new_record = json.loads(Path(files_after[0]).read_text())
        assert new_record["pid"] != old_pid
        assert new_record["executor_id"] != old_executor_id
    finally:
        _cleanup_session("rdp", sid)


def _end_session(runtime_dir: str, sid: str) -> subprocess.CompletedProcess:
    """Invoke `browserwright session end --session=<sid>` against the test
    daemon (same isolated runtime dir / BS_HOME)."""
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BS_HOME"] = str(_BS_HOME_RDP)
    env["BD_SESSION"] = sid
    # NB: `python -m browserwright` (NOT `browserwright.cli`) — cli.py has no
    # `__main__` guard, so `-m browserwright.cli` runs nothing (rc 0, no-op);
    # the package `__main__.py` is what routes to `cli.main()`.
    return subprocess.run(
        [sys.executable, "-m", "browserwright", "session", "end",
         "--session", sid],
        capture_output=True, text=True, env=env, timeout=30)


def test_end_session_kills_executor_rdp(rdp_autofacade_daemon):
    """PR2 ACCEPTANCE (rdp): after a browser heredoc spawns the executor,
    `session end` makes the daemon SIGTERM it — its discovery file disappears
    (no leaked subprocess).

    HONEST GAP: needs a live daemon; NOT run in the implementing agent's
    sandbox. Written to the same green pattern as the sibling reuse tests.

    OWNERSHIP: the session is seeded ``owner="create"`` ON PURPOSE — only a
    create-owned session makes `session end` drive the daemon's endSession verb
    (which kills the executor). An attach-owned session deliberately leaves the
    browser untouched and never contacts the daemon (`session_create.end`), so
    its executor would never be reaped via this path — the test's premise is
    impossible with an attach seed."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = rdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "rdp", owner="create")
    extra = {"BD_SESSION": sid}
    try:
        r = run_skill(
            "page.goto('data:text/html,<title>e</title>', wait_until='load')\n"
            "print('OK')\n",
            backend="rdp", runtime_dir=runtime_dir, extra_env=extra)
        assert r.returncode == 0, f"heredoc failed: {r.stderr}"
        assert _executor_files(runtime_dir), "executor never spawned"

        end = _end_session(runtime_dir, sid)
        assert end.returncode == 0, f"session end failed: {end.stderr}"
        assert _wait_gone(runtime_dir), (
            "executor discovery file survived endSession (process leaked)")
    finally:
        _cleanup_session("rdp", sid)


def test_daemon_restart_cold_starts_fresh_executor_rdp(
        rdp_autofacade_daemon, e2e_artifacts_dir):
    """PR2/Fork-4 ACCEPTANCE (rdp): after the daemon restarts (facade ws gone),
    the old executor self-exits; the NEXT heredoc cold-starts a fresh executor
    that re-binds the session's current tab via the ledger fast-path. `state`
    is lost on this path (documented), but `page` re-binds to the same tab
    (`current_target_id` is stable in the ledger).

    HONEST GAP: needs a live daemon + a controlled restart; NOT run in the
    implementing agent's sandbox. Written to the proven harness pattern."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = rdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "rdp")
    extra = {"BD_SESSION": sid}
    try:
        r1 = run_skill(
            "page.goto('data:text/html,<title>before</title>', wait_until='load')\n"
            "state['x'] = 1\n"
            "print('NPAGES=' + str(len(context.pages)))\n",
            backend="rdp", runtime_dir=runtime_dir, extra_env=extra)
        assert r1.returncode == 0, f"pre-restart heredoc failed: {r1.stderr}"
        tid_before = _bound_target("rdp", sid)
        assert tid_before, "no persisted target before restart"

        # Restart the daemon: stop, then re-serve with the SAME isolated env as
        # the `rdp_autofacade_daemon` fixture. CRITICAL: carry `BD_RDP_PORT` —
        # the fixture pins the rdp Chrome's port via it (conftest.py), and the
        # facade resolves Chrome through the SAME cfg port. Omitting it makes the
        # restarted daemon default to 9222 (no Chrome there) so the facade 404s
        # and the executor cold-start fails — a test-env regression, not a code
        # bug. Mirror every fixture env key here.
        from .conftest import TEST_RDP_PORT
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["TMPDIR"] = runtime_dir
        env["BD_RDP_PORT"] = str(TEST_RDP_PORT)
        env["BS_HOME"] = str(_BS_HOME_RDP)
        env["BD_CONFIG"] = ""
        subprocess.run(["browserwright-daemon", "stop"],
                       capture_output=True, env=env, timeout=15)
        log_fh = open(e2e_artifacts_dir / "daemon-rdp-restart.log", "wb")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-m", "browserwright.daemon.cli", "serve",
             "--backend", "rdp", "-v"],
            stdout=log_fh, stderr=subprocess.STDOUT, env=env)
        try:
            assert _status_facade_ws(env) is not None, "daemon did not re-advertise"
            # The old executor's discovery file may linger briefly until its
            # facade-death self-exit + the new daemon's reap/orphan-sweep clears
            # it; the cold-start below is what we actually assert on.
            r2 = run_skill(
                "print('X=' + repr(state.get('x')))\n"  # state lost → None
                "print('NPAGES=' + str(len(context.pages)))\n",
                backend="rdp", runtime_dir=runtime_dir, extra_env=extra)
            assert r2.returncode == 0, f"post-restart heredoc failed: {r2.stderr}"
            # state was lost on cold restart (documented, same as reset()).
            assert _grep(r2.stdout, "X") == "None", "state survived a restart?"
            # page re-bound to the SAME ledger tab (current_target_id stable).
            assert _bound_target("rdp", sid) == tid_before, "tab not re-bound"
        finally:
            subprocess.run(["browserwright-daemon", "stop"],
                           capture_output=True, env=env, timeout=15)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_fh.close()
    finally:
        _cleanup_session("rdp", sid)


def test_memory_only_does_not_spawn_executor_rdp(rdp_autofacade_daemon):
    """ACCEPTANCE (lazy): a pure-memory heredoc runs in-process and never spawns
    an executor (no discovery file appears)."""
    runtime_dir, _facade_ws = rdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "rdp")
    try:
        r = run_skill("print('answer=' + str(6 * 7))",
                      backend="rdp", runtime_dir=runtime_dir,
                      extra_env={"BD_SESSION": sid})
        assert r.returncode == 0, f"memory heredoc failed: {r.stderr}"
        assert "answer=42" in r.stdout
        assert not _executor_files(runtime_dir), (
            "memory-only heredoc spawned an executor (not lightweight)")
    finally:
        _cleanup_session("rdp", sid)


# ---------------------------------------------------------------------------
#   extension backend (CfT harness)
# ---------------------------------------------------------------------------

# Extension backend: data: navigations abort over chrome.debugger — use
# set_content. The bound page is already on about:blank, so we set_content.
_EXT_STATE_1 = (
    "page.set_content('<title>persist</title>', wait_until='load')\n"
    "state['x'] = 1\n"
    "state['title'] = page.title()\n"
    "print('SET_OK')\n"
)

_EXT_STATE_2 = (
    "print('X=' + str(state.get('x')))\n"
    "print('SAMETITLE=' + str(page.title() == state.get('title')))\n"
)


def test_state_and_page_persist_across_heredocs_extension(ext_autofacade_ready):
    """ACCEPTANCE (extension/CfT): `state` survives + `page` is the same live
    object across heredocs over the extension facade."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    extra = {"BD_SESSION": sid}
    try:
        r1 = run_skill(_EXT_STATE_1, backend="extension",
                       runtime_dir=runtime_dir, extra_env=extra, timeout=60)
        assert r1.returncode == 0, f"ext heredoc#1 failed: {r1.stderr}"
        assert "SET_OK" in r1.stdout
        assert _executor_files(runtime_dir), "no executor discovery file"

        r2 = run_skill(_EXT_STATE_2, backend="extension",
                       runtime_dir=runtime_dir, extra_env=extra, timeout=60)
        assert r2.returncode == 0, f"ext heredoc#2 failed: {r2.stderr}"
        assert _grep(r2.stdout, "X") == "1", "ext state.x did not survive"
        assert _grep(r2.stdout, "SAMETITLE") == "True", "ext page not same live obj"
    finally:
        _cleanup_session("extension", sid)
