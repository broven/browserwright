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
(``end-session`` / ``kill-executor`` / ``attach-active``), run through
:func:`daemon_lifecycle.run_verb`; starting or replacing the daemon itself is
:func:`daemon_lifecycle.ensure`'s job alone.

Ownership rule: who ``create``s, closes; ``attach`` only reminds.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Optional

from . import daemon_lifecycle as lifecycle
from . import session_registry as reg


def _free_port() -> int:
    """Ask the OS for an unused localhost TCP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


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
    # A timed-out verb is a failure (VerbResult exit 3), never a crash: the
    # row is kept for retry, and the retry joins the daemon-side teardown
    # (issue #32 initiate contract).
    return lifecycle.run_verb(["end-session", "--session", str(sid)],
                              timeout=_END_SESSION_CLI_TIMEOUT).returncode == 0


#: How long `session reset` keeps retrying `kill-executor` against a daemon
#: that answers but has not finished starting (issue #40 after a SIGKILL).
_RESET_DAEMON_STARTUP_S = 10.0


def reset_executor(record: dict) -> str:
    """Recycle only this session's resident executor.

    The session ledger entry, browser, context, tabs, and ownership semantics
    are intentionally left intact. The next ``browserwright -s <id> -e ...``
    call cold-starts a fresh executor against the same session.
    """
    lifecycle.ensure("session reset")
    sid = record["id"]
    args = ["kill-executor", "--session", str(sid)]
    rc = lifecycle.run_verb(args).returncode
    # `lifecycle.ensure` returns once a spawned daemon answers, which can be
    # while it is still starting (and still sweeping the old one's orphans).
    # Retry while it answers; a daemon that does not answer falls through to
    # the local reap below at once.
    deadline = time.monotonic() + _RESET_DAEMON_STARTUP_S
    while (rc != 0 and time.monotonic() < deadline
           and lifecycle.diagnose().up):
        time.sleep(0.5)
        rc = lifecycle.run_verb(args).returncode
    if rc != 0:
        # Issue #40: when the daemon is unreachable, the executor cannot be
        # reaped through it and the session is stuck — the orphan blocks the
        # next bind with a CDP attach conflict and no retry can clear it.
        # Reap it locally from the discovery record instead. (When the daemon
        # IS up, the daemon path is authoritative and a failed confirm keeps
        # the existing retry semantics.)
        if not lifecycle.diagnose().up:
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
                "check the global daemon with `browserwright-daemon status` "
                "and `browserwright doctor` (reset starts one on the default "
                "endpoint; doctor says why that did not happen), then retry "
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
    lifecycle.ensure("session attach-active")
    sid = str(record.get("id"))
    proc = lifecycle.run_verb(["attach-active", "--session", sid, "--json"],
                              timeout=15.0)
    if proc.timed_out:
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
    payload = proc.json() or {}
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


_REUSABLE_RECOVERY_STATES = frozenset({
    "healthy",
    "extension-disconnected",
    "tab-gone",
    "executor-unbound",
    "executor-dead",
})


def _recovery_is_reusable(row: dict) -> bool:
    """Whether a ledger row can converge without human intervention.

    Rows written before recovery state existed remain reusable.  Once a daemon
    has persisted a diagnosis, only the known healthy/recoverable states are
    eligible; ``needs-human`` and malformed/unknown diagnoses must not hand the
    agent straight back to a session it cannot repair.
    """
    if "recovery" not in row:
        return True
    recovery = row.get("recovery")
    return (
        isinstance(recovery, dict)
        and recovery.get("state") in _REUSABLE_RECOVERY_STATES
    )


def find_reusable(*, backend: str, name: str,
                  owner: Optional[str] = None) -> Optional[dict]:
    """The most recent ledger session with this ``backend`` and ``name``
    (and, when given, ``owner`` — so a cdp ``--attach`` request never gets
    back a ``--create`` session of the same name, or vice versa).

    ``session new --reuse`` (ADR-0013 rule 4) hands an agent back a healthy or
    daemon-recoverable session instead of a second one.  Legacy rows without a
    recovery record remain eligible. ``session end`` removes the row, so an
    ended session is never matched.
    """
    matches = [
        r for r in reg.list_all()
        if r.get("backend") == backend and r.get("name") == name
        and (owner is None or r.get("owner") == owner)
        and _recovery_is_reusable(r)
    ]
    return matches[-1] if matches else None


@dataclass(frozen=True)
class NewSession:
    """What :func:`new` did: the session id, whether ``--reuse`` handed back
    an existing one, and the daemon verdict after :func:`daemon_lifecycle.ensure`."""

    id: str
    reused: bool
    daemon: lifecycle.DaemonVerdict


def new(*, backend: str, create: bool = False, attach: Optional[object] = None,
        name: Optional[str] = None, reuse: bool = False) -> NewSession:
    """Register a session (or, with ``reuse``, find one) and ensure the daemon.

    With ``reuse`` and an existing session of the same backend and name, no
    new row is allocated: the existing id comes back with ``reused=True``, so
    the CLI can say so.

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
    running the installed version (this is where version coherence is
    enforced). The daemon does the Chrome launch on ``ensureSession``.
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
    if reuse and backend in ("extension", "cdp"):
        owner = None
        if backend == "cdp":
            owner = "create" if create else ("attach" if attach is not None else None)
        existing = find_reusable(backend=backend, name=name, owner=owner)
        if existing is not None:
            sid = str(existing["id"])
            reg.touch(sid)
            return NewSession(sid, True, lifecycle.ensure("session new"))
    if backend == "extension":
        sid = reg.allocate(backend="extension",
                           owner="attach", name=name)
        return NewSession(sid, False, lifecycle.ensure("session new"))
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
        return NewSession(sid, False, lifecycle.ensure("session new"))
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
    if record.get("backend") == "extension" and not lifecycle.diagnose().up:
        lifecycle.ensure("session teardown")
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
        daemon_up = lifecycle.diagnose().up
        if not daemon_up:
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
        if not daemon_up:
            hint = (
                " No daemon is answering on this XDG_RUNTIME_DIR; "
                "`browserwright recover --session "
                f"{sid}` starts the installed one (`browserwright doctor` says "
                "why none is running). Then retry ending this session."
            )
        raise DaemonUnavailable(
            f"session {sid} termination was incomplete; its ledger entry was "
            f"kept for retry.{hint}")
    if record.get("owner") == "create":
        msg = f"session {sid} ended; the browser it launched was closed."
    elif record.get("backend") == "extension":
        # BUG B: the `attach` wording below is a cdp story — it claims nothing
        # in the browser was touched, while the daemon has just closed every
        # tab in this session's tab group. Reporting "left untouched" for a
        # teardown that closes tabs is how a leaked tab and a torn-down one
        # became indistinguishable to the user.
        msg = (f"session {sid} ended; its tab group was closed. Your Chrome "
               f"is still running — only this session's tabs were touched.")
    else:
        msg = (f"session {sid} ended. The browser is still running — you "
               f"attached to it, so it was left untouched.")
    reg.remove(sid)
    return msg
