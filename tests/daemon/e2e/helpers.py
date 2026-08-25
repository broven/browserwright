"""Helpers for running browserwright against the test daemon."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .conftest import (
    TEST_CDP_PORT,
    TEST_EXT_PORT,
    published_endpoint,
    scrubbed_env,
)


@dataclass
class SkillResult:
    returncode: int
    stdout: str
    stderr: str


def run_skill(script: str, *, backend: str, runtime_dir: str | None = None,
              extra_env: dict[str, str] | None = None,
              timeout: float = 30.0) -> SkillResult:
    """Invoke `browserwright` with the given heredoc-style Python script.

    Single-global-daemon: the skill reaches the *test* daemon (not the
    developer's) via `BW_DAEMON_URL`, read out of the endpoint state file the
    daemon that owns `runtime_dir` published when it bound (ADR-0011). Deriving
    it from the directory rather than hardcoding a port keeps the harness's
    long-standing contract — *`runtime_dir` identifies the daemon* — so a test
    that spins up its own daemon on its own endpoint port needs no change here.
    The ledger record carries only the session's `backend`; the daemon routes
    per session. Relay/cdp upstream isolation is via `BD_EXTENSION_PORT` /
    `BD_CDP_PORT`.

    Args:
        script: Python source the skill REPL will execute (heredoc body).
        backend: "extension" or "cdp".
        runtime_dir: XDG_RUNTIME_DIR of the test daemon (yielded by the
            `e2e_daemon` / `e2e_cdp_daemon` fixtures). May also be supplied via
            `extra_env["XDG_RUNTIME_DIR"]`.
        extra_env: extra env merged on top.
        timeout: subprocess timeout in seconds.

    Returns SkillResult (does NOT raise on non-zero exit; caller asserts).
    """
    if backend not in ("extension", "cdp"):
        raise ValueError(f"backend must be 'extension' or 'cdp', got {backend!r}")

    skill_bin = Path(sys.executable).with_name("browserwright")
    if not skill_bin.exists():
        found = shutil.which("browserwright")
        if not found:
            raise RuntimeError(
                "browserwright not on PATH; install browserwright in editable "
                "mode: `pip install -e browserwright[test]`"
            )
        skill_bin = Path(found)

    env = scrubbed_env()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # The runtime dir carries the executor sockets, the pid file and — since
    # ADR-0011 — the endpoint the daemon published when it bound.
    if runtime_dir is not None:
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["TMPDIR"] = runtime_dir
        url = published_endpoint(runtime_dir)
        if url is not None:
            env["BW_DAEMON_URL"] = url
    # Isolated BS_HOME per backend (ledger + memory).
    env["BS_HOME"] = str(Path(__file__).resolve().parent / "_bs_home" / backend)
    # Bypass proxy for localhost
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    if backend == "extension":
        # Pin the relay port so the daemon (and any doctor probe) targets the
        # test relay, not DEFAULT_RELAY_PORT (19989) — the isolation wall.
        env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    else:  # cdp
        # The cdp daemon resolves its upstream against this port.
        env["BD_CDP_PORT"] = str(TEST_CDP_PORT)
        # Isolation wall for ANY daemon this skill process might spawn
        # (session_create._ensure_daemon_running / coherence respawn): without
        # BD_EXTENSION_PORT the spawned `serve` binds the PRODUCTION relay port
        # 19989 — the user's real Chrome extension dials that, and sibling
        # test daemons' startup reclaim then fight/kill it. Pin the test relay
        # port for cdp skills too (the cdp daemon simply never binds it).
        env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    if extra_env:
        env.update(extra_env)
    if not env.get("BW_DAEMON_URL"):
        # Refuse to run un-addressed: with no endpoint pinned the skill would
        # resolve the DEFAULT (:19990) — the developer's own daemon — and drive
        # their real browser. A caller that wiped the runtime dir on purpose
        # must pass the URL in `extra_env`.
        raise RuntimeError(
            "run_skill has no BW_DAEMON_URL: pass a `runtime_dir` whose daemon "
            "published its endpoint, or set BW_DAEMON_URL in `extra_env`")

    # P1 session model: inline `browserwright <<PY` refuses to run unless a
    # ledger session is explicitly in scope. E2E helpers create a lightweight
    # ledger record directly in the isolated BS_HOME so tests don't depend on
    # the developer's session state. The record carries only the session's
    # backend — the single daemon routes per session (no daemon_endpoint).
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
            [str(skill_bin), "-s", env["BD_SESSION"], "--code-stdin"],
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
