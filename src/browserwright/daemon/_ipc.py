"""IPC plumbing for Mode B (§6.7).

Ported from browser-harness `_ipc.py` — the file-naming / ping patterns are
field-tested. Two changes from the source:
1. Prefix is `browserwright-daemon` instead of `bu-` (separate product).
2. Ping is HTTP (`GET /__ping__`) over the local socket *before* a ws upgrade
   ever happens — this lets stale-detection work without negotiating a CDP
   session. Spec §6.7 says the ping should be CDP `Browser.getVersion`; we
   defer that to the ws layer once a daemon is live, but the cold-start
   stale-check before bind needs cheaper plumbing.

There is exactly ONE daemon, so the endpoint is a fixed path (no per-instance
name — the `BD_NAME` concept was removed; see docs/refactor-single-daemon.md):

POSIX:
    sock_path     = {XDG_RUNTIME_DIR | /tmp}/browserwright-daemon.sock
    log_path      = {TMPDIR | /tmp}/browserwright-daemon.log
    pid_path      = {XDG_RUNTIME_DIR | /tmp}/browserwright-daemon.pid

Windows:
    port_path     = %TEMP%/browserwright-daemon.port  (atomic-written JSON)
    log_path      = %TEMP%/browserwright-daemon.log
    pid_path      = %TEMP%/browserwright-daemon.pid
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path


IS_WINDOWS = sys.platform == "win32"
_PREFIX = "browserwright-daemon"


# ---- file paths ------------------------------------------------------------


def _runtime_dir() -> Path:
    """Where sock + pid + (Windows) port file live.

    AF_UNIX sun_path has a hard 104-byte budget on macOS. `tempfile.gettempdir()`
    on macOS returns `/var/folders/...` which would blow that budget — so we use
    `/tmp` on POSIX explicitly. On Windows we use `%TEMP%`, no path-length issue.
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


def sock_path() -> Path:
    return _runtime_dir() / f"{_PREFIX}.sock"


def port_path() -> Path:
    """Windows token file. Holds JSON {port, token}."""
    return _runtime_dir() / f"{_PREFIX}.port"


def log_path() -> Path:
    return _tmp_dir() / f"{_PREFIX}.log"


def pid_path() -> Path:
    return _runtime_dir() / f"{_PREFIX}.pid"


def facade_path() -> Path:
    """Discovery file for the Playwright CDP facade.

    Holds JSON ``{"ws": "ws://host:port/cdp", "port": N}`` written by the daemon
    when the facade binds (Phase C). The skill layer reads this to
    ``connect_over_cdp`` without parsing daemon logs or guessing the port. Lives
    beside the socket/pid under XDG_RUNTIME_DIR so e2e isolation (a throwaway
    XDG_RUNTIME_DIR) gives each test daemon its own discovery file."""
    return _runtime_dir() / f"{_PREFIX}.facade"


def write_facade_file(ws_url: str, port: int) -> None:
    """Atomic write of the facade discovery file (mirrors write_port_file)."""
    fp = facade_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text(json.dumps({"ws": ws_url, "port": port}))
    os.replace(tmp, fp)


def read_facade_file() -> tuple[str | None, int | None]:
    """Return ``(ws_url, port)`` of the running daemon's facade, or
    ``(None, None)`` when the file is absent/unreadable (no daemon / facade off).
    """
    try:
        d = json.loads(facade_path().read_text())
        return str(d["ws"]), int(d["port"])
    except (FileNotFoundError, ValueError, KeyError, TypeError, OSError):
        return None, None


def endpoint_describe() -> dict:
    """Public-facing description of the IPC endpoint for `status` / `url --mode-b-proxy`.
    Spec §6.1 --json shape."""
    if IS_WINDOWS:
        port, token = read_port_file()
        if port is None:
            return {"schema_version": 1, "transport": "tcp",
                    "host": "127.0.0.1", "port": None, "token": None}
        return {"schema_version": 1, "transport": "tcp",
                "host": "127.0.0.1", "port": port, "token": token}
    return {"schema_version": 1, "transport": "unix",
            "path": str(sock_path())}


# ---- Windows port-file: atomic write + read --------------------------------


