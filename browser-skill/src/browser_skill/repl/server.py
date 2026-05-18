"""Long-lived REPL daemon (spec §A.1 ``repl start``).

Listens on ``$BS_HOME/repl.sock`` and accepts ``{"id":N,"code":"..."}`` JSON
messages. Each request is exec'd in a shared globals namespace; stdout / any
exception is returned as ``{"id":N,"stdout":...,"stderr":...,"exception":...}``.

Lifecycle:
  - ``start``: fork the server detached, write PID to ``$BS_HOME/repl.pid``.
  - ``stop``: SIGTERM the PID, unlink the socket.
  - ``status``: report alive / dead.
"""
from __future__ import annotations

import io
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ..errors import BrowserSkillError, serialize
from ..session import current_session
from . import _namespace, _proto


def _is_running() -> bool:
    p = _proto.pid_path()
    if not p.exists():
        return False
    try:
        pid = int(p.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — count as running.
        return True


def _serve_one(client: socket.socket, globals_: dict) -> None:
    try:
        req = _proto.recv_json(client)
    except Exception as e:  # noqa: BLE001
        try:
            _proto.send_json(client, {"id": -1, "exception": {"type": type(e).__name__, "msg": str(e)}})
        except Exception:
            pass
        return
    if not req:
        return
    mid = req.get("id")
    if req.get("op") == "ping":
        _proto.send_json(client, {"id": mid, "pong": True})
        return
    if req.get("op") == "shutdown":
        _proto.send_json(client, {"id": mid, "ok": True})
        os._exit(0)
    code = req.get("code") or ""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    reply: dict = {"id": mid, "stdout": "", "stderr": "", "exception": None}
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            exec(compile(code, "<repl>", "exec"), globals_)
    except BrowserSkillError as e:
        reply["exception"] = serialize(e)
        current_session().record(code, ok=False, stdout=out_buf.getvalue(),
                                 exception=type(e).__name__)
    except SystemExit:
        # Don't kill the server.
        pass
    except Exception as e:  # noqa: BLE001
        reply["exception"] = {"type": type(e).__name__, "msg": str(e),
                              "traceback": traceback.format_exc()}
        current_session().record(code, ok=False, stdout=out_buf.getvalue(),
                                 exception=type(e).__name__)
    else:
        current_session().record(code, ok=True, stdout=out_buf.getvalue())
    reply["stdout"] = out_buf.getvalue()
    reply["stderr"] = err_buf.getvalue()
    try:
        _proto.send_json(client, reply)
    except OSError:
        pass


def _server_loop(sock: socket.socket, globals_: dict, *,
                 ready_event: threading.Event | None = None) -> None:
    if ready_event is not None:
        ready_event.set()
    while True:
        try:
            client, _addr = sock.accept()
        except OSError:
            return
        t = threading.Thread(target=_serve_one, args=(client, globals_), daemon=True)
        t.start()


def start(*, daemonize: bool = True) -> int:
    """Start the server. Returns the child PID (or 0 if already running)."""
    if _is_running():
        print("repl daemon already running")
        return 0
    sock_path = _proto.default_socket_path()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    if daemonize and hasattr(os, "fork"):
        pid = os.fork()
        if pid:
            # parent waits for the child to come online so the user gets a
            # crisp "ready" signal.
            for _ in range(40):
                if sock_path.exists() and _proto.pid_path().exists():
                    print(f"repl daemon started (pid {pid})")
                    return pid
                time.sleep(0.05)
            print(f"repl daemon launched (pid {pid}) — socket not ready yet",
                  file=sys.stderr)
            return pid
        # double-fork to fully detach.
        if os.fork():
            os._exit(0)
        # detach stdio
        for fd in (0, 1, 2):
            try:
                os.close(fd)
            except OSError:
                pass
        for fd in (0, 1, 2):
            os.open(os.devnull, os.O_RDWR if fd == 0 else os.O_WRONLY)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(sock_path))
        os.chmod(str(sock_path), 0o600)
        sock.listen(8)
    except OSError as e:
        print(f"repl bind failed: {e}", file=sys.stderr)
        os._exit(1)

    def _on_term(signum, frame):  # noqa: ARG001
        try:
            sock_path.unlink()
        except OSError:
            pass
        try:
            _proto.pid_path().unlink()
        except OSError:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    # Build globals first so when the pidfile lands, the accept loop is ready
    # to actually service requests. Without this the parent's "ready" wait
    # races against our imports, and the first `browser-skill exec` after
    # ``repl start`` may time out waiting on a queued connection.
    globals_ = _namespace.build_globals()
    ready_event = threading.Event()
    accept_thread = threading.Thread(
        target=_server_loop,
        args=(sock, globals_),
        kwargs={"ready_event": ready_event},
        daemon=False,
        name="repl-accept",
    )
    accept_thread.start()
    ready_event.wait(timeout=2.0)
    # Now the accept loop is in place — write the pidfile so the parent
    # subprocess returns from its readiness wait.
    _proto.pid_path().write_text(str(os.getpid()))
    accept_thread.join()
    return 0


def stop() -> int:
    p = _proto.pid_path()
    if not p.exists():
        print("repl daemon not running")
        return 0
    try:
        pid = int(p.read_text().strip())
    except ValueError:
        p.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(40):
        if not _is_running():
            break
        time.sleep(0.05)
    # Force cleanup in case the daemon didn't clean its own files.
    try:
        _proto.pid_path().unlink()
    except OSError:
        pass
    try:
        _proto.default_socket_path().unlink()
    except OSError:
        pass
    print("repl daemon stopped")
    return 0


def status() -> int:
    if _is_running():
        try:
            pid = int(_proto.pid_path().read_text().strip())
        except (ValueError, FileNotFoundError):
            pid = -1
        print(f"repl daemon running (pid {pid}) — socket {_proto.default_socket_path()}")
        return 0
    print("repl daemon not running")
    return 1
