"""Real-browser acceptance coverage for executor timeout dispositions.

An ordinary Playwright action timeout belongs to user code and must leave the
resident executor alive.  The outer executor request deadline is terminal: the
command returns only after that exact process is gone, while its browser tab is
left intact for the next cold-started executor.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

import pytest

from browserwright._executor.client import run_on_executor
from browserwright._executor.protocol import TERMINAL_DEADLINE_EXCEEDED
from browserwright.daemon import _ipc
from browserwright.session import Session
from browserwright.session_ctx import resolve_session
from browserwright.session_runtime import close_session_tab, session_tabs

from .conftest import TEST_CDP_PORT
from .helpers import run_skill
from .test_l2_heredoc_playwright_page import (
    _bound_target,
    _cleanup_session,
    _grep,
    _seed_session,
)
from .test_l2_heredoc_playwright_page import (
    cdp_autofacade_daemon as _cdp_autofacade_daemon,  # noqa: F401 - fixture
)

_BS_HOME_CDP = Path(__file__).resolve().parent / "_bs_home" / "cdp"


def test_action_timeout_survives_but_outer_deadline_recycles_executor_cdp(
    _cdp_autofacade_daemon,  # noqa: F811 - imported pytest fixture
    monkeypatch,
):
    """Only the terminal outer deadline replaces the resident executor."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = _cdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "cdp")
    extra = {"BD_SESSION": sid}
    sess: Session | None = None
    target_id: str | None = None

    # The direct executor-client call below runs in this pytest process, so it
    # must use the same isolated daemon/ledger configuration as run_skill().
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)
    monkeypatch.setenv("TMPDIR", runtime_dir)
    monkeypatch.setenv("BS_HOME", str(_BS_HOME_CDP))
    monkeypatch.setenv("BD_CDP_PORT", str(TEST_CDP_PORT))
    monkeypatch.setenv("BD_CONFIG", "")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    try:
        warm = run_skill(
            (
                "import os\n"
                "page.goto("
                "'data:text/html,<title>deadline-preserved</title><main>ready</main>', "
                "wait_until='load')\n"
                "state['sentinel'] = 'alive'\n"
                "print('PID=' + str(os.getpid()))\n"
                "print('URL=' + page.url)\n"
                "print('TITLE=' + page.title())\n"
            ),
            backend="cdp",
            runtime_dir=runtime_dir,
            extra_env=extra,
            timeout=60,
        )
        assert warm.returncode == 0, (
            f"executor warm-up failed: stdout={warm.stdout!r} stderr={warm.stderr!r}"
        )
        warm_url = _grep(warm.stdout, "URL")
        assert _grep(warm.stdout, "TITLE") == "deadline-preserved"
        target_id = _bound_target("cdp", sid)
        assert target_id, "warm-up did not persist the bound target"

        old_record = _ipc.read_executor_record(sid)
        assert old_record is not None, "warm-up did not publish an executor"
        old_pid = old_record["pid"]
        old_executor_id = old_record["executor_id"]
        assert int(_grep(warm.stdout, "PID")) == old_pid

        # A caught Playwright timeout is an ordinary successful request.  It
        # must preserve both executor identity and in-memory state.
        action_timeout = run_skill(
            (
                "from playwright.sync_api import TimeoutError as PlaywrightTimeoutError\n"
                "caught = False\n"
                "try:\n"
                "    page.locator('#never-appears').click(timeout=100)\n"
                "except PlaywrightTimeoutError:\n"
                "    caught = True\n"
                "print('CAUGHT=' + str(caught))\n"
                "print('STATE=' + repr(state.get('sentinel')))\n"
            ),
            backend="cdp",
            runtime_dir=runtime_dir,
            extra_env=extra,
            timeout=30,
        )
        assert action_timeout.returncode == 0, (
            f"caught action timeout failed: stdout={action_timeout.stdout!r} "
            f"stderr={action_timeout.stderr!r}"
        )
        assert _grep(action_timeout.stdout, "CAUGHT") == "True"
        assert _grep(action_timeout.stdout, "STATE") == "'alive'"
        after_action = _ipc.read_executor_record(sid)
        assert after_action is not None
        assert after_action["pid"] == old_pid
        assert after_action["executor_id"] == old_executor_id

        sess = Session(record=resolve_session(sid))
        terminal = run_on_executor(
            sess,
            "import time\ntime.sleep(60)\n",
            timeout_ms=200,
        )

        assert terminal.terminal_reason == TERMINAL_DEADLINE_EXCEEDED
        assert terminal.exit_code == 3
        assert terminal.error is not None
        assert terminal.error["type"] == "TimeoutError"

        # This is intentionally immediate, not a polling assertion: the
        # client contract says a terminal response is withheld until daemon
        # supervision has confirmed process death.
        assert _ipc.read_executor_record(sid) is None, (
            "outer deadline returned before executor discovery was removed"
        )
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)

        # The executor died, but its authoritative target and live URL did not.
        assert _bound_target("cdp", sid) == target_id
        preserved = next(
            (tab for tab in session_tabs(sess) if tab["targetId"] == target_id),
            None,
        )
        assert preserved is not None, "terminal deadline closed the browser tab"
        assert preserved["url"] == warm_url

        cold = run_skill(
            (
                "import os\n"
                "print('PID=' + str(os.getpid()))\n"
                "print('STATE=' + repr(state.get('sentinel')))\n"
                "print('URL=' + page.url)\n"
                "print('TITLE=' + page.title())\n"
            ),
            backend="cdp",
            runtime_dir=runtime_dir,
            extra_env=extra,
            timeout=60,
        )
        assert cold.returncode == 0, (
            f"cold restart failed: stdout={cold.stdout!r} stderr={cold.stderr!r}"
        )
        assert _grep(cold.stdout, "STATE") == "None"
        assert _grep(cold.stdout, "URL") == warm_url
        assert _grep(cold.stdout, "TITLE") == "deadline-preserved"

        new_record = _ipc.read_executor_record(sid)
        assert new_record is not None
        assert int(_grep(cold.stdout, "PID")) == new_record["pid"]
        assert new_record["pid"] != old_pid
        assert new_record["executor_id"] != old_executor_id
        assert _bound_target("cdp", sid) == target_id
    finally:
        if target_id is not None:
            cleanup_sess = sess
            if cleanup_sess is None:
                with suppress(Exception):
                    cleanup_sess = Session(record=resolve_session(sid))
            if cleanup_sess is not None:
                with suppress(Exception):
                    close_session_tab(cleanup_sess, target_id=target_id)
        if sess is not None:
            sess.close()
        _cleanup_session("cdp", sid)
