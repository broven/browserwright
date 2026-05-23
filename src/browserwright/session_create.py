"""Session creation/teardown per backend (P2 + P3 + P6).

Creation is **explicit**: an agent picks ``extension`` / ``rdp --create`` /
``rdp --attach``. This module allocates the ledger entry and (for rdp) launches
or points the per-session daemon (P6). The subprocess seams (``_spawn_detached``,
``_run``) are factored out so tests can mock them and assert only the issued
commands + ledger bookkeeping — there's no live Chrome in CI.

Ownership rule (P3): who ``create``s, closes; ``attach`` only reminds.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
from typing import Optional

from . import session_registry as reg


def _shared_extension_endpoint() -> str:
    """Extension multiplexes ONE shared daemon across sessions; its endpoint is
    that daemon's name — the live ``BD_NAME`` (default ``"default"``), NOT a
    hardcoded constant. Hardcoding ``"default"`` made sessions target the wrong
    socket whenever the daemon ran under a different name."""
    return os.environ.get("BD_NAME", "default")


def _rdp_endpoint(session_id: str) -> str:
    """1:1 daemon name for an rdp session."""
    return f"browserwright-daemon-s{session_id}"


def _free_port() -> int:
    """Ask the OS for an unused localhost TCP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _spawn_detached(cmd: list[str]) -> int:
    """Start a long-lived background process detached from this one; return pid."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    return proc.pid


def _run(cmd: list[str]) -> int:
    """Run a short-lived command; return its exit code (best-effort)."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1


def _launch_daemon(session_id: str, *, create: bool, target) -> None:
    """Launch (create) or point (attach) the per-session rdp daemon (P6).

    create → launch an isolated Chrome on a fresh port + a daemon named for the
    session bound to it; record the port + Chrome pid in the ledger workspace.
    attach → point a session-named daemon at an already-running browser's port.
    """
    endpoint = _rdp_endpoint(session_id)
    if create:
        port = int(target) if target else _free_port()
        chrome_pid = _spawn_detached([
            "browserwright-daemon", "launch-chrome",
            "--port", str(port), "--profile", f"bs-s{session_id}",
        ])
        _spawn_detached([
            "browserwright-daemon", "serve",
            "--backend", "rdp", "--name", endpoint, "--port", str(port),
        ])
        reg.update(session_id, workspace={"port": port, "chrome_pid": chrome_pid})
    elif target is not None:
        port = int(target)
        _spawn_detached([
            "browserwright-daemon", "serve",
            "--backend", "rdp", "--name", endpoint, "--port", str(port),
        ])


def _close_browser(record: dict) -> None:
    """Stop the session's daemon and kill the Chrome it launched (P6).

    Only meaningful for create-owned sessions — attach sessions never reach
    here (``end`` reminds instead)."""
    endpoint = record.get("daemon_endpoint")
    if endpoint:
        _run(["browserwright-daemon", "stop", "--name", endpoint])
    ws = record.get("workspace") or {}
    pid = ws.get("chrome_pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def reap(*, idle_seconds: float) -> list[dict]:
    """Prune idle sessions; for create-owned ones, also stop their daemon +
    Chrome (P6.4). Returns the pruned records."""
    pruned = reg.prune(idle_seconds=idle_seconds)
    for rec in pruned:
        if rec.get("owner") == "create":
            _close_browser(rec)
    return pruned


def new(*, backend: str, create: bool = False, attach: Optional[object] = None,
        name: Optional[str] = None) -> str:
    """Register a session and return its id.

    - ``extension`` → an *attach* session sharing the default daemon; the tab
      group is created lazily on first use (Phase 5), so ``workspace`` is None.
    - ``rdp --create`` → owns a freshly-launched isolated browser+daemon.
    - ``rdp --attach <target>`` → attaches to an already-running browser; the
      target (port/recipe) is recorded and the browser is left alone on end.
    """
    name = name.strip() if isinstance(name, str) else None
    if not name:
        raise ValueError(
            "session new requires --name=NAME — a short, globally-unique label "
            "(e.g. --name=cf-bots). It becomes the Chrome tab group title and "
            "the reconnect-recovery anchor for this session."
        )
    if backend == "extension":
        return reg.allocate(backend="extension",
                            daemon_endpoint=_shared_extension_endpoint(),
                            owner="attach", name=name, unique_name=True)
    if backend == "rdp":
        owner = "create" if create else "attach"
        workspace = {"target": attach} if attach is not None else None
        sid = reg.allocate(backend="rdp", daemon_endpoint="", owner=owner,
                           name=name, workspace=workspace, unique_name=True)
        reg.update(sid, daemon_endpoint=_rdp_endpoint(sid))
        _launch_daemon(sid, create=create, target=attach)
        return sid
    raise ValueError(f"unknown backend {backend!r} (use extension|rdp)")


def choose(situation: str) -> dict:
    """Decide how to start a session for ``situation`` (P7).

    Hit → return the recorded decision (auto-start). Miss → raise
    :class:`NeedsUserConfirm` carrying a proposal that lists the three modes,
    so the agent asks the user and then records the answer.
    """
    from .errors import NeedsUserConfirm
    from .memory import session_decisions

    hit = session_decisions.lookup(situation)
    if hit is not None:
        return hit
    raise NeedsUserConfirm(
        what=f"how to start a browser session for: {situation}",
        proposal={
            "situation": situation,
            "options": [
                {"backend": "extension", "mode": "attach",
                 "desc": "drive the user's everyday Chrome via the extension (shared)"},
                {"backend": "rdp", "mode": "create",
                 "desc": "launch a fresh isolated Chrome the session owns"},
                {"backend": "rdp", "mode": "attach", "target": "<port|recipe>",
                 "desc": "attach to an already-running browser (e.g. a fingerprint browser)"},
            ],
            "after_choice": "record it via memory.session_decisions.record(situation, decision)",
        },
    )


def _end_extension_workspace(record: dict) -> None:
    """Close the session's agent-owned extension tabs via the daemon (P5).

    Best-effort: the shared browser itself stays open (extension sessions are
    attach-owned); only the background tabs this session opened are closed,
    borrowed tabs are kept."""
    endpoint = record.get("daemon_endpoint") or _shared_extension_endpoint()
    cmd = ["browserwright-daemon", "end-session", "--name", endpoint,
           "--session", record["id"]]
    # Thread the durable group title so the daemon can fall back to closing
    # tabs by group when its in-memory owned-tab table was wiped (restart).
    group_name = record.get("name")
    if group_name:
        cmd += ["--group-name", group_name]
    _run(cmd)


def end(record: dict) -> str:
    """Tear down a session honoring ownership. Returns a human-readable line.

    create-owned → close the browser/daemon we launched.
    attach       → leave the browser running, remind the user.
    extension    → also close the session's agent-owned tabs (browser stays).
    Always removes the ledger entry.
    """
    sid = record["id"]
    if record.get("backend") == "extension":
        _end_extension_workspace(record)
    if record.get("owner") == "create":
        _close_browser(record)
        msg = f"session {sid} ended; the browser it launched was closed."
    else:
        msg = (f"session {sid} ended. The browser is still running — you "
               f"attached to it, so it was left untouched.")
    reg.remove(sid)
    return msg
