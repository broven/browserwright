"""Session creation/teardown per backend.

Creation is **explicit**: an agent picks ``extension`` / ``rdp --create`` /
``rdp --attach``. This module allocates the ledger entry and makes sure the
ONE global daemon is running.

Single-daemon model (docs/refactor-single-daemon.md §P3): there is exactly one
global daemon on a fixed socket (no ``--name`` / ``BD_NAME``). It serves both
backends simultaneously, routing per session. For rdp the daemon itself launches
and owns the per-session Chrome on ``ensureSession`` and tears it down on
``endSession`` — this module no longer spawns a per-session daemon or launches
Chrome directly. ``new()`` only:
  - allocates the ledger entry (recording the chosen port in ``workspace`` so
    the daemon pins the rdp Chrome to it), and
  - ensures the single daemon is up.

Teardown talks to the single daemon via the ``browserwright-daemon`` CLI
(``end-session`` / ``disconnect``), which the daemon already understands — no
``--name`` is passed anymore.

Ownership rule: who ``create``s, closes; ``attach`` only reminds.
"""
from __future__ import annotations

import socket
import subprocess
from typing import Optional

from . import session_registry as reg


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


def _ensure_daemon_running() -> None:
    """Make sure the ONE global daemon is up; spawn ``serve`` detached if not.

    There is no ``--name`` anymore — a single fixed-socket daemon serves every
    session. ``serve`` itself stale-detects an already-running daemon and exits
    1, so spawning unconditionally is safe (a redundant spawn is a no-op), but
    we ping first to avoid the churn.
    """
    from .daemon import _ipc
    from .version import package_version
    try:
        pid, running_version = _ipc.ping_status_sync(timeout=1.0)
        if pid is not None and running_version == package_version():
            return  # already running the installed version
        if pid is not None:
            _run(["browserwright-daemon", "stop"])
    except Exception:
        pass
    _spawn_detached(["browserwright-daemon", "serve"])


def _close_browser(record: dict) -> None:
    """Tear down a create-owned rdp session's browser via the single daemon.

    The daemon owns the per-session Chrome (launched on ``ensureSession``), so
    ``endSession`` closes the upstream + SIGTERMs that Chrome + drops the
    context. Best-effort: a dead daemon just means the (ephemeral, C2) Chrome
    already died with it. Only create-owned sessions reach here — attach
    sessions never launched a browser we own."""
    sid = record.get("id")
    if sid:
        _run(["browserwright-daemon", "end-session", "--session", str(sid)])


def _reap_executor(record: dict) -> None:
    """Best-effort: reap this session's resident Phase B executor (no browser
    teardown). Called for EVERY owner on `end()` so an attach session's
    long-lived executor subprocess doesn't leak — the full `endSession` path is
    create-only and would also close the browser an attach session must keep.

    Best-effort by contract: a dead daemon / no-executor / stale binary all
    return non-zero from `_run`, which we ignore — `session end` must never fail
    because the executor couldn't be reaped (the orphan-sweep on the next daemon
    start is the backstop)."""
    sid = record.get("id")
    if sid:
        _run(["browserwright-daemon", "kill-executor", "--session", str(sid)])


def reset_executor(record: dict) -> str:
    """Recycle only this session's resident executor.

    The session ledger entry, browser, context, tabs, and ownership semantics
    are intentionally left intact. The next ``browserwright -s <id> -e ...``
    call cold-starts a fresh executor against the same session.
    """
    _ensure_daemon_running()
    _reap_executor(record)
    sid = record["id"]
    return (
        f"session {sid} reset; executor was recycled. "
        "The browser and tabs were left untouched."
    )


def reap(*, idle_seconds: float) -> list[dict]:
    """Prune idle sessions; for create-owned ones, also tear down the browser
    the daemon launched. Returns the pruned records."""
    pruned = reg.prune(idle_seconds=idle_seconds)
    for rec in pruned:
        if rec.get("owner") == "create":
            _close_browser(rec)
    return pruned


def new(*, backend: str, create: bool = False, attach: Optional[object] = None,
        name: Optional[str] = None) -> str:
    """Register a session and return its id.

    - ``extension`` → an *attach* session sharing the one global daemon's
      relay-backed upstream; the tab group is created lazily on first use, so
      ``workspace`` is None.
    - ``rdp --create`` → owns an isolated browser the daemon launches on
      ``ensureSession``. We pick a free port now and record it in ``workspace``
      so the daemon pins the per-session Chrome to it.
    - ``rdp --attach <target>`` → attaches to an already-running browser; the
      target (port) is recorded and the browser is left alone on end.

    In every case we only allocate the ledger entry + ensure the one daemon is
    running. The daemon does the Chrome launch on ``ensureSession``.
    """
    name = name.strip() if isinstance(name, str) else None
    if not name:
        raise ValueError(
            "session new requires --name=NAME — a short label (e.g. "
            "--name=cf-bots) that becomes the Chrome tab group title. It need "
            "not be unique; the session is bound internally by its numeric "
            "tab-group id, not the name."
        )
    if backend == "extension":
        sid = reg.allocate(backend="extension",
                           owner="attach", name=name)
        _ensure_daemon_running()
        return sid
    if backend == "rdp":
        owner = "create" if create else "attach"
        # workspace["port"]: for --create pick a free port the daemon launches
        # Chrome on; for --attach record the target port the daemon resolves.
        if create:
            workspace = {"port": _free_port()}
        elif attach is not None:
            workspace = {"port": int(attach), "target": attach}
        else:
            workspace = None
        sid = reg.allocate(backend="rdp", owner=owner,
                           name=name, workspace=workspace)
        _ensure_daemon_running()
        return sid
    raise ValueError(f"unknown backend {backend!r} (use extension|rdp)")


def choose(situation: str) -> dict:
    """Decide how to start a session for ``situation``.

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
    """Close the session's agent-owned extension tabs via the single daemon.

    Best-effort: the shared browser itself stays open (extension sessions are
    attach-owned); only the tabs this session opened are closed. The
    ``end-session`` CLI no longer takes ``--name`` — there is one daemon."""
    cmd = ["browserwright-daemon", "end-session", "--session", record["id"]]
    # Thread the durable numeric groupId (persisted in ledger.runtime on every
    # open) so the daemon can close the whole group even when its in-memory
    # binding was wiped (restart). The title is not used — names aren't unique.
    runtime = record.get("runtime") or {}
    gid = runtime.get("group_id")
    if isinstance(gid, int) and gid >= 0:
        cmd += ["--group-id", str(gid)]
    _run(cmd)


def end(record: dict) -> str:
    """Tear down a session honoring ownership. Returns a human-readable line.

    create-owned → the daemon closes the browser it launched (endSession).
    attach       → leave the browser running, remind the user.
    extension    → also close the session's agent-owned tabs (browser stays).
    Always removes the ledger entry.
    """
    sid = record["id"]
    if record.get("backend") == "extension":
        _end_extension_workspace(record)
    if record.get("owner") == "create":
        # `_close_browser` → daemon `endSession`, which ALSO kills the executor
        # (symmetric in `_handle_end_session`), so no separate reap needed here.
        _close_browser(record)
        msg = f"session {sid} ended; the browser it launched was closed."
    else:
        # attach: leave the browser running (semantics unchanged) but still reap
        # the session's resident executor so it doesn't leak — `endSession` is
        # create-only and the attach path never otherwise contacts the daemon.
        _reap_executor(record)
        msg = (f"session {sid} ended. The browser is still running — you "
               f"attached to it, so it was left untouched.")
    reg.remove(sid)
    return msg
