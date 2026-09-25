"""ADR-0013 executor handoff and adoption boundary tests.

These tests stay below Chrome/Playwright.  They prove that a replacement is
distinguished from a real stop, that startup only adopts an exactly identified
live executor, and that an adopted handle behaves like a registry-owned one.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
import websockets

from browserwright._executor import protocol
from browserwright.daemon import _ipc
from browserwright.daemon import platforms
from browserwright.daemon.config import Config
from browserwright.daemon.server import executor_registry as er
from browserwright.daemon.server import listener
from browserwright.daemon.server.executor_registry import ExecutorRegistry
from browserwright.daemon.server.facade import PlaywrightFacade


@pytest.fixture
def isolated_runtime(monkeypatch):
    # AF_UNIX has a ~104-byte pathname ceiling on macOS; pytest's nested temp
    # path is intentionally too long for the production socket shape.
    path = Path(tempfile.mkdtemp(prefix="bw-adopt-", dir="/tmp"))
    monkeypatch.setattr(_ipc, "runtime_dir", lambda: path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_handoff_marker_requires_pid_fingerprint_and_freshness(
    isolated_runtime, monkeypatch,
):
    starts = {101: "start-101", 202: "start-202"}
    monkeypatch.setattr(platforms, "proc_start_time", starts.get)

    assert _ipc.request_executor_handoff(101, max_age_s=10.0) is True
    marker = _ipc.executor_handoff_path()
    assert marker.exists()
    assert _ipc.consume_executor_handoff(202) is False
    assert not marker.exists(), "a rejected marker is still one-shot"

    assert _ipc.request_executor_handoff(101, max_age_s=10.0) is True
    starts[101] = "recycled-pid"
    assert _ipc.consume_executor_handoff(101) is False

    starts[101] = "start-101"
    assert _ipc.request_executor_handoff(101, max_age_s=1.0) is True
    payload = json.loads(marker.read_text())
    payload["created_at"] = time.time() - 2.0
    marker.write_text(json.dumps(payload))
    assert _ipc.consume_executor_handoff(101) is False

    assert _ipc.request_executor_handoff(101) is True
    assert _ipc.consume_executor_handoff(101) is True
    assert _ipc.consume_executor_handoff(101) is False


class _Holder:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def trigger_close(self, reason: str) -> None:
        self.closed.append(reason)


class _ShutdownRegistry:
    def __init__(self) -> None:
        self._handles = {"s1": object()}
        self._pending_teardowns = {}
        self.kill_all_calls = 0

    async def kill_all(self) -> None:
        self.kill_all_calls += 1


def _shutdown_daemon() -> tuple[SimpleNamespace, _Holder, _ShutdownRegistry]:
    holder = _Holder()
    registry = _ShutdownRegistry()
    daemon = SimpleNamespace(
        executors=registry,
        all_contexts=lambda: [SimpleNamespace(backend="extension", holder=holder)],
    )
    return daemon, holder, registry


@pytest.mark.asyncio
async def test_graceful_shutdown_preserves_executors_only_for_handoff(
    isolated_runtime, monkeypatch,
):
    pid = os.getpid()
    monkeypatch.setattr(platforms, "proc_start_time", lambda candidate: (
        "this-process" if candidate == pid else None
    ))
    assert _ipc.request_executor_handoff(pid) is True
    daemon, holder, registry = _shutdown_daemon()

    await listener._graceful_shutdown(daemon)

    assert holder.closed == ["daemon_shutdown"]
    assert registry.kill_all_calls == 0
    assert not _ipc.executor_handoff_path().exists()


@pytest.mark.asyncio
async def test_graceful_shutdown_real_stop_reaps_executors(isolated_runtime):
    daemon, holder, registry = _shutdown_daemon()

    await listener._graceful_shutdown(daemon)

    assert holder.closed == ["daemon_shutdown"]
    assert registry.kill_all_calls == 1


def _write_discovery(runtime, *, sid: str, pid: int, started: str,
                     executor_id: str | None = "00000000000000000000000000000001"):
    sock = _ipc.executor_sock_path(sid)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock))
    listener.close()
    record = {
        "session": sid,
        "pid": pid,
        "sock": str(sock),
        "start_time": started,
    }
    if executor_id is not None:
        record["executor_id"] = executor_id
    discovery = _ipc.executor_file_path(sid)
    discovery.write_text(json.dumps(record))
    return discovery, sock


def test_orphan_cleanup_keeps_only_live_exactly_fingerprinted_records(
    isolated_runtime, monkeypatch,
):
    valid, valid_sock = _write_discovery(
        isolated_runtime, sid="valid", pid=101, started="start-101",
        executor_id="00000000000000000000000000000101")
    mismatch, mismatch_sock = _write_discovery(
        isolated_runtime, sid="mismatch", pid=202, started="old-202")
    dead, dead_sock = _write_discovery(
        isolated_runtime, sid="dead", pid=303, started="start-303")
    invalid, invalid_sock = _write_discovery(
        isolated_runtime, sid="invalid", pid=404, started="start-404",
        executor_id=None)

    alive = {101: True, 202: True, 303: False, 404: True}
    observed_starts = {
        101: "start-101",
        202: "new-202",
        303: "start-303",
        404: "start-404",
    }
    reaped: list[tuple[int, str | None]] = []
    monkeypatch.setattr(er, "_pid_alive", lambda pid: alive[pid])
    monkeypatch.setattr(platforms, "proc_start_time", observed_starts.get)
    monkeypatch.setattr(
        er,
        "_terminate_orphan_and_wait",
        lambda pid, started=None: reaped.append((pid, started)) or True,
    )

    kept = er.cleanup_orphan_executors()

    assert kept == [{
        "session": "valid",
        "pid": 101,
        "sock": str(valid_sock),
        "executor_id": "00000000000000000000000000000101",
        "start_time": "start-101",
    }]
    assert valid.exists() and valid_sock.exists()
    assert set(reaped) == {
        (202, "old-202"),
        (303, "start-303"),
        (404, "start-404"),
    }
    for path in (mismatch, mismatch_sock, dead, dead_sock, invalid, invalid_sock):
        assert not path.exists()


@pytest.mark.asyncio
async def test_adopted_executor_is_reused_with_same_socket_and_identity_then_killed(
    monkeypatch,
):
    alive = {5150: True}
    monkeypatch.setattr(er, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(platforms, "proc_start_time", lambda pid: "start-5150")
    cleaned: list[str] = []
    monkeypatch.setattr(_ipc, "cleanup_executor", cleaned.append)

    def terminate(pid, started, *, grace):
        assert (pid, started) == (5150, "start-5150")
        alive[pid] = False
        return True

    monkeypatch.setattr(er, "terminate_orphan", terminate)
    registry = ExecutorRegistry()

    adopted = registry.adopt([{
        "session": "s-adopted",
        "pid": 5150,
        "sock": "/tmp/bw-exec-s-adopted.sock",
        "executor_id": "executor-original",
        "start_time": "start-5150",
    }])

    assert adopted == ["s-adopted"]
    assert await registry.ensure("s-adopted") == "/tmp/bw-exec-s-adopted.sock"
    handle = registry.get("s-adopted")
    assert handle is not None
    assert handle.executor_id == "executor-original"
    assert handle.adopted is True

    result = await registry.kill_and_wait(
        "s-adopted", executor_id="executor-original")
    assert result == {
        "killed": True,
        "reaped": True,
        "matched": True,
        "executor_id": "executor-original",
    }
    assert registry.get("s-adopted") is None
    assert cleaned == ["s-adopted"]


@pytest.mark.asyncio
async def test_boot_adopts_a_live_executor_process_and_relays_real_exec_roundtrip(
        isolated_runtime):
    sid = "process-adopted"
    executor_id = "00000000000000000000000000ad0bed"
    sock = _ipc.executor_sock_path(sid)
    server = r"""
