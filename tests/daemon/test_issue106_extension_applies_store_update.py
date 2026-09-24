"""Issue #106 -- the extension must apply a downloaded Web Store update itself.

Chrome installs an extension update only once the extension goes idle. Ours
never does: `pingLoop` and `chrome.alarms` keep the service worker alive for as
long as Chrome runs, so a downloaded update sat unapplied until the user
restarted Chrome -- the #106 reporter had 0.17.2 on disk while 0.15.0 kept
running (and kept livelocking against a newer daemon).

`chrome.runtime.onUpdateAvailable` is Chrome's hook for exactly this; the
extension records the update and reloads into it when no session is using it.
These tests run the real decision function from `background.js` in Node.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"


def _decl(source: str, name: str) -> str:
    match = re.search(
        rf"\n(async )?function {name}\([^)]*\) \{{.*?\n\}}", source, re.DOTALL)
    assert match is not None, f"{name} not found in background.js"
    return match.group(0)


def _run(scenario: str) -> dict:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    const = re.search(r"\nconst UPDATE_MAX_DEFER_MS = [^\n]*", source)
    assert const is not None, "UPDATE_MAX_DEFER_MS not found in background.js"
    program = f"""
let now = 1_000_000;
Date.now = () => now;
let reloads = 0, cleanups = 0;
const chrome = {{ runtime: {{ reload: () => {{ reloads += 1; }} }} }};
async function cleanupMarkersBeforeReload() {{ cleanups += 1; }}
const attachedTabs = new Set();
let inflightCommands = 0;
let pendingUpdate = null;
{const.group(0)}
{_decl(source, "applyPendingUpdateIfIdle")}
(async () => {{
{scenario}
  console.log(JSON.stringify({{ reloads, cleanups }}));
}})();
"""
    out = subprocess.run(["node", "-e", program], capture_output=True,
                         text=True, check=True, timeout=10)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_no_update_pending_never_reloads():
    assert _run("await applyPendingUpdateIfIdle();")["reloads"] == 0


def test_idle_extension_reloads_into_the_update():
    result = _run("""
  pendingUpdate = { version: "0.17.2", since: now };
  await applyPendingUpdateIfIdle();
  await applyPendingUpdateIfIdle();
""")
    # Markers come off the user's tabs before the reload, and only once.
    assert result == {"reloads": 1, "cleanups": 1}


def test_update_waits_while_a_session_holds_a_tab():
    result = _run("""
  pendingUpdate = { version: "0.17.2", since: now };
  attachedTabs.add(7);
  await applyPendingUpdateIfIdle();
  now += 5 * 60 * 1000;
  await applyPendingUpdateIfIdle();
""")
    assert result["reloads"] == 0


def test_update_lands_once_the_session_lets_go():
    result = _run("""
  pendingUpdate = { version: "0.17.2", since: now };
  attachedTabs.add(7);
  await applyPendingUpdateIfIdle();
  attachedTabs.delete(7);
  await applyPendingUpdateIfIdle();
""")
    assert result["reloads"] == 1


def test_a_session_that_never_lets_go_cannot_pin_the_old_build_forever():
    result = _run("""
  pendingUpdate = { version: "0.17.2", since: now };
  attachedTabs.add(7);
  now += UPDATE_MAX_DEFER_MS + 1;
  await applyPendingUpdateIfIdle();
""")
    assert result["reloads"] == 1


def test_never_reloads_under_an_in_flight_command():
    result = _run("""
  pendingUpdate = { version: "0.17.2", since: now };
  inflightCommands = 1;
  await applyPendingUpdateIfIdle();
  now += UPDATE_MAX_DEFER_MS + 1;
  await applyPendingUpdateIfIdle();
""")
    assert result["reloads"] == 0
