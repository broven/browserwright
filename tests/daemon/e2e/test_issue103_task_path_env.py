"""Issue #103: direct tasks explicitly select their request environment."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .conftest import published_endpoint, scrubbed_env
from .test_l2_heredoc_playwright_page import (
    _cleanup_session,
    _seed_session,
    cdp_autofacade_daemon,  # noqa: F401 - fixture
)


_BS_HOME_CDP = Path(__file__).resolve().parent / "_bs_home" / "cdp"


def _run_direct_task(
    runtime_dir: str,
    sid: str,
    site: str,
    *,
    path: str,
    select_path: bool,
) -> subprocess.CompletedProcess[str]:
    env = scrubbed_env()
    env.update({
        "PATH": path,
        "XDG_RUNTIME_DIR": runtime_dir,
        "TMPDIR": runtime_dir,
        "BS_HOME": str(_BS_HOME_CDP),
        "BW_DAEMON_URL": published_endpoint(runtime_dir) or "",
        "BD_SESSION": sid,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    args = [
        sys.executable,
        "-m",
        "browserwright",
        "-s",
        sid,
        "task",
        f"{site}/path_probe",
        "--output",
        "json",
    ]
    if select_path:
        args.extend(["--env", "PATH"])
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_direct_task_path_selection_is_request_scoped(
    cdp_autofacade_daemon, tmp_path,
):
    runtime_dir, _facade_ws = cdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "cdp")
    site = f"issue103-{sid}.test"
    site_dir = _BS_HOME_CDP / "site-skills" / site
    task_dir = site_dir / "tasks"
    task_dir.mkdir(parents=True)
    task_dir.joinpath("path_probe.py").write_text(
        """\
import shutil
import subprocess

ARGS = {}

def run(args, ctx=None):
    executable = shutil.which("bw-issue103-path-probe")
    if executable is None:
        return {"state": "not-found"}
    completed = subprocess.run([executable], check=False)
    return {"state": "ok", "returncode": completed.returncode}
""",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "caller-bin"
    fake_bin.mkdir()
    executable = fake_bin / "bw-issue103-path-probe"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    caller_path = str(fake_bin) + os.pathsep + os.environ["PATH"]

    try:
        without_selection = _run_direct_task(
            runtime_dir, sid, site, path=caller_path, select_path=False,
        )
        assert without_selection.returncode == 0, without_selection.stderr
        assert json.loads(without_selection.stdout) == {"state": "not-found"}

        with_selection = _run_direct_task(
            runtime_dir, sid, site, path=caller_path, select_path=True,
        )
        assert with_selection.returncode == 0, with_selection.stderr
        assert json.loads(with_selection.stdout) == {
            "state": "ok",
            "returncode": 0,
        }

        after_selection = _run_direct_task(
            runtime_dir, sid, site, path=caller_path, select_path=False,
        )
        assert after_selection.returncode == 0, after_selection.stderr
        assert json.loads(after_selection.stdout) == {"state": "not-found"}
    finally:
        _cleanup_session("cdp", sid)
        shutil.rmtree(site_dir, ignore_errors=True)
