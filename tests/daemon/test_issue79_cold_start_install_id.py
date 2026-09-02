"""Issue #79 (cold start) -- `hello` must not be awaited behind MV3 storage.

Measured on a fresh Chrome profile with the extension freshly loaded: 25s after
launch the service worker exists, its relay websocket is `readyState === OPEN`,
its event loop is healthy (`setTimeout` fires in 65ms) and a **fresh**
`chrome.storage.local.get(["installId"])` answers in **2ms with the key already
present** -- while the very first one, the one `ws.onopen` is awaiting, is still
pending. An MV3 storage call issued during service-worker startup can simply
never settle.

`background.js` anticipated that promise *rejecting* (there is a comment about
it on the `catch`) but not never-settling, and a hang is not a rejection: the
socket stayed OPEN with no `hello` on it, so `/__status__` reported
`extensions: 0` for the whole window -- and everything reading that count went
blind with it: `wait_ready`, `doctor`, and the e2e fixture's "extension never
connected". The only recovery was the 45s `LEGACY_PONG_STALE_MS`
staleness sweep, which is where the measured 26s and 64s connect times came
from.

These tests extract the real `getInstallId` / `readStoredInstallId` / `connect`
out of `chrome-extension/background.js` and run them in Node against a
`chrome.storage.local` stub that can hang on demand -- no Chrome, no extension
runtime. Same technique as `test_extension_title_marker_unit.py`.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"

_WANTED = (
    "connect", "safeSend", "sendHello", "sleep",
    "getInstallId", "readStoredInstallId", "reannounceInstallIdWhenReadable",
)


def _function_decl(source: str, name: str) -> str:
    match = re.search(
        rf"\n(?:async )?function {name}\([^)]*\) \{{(?P<body>.*?)\n\}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{name} not found in background.js"
    return match.group(0)


def _harness(scenario: str, *, hang: str = "none", stored: str | None = None) -> dict:
    """Run `scenario` with the real install-id code loaded in Node.

    `hang`: "none" (storage always answers), "first" (only the FIRST get hangs
    -- the measured production behaviour), or "always".
    """
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    decls = "\n".join(_function_decl(source, name) for name in _WANTED)
    program = f"""
const sockets = [];
class WebSocket {{
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) {{
    this.url = url; this.readyState = WebSocket.CONNECTING;
    this.sent = []; this.closedWith = null; sockets.push(this);
  }}
  send(d) {{ this.sent.push(d); }}
  close(code, reason) {{
    this.closedWith = {{code, reason}}; this.readyState = WebSocket.CLOSING;
  }}
  fireOpen() {{ this.readyState = WebSocket.OPEN; return this.onopen && this.onopen(); }}
  fireClose() {{ this.readyState = WebSocket.CLOSED; if (this.onclose) this.onclose(); }}
}}
globalThis.WebSocket = WebSocket;
const RELAY_URL = "ws://127.0.0.1:19989/";
const BROWSERWRIGHT_EXTENSION_PROTOCOL_VERSION = "2";
// Shrunk so a 5-second production budget is a millisecond test. These are
// module-level tuning constants in background.js, not behaviour stubs.
const STORAGE_CALL_TIMEOUT_MS = 20;
const STORAGE_GET_ATTEMPTS = 5;
const INSTALL_ID_REANNOUNCE_DELAY_MS = 25;
const INSTALL_ID_REANNOUNCE_ATTEMPTS = 6;
const STORAGE_UNAVAILABLE = Symbol("storage-unavailable");
let ws = null;
let reconnectIdx = 0;
let installId = null;
let lastPongTs = 0;
let lastInboundFrameTs = 0;
const attachedTabs = new Set();
async function announceAttached() {{}}
async function handleDaemonMessage() {{}}

const store = {json.dumps({} if stored is None else {"installId": stored})};
const HANG = {json.dumps(hang)};
let gets = 0;
const writes = [];
const chrome = {{
  runtime: {{ getManifest: () => ({{version: "0.0.0"}}) }},
  storage: {{ local: {{
    get: (keys) => {{
      gets += 1;
      const shouldHang = HANG === "always" || (HANG === "first" && gets === 1);
      if (shouldHang) return new Promise(() => {{}});   // never settles
      const out = {{}};
      for (const k of [].concat(keys)) if (k in store) out[k] = store[k];
      return Promise.resolve(out);
    }},
    set: (obj) => {{
      const shouldHang = HANG === "always";
      writes.push(obj);
      Object.assign(store, obj);
      return shouldHang ? new Promise(() => {{}}) : Promise.resolve();
    }},
  }} }},
}};
const crypto = {{ getRandomValues: (a) => {{ a.fill(7); return a; }} }};

{decls}

