"""Issue #106 -- the GH#79 supersede must not livelock a pre-0.17.2 extension.

GH#79 made the relay force-close the older connection when a second `hello`
arrives for the same `install_id`. The extension-side half of that fix
(socket-bound handlers) first shipped in 0.17.2. Before it, a socket's `onclose`
nulled the module-level `ws` unconditionally, so *every* close of a superseded
socket abandons the extension's live socket and `maintainLoop` dials again. That
dial supersedes the live socket, whose close strands the next one:

    superseding older relay connection ... (the extension dialled twice)
    extension hello (reconnect) ...
    force-closing stale extension relay connection ... superseded ...

repeating about twice a second, forever. Every in-flight request dies with
"non-replayable extension request lost its connection".

This drives the *verbatim* v0.15.0 ws-lifecycle code (the Chrome Web Store
build in the report, vendored in `fixtures/`) in Node, over real websockets,
against a real `RelayServer`, and fires one ordinary `forceReconnect()`.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from browserwright.daemon.server import relay as relay_mod
from browserwright.daemon.server.relay import RelayServer

LEGACY_JS = Path(__file__).parent / "fixtures" / "legacy_ws_lifecycle_v0_15_0.js"

_PRELUDE = """
const RELAY_URL = "ws://127.0.0.1:%(port)d/";
const BROWSERWRIGHT_EXTENSION_PROTOCOL_VERSION = "2";
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000, 10000];
const SERVER_PONG_STALE_MS = 25000, LEGACY_PONG_STALE_MS = 45000;
let ws = null, reconnectIdx = 0, lastPongTs = 0, lastInboundFrameTs = 0;
let seenServerPing = false;
const attachedTabs = new Set();
const chrome = { runtime: { getManifest: () => ({ version: "0.15.0" }) } };
async function getInstallId() { return "bd-legacy"; }
async function announceAttached() {}
async function handleDaemonMessage(msg) {
  if (msg.type === "ping") { seenServerPing = true; safeSend({ type: "pong" }); }
}
"""

_SCENARIO = """
maintainLoop();
// One ordinary reconnect once the first connection is up (a stale heartbeat,
// a service-worker wake) -- enough to open one duplicate on a legacy build.
setTimeout(() => forceReconnect("test trigger"), 1500);
setTimeout(() => process.exit(0), %(run_ms)d);
"""


class _HelloCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.hellos = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("extension hello"):
            self.hellos += 1


@pytest.mark.asyncio
async def test_one_duplicate_dial_from_a_legacy_extension_settles(
        monkeypatch, tmp_path):
    # Compress the relay's stale-frame reaper so the run also covers the slow
    # variant of the loop: the superseded socket's pongs go out on the *live*
    # socket (legacy `safeSend` uses the global `ws`), so the reaper would close
    # it -- and that close strands the live socket just like a supersede does.
    monkeypatch.setattr(relay_mod, "APP_PING_INTERVAL", 0.2)
    monkeypatch.setattr(relay_mod, "STALE_FRAME_AFTER", 1.0)
    counter = _HelloCounter()
    relay_logger = logging.getLogger(relay_mod.__name__)
    relay_logger.addHandler(counter)
    monkeypatch.setattr(relay_logger, "level", logging.INFO)

    relay = RelayServer(port=0)
    await relay.start()
    try:
        script = tmp_path / "legacy_ext.mjs"
        script.write_text(
            _PRELUDE % {"port": relay.port}
            + LEGACY_JS.read_text(encoding="utf-8")
            + _SCENARIO % {"run_ms": 6000},
            encoding="utf-8",
        )
        proc = await asyncio.create_subprocess_exec("node", str(script))
        assert await proc.wait() == 0
        # One first connect plus the single duplicate the trigger opens. A
        # livelock reconnects about twice a second for the whole run.
        assert counter.hellos <= 3, (
            f"{counter.hellos} hellos in 6s: the relay and the legacy "
            "extension are superseding each other in a loop")
    finally:
        relay_logger.removeHandler(counter)
        await relay.stop()
