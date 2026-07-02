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
import signal
import socket
import subprocess
import time


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


def pid_alive(pid: int) -> bool:
    """True if ``pid`` exists (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


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
    (or an equivalent lsof+cmdline check) — this function does not re-verify."""
    def _all_free() -> bool:
        return all(not port_is_listening("127.0.0.1", p) for p in ports)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return _all_free()
    except OSError:
        return False
    if _wait(_all_free, timeout):
        return True
    # Escalate — a wedged daemon may never handle SIGTERM.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return _wait(_all_free, 2.0)


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.15)
    return pred()
