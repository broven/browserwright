"""Helpers for running browserwright against the test daemon."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .conftest import (
    TEST_EXT_PORT,
    TEST_NAME,
    TEST_RDP_NAME,
    TEST_RDP_PORT,
    scrubbed_env,
)


@dataclass
class SkillResult:
    returncode: int
    stdout: str
    stderr: str


def run_skill(script: str, *, backend: str, extra_env: dict[str, str] | None = None,
              timeout: float = 30.0) -> SkillResult:
    """Invoke `browserwright` with the given heredoc-style Python script.

    Sets env vars so the skill talks to the *test* Mode B daemon, not the
    user's production daemon:

        - `BD_NAME`            -> picks the scenario's daemon socket
        - `BD_EXTENSION_PORT` / `BD_RDP_PORT` -> the scenario's upstream
        - a ledger record (below) -> the session's backend + daemon_endpoint

    Args:
        script: Python source the skill REPL will execute (heredoc body).
        backend: "extension" or "rdp".
        extra_env: extra env merged on top.
        timeout: subprocess timeout in seconds.

    Returns SkillResult (does NOT raise on non-zero exit; caller asserts).
    """
    if backend not in ("extension", "rdp"):
        raise ValueError(f"backend must be 'extension' or 'rdp', got {backend!r}")

    skill_bin = shutil.which("browserwright")
    if not skill_bin:
        raise RuntimeError(
            "browserwright not on PATH; install browserwright in editable mode: "
            "`pip install -e browserwright[test]`"
        )

    # Both scenarios drive the browser *through* a Mode B daemon (Mode A — the
    # direct-ws resolver — was removed). `extension` talks to the daemon at
    # TEST_NAME (the `e2e_daemon` fixture, serving extension); `rdp` talks to a
    # separate daemon at TEST_RDP_NAME (the `e2e_rdp_daemon` fixture, serving
    # rdp). The skill routes by the ledger record's daemon_endpoint (set below
    # to BD_NAME); each daemon serves exactly its backend.
    daemon_name = TEST_NAME if backend == "extension" else TEST_RDP_NAME
    env = scrubbed_env()
    env["BD_NAME"] = daemon_name
    env["BS_HOME"] = str(Path(__file__).resolve().parent / "_bs_home" / daemon_name)
    # Bypass proxy for localhost
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    if backend == "extension":
        # Pin the relay port so the daemon (and any doctor probe) targets the
        # test relay, not DEFAULT_RELAY_PORT (19989) — the isolation wall.
        env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    else:  # rdp
        # The rdp daemon resolves its upstream against this port.
        env["BD_RDP_PORT"] = str(TEST_RDP_PORT)
    if extra_env:
        env.update(extra_env)

    # P1 session model: inline `browserwright <<PY` refuses to run unless a
    # ledger session is explicitly in scope. E2E helpers create a lightweight
    # ledger record directly in the isolated BS_HOME so tests don't depend on
    # the developer's session state, with the record's daemon_endpoint pointing
    # at the scenario's Mode B daemon.
    created_session_id = None
    if "BD_SESSION" not in env:
        import json
        import time

        created_session_id = f"e2e-{uuid.uuid4().hex}"
        sessions_dir = Path(env["BS_HOME"]) / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = sessions_dir / "ledger.json"
        now = time.time()
        record = {
            "id": created_session_id,
            "backend": backend,
            "daemon_endpoint": daemon_name,
            "workspace": None,
            "owner": "attach",
            "name": "e2e-run-skill",
            "created_at": now,
            "last_seen": now,
        }
        ledger_path.write_text(
            json.dumps({"next_id": 1, "sessions": {created_session_id: record}}),
            encoding="utf-8",
        )
        env["BD_SESSION"] = created_session_id

    try:
        proc = subprocess.run(
            [skill_bin],
            input=script,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout,
        )
    finally:
        if created_session_id is not None:
            try:
                ledger_path.unlink()
            except OSError:
                pass
    return SkillResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
