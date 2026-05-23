"""v0.5 observability tests: metrics counters + JSON log formatter + stats
subcommand end-to-end.

Counters: increment-at-call-site assertions for the hot paths.
JSON logs: format-conforming output via `BD_LOG_JSON=1`.
Stats CLI: query a live daemon over its unix socket and verify the snapshot.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import websockets

from browserwright.daemon import _ipc
from browserwright.daemon.config import load
from browserwright.daemon.observability import (
    JSONLogFormatter, Metrics, install_json_logging_if_requested,
    metrics, reset_metrics_for_test,
)
from browserwright.daemon.server import listener as listener_mod


# ---- Metrics dataclass ----------------------------------------------------


def test_metrics_snapshot_includes_every_counter_and_uptime():
    reset_metrics_for_test()
    m = metrics()
    snap = m.snapshot()
    # Stable schema: a representative sample of expected keys.
    for k in (
        "client_connected_total", "client_disconnected_total",
        "client_frame_received_total",
        "upstream_open_attempts_total", "upstream_open_succeeded_total",
        "upstream_open_failed_total", "upstream_closed_total",
        "proxy_pre_open_buffered_total", "proxy_pre_open_overflow_total",
        "proxy_pre_open_drained_total",
        "auth_headers_resolved_total", "auth_resolution_failures_total",
        "uptime_seconds",
    ):
        assert k in snap, f"missing key {k!r}"
    assert isinstance(snap["uptime_seconds"], float)
    assert snap["uptime_seconds"] >= 0


def test_metrics_reset_zeroes_counters_and_advances_started_at():
    reset_metrics_for_test()
    m = metrics()
    m.client_connected_total = 42
    m.proxy_pre_open_overflow_total = 7
    old_start = m.started_at
    import time
    time.sleep(0.01)
    m.reset()
    assert m.client_connected_total == 0
    assert m.proxy_pre_open_overflow_total == 0
    assert m.started_at > old_start


def test_metrics_singleton_returns_same_instance():
    reset_metrics_for_test()
    a = metrics()
    b = metrics()
    assert a is b


# ---- JSON log formatter ---------------------------------------------------


def test_json_log_formatter_emits_valid_json_with_schema():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="browserwright.daemon.test", level=logging.INFO,
        pathname=__file__, lineno=42, msg="hello %s", args=("world",),
        exc_info=None,
    )
    out = formatter.format(record)
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "browserwright.daemon.test"
    assert payload["msg"] == "hello world"
    assert "ts" in payload and payload["ts"].endswith("Z")


def test_json_log_formatter_surfaces_extra_kwargs():
    """`logger.info('m', extra={'client_id': 7})` shows up in the JSON
    record's `extra` field, not at the top level."""
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=1,
        msg="m", args=(), exc_info=None,
    )
    record.client_id = 7
    record.tab_id = "abc"
    out = formatter.format(record)
    payload = json.loads(out)
    assert payload["extra"]["client_id"] == 7
    assert payload["extra"]["tab_id"] == "abc"


def test_install_json_logging_noop_when_env_not_set(monkeypatch):
    monkeypatch.delenv("BD_LOG_JSON", raising=False)
    changed = install_json_logging_if_requested()
    assert changed is False


def test_install_json_logging_replaces_formatter_when_env_set(monkeypatch):
    monkeypatch.setenv("BD_LOG_JSON", "1")
    root = logging.getLogger()
    # Make sure there's at least one handler so we can verify formatter swap.
    h = logging.StreamHandler(io.StringIO())
    h.setFormatter(logging.Formatter("plain: %(message)s"))
    root.addHandler(h)
    try:
        changed = install_json_logging_if_requested()
        assert changed is True
        assert isinstance(h.formatter, JSONLogFormatter)
    finally:
        root.removeHandler(h)


# ---- counter wiring (smoke) -----------------------------------------------


@pytest.mark.asyncio
async def test_pre_open_overflow_counter_increments():
    """Drive the proxy's overflow path and verify the counter ticks.
    Mirrors the existing proxy unit-test for #76 but asserts on metrics
    instead of the response shape."""
    from browserwright.daemon.server.proxy import Router
    from browserwright.daemon.server.state import (
        DaemonState, UpstreamPhase, PRE_OPEN_BUFFER_LIMIT,
    )

    reset_metrics_for_test()
    state = DaemonState(name="t", backend_name="x")
    state.upstream_phase = UpstreamPhase.DISCONNECTED
    router = Router(state)

    async def _send(_text: str) -> None:
        pass

    async def _ensure() -> None:
        pass

    async def _disc(_reason: str) -> None:
        pass

    client = state.allocate_client("c1")
    router.register_client(client.client_id, _send)
    router.bind_lifecycle(_ensure, _disc)

    # Fill the buffer + overflow.
    for i in range(PRE_OPEN_BUFFER_LIMIT + 2):
        await router.route_from_client(client, json.dumps({
            "id": i, "method": "Browser.getVersion",
        }))
    snap = metrics().snapshot()
    assert snap["proxy_pre_open_buffered_total"] == PRE_OPEN_BUFFER_LIMIT
    assert snap["proxy_pre_open_overflow_total"] == 2


# ---- stats CLI E2E --------------------------------------------------------


@pytest.fixture
def short_runtime(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="bd-o-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    monkeypatch.setenv("TMPDIR", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_browserwright_daemon_stats_method_returns_snapshot(
    short_runtime, monkeypatch,
):
    """End-to-end: a real daemon listener accepts a client + answers
    BrowserwrightDaemon.stats with the metrics snapshot."""
    reset_metrics_for_test()
    # Patch resolver so the daemon doesn't try to reach a real Chrome.
    from browserwright.daemon.backends.base import ResolveResult
    import socket
    import http
    from websockets.asyncio.server import serve as ws_serve_fn

    # Tiny fake upstream so set_connected can fire.
    async def _handler(ws):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg:
                    await ws.send(json.dumps({"id": msg["id"], "result": {}}))
        except websockets.exceptions.ConnectionClosed:
            pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    fake_up = await ws_serve_fn(_handler, "127.0.0.1", port, compression=None)
    fake_url = f"ws://127.0.0.1:{port}/"

    async def fake_resolve(cfg):
        return ResolveResult(ws_url=fake_url, backend="env", extras={})

    monkeypatch.setattr(listener_mod, "resolve", fake_resolve)

    cfg = load(env={"NO_PROXY": "127.0.0.1,localhost"}, cli_name="stats-test")
    cfg.backend = "env"
    cfg.timeout = 3.0

    task = asyncio.create_task(listener_mod.run_serve(cfg))
    # Wait for socket bind.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if _ipc.sock_path(cfg.name).exists():
            break

    try:
        # Connect a client + invoke BrowserwrightDaemon.stats.
        ws = await websockets.unix_connect(
            str(_ipc.sock_path(cfg.name)),
            uri="ws://localhost/?client=stats-test",
            compression=None,
        )
        try:
            await ws.send(json.dumps({"id": 1, "method": "BrowserwrightDaemon.stats"}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
            assert resp["id"] == 1
            snap = resp["result"]
            # The client we just opened bumped client_connected_total.
            assert snap["client_connected_total"] >= 1
            # uptime_seconds is float.
            assert isinstance(snap["uptime_seconds"], (int, float))
        finally:
            await ws.close()
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _ipc.cleanup_endpoint(cfg.name)
        fake_up.close()
        await fake_up.wait_closed()
