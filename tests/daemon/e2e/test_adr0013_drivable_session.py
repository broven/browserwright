"""ADR-0013 rule 1 end to end: one path makes a session drivable.

`/exec` may spawn a session's executor on its own (a direct or remote client,
or an executor that died between `ensureExecutor` and the data-plane dial). It
used to run its own copy of the readiness preflight, and that copy returned
early whenever the shared upstream was CONNECTED. Every extension session
shares that upstream, so any other active session keeps it connected — and the
copy then skipped tab convergence: a session whose tab was gone got an
executor, but the daemon never re-established the tab and the recovery state
kept saying `tab-gone` for a session that was being driven.

Driven end to end against real Chrome: the tab is killed from outside
browserwright (`chrome.tabs.remove`), the relay's Target detach makes the
daemon diagnose `tab-gone`, a second session runs a call (re-opening the
shared upstream, as any neighbour would), and then a raw `/exec` websocket —
no `ensureExecutor` first — runs one call. The call itself fails (an agent
bug, the ordinary first call after a loss), so nothing but the drivable path
can move the diagnosis.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from .conftest import TEST_EXT_FACADE_PORT
from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)

_LEDGER = (Path(__file__).resolve().parent / "_bs_home" / "extension"
           / "sessions" / "ledger.json")


def _recovery_state(sid: str) -> str | None:
    try:
        rec = json.loads(_LEDGER.read_text())["sessions"][sid]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None
    return (rec.get("recovery") or {}).get("state")


def _wait_state(sid: str, want: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    state = _recovery_state(sid)
    while state != want and time.monotonic() < deadline:
        time.sleep(0.2)
        state = _recovery_state(sid)
    return state


def _raw_exec(sid: str, code: str, timeout: float = 90) -> dict:
    """One ExecuteRequest over the endpoint's `/exec` surface, nothing else."""
    from websockets.sync.client import connect

    url = f"ws://127.0.0.1:{TEST_EXT_FACADE_PORT}/exec?session={sid}"
    with connect(url, open_timeout=30, max_size=None) as ws:
        ws.send(json.dumps({"code": code, "timeout_ms": 60000}))
        return json.loads(ws.recv(timeout=timeout))


def test_exec_relay_converges_a_session_whose_tab_is_gone(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    e2e_chrome,
    patched_ext_dir,
):
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
    neighbour = _seed_session(runtime_dir, "extension")
    try:
        script = "page.set_content('<title>one</title>')\nprint(page.title())\n"
        r0 = _run_execute(script, sid=sid, runtime_dir=runtime_dir, timeout=90)
        if r0.returncode != 0:
            # The known cold-start announce race other e2e tests retry through.
            _run_execute("reset()\n", sid=sid, runtime_dir=runtime_dir,
                         timeout=60)
            r0 = _run_execute(script, sid=sid, runtime_dir=runtime_dir,
                              timeout=90)
        assert r0.returncode == 0, f"round 0 failed: {r0.stdout!r} {r0.stderr!r}"
        # Let the extension's first-hello recovery sweep (delayed, then run
        # over every extension session) finish: it is a different recovery
        # input, and landing inside this test would make it the one that
        # repairs the diagnosis.
        from browserwright.daemon.server.extension_upstream import (
            _AUTO_RECOVER_DELAY_S,
        )
        time.sleep(_AUTO_RECOVER_DELAY_S + 2)

        # Recycle the executor (tabs survive), then kill the tab from outside
        # browserwright: a session with no executor and no tab.
        _run_execute("reset()\n", sid=sid, runtime_dir=runtime_dir, timeout=60)
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        assert gid is not None, "session has no tab group"
        _chrome_close_tabs(e2e_chrome, extension_id,
                           _chrome_group_tab_ids(e2e_chrome, extension_id, gid))
        assert _wait_state(sid, "tab-gone", 15) == "tab-gone", (
            "precondition: the daemon should diagnose the killed tab")
        rn = _run_execute(script, sid=neighbour, runtime_dir=runtime_dir,
                          timeout=90)
        assert rn.returncode == 0, (
            f"neighbour session failed: {rn.stdout!r} {rn.stderr!r}")
        assert _recovery_state(sid) == "tab-gone", (
            "precondition: a neighbour's call must not touch this session")

        response = _raw_exec(sid, "raise RuntimeError('agent bug')")
        assert (response.get("error") or {}).get("type") == "RuntimeError", (
            f"the call should reach the executor and fail in user code: "
            f"{response!r}")

        # The daemon made the session drivable before handing out the
        # executor, so its diagnosis follows the facts.
        assert _wait_state(sid, "healthy", 10) == "healthy", (
            "/exec spawned an executor without converging the session's tab")
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        assert gid is not None, "session has no tab group after /exec"
        tabs = _chrome_group_tab_ids(e2e_chrome, extension_id, gid)
        assert len(tabs) == 1, f"expected one session tab, found {tabs}"
    finally:
        for each in (sid, neighbour):
            gid = _session_group_id(e2e_chrome, extension_id, each)
            if gid is not None:
                _chrome_close_tabs(
                    e2e_chrome, extension_id,
                    _chrome_group_tab_ids(e2e_chrome, extension_id, gid))
            _cleanup_session("extension", each)

