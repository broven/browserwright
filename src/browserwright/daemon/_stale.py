"""Detect + reclaim a stale / half-alive browserwright daemon (issue #15 P2).

A daemon killed mid-life — or one whose serve loop failed *after* binding its
relay/facade TCP ports — can linger holding those ports while its control
socket is dead. Then:

  - `browserwright-daemon status` lies (`not_running`) though a process holds
    the ports (issue #15, 2.1), and
  - a fresh `serve` crash-loops on `EADDRINUSE` because nothing reclaims the
    ports from the zombie (issue #15, 2.2).

These helpers give `status` a truthful third state and let `serve` take over
the ports from a *confirmed* browserwright daemon. The safety rule for any
kill: only ever signal a pid that lsof confirms is holding the port AND whose
command line confirms it is a browserwright daemon. When lsof is unavailable we
never auto-kill — we surface the pid-file pid for the user to handle.
"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

from .supervise import Outcome, pid_alive as _pid_alive, terminate


def daemon_tcp_ports(cfg) -> list[int]:
    """The relay + facade TCP ports a daemon binds, derived from ``cfg``.

    Best-effort + deduped — used by both `status` (to detect a port-holding
    zombie) and `serve` (to reclaim those ports before binding). Kept here so
    the two paths can never disagree on which ports matter."""
    ports: list[int] = []
    try:
        _, relay_port = cfg.backends.extension.resolved_host_port()
        if relay_port:
            ports.append(int(relay_port))
    except Exception:  # noqa: BLE001 - best-effort
        pass
    try:
        fp = cfg.resolved_facade_port()
        if fp:
            ports.append(int(fp))
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(ports))


def port_is_listening(host: str, port: int) -> bool:
    """True if something already holds ``host:port`` (a fresh bind fails).

    Uses a plain bind attempt (no ``SO_REUSEADDR``) so a live listener reliably
    trips ``EADDRINUSE``. Cheap, cross-platform, no external process."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return False  # we could bind → nobody holds it
    except OSError:
        return True
    finally:
        s.close()


def port_holder_pids(port: int) -> list[int]:
    """PIDs listening on TCP ``port`` (best-effort via ``lsof``).

    Returns ``[]`` when lsof is missing / errors / finds nothing — callers must
    treat an empty list as "unknown", never as "nobody"."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for tok in out.stdout.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def pid_is_browserwright_daemon(pid: int) -> bool:
    """True iff ``pid``'s command line looks like a browserwright daemon.

    The safety gate before any SIGTERM — we must never signal a stranger that
    happens to hold the port. Matches both the installed console-script
    (``browserwright-daemon serve``) and the dev module invocation
    (``python -m browserwright.daemon.cli serve``)."""
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    cmd = out.stdout.strip()
    if not cmd:
        return False
    return "browserwright-daemon" in cmd or "browserwright.daemon" in cmd


def pid_env(pid: int, name: str) -> str | None:
    """Best-effort read of one environment variable from another process.

    Linux: ``/proc/<pid>/environ``. macOS (BSD ps): ``ps eww -p <pid> -o
    command=`` appends the environment to the command column. Returns None when
    unreadable or unset — callers must treat None as "the daemon default",
    never as a confirmed value."""
    try:
        environ = Path(f"/proc/{pid}/environ")
        if environ.exists():
            for kv in environ.read_bytes().split(b"\0"):
                key, _sep, val = kv.partition(b"=")
                if key.decode(errors="replace") == name:
                    return val.decode(errors="replace")
            return None
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    for tok in out.stdout.split():
        key, _sep, val = tok.partition("=")
        if key == name:
            return val
    return None


def pid_runtime_dir(pid: int) -> str:
    """The runtime dir ``pid`` derives its control socket from (issue #44 B).

    Mirrors ``_ipc``'s rule ``{XDG_RUNTIME_DIR | /tmp}``: a daemon with no
    XDG_RUNTIME_DIR set lives at /tmp. Returns "(unknown)" when the env is
    genuinely unreadable — callers must treat that as unverifiable, and
    :func:`same_runtime_dir_as_us` falls back to the /tmp default for it, which
    is the conservative direction for the reclaim (see its docstring)."""
    val = pid_env(pid, "XDG_RUNTIME_DIR")
    if val is None:
        # Unset OR unreadable. The daemon default is /tmp; report that, since
        # a daemon without XDG_RUNTIME_DIR genuinely lives there.
        return str(Path("/tmp"))
    return val


def same_runtime_dir_as_us(pid: int) -> bool:
    """True iff ``pid``'s control-socket runtime dir matches ours.

    The reclaim safety rule (issue #44 B): a *stale* daemon — the only
    legitimate SIGTERM target — is one that crashed on the SAME control socket
    we are about to bind, i.e. the same runtime dir. A browserwright daemon
    from a DIFFERENT runtime dir (the machine-global daemon at /tmp, or a
    sibling worktree's isolated e2e daemon) is someone else's live daemon:
    signalling it would kill the user's daily daemon, so callers must refuse.

    ``pid_env`` returning None is treated as the daemon default /tmp; if the
    holder's env is genuinely unreadable this may misjudge, but only toward
    reclaiming a same-/tmp-dir holder — and a live same-dir daemon is already
    caught by ``serve``'s control-socket ping before the reclaim ever runs."""
    from . import _ipc

    theirs = Path(pid_runtime_dir(pid))
    ours = _ipc.runtime_dir()
    try:
        return theirs.resolve() == ours.resolve()
    except OSError:
        return False


#: Re-exported from `supervise` so this module keeps its self-contained
#: "detect + reclaim" vocabulary while there is only one implementation.
pid_alive = _pid_alive


def confirmed_stale_holder(ports: list[int]) -> int | None:
    """Return the pid of a *confirmed browserwright* daemon holding any of
    ``ports`` (excluding ourselves), or None.

    Confirmation = lsof says it holds the port AND its command line is a
    browserwright daemon. Callers must have already established that the control
    socket is dead (ping negative) before treating this as "stale". lsof
    unavailable → None (we won't guess a kill target)."""
    me = os.getpid()
    for port in ports:
        for pid in port_holder_pids(port):
            if pid != me and pid_is_browserwright_daemon(pid):
                return pid
    return None


def reclaim_ports(pid: int, ports: list[int], *, timeout: float = 5.0) -> bool:
    """SIGTERM ``pid`` and wait until every port in ``ports`` frees; escalate to
    SIGKILL as a last resort. Returns True once the ports are free.

    The caller MUST have confirmed ``pid`` via :func:`confirmed_stale_holder`
    (or an equivalent lsof+cmdline check) — this function does not re-verify, so
    no ``guard`` is passed to :func:`supervise.terminate`.

    Note the death observable: *ports free*, not *process gone*. Reclaiming is
    about being able to bind, and a daemon that has released its listeners has
    already given us what we need.
    """
    def _all_free() -> bool:
        return all(not port_is_listening("127.0.0.1", p) for p in ports)

    outcome = terminate(pid, is_dead=_all_free, grace=timeout, kill_grace=2.0)
    if outcome is Outcome.ALREADY_GONE:
        return _all_free()
    if outcome is Outcome.SIGNAL_FAILED:
        return False
    return outcome in (Outcome.EXITED, Outcome.KILLED)
