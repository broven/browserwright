"""Issue #31: every chrome.debugger call in background.js must be bounded.

The extension relays `chrome.debugger.*` results back to the daemon as
`response` frames. When Chrome leaves one of those promises unsettled
forever (the OOPIF throttle, a wedged renderer), an unbounded `await` means
the extension never sends its frame, the daemon's `_ExtensionConn.pending`
future never resolves, and the caller eats the full `_request(timeout=10.0)`
while the extension-side promise leaks.

These tests follow the `test_extension_title_marker_unit.py` pattern: they
extract the real handlers from ``chrome-extension/background.js`` and run
them in Node with a mock `chrome` whose `chrome.debugger` promises NEVER
settle (or settle late, for the stale-completion semantics).

The daemon-side tests at the bottom lock the two sides' agreement: the
extension's budgets must stay below the matching `_request` timeouts in
relay.py (so the extension answers FIRST with a distinguishable -32001
error frame), and a -32001 error frame must survive the relay as a
`_CommandError` rather than the daemon's own bare `TimeoutError`.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from browserwright.daemon.server.relay import _CommandError

from .test_relay_reconnect_paths import _FakeExtension, _relay_running


ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"
RELAY_PY = ROOT / "src" / "browserwright" / "daemon" / "server" / "relay.py"

#: Probe wall-clock budget — how long a handler may take to answer. The
#: extension budgets are shrunk to tens of ms inside the probes, so this is
#: generous for a loaded CI box and tight against a real hang.
PROBE_WAIT_S = 3.0

#: Budget overrides injected into the extracted wrapper region. Deliberately
#: tiny: the probes assert *semantics* (a bounded answer, a distinguishable
#: error, no stale completion), not wall-clock durations.
TINY_BUDGET_MS = 30


def _region(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _function_decl(source: str, name: str) -> str:
    match = re.search(
        rf"function {name}\([^)]*\) \{{(?P<body>.*?)\n\}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{name} function not found"
    return match.group(0)


def _sources() -> dict[str, str]:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    return {
        "wrapperRegion": _region(
            source, "// ---- bounded chrome.debugger calls",
            "// Shared attach sequence:"),
        "attachTab": _region(
            source, "async function attachTab(", "\n// Shared detach cleanup:"),
        "detachTab": _region(
            source, "async function detachTab(", "\n\nasync function doAttach("),
        "doAttach": _region(
            source, "async function doAttach(", "\n\nasync function doAttachActive("),
        "doDetach": _region(
            source, "async function doDetach(", "\n\nasync function doCommand("),
        "doCommand": _region(
            source, "async function doCommand(", "\n\nasync function armAutoAttach("),
        "armAutoAttach": _region(
            source, "async function armAutoAttach(", "\n\nasync function doQueryGroup("),
        "errMessage": _function_decl(source, "errMessage"),
    }


def _run_node_probe(probe: str, payload: dict[str, str],
                    *, timeout: float = 15.0) -> dict[str, object]:
    proc = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


# ---- probes ----------------------------------------------------------------


def _run_do_command_timeout_probe(payload: dict[str, str]) -> dict[str, object]:
    """`doCommand` against a sendCommand that settles only when released.

    Reports the response frames sent before and after the late completion —
    the -32001 timeout answer, and proof that the late completion is
    discarded (no second frame, no state change).
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const frames = [];
const sent = [];
let release;
const gate = new Promise((resolve) => { release = resolve; });
const chrome = {
  debugger: {
    sendCommand(target, method, params) {
      sent.push({ tabId: target.tabId, method, params });
      return gate.then(() => ({ late: true }));   // Chrome settles LATE
    },
  },
};
const realm = vm.createContext({
  chrome,
  setTimeout, clearTimeout,
  console: { info() {}, warn() {}, error: console.error },
  safeSend: (frame) => frames.push(frame),
});
const wrapper = input.wrapperRegion
  .replace("const DEBUGGER_COMMAND_TIMEOUT_MS = 9000;",
           "const DEBUGGER_COMMAND_TIMEOUT_MS = 30;");
vm.runInContext(
  wrapper + "\n" + input.errMessage + "\n" + input.doCommand, realm);
