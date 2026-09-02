"""Issue #79 -- the relay socket handlers must be bound to *their own* socket.

`chrome-extension/background.js` keeps the live relay socket in a module-level
`ws`. Every handler (`onopen` / `onmessage` / `onclose`) used to read and write
that global unconditionally, so a **superseded** socket -- one that was replaced
by `forceReconnect()` but whose close event had not landed yet -- could still
mutate the live connection's state.

Two consequences, both observed as "the service worker disconnects and
reconnects between commands":

* a stale `onclose` nulls the global `ws` that now points at the *new* socket,
  so `maintainLoop` sees "no socket" and dials a **second** connection: the
  daemon logs two `extension hello` lines with the same `install_id` ~1s apart
  (the maintainLoop tick), and one of the two sockets is orphaned;
* an orphan's `onmessage` keeps refreshing `lastInboundFrameTs`, so
  `wsLooksHealthy()` reports the *live* socket healthy on the strength of
  frames that arrived on a socket nobody reads any more.

These tests extract the real ws-lifecycle functions from `background.js` and run
them in Node against a fake WebSocket, in the same style as
`test_extension_title_marker_unit.py` -- no Chrome, no extension runtime.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"

_WANTED = ("connect", "wsLooksHealthy", "forceReconnect", "safeSend",
           "sendHello")


def _function_decl(source: str, name: str) -> str:
    match = re.search(
        rf"\nfunction {name}\([^)]*\) \{{(?P<body>.*?)\n\}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{name} function not found in background.js"
    return match.group(0)


def _harness(scenario: str) -> dict:
    """Run `scenario` with the real ws-lifecycle functions loaded in Node."""
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    decls = "\n".join(_function_decl(source, name) for name in _WANTED)
    program = f"""
// ---- stubs -------------------------------------------------------------
const sockets = [];
class WebSocket {{
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) {{
    this.url = url;
    this.readyState = WebSocket.CONNECTING;
    this.sent = [];
    this.closedWith = null;
    sockets.push(this);
  }}
  send(data) {{ this.sent.push(data); }}
  close(code, reason) {{
    // Real sockets close asynchronously: the event lands later, via
    // `fireClose()` in the scenario, not from inside close().
    this.closedWith = {{ code, reason }};
    this.readyState = WebSocket.CLOSING;
  }}
  fireOpen() {{ this.readyState = WebSocket.OPEN; return this.onopen && this.onopen(); }}
  fireClose() {{ this.readyState = WebSocket.CLOSED; if (this.onclose) this.onclose(); }}
  fireMessage(obj) {{ if (this.onmessage) this.onmessage({{data: JSON.stringify(obj)}}); }}
}}
globalThis.WebSocket = WebSocket;
const RELAY_URL = "ws://127.0.0.1:19989/";
const BROWSERWRIGHT_EXTENSION_PROTOCOL_VERSION = "2";
const SERVER_PONG_STALE_MS = 25000;
const LEGACY_PONG_STALE_MS = 45000;
let ws = null;
let reconnectIdx = 0;
let installId = "bd-test";
let lastPongTs = 0;
let lastInboundFrameTs = 0;
let seenServerPing = false;
let daemonVersion = null;
const attachedTabs = new Set();
async function getInstallId() {{ return installId; }}
async function reannounceInstallIdWhenReadable() {{}}
async function announceAttached() {{}}
async function handleDaemonMessage() {{}}
const chrome = {{ runtime: {{ getManifest: () => ({{version: "0.0.0"}}) }} }};

{decls}

// ---- scenario ----------------------------------------------------------
(async () => {{
  const out = await (async () => {{ {scenario} }})();
  process.stdout.write(JSON.stringify(out));
}})();
"""
    proc = subprocess.run(
        ["node"], input=program, text=True, capture_output=True,
        timeout=15, cwd=ROOT,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_superseded_socket_close_does_not_null_the_live_socket():
    """The core race: an old socket's close event must not clear the new `ws`.

    `forceReconnect()` replaces `ws` immediately but the old socket's `onclose`
    only lands afterwards. If that handler writes the global unconditionally,
    `ws` becomes null while a perfectly good socket is open -- and
    `maintainLoop` then dials a duplicate.
    """
    result = _harness(
        """
        connect();                 // socket A
        const a = sockets[0];
        await a.fireOpen();
        forceReconnect("stale");   // closes A, opens socket B
        const b = sockets[1];
        await b.fireOpen();
        a.fireClose();             // A's close event lands LATE
        return {
          sockets: sockets.length,
          wsIsB: ws === b,
          wsIsNull: ws === null,
        };
        """
    )
    assert result["sockets"] == 2, result
    assert result["wsIsB"] is True, (
        "a superseded socket's close event cleared the live connection; "
        "maintainLoop will now dial a duplicate"
    )
    assert result["wsIsNull"] is False, result


def test_superseded_socket_does_not_send_a_second_hello():
    """A socket that lost the race must not announce itself to the relay.

    This is what the daemon sees as two `extension hello` lines with the same
    `install_id` seconds apart -- the log evidence issue #79 was filed on.
    """
    result = _harness(
        """
        connect();                 // socket A (still CONNECTING)
        const a = sockets[0];
        forceReconnect("stale");   // A superseded by socket B before it opened
        const b = sockets[1];
        await b.fireOpen();
        await a.fireOpen();        // A opens anyway -- it was already dialling
        const hello = (s) => s.sent.filter(
          (m) => JSON.parse(m).type === "hello").length;
        return {sentA: hello(a), sentB: hello(b), aClosed: !!a.closedWith};
        """
    )
    assert result["sentB"] == 1, result
    assert result["sentA"] == 0, (
        "a superseded socket sent its own `hello`: the daemon sees two live "
        "connections for one install_id"
    )


def test_superseded_socket_frames_do_not_refresh_liveness():
    """An orphan's inbound frames must not make the live socket look healthy.

    `wsLooksHealthy()` is the daemon-independent staleness check that drives
    `forceReconnect`. If a superseded socket refreshes `lastInboundFrameTs`,
    a genuinely dead live socket is reported healthy and never replaced.
    """
    result = _harness(
        """
        connect();
        const a = sockets[0];
        await a.fireOpen();
        forceReconnect("stale");
        const b = sockets[1];
        await b.fireOpen();
        // The live socket goes quiet well past the stale window...
        lastPongTs = Date.now() - 120000;
        lastInboundFrameTs = Date.now() - 120000;
        // ...while the orphan keeps receiving frames.
        a.fireMessage({type: "pong", ts: Date.now()});
        return {healthy: wsLooksHealthy()};
        """
    )
    assert result["healthy"] is False, (
        "frames on a superseded socket refreshed the live socket's liveness "
        "timestamps"
    )
