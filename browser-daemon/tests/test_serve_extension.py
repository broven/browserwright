"""End-to-end Mode B serve with `--backend extension` (v0.4).

These tests start a real daemon (unix socket listener + relay ws server),
connect a mock extension to the relay, then a Skill-style client to the
daemon's unix socket, and verify that standard CDP frames flow through
the extension translation layer correctly.

Compared to the conventional `tests/test_serve.py` setup, the upstream is
NOT a fake-Chrome ws server — there is no upstream ws at all. The daemon
IS the upstream (LOCAL_RELAY kind), and the mock extension feeds responses
through the daemon-owned relay server.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest
import websockets

from browser_daemon import _ipc
from browser_daemon.config import load
from browser_daemon.server import listener as listener_mod
from browser_daemon.server.relay import DEFAULT_RELAY_PORT

from tests.test_relay import _MockExtension  # type: ignore


# ---- fixture: extension-mode daemon --------------------------------------


@pytest.fixture
def short_runtime(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="bd-x-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    monkeypatch.setenv("TMPDIR", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def patch_relay_port():
    """Pick an ephemeral relay port so concurrent tests don't fight over 19989.

    v0.5.3 Task #24: the canonical way to override the port is now
    `cfg.backends.extension.port` (set by toml / env / CLI). Tests use the
    same knob — no more class-level monkeypatching needed.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def ext_daemon(short_runtime, patch_relay_port):
    """Start a daemon in extension-backend mode. Yields (cfg, relay_port)."""
    cfg = load(env={"NO_PROXY": "127.0.0.1,localhost"}, cli_name="serve-x")
    cfg.backend = "extension"
    cfg.backends.extension.port = patch_relay_port  # Task #24 knob
    cfg.timeout = 2.0

    task = asyncio.create_task(listener_mod.run_serve(cfg))
    # Wait for the unix socket to be ready.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if _ipc.sock_path(cfg.name).exists():
            break
    else:
        task.cancel()
        raise RuntimeError("daemon never bound")

    # Wait for the relay HTTP /__status__ endpoint to answer.
    deadline = asyncio.get_running_loop().time() + 3.0
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=0.3) as client:
                r = await client.get(
                    f"http://127.0.0.1:{patch_relay_port}/__status__")
                if r.status_code == 200:
                    break
        except Exception:
            await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise RuntimeError("relay /__status__ never responded")

    try:
        yield cfg, patch_relay_port
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _ipc.cleanup_endpoint(cfg.name)


async def _client_connect(sock_path: Path, *, label: str = "test-client"):
    return await websockets.unix_connect(
        str(sock_path),
        uri=f"ws://localhost/?client={label}",
        compression=None,
        open_timeout=3.0,
    )


async def _recv_response(ws, request_id: int, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"no response for id={request_id}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg = json.loads(raw)
        if msg.get("id") == request_id:
            return msg


# ---- doctor probe sees relay --------------------------------------------


@pytest.mark.asyncio
async def test_doctor_probe_sees_relay_when_serve_running(ext_daemon):
    """Spec §5.2: extension backend probe → available=true once an extension
    has connected. The relay is up; with zero connected extensions,
    available=false + needs_user_action."""
    cfg, port = ext_daemon

    from browser_daemon.backends.extension import ExtensionBackend
    backend = ExtensionBackend(cfg)
    backend._port = port  # noqa: SLF001 — test seam

    r = await backend.probe()
    assert r.available is False  # no extension connected yet
    assert "no Chrome extension has connected" in r.detail

    # Now connect a mock extension and re-probe.
    ext = _MockExtension()
    await ext.connect(port, install_id="install-A")
    # Give the relay a tick to ingest the hello.
    await asyncio.sleep(0.1)
    try:
        r2 = await backend.probe()
        assert r2.available is True
        assert "install-A" in r2.detail
    finally:
        await ext.close()


# ---- E2E: client → daemon → mock extension → response back ---------------


@pytest.mark.asyncio
async def test_end_to_end_target_get_targets_via_extension(ext_daemon):
    """Skill-style client sends Target.getTargets through the daemon's unix
    socket; the daemon's ExtensionUpstream answers from the relay's ghost
    target table, populated by the mock extension's `attached` push.
    """
    cfg, port = ext_daemon
    sock = _ipc.sock_path(cfg.name)

    # Mock extension connects + announces a tab BEFORE the client.
    ext = _MockExtension()
    await ext.connect(port, install_id="install-end-to-end")
    await asyncio.sleep(0.1)
    await ext.announce_attached(tab_id=42, url="https://e2e/", title="e2e")
    await asyncio.sleep(0.1)

    try:
        async with await _client_connect(sock, label="skill") as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Target.getTargets",
            }))
            resp = await _recv_response(ws, 1, timeout=5.0)
            target_infos = resp["result"]["targetInfos"]
            assert any(ti["targetId"] == "ext-tab-42"
                       and ti["url"] == "https://e2e/"
                       for ti in target_infos)
    finally:
        await ext.close()


@pytest.mark.asyncio
async def test_end_to_end_browser_crash_returns_method_not_found(ext_daemon):
    """Spec §8.4: unsupported browser-level commands → -32601 surfaced to
    the client through the daemon's normal id-translation path."""
    cfg, port = ext_daemon
    sock = _ipc.sock_path(cfg.name)

    ext = _MockExtension()
    await ext.connect(port, install_id="install-method-not-found")
    await asyncio.sleep(0.1)
    try:
        async with await _client_connect(sock, label="skill") as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Browser.crash",
            }))
            resp = await _recv_response(ws, 1, timeout=5.0)
            assert "error" in resp
            assert resp["error"]["code"] == -32601
    finally:
        await ext.close()
