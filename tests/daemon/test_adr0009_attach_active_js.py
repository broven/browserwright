"""JS-level unit checks for the extension's attachActive adopt rules.

Migrated from `test_issue29_ownership_markers_js.py` (deleted by ADR-0009,
which removed the ownership-marker subsystem): the two attach-active rules
added there — refuse to steal a tab from a foreign group, adopt in place a
tab already in the session's own group — survive ADR-0009 with the same
shape, but the group is now identified by its TITLE (`<name>-BW<sid>`)
instead of the numeric id + per-tab markers.

These probes extract the real `doAttachActive` / `_resolveSessionGroup` /
`_ensureTabInGroup` code from `chrome-extension/background.js` and run it
under Node with a Chrome stub — they do not load the extension or launch
Chrome.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"


def _region(source: str, start: str, end: str) -> str:
    s = source.index(start)
    e = source.index(end, s)
    return source[s:e]


def _attach_active_sources() -> dict[str, str]:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    return {
        "doAttachActive": _region(
            source, "async function doAttachActive(",
            "\n\nasync function _resolveSessionGroup("),
        "resolveSessionGroup": _region(
            source, "\n\nasync function _resolveSessionGroup(",
            "\n\nasync function doCreateTab("),
        "ensureTabInGroup": _region(
            source, "\n\nasync function _ensureTabInGroup(",
            "\n\nasync function doCloseTab("),
    }


def _run_node_probe(payload: dict[str, str], probe: str) -> dict:
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


def test_attach_active_refuses_tab_in_other_group_fresh_session() -> None:
    """Adopt rule: the focused tab sitting in ANY group other than the
    session's own is refused — even when the session has no live group yet.
    Before this rule a fresh session (ourGroupId == -1) silently stole the
    tab out of the user's own manual group."""
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const responses = [];
const chrome = {
  tabs: {
    async query({ active, currentWindow }) {
      calls.push({ kind: "query", active, currentWindow });
      return [{ id: 17, url: "https://active/", title: "Active", groupId: 5 }];
    },
    async group(props) {
      calls.push({ kind: "group", props });
      return 101;
    },
  },
  tabGroups: {
    // Fresh session: no group carries the session's title yet.
    async query() { return []; },
  },
};
const realm = vm.createContext({
  chrome, console,
  stripMarker: (t) => t || "",
  safeSend: (m) => responses.push(m),
  errorCode: () => -32000,
  errMessage: (e) => String((e && e.message) || e),
});
vm.runInContext(input.resolveSessionGroup, realm);
vm.runInContext(input.doAttachActive, realm);

(async () => {
  await vm.runInContext("doAttachActive(2, 'Agent-BW1')", realm);
  process.stdout.write(JSON.stringify({
    responses,
    stole: calls.some((c) => c.kind === "group"),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_attach_active_sources(), probe)

    assert result["stole"] is False, "must not move the tab into a group"
    err = result["responses"][0]["error"]
    assert err["code"] == -32000
    assert "tab group" in err["message"]
    assert "Drag the tab out" in err["message"]


def test_attach_active_reuses_tab_in_own_group() -> None:
    """A tab already inside the session's own live group is adopted in place
    (idempotent re-attach), not refused and not moved again. Under ADR-0009
    "own group" means: a live group whose TITLE is the session's
    `<name>-BW<sid>` — resolved by exact title compare, never by numeric id."""
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const responses = [];
const chrome = {
  tabs: {
    async query({ active, currentWindow }) {
      calls.push({ kind: "query", active, currentWindow });
      return [{ id: 17, url: "https://active/", title: "Active", groupId: 100 }];
    },
    async group({ groupId, tabIds }) {
      calls.push({ kind: "group", groupId, tabIds });
      return typeof groupId === "number" ? groupId : 101;
    },
  },
  tabGroups: {
    async query() {
      return [{ id: 100, title: "Agent-BW1" }];
    },
    async update(id, props) {
      calls.push({ kind: "tabGroups.update", id, props });
      return { id, ...props };
    },
  },
};
const realm = vm.createContext({
  chrome, console,
  stripMarker: (t) => t || "",
  safeSend: (m) => responses.push(m),
  errorCode: () => -32000,
  errMessage: (e) => String((e && e.message) || e),
});
vm.runInContext(input.resolveSessionGroup, realm);
vm.runInContext(input.ensureTabInGroup, realm);
vm.runInContext(input.doAttachActive, realm);
vm.runInContext(
  "async function attachTab(tabId, opts) {}\n" +
  "async function postAttachCosmetics(tabId) {}",
  realm,
);

(async () => {
  await vm.runInContext("doAttachActive(2, 'Agent-BW1')", realm);
  process.stdout.write(JSON.stringify({
    responses,
    joinedOwnGroup: calls.some(
      (c) => c.kind === "group" && c.groupId === 100),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_attach_active_sources(), probe)

    assert result["responses"][0]["result"]["groupId"] == 100
    # Joined the existing group directly; never created a second one.
    assert result["joinedOwnGroup"] is True