(async () => {
  const call = vm.runInContext(
    'doCommand(9, 17, "Page.navigate", {url: "http://x/"})', realm);
  const deadline = Date.now() + 2000;
  while (frames.length === 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  const framesBeforeLate = frames.slice();
  release();  // the command lands AFTER the budget expired
  await call;
  await new Promise((resolve) => setTimeout(resolve, 30));
  process.stdout.write(JSON.stringify({
    framesBeforeLate,
    frames,
    sent: sent.length,
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    return _run_node_probe(probe, payload)


def _run_attach_timeout_probe(payload: dict[str, str], *,
                              release_mode: str) -> dict[str, object]:
    """`doAttach` against a chrome.debugger.attach that settles only when
    released, with a detach racing in mid-flight.

    release_mode="afterResponse" — the attach lands AFTER the budget
    expired: the answer must be the -32001 timeout error.
    release_mode="immediate" — the attach lands after the detach but
    before the budget: the answer is the (truthful) attach result, the tab
    is abandoned, no tracking, no cosmetics.

    Both modes must leave `attachedTabs` empty: the stale completion can
    never resurrect bookkeeping.
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const frames = [];
const calls = [];
let releaseAttach;
const attachGate = new Promise((resolve) => { releaseAttach = resolve; });
const chrome = {
  debugger: {
    attach(target) {
      calls.push({ kind: "attach", tabId: target.tabId });
      return attachGate;   // settles only when released
    },
    detach(target) {
      calls.push({ kind: "detach", tabId: target.tabId });
      return Promise.resolve();
    },
    sendCommand() {
      calls.push({ kind: "sendCommand" });
      return Promise.resolve({});
    },
  },
  tabs: {
    get: async (tabId) => ({ id: tabId, url: "http://x/", title: "Page" }),
  },
};
const realm = vm.createContext({
  chrome,
  setTimeout, clearTimeout,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  console: { info() {}, warn() {}, error: console.error },
  safeSend: (frame) => frames.push(frame),
});
const wrapper = input.wrapperRegion
  .replace("const DEBUGGER_ATTACH_TIMEOUT_MS = 3000;",
           "const DEBUGGER_ATTACH_TIMEOUT_MS = 30;");
vm.runInContext(`
  const PROTOCOL_VERSION = "1.3";
  const attachedTabs = new Set();
  function stripMarker(t) { return t || ""; }
  async function unmarkTabBeforeDetach() {}
  async function postAttachCosmetics() {}
  async function announceAttached() {}
  ${wrapper}
  ${input.errMessage}
  ${input.attachTab}
  ${input.detachTab}
  ${input.doAttach}
`, realm);
(async () => {
  const pending = vm.runInContext("doAttach(3, 17)", realm);
  while (!calls.some((c) => c.kind === "attach")) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  // A detach (drag-out / DevTools) races in while the attach is in flight.
  await vm.runInContext(
    'detachTab(17, { announceReason: "dragged_out_of_group" })', realm);
  if (input.releaseMode === "immediate") {
    releaseAttach();
  } else {
    // Wait for the timeout answer before releasing the attach.
    const deadline = Date.now() + 2000;
    while (!frames.some((f) => f.type === "response") && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    releaseAttach();
  }
  await pending;
  await new Promise((resolve) => setTimeout(resolve, 30));
  process.stdout.write(JSON.stringify({
    frames,
    calls,
    tracked: vm.runInContext("attachedTabs.has(17)", realm),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    return _run_node_probe(probe, {**payload, "releaseMode": release_mode})


def _run_detach_timeout_probe(payload: dict[str, str]) -> dict[str, object]:
    """`doDetach` against a chrome.debugger.detach that never settles, with a
    late landing after the timeout.

    The detach decision stands on both sides, so the timeout is reported as
    benign success and the late landing must not emit anything extra.
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const frames = [];
const calls = [];
let releaseDetach;
const detachGate = new Promise((resolve) => { releaseDetach = resolve; });
const chrome = {
  debugger: {
    detach(target) {
      calls.push({ kind: "detach", tabId: target.tabId });
      return detachGate;   // never settles until released
    },
    sendCommand() {
      calls.push({ kind: "sendCommand" });
      return Promise.resolve({});
    },
  },
};
const realm = vm.createContext({
  chrome,
  setTimeout, clearTimeout,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  console: { info() {}, warn() {}, error: console.error },
  safeSend: (frame) => frames.push(frame),
});
const wrapper = input.wrapperRegion
  .replace("const DEBUGGER_DETACH_TIMEOUT_MS = 3000;",
           "const DEBUGGER_DETACH_TIMEOUT_MS = 30;");
vm.runInContext(`
  const PROTOCOL_VERSION = "1.3";
  const attachedTabs = new Set([17]);
  async function unmarkTabBeforeDetach() {}
  ${wrapper}
  ${input.detachTab}
  ${input.doDetach}
`, realm);
(async () => {
  await vm.runInContext("doDetach(5, 17)", realm);
  const framesBeforeLate = frames.slice();
  releaseDetach();  // Chrome detaches LATE
  await new Promise((resolve) => setTimeout(resolve, 30));
  process.stdout.write(JSON.stringify({
    framesBeforeLate,
    frames,
    calls,
    tracked: vm.runInContext("attachedTabs.has(17)", realm),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    return _run_node_probe(probe, payload)


def _run_arm_hang_probe(payload: dict[str, str]) -> dict[str, object]:
    """`doAttach` where attach succeeds but Target.setDiscoverTargets hangs.

    Post-attach arming shares one small budget and must not hold the attach
    RPC response hostage: the response arrives with the attach result, the
    hung arm command is abandoned (setAutoAttach never issued).
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const frames = [];
const calls = [];
const chrome = {
  debugger: {
    attach() { calls.push({ kind: "attach" }); return Promise.resolve(); },
    detach() { calls.push({ kind: "detach" }); return Promise.resolve(); },
    sendCommand(target, method) {
      calls.push({ kind: "command", method });
      if (method === "Target.setDiscoverTargets") {
        return new Promise(() => {});   // wedged renderer
      }
      return Promise.resolve({});
    },
  },
  tabs: {
    get: async (tabId) => ({ id: tabId, url: "http://x/", title: "Page" }),
  },
};
const realm = vm.createContext({
  chrome,
  setTimeout, clearTimeout,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  console: { info() {}, warn() {}, error: console.error },
  safeSend: (frame) => frames.push(frame),
});
const wrapper = input.wrapperRegion
  .replace("const DEBUGGER_ATTACH_TIMEOUT_MS = 3000;",
           "const DEBUGGER_ATTACH_TIMEOUT_MS = 20;")
  .replace("const DEBUGGER_ARM_TIMEOUT_MS = 1500;",
           "const DEBUGGER_ARM_TIMEOUT_MS = 40;");
vm.runInContext(`
  const PROTOCOL_VERSION = "1.3";
  const attachedTabs = new Set();
  function stripMarker(t) { return t || ""; }
  async function postAttachCosmetics() {}
  ${wrapper}
  ${input.attachTab}
  ${input.armAutoAttach}
  ${input.doAttach}
`, realm);
(async () => {
  const deadline = Date.now() + 2000;
  while (frames.length === 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  await vm.runInContext("doAttach(3, 17)", realm).catch(() => {});
  await new Promise((resolve) => setTimeout(resolve, 30));
  process.stdout.write(JSON.stringify({
    frames,
    calls,
    tracked: vm.runInContext("attachedTabs.has(17)", realm),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    return _run_node_probe(probe, payload)


def _run_epoch_abandon_probe(payload: dict[str, str]) -> dict[str, object]:
    """attach succeeds, arming hangs, a detach races in mid-arm, then the arm
    command completes late: the whole sequence must be abandoned — no re-add,
    no `attached` announce, no cosmetics.
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const frames = [];
const calls = [];
const announced = [];
let releaseArm;
const armGate = new Promise((resolve) => { releaseArm = resolve; });
const chrome = {
  debugger: {
    attach() { calls.push({ kind: "attach" }); return Promise.resolve(); },
    detach() { calls.push({ kind: "detach" }); return Promise.resolve(); },
    sendCommand(target, method) {
      calls.push({ kind: "command", method });
      if (method === "Target.setDiscoverTargets") return armGate;
      return Promise.resolve({});
    },
  },
  tabs: {
    get: async (tabId) => ({ id: tabId, url: "http://x/", title: "Page" }),
  },
};
const realm = vm.createContext({
  chrome,
  setTimeout, clearTimeout,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  console: { info() {}, warn() {}, error: console.error },
  safeSend: (frame) => frames.push(frame),
});
const wrapper = input.wrapperRegion
  .replace("const DEBUGGER_ATTACH_TIMEOUT_MS = 3000;",
           "const DEBUGGER_ATTACH_TIMEOUT_MS = 20;")
  .replace("const DEBUGGER_ARM_TIMEOUT_MS = 1500;",
           "const DEBUGGER_ARM_TIMEOUT_MS = 5000;");
vm.runInContext(`
  const PROTOCOL_VERSION = "1.3";
  const attachedTabs = new Set();
  async function unmarkTabBeforeDetach() {}
  async function announceAttached(tabId) { announced.push(tabId); }
  ${wrapper}
  ${input.attachTab}
  ${input.armAutoAttach}
  ${input.detachTab}
`, realm);
(async () => {
  const pending = vm.runInContext("attachTab(17)", realm);
  while (!calls.some((c) => c.method === "Target.setDiscoverTargets")) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  // The tab is tracked at this point (attach succeeded, arm in flight).
  const trackedDuringArm = vm.runInContext("attachedTabs.has(17)", realm);
  await vm.runInContext(
    'detachTab(17, { announceReason: "dragged_out_of_group" })', realm);
  releaseArm();  // the arm command completes AFTER the detach
  await pending;
  await new Promise((resolve) => setTimeout(resolve, 30));
  process.stdout.write(JSON.stringify({
    frames,
    calls,
    announced,
    trackedDuringArm,
    tracked: vm.runInContext("attachedTabs.has(17)", realm),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    return _run_node_probe(probe, payload)


# ---- extension-side tests --------------------------------------------------


def test_never_settling_send_command_answers_with_distinguishable_timeout() -> None:
    """A chrome.debugger.sendCommand that never settles gets a bounded,
    distinguishable error answer instead of a leaked promise.

    This is the #31 failure shape: previously `doCommand` awaited forever,
    the daemon's pending future never resolved, and the caller ate the full
    10s `_request` timeout (repro committed in d41bd6e).
    """
    result = _run_do_command_timeout_probe(_sources())

    (frame,) = result["framesBeforeLate"]
    assert frame["type"] == "response"
    assert frame["id"] == 9
    err = frame["error"]
    assert err["code"] == -32001, "timeout must be distinguishable from a real CDP error"
    assert "timed out after 30ms" in err["message"]
    assert "Page.navigate tabId=17" in err["message"]
    assert "may still land" in err["message"]


def test_late_send_command_completion_is_discarded_no_double_response() -> None:
    """A command that lands AFTER its budget expired must not produce a
    second response frame (the daemon has already moved on) and must not
    mutate state."""
    result = _run_do_command_timeout_probe(_sources())

    assert result["sent"] == 1
    assert result["frames"] == result["framesBeforeLate"], (
        "the late completion produced an extra frame — stale completions "
        "must be discarded"
    )


def test_attach_timeout_is_bounded_and_stale_landing_cannot_resurrect_tracking() -> None:
    """chrome.debugger.attach that never settles, with a detach racing in:
    doAttach must answer within its budget, and once the attach lands —
    whether after the budget (timeout error) or before it (truthful success
    on an abandoned tab) — attachedTabs must stay empty."""
    sources = _sources()

    late = _run_attach_timeout_probe(sources, release_mode="afterResponse")
    (detached, response) = late["frames"]
    assert detached == {
        "type": "detached", "tabId": 17, "reason": "dragged_out_of_group",
    }
    assert response["id"] == 3
    assert response["error"]["code"] == -32001
    assert "attach timed out" in response["error"]["message"]
    assert late["tracked"] is False, (
        "the late attach landing resurrected bookkeeping for a tab that was "
        "detached while the attach was in flight"
    )

    immediate = _run_attach_timeout_probe(sources, release_mode="immediate")
    (detached, response) = immediate["frames"]
    assert detached["type"] == "detached"
    # The attach genuinely landed before the budget — the answer is truthful
    # (the tab IS attached in Chrome), but it is abandoned, not tracked.
    assert "targetInfo" in response["result"]
    assert immediate["tracked"] is False

    # Either way the attach was the only attempt, and the racing detach ran.
    for result in (late, immediate):
        assert [c["kind"] for c in result["calls"]] == ["attach", "detach"]


def test_detach_timeout_is_bounded_and_reported_benign() -> None:
    """chrome.debugger.detach that never settles: the detach decision stands
    (the daemon pops its ghost regardless), so doDetach reports benign
    success, clears tracking, and discards the late landing."""
    result = _run_detach_timeout_probe(_sources())

    (frame,) = result["framesBeforeLate"]
    assert frame == {"type": "response", "id": 5, "result": {}}
    assert result["tracked"] is False
    assert result["frames"] == result["framesBeforeLate"], (
        "the late detach landing emitted an extra frame"
    )
    assert [c["kind"] for c in result["calls"]] == ["detach"]


def test_hung_auto_attach_arm_cannot_hold_attach_response_hostage() -> None:
    """Post-attach arming (setDiscoverTargets/setAutoAttach) shares one small
    budget: a wedged renderer there must not push the attach RPC response
    past the daemon's wait. The response carries the attach result; the hung
    command is abandoned and the second arm command is never issued."""
    result = _run_arm_hang_probe(_sources())

    (frame,) = result["frames"]
    assert frame["id"] == 3
    assert "targetInfo" in frame["result"], frame
    assert result["tracked"] is True
    methods = [c["method"] for c in result["calls"] if c["kind"] == "command"]
    assert methods == ["Target.setDiscoverTargets"], (
        "setAutoAttach must be skipped once the shared arm budget is gone: "
        f"{methods}"
    )


def test_epoch_abandons_attach_when_detach_races_the_arm() -> None:
    """The \"completes after detach\" race, general path: attach succeeds,
    a detach races in while arming is in flight, and the arm command then
    completes late. The stale completion must not re-add the tab, announce
    `attached`, or run cosmetics."""
    result = _run_epoch_abandon_probe(_sources())

    assert result["trackedDuringArm"] is True
    assert result["tracked"] is False
    assert result["announced"] == [], (
        "a stale completion announced `attached` for a tab that left the session"
    )
    # attach → setDiscoverTargets → detach. No setAutoAttach, no announce.
    assert [c["kind"] for c in result["calls"]] == ["attach", "command", "detach"]
    assert result["frames"] == [
        {"type": "detached", "tabId": 17, "reason": "dragged_out_of_group"},
    ]


# ---- two sides agree -------------------------------------------------------


def test_extension_budgets_stay_below_daemon_request_timeouts() -> None:
    """The extension must answer FIRST: each DEBUGGER_*_TIMEOUT_MS budget in
    background.js must stay below the matching `_request` timeout in
    relay.py, so the daemon receives a distinguishable -32001 error frame
    instead of giving up with its own bare TimeoutError."""
    js = BACKGROUND_JS.read_text(encoding="utf-8")
    py = RELAY_PY.read_text(encoding="utf-8")

    def js_const(name: str) -> int:
        match = re.search(rf"const {name}\s*=\s*(\d+);", js)
        assert match is not None, f"const {name} not found in background.js"
        return int(match.group(1))

    def py_timeout(func: str) -> float:
        match = re.search(
            rf"def {func}\(.*?timeout: float = ([\d.]+)", py, re.S)
        assert match is not None, f"{func} timeout not found in relay.py"
        return float(match.group(1))

    command_ms = js_const("DEBUGGER_COMMAND_TIMEOUT_MS")
    attach_ms = js_const("DEBUGGER_ATTACH_TIMEOUT_MS")
    detach_ms = js_const("DEBUGGER_DETACH_TIMEOUT_MS")

    assert command_ms < py_timeout("send_cdp") * 1000, (
        "sendCommand budget must stay below relay send_cdp's timeout"
    )
    assert attach_ms < min(
        py_timeout("attach_tab"),
        py_timeout("attach_active_tab"),
        py_timeout("create_background_tab"),
    ) * 1000, "attach budget must stay below every daemon attach wait"
    assert detach_ms < py_timeout("detach_tab") * 1000, (
        "detach budget must stay below relay detach_tab's timeout"
    )


@pytest.mark.asyncio
async def test_daemon_surfaces_extension_timeout_code_distinguishably() -> None:
    """A -32001 error frame from the extension must reach the relay caller
    as a `_CommandError` carrying that code — never as the daemon's own bare
    `TimeoutError` — so agents can tell \"the extension gave up on Chrome\"
    from a genuine CDP failure."""
    async with _relay_running() as relay:
        ext = _FakeExtension()
        await ext.connect(relay.port, install_id="ext-A")
        await relay.wait_ready(timeout=5.0)

        # Seed a ghost target through the public attach API.
        attach_task = asyncio.create_task(relay.attach_tab(17, timeout=5.0))
        attach_cmd = await ext.next_command(timeout=5.0)
        assert attach_cmd["type"] == "attach"
        await ext.respond(attach_cmd["id"], result={
            "targetInfo": {"url": "http://x/", "title": "Page"},
        })
        await asyncio.wait_for(attach_task, timeout=5.0)

        call = asyncio.create_task(relay.send_cdp(
            17, "Page.navigate", {"url": "http://x/"}, timeout=10.0))
        cmd = await ext.next_command(timeout=5.0)
        assert cmd["type"] == "command"
        await ext.ws.send(json.dumps({
            "type": "response",
            "id": cmd["id"],
            "error": {
                "code": -32001,
                "message": "chrome.debugger.sendCommand timed out after "
                           "9000ms (Page.navigate tabId=17); the command may "
                           "still land in Chrome",
            },
        }))
        with pytest.raises(_CommandError) as exc_info:
            await asyncio.wait_for(call, timeout=5.0)
        assert exc_info.value.code == -32001
        assert "timed out" in exc_info.value.message
        assert not isinstance(exc_info.value, asyncio.TimeoutError)
