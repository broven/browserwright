"""Unit checks for the extension ownership markers (issue #29).

The extension's per-tab ownership markers (``ownedTabs`` in
``chrome-extension/background.js``, persisted in ``chrome.storage.session``)
are the durable anchor proving "which tab group belongs to which session".
These probes extract the real marker/group code from background.js and run it
under Node with a DOM/Chrome stub — they do not load the extension or launch
Chrome.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# tests/ is not a package (no __init__.py); pytest collects these files by
# inserting tests/daemon into sys.path, so import the sibling module the same
# way instead of via a package-qualified name.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extension_title_marker_unit import (  # noqa: E402
    _service_worker_marker_sources,
)


ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"


def _region(source: str, start: str, end: str) -> str:
    s = source.index(start)
    e = source.index(end, s)
    return source[s:e]


def _ownership_sources() -> dict[str, str]:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    return {
        **_service_worker_marker_sources(),
        "doAttachActive": _region(
            source, "async function doAttachActive(",
            "\n\nasync function _resolveSessionGroup("),
        "resolveSessionGroup": _region(
            source, "\n\nasync function _resolveSessionGroup(",
            "\n\nasync function doCreateTab("),
        "doCreateTab": _region(
            source, "\n\nasync function doCreateTab(",
            "\n\nasync function _ensureTabInGroup("),
        "ensureTabInGroup": _region(
            source, "\n\nasync function _ensureTabInGroup(",
            "\n\nasync function doCloseTab("),
        "doQueryGroup": _region(
            source, "\n\nasync function doQueryGroup(",
            "\n\nasync function announceAttached("),
        "onUpdatedHandler": _region(
            source, "chrome.tabs.onUpdated.addListener(",
            "// Note: no chrome.tabGroups.onRemoved"),
        "onRemovedHandler": _region(
            source, "chrome.tabs.onRemoved.addListener(",
            "// ---- popup → background message bridge"),
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


def test_ownership_marker_persists_and_loads_across_sw_restart() -> None:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const storageData = {};
const storageCalls = [];
const chrome = {
  storage: {
    session: {
      async get(keys) {
        storageCalls.push({ kind: "get", keys });
        const out = {};
        for (const key of Array.isArray(keys) ? keys : [keys]) {
          if (key in storageData) out[key] = storageData[key];
        }
        return out;
      },
      async set(value) {
        storageCalls.push({ kind: "set", value });
        Object.assign(storageData, value);
      },
    },
  },
};
const realm = vm.createContext({ chrome, console });
vm.runInContext(input.ownershipRegion, realm);

(async () => {
  vm.runInContext("markTabOwned(17, 'SESS-A', 100)", realm);
  vm.runInContext("markTabOwned(18, 'SESS-A', 100)", realm);
  await vm.runInContext("ownedPersistChain", realm);
  const persisted = JSON.parse(JSON.stringify(storageData["bdOwnedTabs"]));

  // A service-worker restart rebuilds the realm; markers must come back from
  // chrome.storage.session (which survives SW restarts).
  const secondRealm = vm.createContext({ chrome, console });
  vm.runInContext(input.ownershipRegion, secondRealm);
  await vm.runInContext("loadOwnedTabs()", secondRealm);

  // A browser restart clears chrome.storage.session; markers must be gone —
  // no stale marker may ever attach to a recycled tab id.
  delete storageData["bdOwnedTabs"];
  const thirdRealm = vm.createContext({ chrome, console });
  vm.runInContext(input.ownershipRegion, thirdRealm);
  await vm.runInContext("loadOwnedTabs()", thirdRealm);

  process.stdout.write(JSON.stringify({
    persisted,
    afterSwRestart: vm.runInContext(
      "Array.from(ownedTabs.entries()).map(([t, m]) => [t, m.s, m.g])",
      secondRealm),
    afterBrowserRestart: vm.runInContext("ownedTabs.size", thirdRealm),
    lastGet: storageCalls[storageCalls.length - 1],
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_ownership_sources(), probe)

    assert result["persisted"] == {"17": {"s": "SESS-A", "g": 100},
                                   "18": {"s": "SESS-A", "g": 100}}
    assert result["afterSwRestart"] == [[17, "SESS-A", 100], [18, "SESS-A", 100]]
    assert result["afterBrowserRestart"] == 0


def test_ownership_marker_dropped_on_unmark_and_drag_out() -> None:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const storageData = {};
const detaches = [];
const detached = [];
const chrome = {
  tabs: {},
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { Object.assign(storageData, value); },
    },
  },
};
const realm = vm.createContext({
  chrome, console,
  attachedTabs: new Set([17]),
  markedTabs: new Map(),
  safeSend: () => {},
  invalidateMarkerInstall: () => {},
  _detachAttachedTab: async (tabId, reason) => { detaches.push([tabId, reason]); },
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.wrapperRegion, realm);
vm.runInContext(input.onUpdatedHandler, realm);
vm.runInContext(input.onRemovedHandler, realm);

(async () => {
  vm.runInContext("markTabOwned(17, 'SESS-A', 100)", realm);
  await vm.runInContext("ownedPersistChain", realm);

  // Drag-out: the tab leaves the session's group → marker dropped.
  await vm.runInContext(
    "onUpdatedCb(17, { groupId: 200 })", realm);
  const afterDragOut = vm.runInContext("ownedTabs.size", realm);

  // Re-mark, then tab closed outright → marker dropped + detached announced.
  vm.runInContext("markTabOwned(17, 'SESS-A', 100)", realm);
  await vm.runInContext("ownedPersistChain", realm);
  await vm.runInContext("onRemovedCb(17)", realm);
  const afterClose = vm.runInContext("ownedTabs.size", realm);

  process.stdout.write(JSON.stringify({
    afterDragOut,
    afterClose,
    detaches,
    storageAfterDragOut: storageData["bdOwnedTabs"],
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    payload = _ownership_sources()
    # The onUpdated/onRemoved handlers register listeners; bind their callbacks
    # to realm variables so the probe can fire them.
    payload = {
        **payload,
        "onUpdatedHandler":
            payload["onUpdatedHandler"].replace(
                "chrome.tabs.onUpdated.addListener(",
                "chrome.tabs.onUpdated = { addListener(cb) { globalThis.onUpdatedCb = cb; } };\n"
                "chrome.tabs.onUpdated.addListener("),
        "onRemovedHandler":
            payload["onRemovedHandler"].replace(
                "chrome.tabs.onRemoved.addListener(",
                "chrome.tabs.onRemoved = { addListener(cb) { globalThis.onRemovedCb = cb; } };\n"
                "chrome.tabs.onRemoved.addListener("),
    }
    result = _run_node_probe(payload, probe)

    assert result["afterDragOut"] == 0
    assert result["afterClose"] == 0
    assert result["detaches"] == [[17, "dragged_out_of_group"]]
    assert result["storageAfterDragOut"] == {}


def test_create_tab_stamps_ownership_marker() -> None:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const storageData = {};
const responses = [];
const chrome = {
  tabs: {
    async create({ url, active }) {
      calls.push({ kind: "create", url, active });
      return { id: 17, url };
    },
    async get() { return { id: 17, url: "https://x/", title: "X" }; },
    async group({ groupId, tabIds }) {
      calls.push({ kind: "group", groupId, tabIds });
      return typeof groupId === "number" ? groupId : 100;
    },
  },
  tabGroups: {
    async get(id) {
      calls.push({ kind: "tabGroups.get", id });
      return { id, title: "Agent" };
    },
    async update(id, props) {
      calls.push({ kind: "tabGroups.update", id, props });
      return { id, ...props };
    },
  },
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { Object.assign(storageData, value); },
    },
  },
};
const realm = vm.createContext({
  chrome, console,
  stripMarker: (t) => t || "",
  safeSend: (m) => responses.push(m),
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.resolveSessionGroup, realm);
vm.runInContext(input.ensureTabInGroup, realm);
vm.runInContext(input.doCreateTab, realm);
vm.runInContext(
  "async function attachTab(tabId, opts) {}\n" +
  "async function postAttachCosmetics(tabId) {}",
  realm,
);

(async () => {
  await vm.runInContext(
    "doCreateTab(1, 'https://x/', 'Agent', 100, true, true, 'SESS-A')",
    realm);
  await vm.runInContext("ownedPersistChain", realm);
  process.stdout.write(JSON.stringify({
    responses,
    storage: storageData["bdOwnedTabs"],
    marked: vm.runInContext(
      "ownedTabs.has(17) && ownedTabs.get(17).s === 'SESS-A' && ownedTabs.get(17).g === 100",
      realm),
    joinedExistingGroup: calls.some(
      (c) => c.kind === "group" && c.groupId === 100),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_ownership_sources(), probe)

    assert result["marked"] is True
    assert result["storage"] == {"17": {"s": "SESS-A", "g": 100}}
    # Reused the daemon-validated live group; did not create a second one.
    assert result["joinedExistingGroup"] is True
    assert result["responses"][0]["result"]["groupId"] == 100


def test_attach_active_stamps_ownership_marker() -> None:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const storageData = {};
const responses = [];
const chrome = {
  tabs: {
    async query({ active, currentWindow }) {
      calls.push({ kind: "query", active, currentWindow });
      return [{ id: 17, url: "https://active/", title: "Active", groupId: -1 }];
    },
    async group({ groupId, tabIds, createProperties }) {
      calls.push({ kind: "group", groupId, tabIds, createProperties });
      if (typeof groupId === "number" && groupId >= 0) return groupId;
      return 101;  // fresh group created on the adopted tab
    },
  },
  tabGroups: {
    async get(id) {
      calls.push({ kind: "tabGroups.get", id });
      return { id, title: "Agent" };
    },
    async update(id, props) {
      calls.push({ kind: "tabGroups.update", id, props });
      return { id, ...props };
    },
  },
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { Object.assign(storageData, value); },
    },
  },
};
const realm = vm.createContext({
  chrome, console,
  stripMarker: (t) => t || "",
  safeSend: (m) => responses.push(m),
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.resolveSessionGroup, realm);
vm.runInContext(input.ensureTabInGroup, realm);
vm.runInContext(input.doAttachActive, realm);
vm.runInContext(
  "async function attachTab(tabId, opts) {}\n" +
  "async function postAttachCosmetics(tabId) {}",
  realm,
);

(async () => {
  await vm.runInContext(
    "doAttachActive(2, null, 'Agent', 'SESS-A')", realm);
  await vm.runInContext("ownedPersistChain", realm);
  process.stdout.write(JSON.stringify({
    responses,
    storage: storageData["bdOwnedTabs"],
    marked: vm.runInContext(
      "ownedTabs.has(17) && ownedTabs.get(17).s === 'SESS-A' && ownedTabs.get(17).g === 101",
      realm),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_ownership_sources(), probe)

    assert result["marked"] is True
    assert result["storage"] == {"17": {"s": "SESS-A", "g": 101}}
    assert result["responses"][0]["result"]["groupId"] == 101


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
    async get(id) {
      calls.push({ kind: "tabGroups.get", id });
      return { id, title: "Agent" };
    },
  },
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { Object.assign(storageData, value); },
    },
  },
};
const storageData = {};
const realm = vm.createContext({
  chrome, console,
  stripMarker: (t) => t || "",
  safeSend: (m) => responses.push(m),
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.resolveSessionGroup, realm);
vm.runInContext(input.doAttachActive, realm);

(async () => {
  await vm.runInContext("doAttachActive(2, null, 'Agent', 'SESS-A')", realm);
  process.stdout.write(JSON.stringify({
    responses,
    stole: calls.some((c) => c.kind === "group"),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_ownership_sources(), probe)

    assert result["stole"] is False, "must not move the tab into a group"
    err = result["responses"][0]["error"]
    assert err["code"] == -32000
    assert "tab group" in err["message"]
    assert "Drag the tab out" in err["message"]


def test_attach_active_reuses_tab_in_own_group() -> None:
    """A tab already inside the session's own live group is adopted in place
    (idempotent re-attach), not refused and not moved again."""
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const storageData = {};
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
    async get(id) {
      calls.push({ kind: "tabGroups.get", id });
      return { id, title: "Agent" };
    },
    async update(id, props) {
      calls.push({ kind: "tabGroups.update", id, props });
      return { id, ...props };
    },
  },
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { Object.assign(storageData, value); },
    },
  },
};
const realm = vm.createContext({
  chrome, console,
  stripMarker: (t) => t || "",
  safeSend: (m) => responses.push(m),
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.resolveSessionGroup, realm);
vm.runInContext(input.ensureTabInGroup, realm);
vm.runInContext(input.doAttachActive, realm);
vm.runInContext(
  "async function attachTab(tabId, opts) {}\n" +
  "async function postAttachCosmetics(tabId) {}",
  realm,
);

(async () => {
  await vm.runInContext(
    "doAttachActive(2, 100, 'Agent', 'SESS-A')", realm);
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
    result = _run_node_probe(_ownership_sources(), probe)

    assert result["responses"][0]["result"]["groupId"] == 100
    # Joined the existing group directly; never created a second one.
    assert result["joinedOwnGroup"] is True


def test_query_group_annotates_owned_session_id_per_member() -> None:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const storageData = {};
const responses = [];
const chrome = {
  tabGroups: {
    async get(id) { return { id, title: "Agent" }; },
  },
  tabs: {
    async query({ groupId }) {
      return [
        { id: 17, url: "https://a/", title: "A", active: true, lastAccessed: 5 },
        { id: 18, url: "https://u/", title: "U", active: false, lastAccessed: 1 },
      ];
    },
  },
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { Object.assign(storageData, value); },
    },
  },
};
const realm = vm.createContext({
  chrome, console,
  stripMarker: (t) => t || "",
  safeSend: (m) => responses.push(m),
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.resolveSessionGroup, realm);
vm.runInContext(input.doQueryGroup, realm);

(async () => {
  vm.runInContext("markTabOwned(17, 'SESS-A', 100)", realm);
  await vm.runInContext("doQueryGroup(3, 'Agent', 100)", realm);
  const result = responses[0].result;
  process.stdout.write(JSON.stringify({
    groupId: result.groupId,
    tabs: result.tabs.map((t) => ({
      tabId: t.tabId,
      ownedSessionId: t.ownedSessionId,
      hasField: "ownedSessionId" in t,
    })),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_ownership_sources(), probe)

    assert result["groupId"] == 100
    # The marked member carries the session id; the unmarked member is null.
    # The field is present on EVERY tab so the daemon can tell "new extension,
    # unproven" from "legacy extension, no marker support".
    assert result["tabs"] == [
        {"tabId": 17, "ownedSessionId": "SESS-A", "hasField": True},
        {"tabId": 18, "ownedSessionId": None, "hasField": True},
    ]


def test_close_tab_success_drops_ownership_marker() -> None:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const storageData = {};
const responses = [];
const chrome = {
  tabs: {
    async remove(tabId) { return {}; },
  },
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { Object.assign(storageData, value); },
    },
  },
};
const realm = vm.createContext({
  chrome, console,
  safeSend: (m) => responses.push(m),
  attachedTabs: new Set([17]),
  markedTabs: new Map([[17, "marker-script"]]),
  invalidateMarkerInstall: () => {},
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.wrapperRegion, realm);
vm.runInContext(input.doCloseTab, realm);

(async () => {
  vm.runInContext("markTabOwned(17, 'SESS-A', 100)", realm);
  await vm.runInContext("doCloseTab(4, 17)", realm);
  await vm.runInContext("ownedPersistChain", realm);
  process.stdout.write(JSON.stringify({
    marked: vm.runInContext("ownedTabs.has(17)", realm),
    storage: storageData["bdOwnedTabs"],
    response: responses[0],
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = _run_node_probe(_ownership_sources(), probe)

    assert result["marked"] is False
    assert result["storage"] == {}  # snapshot written without the tab
    assert result["response"]["result"] == {"ok": True, "tabId": 17}
