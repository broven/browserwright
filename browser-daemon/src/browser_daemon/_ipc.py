"""IPC plumbing for Mode B (§6.7).

Ported from browser-harness `_ipc.py` — the file-naming / path-traversal / ping
patterns are field-tested. Two changes from the source:
1. Prefix is `browser-daemon-` instead of `bu-` (separate product).
2. Ping is HTTP (`GET /__ping__`) over the local socket *before* a ws upgrade
   ever happens — this lets stale-detection work without negotiating a CDP
   session. Spec §6.7 says the ping should be CDP `Browser.getVersion`; we
   defer that to the ws layer once a daemon is live, but the cold-start
   stale-check before bind needs cheaper plumbing.

POSIX:
    sock_path     = {XDG_RUNTIME_DIR | /tmp}/browser-daemon-{NAME}.sock
    log_path      = {TMPDIR | /tmp}/browser-daemon-{NAME}.log
    pid_path      = {XDG_RUNTIME_DIR | /tmp}/browser-daemon-{NAME}.pid

Windows:
    port_path     = %TEMP%/browser-daemon-{NAME}.port  (atomic-written JSON)
    log_path      = %TEMP%/browser-daemon-{NAME}.log
    pid_path      = %TEMP%/browser-daemon-{NAME}.pid
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import sys
import tempfile
from pathlib import Path

from .errors import UserError


IS_WINDOWS = sys.platform == "win32"
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_PREFIX = "browser-daemon"


# ---- file paths ------------------------------------------------------------


def _runtime_dir() -> Path:
    """Where sock + pid + (Windows) port file live.

    AF_UNIX sun_path has a hard 104-byte budget on macOS. `tempfile.gettempdir()`
    on macOS returns `/var/folders/...` which would blow that budget for any
    non-trivial NAME — so we use `/tmp` on POSIX explicitly. On Windows we use
    `%TEMP%`, no path-length issue.
    """
    if (xdg := os.environ.get("XDG_RUNTIME_DIR")):
        return Path(xdg)
    if IS_WINDOWS:
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def _tmp_dir() -> Path:
    """Where the log lives (long paths OK)."""
    if (t := os.environ.get("TMPDIR")):
        return Path(t)
    if IS_WINDOWS:
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def check_name(name: str) -> str:
    """Path-traversal guard. Mirrors browser-harness `_ipc.py:31-33`."""
    if not _NAME_RE.match(name or ""):
        raise UserError(
            f"invalid BD_NAME {name!r}: must match [A-Za-z0-9_-]{{1,64}}"
        )
    return name


def sock_path(name: str) -> Path:
    check_name(name)
    return _runtime_dir() / f"{_PREFIX}-{name}.sock"


def port_path(name: str) -> Path:
    """Windows token file. Holds JSON {port, token}."""
    check_name(name)
    return _runtime_dir() / f"{_PREFIX}-{name}.port"


def log_path(name: str) -> Path:
    check_name(name)
    return _tmp_dir() / f"{_PREFIX}-{name}.log"


def pid_path(name: str) -> Path:
    check_name(name)
    return _runtime_dir() / f"{_PREFIX}-{name}.pid"


def endpoint_describe(name: str) -> dict:
    """Public-facing description of the IPC endpoint for `status` / `url --mode-b-proxy`.
    Spec §6.1 --json shape."""
    if IS_WINDOWS:
        port, token = read_port_file(name)
        if port is None:
            return {"schema_version": 1, "transport": "tcp",
                    "host": "127.0.0.1", "port": None, "token": None, "name": name}
        return {"schema_version": 1, "transport": "tcp",
                "host": "127.0.0.1", "port": port, "token": token, "name": name}
    return {"schema_version": 1, "transport": "unix",
            "path": str(sock_path(name)), "name": name}


# ---- Windows port-file: atomic write + read --------------------------------


def write_port_file(name: str, port: int, token: str) -> None:
    """Atomic write {.tmp → os.replace} so a concurrent reader never sees a
    half-written file. Mirrors browser-harness `_ipc.py:179-181`."""
    pf = port_path(name)
    pf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pf.with_name(pf.name + ".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token}))
    os.replace(tmp, pf)


def read_port_file(name: str) -> tuple[int | None, str | None]:
    try:
        d = json.loads(port_path(name).read_text())
        return int(d["port"]), str(d["token"])
    except (FileNotFoundError, ValueError, KeyError, TypeError, OSError):
        return None, None


def cleanup_endpoint(name: str) -> None:
    """Best-effort: nuke socket / port file. Called on graceful shutdown and
    by `stop` before bind. Silent on missing files."""
    paths = [sock_path(name) if not IS_WINDOWS else port_path(name),
             pid_path(name)]
    for p in paths:
        try:
            p.unlink()
        except (FileNotFoundError, IsADirectoryError, OSError):
            pass


# ---- ping handshake (stale-detect) -----------------------------------------
#
# Spec §6.7 calls for CDP `Browser.getVersion` over ws. But before we know
# whether the listener at `name` is *our* daemon, the cheapest probe is an
# HTTP GET that our daemon recognizes specifically and that anything else
# either rejects or doesn't answer.
#
# We use an HTTP request the ws server can intercept via process_request. The
# `/__ping__` path is reserved for this — daemon's process_request returns a
# 200 with body {"pong": true, "pid": N} on POSIX or {"pong": true, "pid": N,
# "token": "..."} on Windows. A foreign listener might 404 or send garbage;
# anything not matching counts as "stale."


def make_pong_body(pid: int) -> bytes:
    """Daemon side: build the /__ping__ response body."""
    return json.dumps({"pong": True, "pid": pid}).encode()


async def ping_async(name: str, timeout: float = 1.0) -> int | None:
    """Async client-side ping. Returns the daemon's reported PID, or None
    when the endpoint is not a live daemon (refused / wrong / no response).

    Used by `serve` cold-start to decide whether the existing socket file
    belongs to a live daemon (=> exit 0, idempotent) or a stale corpse
    (=> unlink + bind fresh).
    """
    try:
        if IS_WINDOWS:
            port, _ = read_port_file(name)
            if port is None:
                return None
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=timeout)
        else:
            p = sock_path(name)
            if not p.exists():
                return None
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(p)), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        try:
            writer.write(b"GET /__ping__ HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        except (BrokenPipeError, ConnectionResetError, OSError, asyncio.TimeoutError):
            # The peer closed/crashed mid-write — definitely not our daemon.
            return None
        # Read until double-CRLF, then up to a reasonable body size.
        data = b""
        deadline = asyncio.get_running_loop().time() + timeout
        while b"\r\n\r\n" not in data and len(data) < 4096:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if not chunk:
                break
            data += chunk
        # Read possible body
        try:
            body = await asyncio.wait_for(reader.read(1024), timeout=0.2)
            data += body
        except asyncio.TimeoutError:
            pass
        # Extract pid from JSON body — defensive parse, anything-not-our-shape = stale
        idx = data.find(b"\r\n\r\n")
        if idx < 0:
            return None
        body_bytes = data[idx + 4:]
        try:
            payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("pong") is not True:
            return None
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid > (1 << 31):
            return None
        return pid
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        except (OSError, asyncio.TimeoutError):
            pass


def ping_sync(name: str, timeout: float = 1.0) -> int | None:
    """Synchronous variant for CLI status / stop paths that don't already
    have an event loop running. Returns the daemon's PID, or None."""
    coro = ping_async(name, timeout=timeout)
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # asyncio.run() refused — typically because we're already inside a
        # running loop, but other policy errors raise too. The coroutine may
        # or may not have been awaited; .close() is a safe no-op when it has.
        coro.close()
        return None


# ---- POSIX socket bind helper ---------------------------------------------


def make_unix_socket(name: str) -> socket.socket:
    """Create + bind an AF_UNIX SOCK_STREAM with 0600 perms via umask(0o077).

    Returns the bound, listening-ready socket. Pass it to
    `websockets.unix_serve(handler, sock=...)`. Mirrors browser-harness
    `_ipc.py:166-170`.
    """
    path = sock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o077)
    try:
        s.bind(str(path))
    finally:
        os.umask(old_umask)
    s.listen(8)
    return s


def make_tcp_socket() -> tuple[socket.socket, int, str]:
    """Windows path: bind 127.0.0.1:0, return (socket, port, token).

    Caller writes the port-file. We hold the socket and pass it to
    websockets.serve(sock=...).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(8)
    token = secrets.token_hex(32)
    return s, port, token


# ---- pid file helpers ------------------------------------------------------


def write_pid(name: str, pid: int) -> None:
    p = pid_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{pid}\n")


def read_pid(name: str) -> int | None:
    try:
        s = pid_path(name).read_text().strip()
        v = int(s)
        return v if 0 < v < (1 << 31) else None
    except (FileNotFoundError, ValueError, OSError):
        return None