def write_port_file(port: int, token: str) -> None:
    """Atomic write {.tmp → os.replace} so a concurrent reader never sees a
    half-written file. Mirrors browser-harness `_ipc.py:179-181`."""
    pf = port_path()
    pf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pf.with_name(pf.name + ".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token}))
    os.replace(tmp, pf)


def read_port_file() -> tuple[int | None, str | None]:
    try:
        d = json.loads(port_path().read_text())
        return int(d["port"]), str(d["token"])
    except (FileNotFoundError, ValueError, KeyError, TypeError, OSError):
        return None, None


def cleanup_endpoint() -> None:
    """Best-effort: nuke socket / port file. Called on graceful shutdown and
    by `stop` before bind. Silent on missing files."""
    paths = [sock_path() if not IS_WINDOWS else port_path(),
             pid_path(), facade_path()]
    for p in paths:
        try:
            p.unlink()
        except (FileNotFoundError, IsADirectoryError, OSError):
            pass


# ---- ping handshake (stale-detect) -----------------------------------------
#
# Spec §6.7 calls for CDP `Browser.getVersion` over ws. But before we know
# whether the listener is *our* daemon, the cheapest probe is an HTTP GET that
# our daemon recognizes specifically and that anything else either rejects or
# doesn't answer.
#
# We use an HTTP request the ws server can intercept via process_request. The
# `/__ping__` path is reserved for this — daemon's process_request returns a
# 200 with body {"pong": true, "pid": N, "version": "..."}. A foreign listener
# might 404 or send garbage; anything not matching counts as "stale."


def make_pong_body(pid: int) -> bytes:
    """Daemon side: build the /__ping__ response body.

    Carries the daemon's package version so a client can detect a *stale*
    daemon (running older code than what's installed on disk) and auto-restart
    it — S6 (A2-a). A daemon too old to know about this field simply omits it;
    the parser treats a missing version as stale.
    """
    from . import __version__
    return json.dumps(
        {"pong": True, "pid": pid, "version": __version__}).encode()


def parse_pong(body: bytes) -> tuple[int | None, str | None]:
    """Client side: extract ``(pid, version)`` from a /__ping__ pong body.

    Returns ``(None, None)`` for anything that isn't our pong shape. ``version``
    is ``None`` when the daemon predates version-advertising — callers treat
    that as stale (one needless restart on first upgrade beats silent failure).
    """
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict) or payload.get("pong") is not True:
        return None, None
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid > (1 << 31):
        return None, None
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        version = None
    return pid, version


async def ping_status_async(timeout: float = 1.0) -> tuple[int | None, str | None]:
    """Async client-side ping returning ``(pid, version)``.

    ``pid`` is None when the endpoint is not a live daemon (refused / wrong /
    no response). ``version`` is the daemon's advertised package version, or
    None if the daemon is too old to advertise one (S6 — treated as stale).

    Used by `serve` cold-start to decide whether the existing socket file
    belongs to a live daemon (=> exit 0, idempotent) or a stale corpse
    (=> unlink + bind fresh).
    """
    none = (None, None)
    try:
        if IS_WINDOWS:
            port, _ = read_port_file()
            if port is None:
                return none
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=timeout)
        else:
            p = sock_path()
            if not p.exists():
                return none
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(p)), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return none
    try:
        try:
            writer.write(b"GET /__ping__ HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        except (BrokenPipeError, ConnectionResetError, OSError, asyncio.TimeoutError):
            # The peer closed/crashed mid-write — definitely not our daemon.
            return none
        # Read until double-CRLF, then up to a reasonable body size.
        data = b""
        deadline = asyncio.get_running_loop().time() + timeout
        while b"\r\n\r\n" not in data and len(data) < 4096:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return none
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=remaining)
            except asyncio.TimeoutError:
                return none
            if not chunk:
                break
            data += chunk
        # Read possible body
        try:
            body = await asyncio.wait_for(reader.read(1024), timeout=0.2)
            data += body
        except asyncio.TimeoutError:
            pass
        idx = data.find(b"\r\n\r\n")
        if idx < 0:
            return none
        # Defensive parse, anything-not-our-shape = stale.
        return parse_pong(data[idx + 4:])
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        except (OSError, asyncio.TimeoutError):
            pass


async def ping_async(timeout: float = 1.0) -> int | None:
    """Async client-side ping. Returns the daemon's reported PID, or None
    when the endpoint is not a live daemon. Thin wrapper over
    :func:`ping_status_async` for callers that only care about liveness/pid."""
    pid, _version = await ping_status_async(timeout=timeout)
    return pid


def ping_status_sync(timeout: float = 1.0) -> tuple[int | None, str | None]:
    """Synchronous ``(pid, version)`` probe for CLI paths without a running
    loop. Returns ``(None, None)`` when nothing answers."""
    coro = ping_status_async(timeout=timeout)
    try:
        return asyncio.run(coro)
    except RuntimeError:
        coro.close()
        return None, None


def ping_sync(timeout: float = 1.0) -> int | None:
    """Synchronous variant for CLI status / stop paths that don't already
    have an event loop running. Returns the daemon's PID, or None."""
    pid, _version = ping_status_sync(timeout=timeout)
    return pid


# ---- POSIX socket bind helper ---------------------------------------------


def make_unix_socket() -> socket.socket:
    """Create + bind an AF_UNIX SOCK_STREAM with 0600 perms via umask(0o077).

    Returns the bound, listening-ready socket. Pass it to
    `websockets.unix_serve(handler, sock=...)`. Mirrors browser-harness
    `_ipc.py:166-170`.
    """
    path = sock_path()
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


def write_pid(pid: int) -> None:
    p = pid_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{pid}\n")


def read_pid() -> int | None:
    try:
        s = pid_path().read_text().strip()
        v = int(s)
        return v if 0 < v < (1 << 31) else None
    except (FileNotFoundError, ValueError, OSError):
        return None
