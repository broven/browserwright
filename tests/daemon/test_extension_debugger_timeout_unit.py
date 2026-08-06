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

First test is the reproduction: on unmodified code the handler hangs and no
response frame is ever sent.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"
RELAY_PY = ROOT / "src" / "browserwright" / "daemon" / "server" / "relay.py"

#: How long a probe watches a handler that must respond. Well under the
#: extension's own budgets (tens of ms, not seconds), generous for CI.
PROBE_WAIT_MS = 200


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
        "doCommand": _region(
            source, "async function doCommand(", "\n\nasync function armAutoAttach("),
        "errMessage": _function_decl(source, "errMessage"),
    }


def _run_do_command_hang_probe(payload: dict[str, str]) -> dict[str, object]:
    """Run `doCommand` against a `chrome.debugger.sendCommand` that never
    settles, and report whether the handler ever responds.

    This is the exact #31 shape: Chrome holds the promise, the extension
    awaits it forever, no `response` frame is ever sent, and the daemon's
    pending future never resolves.
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const frames = [];
const sent = [];
const chrome = {
  debugger: {
    sendCommand(target, method, params) {
      sent.push({ tabId: target.tabId, method, params });
      // Chrome never settles this promise (OOPIF throttle / wedged renderer).
      return new Promise(() => {});
    },
  },
};
const realm = vm.createContext({
  chrome,
  console: { info() {}, warn() {}, error: console.error },
  safeSend: (frame) => frames.push(frame),
});
vm.runInContext(`${input.errMessage}\n${input.doCommand}`, realm);
(async () => {
  const call = vm.runInContext(
    'doCommand(9, 17, "Page.navigate", {url: "http://x/"})', realm);
  const settled = await Promise.race([
    call.then(() => true, () => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 200)),
  ]);
  process.stdout.write(JSON.stringify({
    settled,
    frames,
    sent: sent.length,
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    proc = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def test_repro_never_settling_send_command_leaks_forever() -> None:
    """Reproduction for #31: a never-settling `chrome.debugger.sendCommand`
    leaves `doCommand` pending forever; no response frame is ever sent.

    The daemon then waits out its full `_request(timeout=10.0)` while the
    extension-side promise leaks, and the tab stays attached and paused.
    """
    result = _run_do_command_hang_probe(_sources())

    assert result["settled"] is False, (
        "doCommand settled despite the hung sendCommand — the repro premise "
        "is gone (was the timeout added?)"
    )
    assert result["frames"] == [], (
        "a response frame was sent for a command Chrome never settled — "
        f"frames={result['frames']!r}"
    )
    assert result["sent"] == 1
