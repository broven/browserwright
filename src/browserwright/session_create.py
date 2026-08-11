"""Session creation/teardown per backend.

Creation is **explicit**: an agent picks ``extension`` / ``cdp --create`` /
``cdp --attach``. This module allocates the ledger entry and makes sure the
ONE global daemon is running.

Single-daemon model (docs/refactor-single-daemon.md §P3): there is exactly one
global daemon on a fixed socket (no ``--name`` / ``BD_NAME``). It serves both
backends simultaneously, routing per session. For cdp the daemon itself launches
and owns the per-session Chrome on ``ensureSession`` and tears it down on
``endSession`` — this module no longer spawns a per-session daemon or launches
Chrome directly. ``new()`` only:
  - allocates the ledger entry (recording the chosen port in ``workspace`` so
    the daemon pins the cdp Chrome to it), and
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


def _reap_executor_locally(session_id: str) -> dict | None:
    """Daemon-independent executor reap for the "daemon is gone" case (issue
    #40). Used ONLY when the daemon is unreachable — the daemon is the
    executor's owner while it is alive, and this path must never race it.

    Reads the session's on-disk executor discovery record (written by the
    executor itself) and:

    - no record        → nothing to reap; the executor is provably gone.
    - record, pid dead → stale files only; purge them, provably gone.
    - record, pid live → fingerprint-guarded TERM→KILL reap, the same graded
      discipline as the daemon's startup orphan sweep (a start-time mismatch
      means the pid was recycled and is NOT signalled).

    Returns a short dict describing what was done (``state`` is ``"absent"`` /
    ``"gone"`` / ``"reaped"``, ``pid`` the executor pid or None), or ``None``
    when the executor could not be provably reaped — the caller keeps the
    existing "kept for retry" semantics in that case.
    """
    from .daemon import _ipc
    from .daemon.server.executor_registry import _terminate_orphan_and_wait
    from .daemon.supervise import pid_alive

    record = _ipc.read_executor_record(session_id)
    if record is None:
        return {"state": "absent", "pid": None}
    pid = int(record["pid"])
    if not pid_alive(pid):
        _ipc.cleanup_executor(session_id)
        return {"state": "gone", "pid": pid}
    if not _terminate_orphan_and_wait(pid, record.get("start_time")):
        return None
    _ipc.cleanup_executor(session_id)
    return {"state": "reaped", "pid": pid}


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
_END_SESSION_CLI_TIMEOUT = 80.0


def _end_daemon_session(record: dict) -> bool:
    """End every session through the daemon's atomic terminal lifecycle.

    The daemon, not Layer 2, applies workspace ownership: extension closes its
    group, cdp create closes its Chrome, while cdp attach keeps the external
    browser.  All three still revoke live control/facade clients and reap the
    executor before this function confirms success.
    """
    sid = record.get("id")
    if not sid:
        return True
    cmd = ["browserwright-daemon", "end-session", "--session", str(sid)]
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
        # Issue #40: when the daemon is unreachable, the executor cannot be
        # reaped through it and the session is stuck — the orphan blocks the
        # next bind with a CDP attach conflict and no retry can clear it.
        # Reap it locally from the discovery record instead. (When the daemon
        # IS up, the daemon path is authoritative and a failed confirm keeps
        # the existing retry semantics.)
        if not _daemon_is_running():
            outcome = _reap_executor_locally(sid)
            if outcome is not None:
                return (
                    f"session {sid} reset; the executor was recycled locally "
                    f"({outcome['state']}, pid={outcome['pid']}) because the "
                    "daemon was unreachable. The browser and tabs were left "
                    "untouched."
                )
        from .errors import DaemonUnavailable

        raise DaemonUnavailable(
            "session reset could not confirm that the old executor exited",
            fix=(
                "check the global daemon is up with `browserwright-daemon status` "
                "(start it with `browserwright-daemon serve` if not), then retry "
                f"`browserwright session reset {sid}`"
            ),
        )
    return (
        f"session {sid} reset; executor was recycled. "
        "The browser and tabs were left untouched."
    )


def attach_active(record: dict, *, json_out: bool = False) -> str:
    """Adopt the focused-window active tab into a session's tab group.

    The agent-side wrapper for ``browserwright-daemon attach-active``: the
    daemon asks the extension to MOVE the currently-focused-window active tab
    into this session's tab group and attach it, so the agent drives the page
    the user is actually looking at. The adopted tab is a regular group
    member — no borrowed flag — so it closes with the group on ``session
    end`` exactly like an agent-opened tab, and a tab already sitting in any
    other tab group (the user's own manual groups included) is refused as
    "occupied"; the user must drag it out first.

    For ``cdp`` sessions the daemon's honest equivalent applies: the session
    is bound to the browser's current page.

    Returns a human-readable confirmation line, or the daemon's payload as
    JSON when ``json_out`` is set.
    """
    _ensure_daemon_running()
    sid = str(record.get("id"))
    cmd = ["browserwright-daemon", "attach-active", "--session", sid, "--json"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15.0)
    except subprocess.TimeoutExpired:
        from .errors import DaemonUnavailable

        raise DaemonUnavailable(
            f"attach-active timed out after 15s for session {sid}",
            fix="retry; if it persists, run `browserwright-daemon status`",
        )
    if proc.returncode != 0:
        # The daemon CLI prints the reason; propagate it verbatim so the
        # agent sees the "tab is in a group — drag it out" guidance.
        from .errors import BrowserwrightError

        msg = proc.stderr.strip() or (
            f"attach-active failed (exit {proc.returncode})")
        raise BrowserwrightError(msg)
    try:
        payload = json.loads(proc.stdout.strip())
    except ValueError:
        payload = {}
    if json_out:
        return json.dumps(payload, sort_keys=True)
    title = payload.get("title") or "(untitled)"
    url = payload.get("url") or ""
    return (
        f"session {sid} adopted the active tab "
        f"(tabId={payload.get('tabId')}, groupId={payload.get('groupId')}): "
        f"{title} — {url}"
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
    - ``cdp --create`` → owns an isolated browser the daemon launches on
      ``ensureSession``. We pick a free port now and record it in ``workspace``
      so the daemon pins the per-session Chrome to it.
    - ``cdp --attach <port|url>`` → attaches to a browser someone else owns; the
      endpoint is recorded and the browser is left alone on end. A port means
      "on this machine"; a ws/http URL means "wherever this points" — a cloud
      or anti-detect browser (#38).

    In every case we only allocate the ledger entry + ensure the one daemon is
    running. The daemon does the Chrome launch on ``ensureSession``.
    """
    name = name.strip() if isinstance(name, str) else None
    if not name:
        raise ValueError(
            "session new requires --name=NAME — a short label (e.g. "
            "--name=cf-bots). For extension sessions this becomes the Chrome "
            "tab group title; for CDP sessions it labels the isolated browser "
            "session. It need not be unique."
        )
    # #38: a ledger carrying retired-backend rows would list them forever.
    # Sweeping here means the first thing a user does after upgrading clears
    # them, not only a daemon restart.
    reg.migrate_legacy_backends()
    if backend == "extension":
        sid = reg.allocate(backend="extension",
                           owner="attach", name=name)
        _ensure_daemon_running()
        return sid
    if backend == "cdp":
        if create and attach is not None:
            raise ValueError(
                "--create and --attach are mutually exclusive: --create launches "
                "a browser we own, --attach borrows one we don't.")
        if not create and attach is None:
            raise ValueError(
                "--backend=cdp needs either --create (launch an isolated browser) "
                "or --attach=<port|url> (use one that is already running).")
        owner = "create" if create else "attach"
        if create:
            # Pick a free port now so the daemon pins the Chrome it launches.
            workspace = {"port": _free_port()}
        else:
            port, endpoint = _checked_attach(attach)
            workspace = {"port": port} if port is not None else {"url": endpoint}
        sid = reg.allocate(backend="cdp", owner=owner,
                           name=name, workspace=workspace)
        _ensure_daemon_running()
        return sid
    raise ValueError(_unknown_backend_message(backend))


def _checked_attach(attach: object) -> tuple[Optional[int], Optional[str]]:
    """Validate `--attach`, translating the daemon's error type to this layer's.

    The validator lives beside `check_name` in `daemon/config.py` and raises
    `UserError`; Layer 2's contract with the CLI is `ValueError`, which
    `cli._cmd_session` catches for the clean exit-1 path. Without the
    translation a bad `--attach` prints a traceback instead of the message.
    """
    from .daemon.config import check_cdp_attach
    from .daemon.errors import UserError

    try:
        return check_cdp_attach(attach)
    except UserError as e:
        raise ValueError(str(e)) from e


def _unknown_backend_message(backend: object) -> str:
    """Name the replacement, not just the rejection (#38).

    `rdp` and `env` (the historical names) were one real-CDP backend differing
    only in where the ws URL came from; they are now both `cdp`. Anyone with
    either in a script needs to be told what to write instead, which a bare
    "invalid choice" never says. The migration text itself lives in Layer 1
    beside the rest of the backend vocabulary, so `browserwright-daemon` can
    print the identical thing.
    """
    from .daemon.config import retired_backend_message

    return (retired_backend_message(backend)
            or f"unknown backend {backend!r} (use extension|cdp)")


def end(record: dict) -> str:
    """Tear down a session honoring ownership. Returns a human-readable line.

    create-owned → the daemon closes the browser it launched (endSession).
    attach       → leave the browser running, remind the user.
    extension    → also close the session's agent-owned tabs (browser stays).
    Removes the ledger entry only after the daemon confirms executor, clients,
    and ownership-aware workspace teardown all completed.
    """
    sid = record["id"]
    # #38: a row naming a retired backend can never be ended through the
    # daemon — it refuses to route one, so the RPC fails, the row is kept "for
    # retry", and every retry repeats that. Clear it here instead of asking the
    # daemon a question with no answer.
    if record.get("backend") not in ("extension", "cdp"):
        reg.remove(sid)
        return (
            f"session {sid} removed from the ledger. Its backend "
            f"{record.get('backend')!r} no longer exists, so there was nothing "
            "for the daemon to tear down. Any browser it used is untouched; if "
            "it was a create-owned Chrome, the daemon sweeps its `bs-s"
            f"{sid}` profile on next start."
        )
    # Terminal teardown needs the daemon, and after a crash, reboot or a
    # foreground `serve` exiting there may not be one — in which case the RPC
    # fails, the entry is kept "for retry", and the retry takes this same path
    # to the same failure. A record that `session end` cannot clear.
    #
    # Only `extension` is auto-recovered by starting one, because a bare
    # `serve` IS the extension daemon.
    #
    # This restriction used to also protect `env`, whose daemon carried
    # per-profile configuration a bare `serve` could not reproduce. That reason
    # is gone with the backend (#38), and an `cdp` session now routes to its own
    # context whatever the shared backend is — so a bare daemon *could* serve
    # one. Broadening this is deliberately left alone: for `--create` the
    # launched Chrome's pid died with the old daemon, so recovery really means
    # the startup orphan sweep, which is a different mechanism with different
    # failure modes than "spawn a daemon and retry the RPC".
    if record.get("backend") == "extension" and not _daemon_is_running():
        _ensure_daemon_running()
    if not _end_daemon_session(record):
        from .errors import DaemonUnavailable

        # Issue #40: when the daemon is unreachable, "kept for retry" is a
        # dead end — the retry hits the same wall, the orphaned executor
        # blocks the next bind with a CDP attach conflict, and the row leaks
        # forever. If the executor is provably gone (or was reaped locally),
        # force-drop the ledger entry instead. The workspace could not be
        # torn down without the daemon, so say so honestly. When the daemon
        # IS up, its teardown is authoritative and a failed confirm keeps the
        # retry semantics (issue #32).
        if not _daemon_is_running():
            outcome = _reap_executor_locally(sid)
            if outcome is not None:
                reg.remove(sid)
                return (
                    f"session {sid} ended (daemon was down): the executor was "
                    f"{outcome['state']} (pid={outcome['pid']}) and the ledger "
                    "entry was removed. The workspace was left as-is — no "
                    "daemon was reachable to close it; close any leftover "
                    "browser tabs/windows manually."
                )
        # The row stays. An unreachable daemon cannot prove clients were
        # revoked, and dropping the row for a session whose external browser
        # may still have live clients driving it is worse than leaving one that
        # needs another attempt.
        hint = ""
        if not _daemon_is_running():
            hint = (
                " No daemon is answering on this XDG_RUNTIME_DIR. Start one "
                "(`browserwright-daemon serve`) — using the same "
                "XDG_RUNTIME_DIR — then run `session end` again."
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