(async () => {{
  const out = await (async () => {{ {scenario} }})();
  process.stdout.write(JSON.stringify(out));
  // Background retry loops hold timers; the scenario's answer is complete, so
  // do not wait for them.
  process.exit(0);
}})();
"""
    proc = subprocess.run(
        ["node"], input=program, text=True, capture_output=True,
        timeout=20, cwd=ROOT,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def _wrap_deadline(body: str, ms: int = 3000) -> str:
    """Run `body` against a wall-clock deadline so a hang fails as a hang."""
    return f"""
    const raced = await Promise.race([
      (async () => {{ {body} }})(),
      new Promise((r) => setTimeout(() => r({{__timedOut: true}}), {ms})),
    ]);
    return raced;
    """


def test_a_hanging_storage_read_does_not_block_the_install_id():
    """The defect itself: one never-settling `get` used to park `onopen` forever."""
    result = _harness(
        _wrap_deadline("const id = await getInstallId(); return {id};"),
        hang="first", stored="bd-persisted",
    )
    assert not result.get("__timedOut"), (
        "getInstallId() never returned: a single unsettled chrome.storage call "
        "parks the caller forever")
    assert result["id"] == "bd-persisted", result


def test_hello_is_sent_even_when_storage_never_answers():
    """`hello` is what makes the daemon see the extension at all.

    With storage permanently unavailable the connection must still announce
    itself -- the relay accepts a hello without an `installId`. Announcing
    nothing is what produced "extension never connected within 25s" against a
    socket that was OPEN the whole time.
    """
    result = _harness(
        _wrap_deadline("""
        connect();
        const a = sockets[0];
        await a.fireOpen();
        const hellos = a.sent.map((m) => JSON.parse(m))
                             .filter((m) => m.type === "hello");
        return {count: hellos.length, installId: hellos[0] && hellos[0].installId};
        """),
        hang="always",
    )
    assert not result.get("__timedOut"), "onopen never finished"
    assert result["count"] == 1, result
    assert result["installId"] == "", result


def test_an_unreadable_store_never_mints_a_competing_install_id():
    """The trap in fixing this: a new id would make `install_id` unstable.

    If storage cannot be read there may still be a persisted id behind it, so
    minting one and writing it over the top would create exactly the defect
    issue #79 wrongly reported as already existing.
    """
    result = _harness(
        _wrap_deadline("""
        const id = await getInstallId();
        return {id, writes: writes.length, cached: installId};
        """),
        hang="always", stored="bd-persisted",
    )
    assert not result.get("__timedOut"), result
    assert result["id"] is None, result
    assert result["writes"] == 0, (
        "an unreadable store was overwritten with a fresh id; the persisted "
        "identity is now lost")
    assert result["cached"] is None, result


def test_a_readable_empty_store_still_mints_and_persists_an_id():
    """The ordinary first-run path must keep working."""
    result = _harness(
        "const id = await getInstallId();"
        " return {id, writes: writes.length, stored: store.installId};",
        hang="none", stored=None,
    )
    assert result["id"].startswith("bd-"), result
    assert result["writes"] == 1, result
    assert result["stored"] == result["id"], result


def test_persisting_a_new_id_does_not_block_hello():
    """`storage.set` is on the same API that just hung; it must not be awaited."""
    result = _harness(
        _wrap_deadline("""
        connect();
        const a = sockets[0];
        await a.fireOpen();
        const hellos = a.sent.map((m) => JSON.parse(m))
                             .filter((m) => m.type === "hello");
        return {count: hellos.length,
                installId: hellos[0] && hellos[0].installId};
        """),
        hang="always", stored=None,
    )
    assert not result.get("__timedOut"), (
        "onopen parked on chrome.storage.local.set")
    assert result["count"] == 1, result


def test_a_late_readable_store_is_re_announced():
    """Identity is restored without a second one being invented.

    A hello with no `installId` costs the daemon its reconnect matching, so the
    extension re-announces once storage answers -- with the id it finally read,
    never a fresh one.
    """
    result = _harness(
        _wrap_deadline("""
        connect();
        const a = sockets[0];
        await a.fireOpen();
        // Storage comes back: `hang: "first"` means every later get answers.
        const deadline = Date.now() + 2000;
        while (Date.now() < deadline) {
          const hellos = a.sent.map((m) => JSON.parse(m))
                               .filter((m) => m.type === "hello");
          if (hellos.length > 1) {
            return {ids: hellos.map((h) => h.installId), writes: writes.length};
          }
          await sleep(25);
        }
        const hellos = a.sent.map((m) => JSON.parse(m))
                             .filter((m) => m.type === "hello");
        return {ids: hellos.map((h) => h.installId), writes: writes.length};
        """, ms=3000),
        hang="first", stored="bd-persisted",
    )
    assert not result.get("__timedOut"), result
    assert result["ids"][-1] == "bd-persisted", result
    assert result["writes"] == 0, result
