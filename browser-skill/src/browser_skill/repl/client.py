"""Client side of the repl daemon (``browser-skill exec '<code>'``)."""
from __future__ import annotations

import os
import socket

from . import _proto


_msg_id = 0


def _next_id() -> int:
    global _msg_id
    _msg_id += 1
    return _msg_id


def _connect(timeout: float = 5.0) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(_proto.default_socket_path()))
    return s


def is_repl_running() -> bool:
    p = _proto.pid_path()
    if not p.exists():
        return False
    try:
        pid = int(p.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Verify the socket is actually accepting (a stale pid file is possible).
    # The accept loop in the daemon may take a moment to spin up after the
    # pidfile lands, so be patient on the first ping.
    import time as _time
    deadline = _time.monotonic() + 2.0
    last_err: OSError | None = None
    while _time.monotonic() < deadline:
        try:
            s = _connect(timeout=1.0)
            _proto.send_json(s, {"id": _next_id(), "op": "ping"})
            reply = _proto.recv_json(s)
            s.close()
            return bool(reply and reply.get("pong"))
        except OSError as e:
            last_err = e
            _time.sleep(0.1)
    return False


def send_exec(code: str) -> dict:
    s = _connect()
    try:
        _proto.send_json(s, {"id": _next_id(), "code": code})
        reply = _proto.recv_json(s) or {}
    finally:
        s.close()
    return reply
