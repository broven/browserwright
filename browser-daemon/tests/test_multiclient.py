"""v0.3 integration tests — §9.3 multi-client scenarios via real sockets.

We reuse the FakeUpstream + daemon fixtures from test_serve. The fake upstream
needs a small extension: it must answer `Target.attachToTarget` with a
sessionId and also push fake session-scoped events on demand so we can
verify event isolation.
"""
from __future__ import annotations

import asyncio
import http
import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest
import websockets

from browser_daemon import _ipc
from browser_daemon.config import load
from browser_daemon.server import listener as listener_mod


# ---- richer fake upstream -------------------------------------------------


class _MulticlientFakeUpstream:
    """Echo-style CDP server with:
    - Browser.getVersion always answers
    - Target.attachToTarget allocates a fresh sessionId per target
    - Target.getTargets returns the known targets
    - exposed `push_event` lets the test inject upstream-originated frames
    """
    def __init__(self):
        self.url: str = ""
        self.server: websockets.asyncio.server.Server | None = None
        self.ws: websockets.ServerConnection | None = None
        self.received: list[dict] = []
        self.target_to_session: dict[str, str] = {}
        self._next_sid = 0

    def _allocate_sid(self, target_id: str) -> str:
        # Reuse if already allocated — Chrome behavior.
        if target_id in self.target_to_session:
            return self.target_to_session[target_id]
        self._next_sid += 1
        sid = f"USID-{self._next_sid:03d}"
        self.target_to_session[target_id] = sid
        return sid

    async def start(self):
        async def handler(ws):
            self.ws = ws
            try:
                async for raw in ws:
                    msg = json.loads(raw if isinstance(raw, str) else raw.decode())
                    self.received.append(msg)
                    method = msg.get("method", "")
                    if method == "Browser.getVersion":
                        await ws.send(json.dumps({
                            "id": msg["id"],
                            "result": {"product": "FakeChrome/1.0"},
                        }))
                    elif method == "Target.setDiscoverTargets":
                        await ws.send(json.dumps({"id": msg["id"], "result": {}}))
                    elif method == "Target.attachToTarget":
                        tid = msg.get("params", {}).get("targetId")
                        sid = self._allocate_sid(tid)
                        await ws.send(json.dumps({
                            "id": msg["id"],
                            "result": {"sessionId": sid},
                        }))
                    elif method == "Target.detachFromTarget":
                        # Just ack — upstream session goes away.
                        sid = msg.get("params", {}).get("sessionId")
                        # Drop from our map so re-attach gets a fresh sid.
                        for tid, s in list(self.target_to_session.items()):
                            if s == sid:
                                del self.target_to_session[tid]
                        await ws.send(json.dumps({"id": msg["id"], "result": {}}))
                    elif method == "Target.getTargets":
                        await ws.send(json.dumps({
                            "id": msg["id"],
                            "result": {"targetInfos": []},
                        }))
                    else:
                        # Generic ack
                        if "id" in msg:
                            await ws.send(json.dumps({"id": msg["id"], "result": {}}))
            except websockets.exceptions.ConnectionClosed:
                pass

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.server = await websockets.serve(handler, "127.0.0.1", port,
                                             compression=None)
        self.url = f"ws://127.0.0.1:{port}/"

    async def push_event(self, frame: dict) -> None:
        """Send a fake event from upstream to the daemon."""
        # Wait briefly for the daemon-side ws to be established.
        for _ in range(50):
            if self.ws is not None:
                break
            await asyncio.sleep(0.02)
        if self.ws is None:
            raise RuntimeError("no daemon-side ws connection")
        await self.ws.send(json.dumps(frame))

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def short_runtime(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="bd-mc-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    monkeypatch.setenv("TMPDIR", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def fake_upstream():
    u = _MulticlientFakeUpstream()
    await u.start()
    try:
        yield u
    finally:
        await u.stop()


@pytest.fixture
def patched_resolver(monkeypatch, fake_upstream):
    from browser_daemon.backends.base import ResolveResult

    async def fake_resolve(cfg):
        return ResolveResult(ws_url=fake_upstream.url, backend="env", extras={})

    monkeypatch.setattr(listener_mod, "resolve", fake_resolve)
    return fake_upstream


@pytest.fixture
async def daemon(short_runtime, patched_resolver):
    cfg = load(env={"NO_PROXY": "127.0.0.1,localhost"}, cli_name="mc")
    cfg.backend = "env"
    cfg.timeout = 5.0
    task = asyncio.create_task(listener_mod.run_serve(cfg))
    for _ in range(30):
        await asyncio.sleep(0.05)
        if _ipc.sock_path(cfg.name).exists():
            break
    else:
        task.cancel()
        raise RuntimeError("daemon never bound")
    try:
        yield cfg
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _ipc.cleanup_endpoint(cfg.name)


async def _client(sock_path: Path, label: str):
    return await websockets.unix_connect(
        str(sock_path),
        uri=f"ws://localhost/?client={label}",
        compression=None,
        open_timeout=3.0,
    )


async def _recv_response(ws, request_id: int, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"no response for id={request_id}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg = json.loads(raw)
        if msg.get("id") == request_id:
            return msg


# ---- §9.3 cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_two_clients_attach_same_target_second_gets_minus_32602(daemon, fake_upstream):
    """Spec §9.3 case 1: two clients both `Target.attachToTarget(X)`. Second
    gets `-32602`, first unaffected, first's sessionId still works for
    subsequent commands.
    """
    sock = _ipc.sock_path(daemon.name)
    async with await _client(sock, "alice") as alice, \
               await _client(sock, "bob") as bob:
        # Alice attaches.
        await alice.send(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "TARGET-X"},
        }))
        resp = await _recv_response(alice, 1)
        alice_sid = resp["result"]["sessionId"]

        # Bob tries the same target. Must get -32602.
        await bob.send(json.dumps({
            "id": 2, "method": "Target.attachToTarget",
            "params": {"targetId": "TARGET-X"},
        }))
        resp = await _recv_response(bob, 2)
        assert "error" in resp
        assert resp["error"]["code"] == -32602

        # Alice's session is still usable — issue a command on it.
        await alice.send(json.dumps({
            "id": 3, "method": "Page.navigate",
            "sessionId": alice_sid,
            "params": {"url": "https://example.com/"},
        }))
        resp = await _recv_response(alice, 3)
        assert "result" in resp


