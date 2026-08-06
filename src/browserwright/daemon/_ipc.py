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

    sock_path     = {XDG_RUNTIME_DIR | /tmp}/browserwright-daemon.sock
    log_path      = {TMPDIR | /tmp}/browserwright-daemon.log
    pid_path      = {XDG_RUNTIME_DIR | /tmp}/browserwright-daemon.pid
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import tempfile
from pathlib import Path


_PREFIX = "browserwright-daemon"


# ---- file paths ------------------------------------------------------------


def runtime_dir() -> Path:
    """Where sock + pid files live.

    AF_UNIX sun_path has a hard 104-byte budget on macOS. `tempfile.gettempdir()`
    on macOS returns `/var/folders/...` which would blow that budget — so we use
    `/tmp` explicitly.
    """
    if (xdg := os.environ.get("XDG_RUNTIME_DIR")):
        return Path(xdg)
    return Path("/tmp")


def _tmp_dir() -> Path:
    """Where the log lives (long paths OK)."""
    if (t := os.environ.get("TMPDIR")):
        return Path(t)
    return Path("/tmp")


def sock_path() -> Path:
    return runtime_dir() / f"{_PREFIX}.sock"


def log_path() -> Path:
    return _tmp_dir() / f"{_PREFIX}.log"


def pid_path() -> Path:
    return runtime_dir() / f"{_PREFIX}.pid"


def facade_path() -> Path:
    """Discovery file for the Playwright CDP facade.

    Holds JSON ``{"ws": "ws://host:port/cdp", "port": N}`` written by the daemon
    when the facade binds (Phase C). The skill layer reads this to
    ``connect_over_cdp`` without parsing daemon logs or guessing the port. Lives
    beside the socket/pid under XDG_RUNTIME_DIR so e2e isolation (a throwaway
    XDG_RUNTIME_DIR) gives each test daemon its own discovery file."""
    return runtime_dir() / f"{_PREFIX}.facade"


def write_facade_file(ws_url: str, port: int) -> None:
    """Atomic write (.tmp then os.replace) of the facade discovery file."""
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


# ---- Phase B: per-session executor discovery -------------------------------
#
# The persistent per-session executor (`browserwright._executor`) binds its OWN
# unix socket (the data plane — Fork 2) and writes a discovery file the thin
# heredoc client reads after the daemon `ensureExecutor` verb spawns it. The
# socket NAME must be short: AF_UNIX `sun_path` has a hard 104-byte budget on
# macOS (see `runtime_dir`), and `runtime_dir()` is already `/tmp` for that
# reason — so we key the per-session socket on a SHORT id digest, not the raw
# session id (which can be long, e.g. `e2e-phasec-<uuid4hex>`).


def _exec_shortid(session_id: str) -> str:
    """A short, filesystem-safe digest of a session id for the socket name.

    Keeps the AF_UNIX path within the 104-byte budget regardless of how long
    the raw session id is. 12 hex chars of SHA-256 is collision-safe enough for
    a per-machine, per-user runtime dir."""
    import hashlib

    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


def executor_sock_path(session_id: str) -> Path:
    """Unix socket the per-session executor binds (`bw-exec-<shortid>.sock`)."""
    return runtime_dir() / f"bw-exec-{_exec_shortid(session_id)}.sock"


def executor_file_path(session_id: str) -> Path:
    """Discovery file the executor writes when its socket is bound + ready.

    Holds JSON ``{"sock": "<path>", "pid": N, "session": "<id>"}``."""
    return runtime_dir() / f"bw-exec-{_exec_shortid(session_id)}.json"


def executor_inflight_path(session_id: str) -> Path:
    """Sidecar file describing what the executor's worker thread is doing NOW.

    Why a file and not an RPC: the executor's accept loop is deliberately
    one-request-per-connection and BLOCKS on ``worker.submit`` (thread-affine
    Playwright, Fork 3), so a hung call is exactly the state in which the
    executor cannot answer a query. A file the worker writes *before* it starts
    the call is readable precisely when asking would fail — which is the only
    time anyone wants to know.

    Deliberately NOT a ``*.json`` name: ``cleanup_orphan_executors`` globs
    ``bw-exec-*.json`` and SIGTERMs whatever ``pid`` it finds inside, so a
    sidecar matching that glob would be read as a second discovery record."""
    return _executor_inflight_dir() / f"bw-exec-{_exec_shortid(session_id)}.inflight"


def _executor_inflight_dir() -> Path:
    """Per-user private home for executor observability sidecars."""
    return runtime_dir() / f"browserwright-{os.geteuid()}"