import json, os, socket, struct, sys
length = struct.Struct(">I")
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1])
listener.listen()
print("ready", flush=True)
while True:
    conn, _ = listener.accept()
    with conn:
        header = conn.recv(4)
        if len(header) != 4:
            continue
        size = length.unpack(header)[0]
        body = b""
        while len(body) < size:
            chunk = conn.recv(size - len(body))
            if not chunk:
                break
            body += chunk
        request = json.loads(body)
        response = {
            "console": "adopted process: " + request["code"] + "\n",
            "return_value": None,
            "error": None,
            "exit_code": 0,
            "warnings": [],
            "screenshots": [],
            "truncated": False,
            "terminal_reason": None,
            "task_result_json": None,
        }
        payload = json.dumps(response).encode()
        conn.sendall(length.pack(len(payload)) + payload)
"""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", server, str(sock)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    facade = None
    registry = ExecutorRegistry()
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        _ipc.write_executor_file(sid, str(sock), proc.pid, executor_id)

        records = er.cleanup_orphan_executors()
        assert registry.adopt(records) == [sid]
        adopted = registry.get(sid)
        assert adopted is not None
        assert (adopted.pid, adopted.executor_id, adopted.sock_path) == (
            proc.pid, executor_id, str(sock))

        # The daemon's drivable path is out of scope (no browser here); the
        # registry's ensure is what must find the adopted process.
        daemon = SimpleNamespace(executors=registry,
                                 ensure_executor=registry.ensure)
        facade = PlaywrightFacade(
            cfg=Config(), port=0, host="127.0.0.1", daemon=daemon)
        port = await facade.start()
        async with websockets.connect(
                f"ws://127.0.0.1:{port}/exec?session={sid}") as ws:
            request = protocol.ExecuteRequest(
                "print('still here')", 5000, executor_id=executor_id)
            await ws.send(json.dumps(request.to_dict()))
            response = protocol.ExecuteResponse.from_dict(
                json.loads(await ws.recv()))

        assert response.console == "adopted process: print('still here')\n"
        assert proc.poll() is None
    finally:
        if facade is not None:
            await facade.stop()
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)