@pytest.mark.asyncio
async def test_event_isolation_per_session(daemon, fake_upstream):
    """Spec §9.3 case 2: client A attached target X, client B attached target
    Y. A `Network.responseReceived` event on X's session reaches only A; on
    Y's session only B. A browser-level event broadcasts to both.
    """
    sock = _ipc.sock_path(daemon.name)
    async with await _client(sock, "alice") as alice, \
               await _client(sock, "bob") as bob:
        # Alice attaches X.
        await alice.send(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "X"},
        }))
        await _recv_response(alice, 1)
        # Bob attaches Y.
        await bob.send(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "Y"},
        }))
        await _recv_response(bob, 1)

        # Upstream session ids are fake_upstream.target_to_session["X"|"Y"].
        usid_x = fake_upstream.target_to_session["X"]
        usid_y = fake_upstream.target_to_session["Y"]

        # Inject a session-X event from upstream.
        await fake_upstream.push_event({
            "method": "Network.responseReceived",
            "sessionId": usid_x,
            "params": {"requestId": "R-X-1"},
        })
        # Inject a session-Y event from upstream.
        await fake_upstream.push_event({
            "method": "Network.responseReceived",
            "sessionId": usid_y,
            "params": {"requestId": "R-Y-1"},
        })
        # Inject a browser-level event (no sessionId).
        await fake_upstream.push_event({
            "method": "Target.targetCreated",
            "params": {"targetInfo": {
                "targetId": "Z", "type": "page",
                "url": "https://z/", "title": "Z",
            }},
        })

        # Collect what each side received.
        alice_events: list[dict] = []
        bob_events: list[dict] = []
        # Drain ~0.5s of events from each.
        async def drain(ws, bucket):
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.4)
                    bucket.append(json.loads(raw))
            except asyncio.TimeoutError:
                return
        await asyncio.gather(drain(alice, alice_events), drain(bob, bob_events))

        # Alice should have seen Network.responseReceived for R-X-1.
        alice_net = [e for e in alice_events
                     if e.get("method") == "Network.responseReceived"]
        assert any(e["params"]["requestId"] == "R-X-1" for e in alice_net), \
            f"alice missed her event: {alice_net}"
        assert not any(e["params"].get("requestId") == "R-Y-1" for e in alice_net), \
            "alice received bob's event"

        bob_net = [e for e in bob_events
                   if e.get("method") == "Network.responseReceived"]
        assert any(e["params"]["requestId"] == "R-Y-1" for e in bob_net), \
            f"bob missed his event: {bob_net}"
        assert not any(e["params"].get("requestId") == "R-X-1" for e in bob_net), \
            "bob received alice's event"

        # Both saw the browser-level Target.targetCreated.
        for events, name in [(alice_events, "alice"), (bob_events, "bob")]:
            assert any(e.get("method") == "Target.targetCreated" for e in events), \
                f"{name} missed browser-level broadcast"


@pytest.mark.asyncio
async def test_shared_read_second_attacher_receives_events_but_not_commands(daemon, fake_upstream):
    """Spec §9.3 case 3: opt-in shared read. Second attach with
    `flags.allowSecondaryReadOnly=true` gets a readonly session.
    Read-only client receives events; commands return -32602.
    """
    sock = _ipc.sock_path(daemon.name)
    async with await _client(sock, "primary") as primary, \
               await _client(sock, "reader") as reader:
        # Primary attaches first.
        await primary.send(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "SHARED"},
        }))
        primary_resp = await _recv_response(primary, 1)
        primary_sid = primary_resp["result"]["sessionId"]

        # Reader attaches with shared-read flag.
        await reader.send(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {
                "targetId": "SHARED",
                "flags": {"allowSecondaryReadOnly": True},
            },
        }))
        reader_resp = await _recv_response(reader, 1)
        reader_sid = reader_resp["result"]["sessionId"]
        assert primary_sid != reader_sid  # different local sessions
        # The upstream session is the same; reader is shadowing primary.

        # Reader tries to issue a command. Must be -32602 daemon-side.
        await reader.send(json.dumps({
            "id": 2, "method": "Page.navigate",
            "sessionId": reader_sid,
            "params": {"url": "https://r/"},
        }))
        resp = await _recv_response(reader, 2)
        assert resp["error"]["code"] == -32602

        # An upstream event on SHARED's session should reach BOTH.
        usid = fake_upstream.target_to_session["SHARED"]
        await fake_upstream.push_event({
            "method": "Network.responseReceived",
            "sessionId": usid,
            "params": {"requestId": "R-SHARED-1"},
        })
        # Drain each side briefly.
        async def find_event(ws, want_rid, timeout):
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if (msg.get("method") == "Network.responseReceived"
                        and msg.get("params", {}).get("requestId") == want_rid):
                    return msg
            return None
        p, r = await asyncio.gather(
            find_event(primary, "R-SHARED-1", 1.0),
            find_event(reader, "R-SHARED-1", 1.0),
        )
        assert p is not None, "primary missed shared-session event"
        assert r is not None, "reader missed shared-session event"
        assert p["sessionId"] == primary_sid
        assert r["sessionId"] == reader_sid