def _ensure_executor_inflight_dir() -> Path:
    """Create and verify the sidecar directory without following a symlink."""
    directory = _executor_inflight_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(directory, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(directory)
        if metadata.st_uid != os.geteuid():
            raise PermissionError(
                f"executor inflight directory is owned by uid {metadata.st_uid}, "
                f"expected {os.geteuid()}: {directory}"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return directory


def write_executor_inflight(session_id: str, payload: dict | None) -> None:
    """Publish (or clear, with ``payload=None``) the executor's current call.

    Best-effort on every path: an unwritable runtime dir costs observability,
    never correctness, and this runs on the executor's hot path."""
    tmp_path: str | None = None
    tmp_fd = -1
    try:
        _ensure_executor_inflight_dir()
        fp = executor_inflight_path(session_id)
        if payload is None:
            fp.unlink(missing_ok=True)
            return
        serialized = json.dumps(payload)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".{fp.name}.", suffix=".tmp", dir=fp.parent)
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w") as tmp_file:
            tmp_fd = -1
            tmp_file.write(serialized)
        os.replace(tmp_path, fp)
        tmp_path = None
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if tmp_fd >= 0:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


def read_executor_inflight(session_id: str) -> dict | None:
    """Read the executor's current-call sidecar, or ``None`` when idle/absent."""
    try:
        _ensure_executor_inflight_dir()
        d = json.loads(executor_inflight_path(session_id).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None
    return d if isinstance(d, dict) else None


def write_executor_file(
    session_id: str,
    sock: str,
    pid: int,
    executor_id: str | None = None,
) -> None:
    """Atomic write of the executor discovery file (mirrors write_facade_file).

    Written by the executor once its socket is bound and the worker is ready,
    so a reader that sees the file can immediately connect."""
    fp = executor_file_path(session_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(fp.name + ".tmp")
    payload = {"sock": sock, "pid": pid, "session": session_id}
    # Start-time fingerprint so a later sweep can tell "our executor is still
    # running" from "the OS handed this pid to somebody else". Without it the
    # orphan cleanup can only check that *a* process holds the pid, and its
    # SIGKILL escalation would take out an unrelated process group.
    from .platforms import proc_start_time
    started = proc_start_time(pid)
    if started is not None:
        payload["start_time"] = started
    if executor_id is not None:
        payload["executor_id"] = executor_id
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, fp)


def read_executor_record(session_id: str) -> dict | None:
    """Return a validated executor discovery record, or ``None``.

    ``executor_id`` was added after the original pid/socket format.  Old files
    remain readable for startup cleanup, while new registry leases require the
    identity to match before they consider an executor ready.
    """
    try:
        d = json.loads(executor_file_path(session_id).read_text())
        sock = d["sock"]
        pid = d["pid"]
        recorded_session = d["session"]
        executor_id = d.get("executor_id")
        if not isinstance(sock, str) or not sock:
            return None
        if not isinstance(pid, int) or pid <= 0:
            return None
        if recorded_session != session_id:
            return None
        if executor_id is not None and (
            not isinstance(executor_id, str) or not executor_id
        ):
            return None
        return {
            "sock": sock,
            "pid": pid,
            "session": recorded_session,
            "executor_id": executor_id,
            "start_time": d.get("start_time"),
        }
    except (FileNotFoundError, ValueError, KeyError, TypeError, OSError):
        return None


def read_executor_file(session_id: str) -> tuple[str | None, int | None]:
    """Return ``(sock_path, pid)`` of the session's executor, or
    ``(None, None)`` when the discovery file is absent/unreadable."""
    record = read_executor_record(session_id)
    if record is None:
        return None, None
    return record["sock"], record["pid"]


def cleanup_executor(session_id: str) -> None:
    """Best-effort: nuke a session's executor socket + discovery file. Called by
    the executor on exit and by the daemon when it reaps/kills the executor."""
    for p in (executor_sock_path(session_id), executor_file_path(session_id)):
        try:
            p.unlink()
        except (FileNotFoundError, IsADirectoryError, OSError):
            pass
    write_executor_inflight(session_id, None)


def make_executor_socket(session_id: str) -> socket.socket:
    """Create + bind the executor's AF_UNIX socket with 0600 perms (mirrors
    `make_unix_socket`, but on the per-session executor path)."""
    path = executor_sock_path(session_id)
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


def endpoint_describe() -> dict:
    """Public-facing description of the IPC endpoint for `status` / `url --mode-b-proxy`.
    Spec §6.1 --json shape."""
    return {"schema_version": 1, "transport": "unix",
            "path": str(sock_path())}


def cleanup_endpoint() -> None:
    """Best-effort: nuke socket / pid / facade files. Called on graceful
    shutdown and by `stop` before bind. Silent on missing files."""
    paths = [sock_path(), pid_path(), facade_path()]
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


# ---- socket bind helper -----------------------------------------------------


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
