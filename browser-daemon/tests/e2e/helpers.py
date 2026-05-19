"""Helpers for running browser-skill against the test daemon."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .conftest import TEST_EXT_PORT, TEST_NAME, TEST_RDP_PORT


@dataclass
class SkillResult:
    returncode: int
    stdout: str
    stderr: str


def run_skill(script: str, *, backend: str, extra_env: dict[str, str] | None = None,
              timeout: float = 30.0) -> SkillResult:
    """Invoke `browser-skill` with the given heredoc-style Python script.

    Sets env vars so the skill resolves the *test* daemon, not the user's
    production daemon:

        - `BS_DAEMON_URL_CMD`  -> invokes the test daemon's URL resolution
        - `BS_DAEMON_BACKEND`  -> pins the backend
        - `BD_NAME`            -> pins the daemon name

    Args:
        script: Python source the skill REPL will execute (heredoc body).
        backend: "extension" or "rdp".
        extra_env: extra env merged on top.
        timeout: subprocess timeout in seconds.

    Returns SkillResult (does NOT raise on non-zero exit; caller asserts).
    """
    if backend not in ("extension", "rdp"):
        raise ValueError(f"backend must be 'extension' or 'rdp', got {backend!r}")

    skill_bin = shutil.which("browser-skill")
    if not skill_bin:
        raise RuntimeError(
            "browser-skill not on PATH; install browser-skill in editable mode: "
            "`pip install -e browser-skill[test]`"
        )

    env = os.environ.copy()
    env["BD_NAME"] = TEST_NAME
    env["BS_DAEMON_BACKEND"] = backend
    # Bypass proxy for localhost
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    if backend == "extension":
        # BD_EXTENSION_PORT drives the relay port in `browser-daemon url`.
        # Without it, the url command falls through to DEFAULT_RELAY_PORT
        # (19989) — the user's production daemon. This is the isolation wall.
        env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
        env["BS_DAEMON_URL_CMD"] = (
            f"browser-daemon url --backend extension --name {TEST_NAME}"
        )
    else:  # rdp
        # Force Mode A for RDP: the session-scoped daemon serves 'extension',
        # so Mode B's backend-match check would reject 'rdp'. Mode A uses
        # BS_DAEMON_URL_CMD (or BS_CDP_WS if set by caller) to resolve directly.
        env["BS_DAEMON_MODE"] = "A"
        env["BS_DAEMON_URL_CMD"] = (
            f"browser-daemon url --backend rdp --port {TEST_RDP_PORT}"
        )
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [skill_bin],
        input=script,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    return SkillResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
