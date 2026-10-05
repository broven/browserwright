"""Helpers for running browserwright against the test daemon."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .conftest import (
    TEST_CDP_PORT,
    TEST_EXT_PORT,
    published_endpoint,
    scrubbed_env,
)


def bs_home(backend: str) -> Path:
    """The isolated BS_HOME (ledger + memory) every e2e caller of `backend`
    shares — and so does the long-lived test daemon for that backend."""
    return Path(__file__).resolve().parent / "_bs_home" / backend


@contextmanager
def locked_ledger(home: str | Path) -> Iterator[dict]:
    """Read-modify-write the ledger under BS_HOME `home` through the production
    lock.

    This is ``session_registry._locked`` pointed at the isolated BS_HOME, so a
    harness edit serializes with the CLI subprocesses allocating sessions in
    the same ledger, and it can only *edit* the ledger — never replace it.

    That distinction is load-bearing. The ledger's ``next_id`` is monotonic and
    never reused (CONTEXT.md, ``ledger``/``binding``), and the long-lived test
    daemon relies on it: it remembers every session id it has ended for its
    whole lifetime and refuses that id again. A harness that rewrote the file
    from scratch (``{"next_id": 1, ...}``) or deleted it rewound the counter,
    so the next numbered session a later test minted reused an id the daemon
    had already ended — ``ensureExecutor failed: browserwright session has
    ended``. Remove your row instead; leave the file and its counter alone.
    """
    from browserwright import session_registry

    prev = os.environ.get("BS_HOME")
    os.environ["BS_HOME"] = str(home)
    try:
        with session_registry._locked() as data:
            data.setdefault("sessions", {})
            yield data
    finally:
        if prev is None:
            os.environ.pop("BS_HOME", None)
        else:
            os.environ["BS_HOME"] = prev


def seed_ledger_session(home: str | Path, sid: str, *, backend: str,
                        name: str, owner: str = "attach") -> dict:
    """Add one session row (keyed by a caller-chosen, non-numeric id) to the
    ledger under `home`, keeping every other row and ``next_id``."""
    now = time.time()
    record = {
        "id": sid, "backend": backend, "workspace": None, "owner": owner,
        "name": name, "created_at": now, "last_seen": now,
    }
    with locked_ledger(home) as data:
        data["sessions"][sid] = record
    return record


def drop_ledger_session(home: str | Path, sid: str) -> None:
    """Remove one session row; the ledger file and ``next_id`` stay."""
    with locked_ledger(home) as data:
        data["sessions"].pop(sid, None)


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
    env["BS_HOME"] = str(bs_home(backend))
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
        # (daemon_lifecycle.ensure at `session new` / `recover`): without
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
        created_session_id = f"e2e-{uuid.uuid4().hex}"
        seed_ledger_session(env["BS_HOME"], created_session_id,
                            backend=backend, name="e2e-run-skill")
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
            drop_ledger_session(env["BS_HOME"], created_session_id)
    return SkillResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
