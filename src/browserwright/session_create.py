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

import json
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


def _run(cmd: list[str], timeout: float = 10.0) -> int:
    """Run a short-lived command; return its exit code (best-effort).
    A timed-out child is reported as failure (exit 3), never a crash — the
    caller keeps the ledger row for retry, and the retry joins the daemon-side
    teardown (issue #32 initiate contract)."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode
    except FileNotFoundError:
        # The daemon CLI binary is missing from PATH — an environment problem.
        return 1
    except subprocess.TimeoutExpired:
        # The CLI was still working (end-session now legitimately covers
        # initiate + join, issue #32). Report failure so the ledger row is
        # kept for retry — the retry joins the daemon-side teardown.
        return 3


def _running_daemon_backend() -> str | None:
    """The shared backend of the daemon on this socket, or None if unknown.

    None means "could not establish", never "mismatch" — callers must not
    block a legitimate action on a probe they could not read.
    """
    try:
        out = subprocess.run(
            ["browserwright-daemon", "backend-info", "--json"],
            capture_output=True, timeout=10, text=True)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("running"):
        return None
    name = data.get("backend") or data.get("name")
    return name if isinstance(name, str) and name else None


def _daemon_is_running() -> bool:
    """True iff a daemon answers on this XDG_RUNTIME_DIR's socket right now.

    Deliberately does not care about version match — callers use it to decide
    whether teardown has anything to talk to, not whether to upgrade.
    """
    from .daemon import _ipc
    try:
        pid, _version = _ipc.ping_status_sync(timeout=1.0)
        return pid is not None
    except Exception:
        return False


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


#: How long `session end` may take end-to-end. The CLI under it implements
#: the issue #32 initiate-then-join contract: initiate is fast, the join
#: covers the daemon-side teardown worst case, and progress is printed while
#: waiting. Must be >= the CLI's own total wait budget.
_END_SESSION_CLI_TIMEOUT = 120.0


def _end_daemon_session(record: dict) -> bool:
    """End every session through the daemon's atomic terminal lifecycle.

    The daemon, not Layer 2, applies workspace ownership: extension closes its
    group, rdp create closes its Chrome, while rdp attach/env keep the external
    browser.  All four still revoke live control/facade clients and reap the
    executor before this function confirms success.
    """
    sid = record.get("id")
    if not sid:
        return True
    cmd = ["browserwright-daemon", "end-session", "--session", str(sid)]
    if record.get("backend") == "extension":
        runtime = record.get("runtime") or {}
        gid = runtime.get("group_id")
        if isinstance(gid, int) and gid >= 0:
            cmd += ["--group-id", str(gid)]
    return _run(cmd, timeout=_END_SESSION_CLI_TIMEOUT) == 0


def reset_executor(record: dict) -> str:
    """Recycle only this session's resident executor.

    The session ledger entry, browser, context, tabs, and ownership semantics
    are intentionally left intact. The next ``browserwright -s <id> -e ...``
    call cold-starts a fresh executor against the same session.
    """
    _ensure_daemon_running()
    sid = record["id"]
    rc = _run([
        "browserwright-daemon",
        "kill-executor",
        "--session",
        str(sid),
    ])
    if rc != 0:
        from .errors import DaemonUnavailable

        raise DaemonUnavailable(
            "session reset could not confirm that the old executor exited",
            fix=(
                "run `browserwright doctor`, then retry "
                f"`browserwright session reset {sid}`"
            ),
        )
    return (
        f"session {sid} reset; executor was recycled. "
        "The browser and tabs were left untouched."
    )


def reap(*, idle_seconds: float) -> list[dict]:
    """Prune idle sessions; for create-owned ones, also tear down the browser
    the daemon launched. Returns the pruned records."""
    stale = reg.stale(idle_seconds=idle_seconds)
    pruned: list[dict] = []
    for rec in stale:
        if not _end_daemon_session(rec):
            continue
        removed = reg.remove(str(rec.get("id")))
        if removed is not None:
            pruned.append(removed)
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
            "--name=cf-bots). For extension sessions this becomes the Chrome "
            "tab group title; for RDP sessions it labels the isolated browser "
            "session. It need not be unique."
        )
    if backend == "extension":
        sid = reg.allocate(backend="extension",
                           owner="attach", name=name)
        _ensure_daemon_running()
        return sid
    if backend == "env":
        # env binds the agent surface to the daemon's shared upstream — the
        # externally-owned browser the daemon was started against (BD_CDP_WS /
        # BD_CDP_URL + `--backend env`). Like extension it is attach-owned, so
        # end()/reap never close that external browser; unlike extension there
        # is no tab group (env speaks real browser-level CDP, not the relay).
        # workspace is None: env sessions route to the shared daemon context
        # (docs/session-workspaces.md §"Routing And Facade"), not a per-session
        # UpstreamContext. Driving N external profiles → one env-backed daemon
        # (isolated XDG_RUNTIME_DIR + --facade-port + BD_CDP_WS) per profile.
        from .daemon import _ipc

        # The guard and allocation share the ledger's file lock, so concurrent
        # creators cannot both observe an empty slot. Scope by the fixed daemon
        # socket (XDG_RUNTIME_DIR) rather than by the global BS_HOME ledger;
        # this preserves the documented N-isolated-daemon fleet pattern.
        daemon_scope = str(_ipc.sock_path().resolve())
        # Check the running daemon's backend BEFORE writing the row. The
        # routing guard rejects every connection whose record does not match
        # the shared context, so allocating first against, say, the default
        # extension daemon reports success and leaves a row that nothing —
        # not even endSession — can use, while it holds this daemon's only
        # env slot. An unreadable probe returns None and is not treated as a
        # mismatch: never block a legitimate create on a probe that failed.
        running = _running_daemon_backend()
        if running is not None and running != "env":
            raise ValueError(
                f"the daemon on this socket is serving backend {running!r}, "
                "so an env session created here could never connect. Start an "
                "env daemon with its own XDG_RUNTIME_DIR and BD_CDP_WS (see "
                "docs/session-workspaces.md, 'Scaling env to N profiles').")
        sid = reg.allocate(
            backend="env", owner="attach", name=name,
            env_daemon_scope=daemon_scope,
        )
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
    raise ValueError(f"unknown backend {backend!r} (use extension|rdp|env)")


def end(record: dict) -> str:
    """Tear down a session honoring ownership. Returns a human-readable line.

    create-owned → the daemon closes the browser it launched (endSession).
    attach       → leave the browser running, remind the user.
    extension    → also close the session's agent-owned tabs (browser stays).
    Removes the ledger entry only after the daemon confirms executor, clients,
    and ownership-aware workspace teardown all completed.
    """
    sid = record["id"]
    # Terminal teardown needs the daemon, and after a crash, reboot or a
    # foreground `serve` exiting there may not be one — in which case the RPC
    # fails, the entry is kept "for retry", and the retry takes this same path
    # to the same failure. A record that `session end` cannot clear.
    #
    # Only `extension` may be recovered by starting one, because a bare
    # `serve` IS the extension daemon. An env fleet's daemon carries per-profile
    # configuration (its own XDG_RUNTIME_DIR, BD_CDP_WS, facade port — see
    # docs/session-workspaces.md §"Scaling env to N profiles"); spawning a bare
    # one would come up on the wrong backend and get the end-session rejected
    # for a mismatch, leaving the record AND still holding the single env slot.
    if record.get("backend") == "extension" and not _daemon_is_running():
        # A bare `serve` IS the extension daemon, so starting one recovers this
        # case. Never do this for env: that fleet's daemon carries per-profile
        # configuration (its own XDG_RUNTIME_DIR, BD_CDP_WS, facade port — see
        # docs/session-workspaces.md §"Scaling env to N profiles"), and a bare
        # one would come up on the wrong backend and have end-session rejected
        # for a mismatch — leaving the record AND still holding the env slot.
        _ensure_daemon_running()
    if not _end_daemon_session(record):
        from .errors import DaemonUnavailable

        # The row stays. An unreachable daemon cannot prove clients were
        # revoked, and dropping the row for a session whose external browser
        # may still have live clients driving it is worse than leaving one that
        # needs another attempt.
        #
        # env cannot be auto-recovered the way extension can: its daemon
        # carries per-profile configuration, so tell the user what to restart
        # instead of guessing. Without this the failure is a dead end — the
        # record also holds that daemon's single env slot.
        hint = ""
        if record.get("backend") == "env" and not _daemon_is_running():
            hint = (
                " No daemon is answering on this XDG_RUNTIME_DIR. Restart this "
                "profile's daemon with the same XDG_RUNTIME_DIR and BD_CDP_WS "
                "(see docs/session-workspaces.md, 'Scaling env to N profiles'), "
                "then run `session end` again."
            )
        raise DaemonUnavailable(
            f"session {sid} termination was incomplete; its ledger entry was "
            f"kept for retry.{hint}")
    if record.get("owner") == "create":
        msg = f"session {sid} ended; the browser it launched was closed."
    else:
        msg = (f"session {sid} ended. The browser is still running — you "
               f"attached to it, so it was left untouched.")
    reg.remove(sid)
    return msg
