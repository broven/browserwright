"""Unit checks for the extension title marker snippets.

The tests extract only the marker helpers/scripts from
``chrome-extension/background.js`` and run them in Node with a tiny DOM stub.
They do not load the extension runtime or launch Chrome.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"
PREFIX = "\U0001f440 "


def _template_const(source: str, name: str) -> str:
    # The marker scripts are assembled from shared source fragments
    # (MARKER_STRIP_PREFIX_SRC / MARKER_RAW_TITLE_SRC in background.js), so
    # evaluate the whole const region in Node instead of regex-slicing a
    # single template literal.
    start = source.index("const MARKER_STRIP_PREFIX_SRC")
    end = source.index("const markedTabs")
    snippet = source[start:end]
    proc = subprocess.run(
        ["node"],
        input=f"{snippet}\nprocess.stdout.write(JSON.stringify({name}));",
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def _function_decl(source: str, name: str) -> str:
    match = re.search(
        rf"function {name}\([^)]*\) \{{(?P<body>.*?)\n\}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{name} function not found"
    return match.group(0)


def _marker_sources() -> dict[str, str]:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    return {
        "installScript": _template_const(source, "MARKER_INSTALL_SCRIPT"),
        "removeScript": _template_const(source, "MARKER_REMOVE_SCRIPT"),
        "stripMarker": _function_decl(source, "stripMarker"),
    }


def _service_worker_marker_sources() -> dict[str, str]:
    """Extract the real SW marker lifecycle without loading the extension."""
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    marker_start = source.index("const markedTabs")
    marker_end = source.index("// ---- chrome.debugger event fan-out", marker_start)
    detach_start = source.index("async function detachTab(")
    detach_end = source.index("\n\nasync function doAttach(", detach_start)
    handler_start = source.index("async function handleDaemonMessage(")
    handler_end = source.index("// Shared attach sequence", handler_start)
    close_start = source.index("async function doCloseTab(")
    close_end = source.index("\n\nasync function doDetach(", close_start)
    # The bounded chrome.debugger wrapper region (issue #31): detachTab and
    # doCloseTab route their chrome.debugger calls through it, so the probes
    # that run those handlers need it injected too.
    wrapper_start = source.index("// ---- bounded chrome.debugger calls")
    wrapper_end = source.index("// Shared attach sequence:", wrapper_start)
    ownership_start = source.index("// ---- ownership markers:")
    ownership_end = source.index(
        "// ---- group-membership = session membership", ownership_start)
    return {
        **_marker_sources(),
        "wrapperRegion": source[wrapper_start:wrapper_end],
        "workerMarkerRegion": source[marker_start:marker_end],
        "detachTab": source[detach_start:detach_end],
        "handleDaemonMessage": source[handler_start:handler_end],
        "doCloseTab": source[close_start:close_end],
        "ownershipRegion": source[ownership_start:ownership_end],
    }


def _run_marker_detach_budget_probe(
    payload: dict[str, str], hang_stage: str,
) -> dict[str, object]:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const chrome = {
  debugger: {
    async sendCommand(target, method, params) {
      calls.push({ kind: "command", method, params });
      const stage = method === "Runtime.evaluate"
        ? (params.expression === input.installScript ? "installEvaluate" : "removeEvaluate")
        : method;
      if (stage === input.hangStage) return await new Promise(() => {});
      if (method === "Page.addScriptToEvaluateOnNewDocument") {
        return { identifier: "marker-script" };
      }
      return {};
    },
    async detach(target) {
      calls.push({ kind: "detach", tabId: target.tabId });
    },
  },
};
const realm = vm.createContext({
  chrome,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  setTimeout,
  clearTimeout,
  console: { info() {}, warn() {}, error: console.error },
});
const markerRegion = input.workerMarkerRegion
  .replace("const MARKER_INSTALL_TIMEOUT_MS = 1500;",
           "const MARKER_INSTALL_TIMEOUT_MS = 10;")
  .replace("const MARKER_REMOVE_TIMEOUT_MS = 1000;",
           "const MARKER_REMOVE_TIMEOUT_MS = 10;");
vm.runInContext(`
  const TITLE_PREFIX = "\\u{1F440} ";
  const MARKER_INSTALL_SCRIPT = ${JSON.stringify(input.installScript)};
  const MARKER_REMOVE_SCRIPT = ${JSON.stringify(input.removeScript)};
  const PROTOCOL_VERSION = "1.3";
  const attachedTabs = new Set([17]);
  function safeSend() {}
  ${input.wrapperRegion}
  ${markerRegion}
  ${input.detachTab}
`, realm);

(async () => {
  if (["Page.enable", "Page.addScriptToEvaluateOnNewDocument", "installEvaluate"]
      .includes(input.hangStage)) {
    vm.runInContext("markTabAttached(17)", realm);
    while (calls.length === 0 ||
           (input.hangStage === "Page.addScriptToEvaluateOnNewDocument" && calls.length < 2) ||
           (input.hangStage === "installEvaluate" && calls.length < 3)) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  } else {
    vm.runInContext('markedTabs.set(17, "marker-script")', realm);
  }
  const completed = await Promise.race([
    vm.runInContext("detachTab(17)", realm).then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 100)),
  ]);
  process.stdout.write(JSON.stringify({
    completed,
    attached: vm.runInContext("attachedTabs.has(17)", realm),
    calls,
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    proc = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps({**payload, "hangStage": hang_stage}),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def _run_marker_late_completion_probe(
    payload: dict[str, str], late_stage: str,
) -> dict[str, object]:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
let releaseLate;
const lateGate = new Promise((resolve) => { releaseLate = resolve; });
let commandQueue = Promise.resolve();
let pageMarked = false;

function stageFor(method, params) {
  if (method !== "Runtime.evaluate") return method;
  return params.expression === input.installScript
    ? "installEvaluate" : "removeEvaluate";
}

const chrome = {
  debugger: {
    sendCommand(target, method, params) {
      const stage = stageFor(method, params);
      calls.push({ kind: "command", stage, method });
      const run = async () => {
        if (stage === input.lateStage) await lateGate;
        if (stage === "installEvaluate") pageMarked = true;
        if (stage === "removeEvaluate") pageMarked = false;
        if (method === "Page.addScriptToEvaluateOnNewDocument") {
          return { identifier: "marker-script" };
        }
        return {};
      };
      const result = commandQueue.then(run);
      commandQueue = result.catch(() => {});
      return result;
    },
    async detach(target) {
      calls.push({ kind: "detach", tabId: target.tabId });
    },
  },
};
const realm = vm.createContext({
  chrome,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  setTimeout,
  clearTimeout,
  console: { info() {}, warn() {}, error: console.error },
});
const markerRegion = input.workerMarkerRegion
  .replace("const MARKER_INSTALL_TIMEOUT_MS = 1500;",
           "const MARKER_INSTALL_TIMEOUT_MS = 100;")
  .replace("const MARKER_REMOVE_TIMEOUT_MS = 1000;",
           "const MARKER_REMOVE_TIMEOUT_MS = 10;");
vm.runInContext(`
  const TITLE_PREFIX = "\\u{1F440} ";
  const MARKER_INSTALL_SCRIPT = ${JSON.stringify(input.installScript)};
  const MARKER_REMOVE_SCRIPT = ${JSON.stringify(input.removeScript)};
  const PROTOCOL_VERSION = "1.3";
  const attachedTabs = new Set([17]);
  function safeSend() {}
  ${input.wrapperRegion}
  ${markerRegion}
  ${input.detachTab}
`, realm);

(async () => {
  const marking = vm.runInContext("markTabAttached(17)", realm);
  while (!calls.some((call) => call.stage === input.lateStage)) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  const detached = await vm.runInContext("detachTab(17)", realm);
  const detachIndex = calls.findIndex((call) => call.kind === "detach");
  releaseLate();
  await Promise.allSettled([marking, commandQueue]);
  await new Promise((resolve) => setTimeout(resolve, 0));
  await commandQueue;
  process.stdout.write(JSON.stringify({
    calls,
    detachIndex,
    pageMarked,
    marked: vm.runInContext("markedTabs.has(17)", realm),
    marking: vm.runInContext("markingTabs.has(17)", realm),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    proc = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps({**payload, "lateStage": late_stage}),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def _run_failed_close_marker_probe(payload: dict[str, str]) -> dict[str, object]:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const attachedTabs = new Set([17]);
const markedTabs = new Map([[17, "marker-script"]]);
const chrome = {
  debugger: {
    async detach() { calls.push({ kind: "detach" }); },
  },
  tabs: {
    async remove() { calls.push({ kind: "remove" }); throw new Error("close denied"); },
  },
  storage: {
    session: {
      async get() { return {}; },
      async set(value) { calls.push({ kind: "storageSet", value }); },
    },
  },
};
function errMessage(error) { return String(error.message || error); }
function safeSend(message) { calls.push({ kind: "response", message }); }
const realm = vm.createContext({
  chrome, console, errMessage, safeSend,
  attachedTabs, markedTabs,
});
vm.runInContext(input.ownershipRegion, realm);
vm.runInContext(input.wrapperRegion, realm);
vm.runInContext(input.doCloseTab, realm);
(async () => {
  await vm.runInContext("doCloseTab(9, 17)", realm);
  process.stdout.write(JSON.stringify({
    attached: attachedTabs.has(17),
    marked: markedTabs.has(17),
    owned: vm.runInContext("ownedTabs.has(17)", realm),
    calls,
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
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def _run_service_worker_realm_rebuild_probe(payload: dict[str, str]) -> dict:
    """Mark in one SW realm, then detach through a fresh realm.

    The page sandbox deliberately survives the service-worker sandbox. This is
    the upgrade/reload shape: extension globals are rebuilt while the old tab's
    injected accessor, observer, and visible title marker remain live.
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const EYE = "\u{1F440}";

class Document {}
Object.defineProperty(Document.prototype, "title", {
  configurable: true,
  enumerable: true,
  get() { return this._rawTitle || ""; },
  set(value) { this._rawTitle = String(value); },
});
class MutationObserver {
  constructor(callback) {
    this.callback = callback;
    this.disconnected = false;
  }
  observe() { this.callback(); }
  disconnect() { this.disconnected = true; }
}

const document = new Document();
document._rawTitle = `${EYE} Legacy`;
document.head = { appendChild() {} };
document.querySelector = () => null;
document.createElement = () => ({ textContent: "" });
document.addEventListener = () => {};
const page = vm.createContext({
  window: {}, document, Document, HTMLDocument: Document, MutationObserver,
});

const calls = [];
const chrome = {
  debugger: {
    async sendCommand(target, method, params) {
      calls.push({ kind: "command", method, params });
      if (method === "Page.addScriptToEvaluateOnNewDocument") {
        return { identifier: "old-realm-script" };
      }
      if (method === "Runtime.evaluate") {
        vm.runInContext(params.expression, page);
      }
      return {};
    },
    async detach(target) {
      calls.push({ kind: "detach", tabId: target.tabId });
    },
  },
};

function newWorkerRealm(attached) {
  const realm = vm.createContext({
    chrome, console, setTimeout, clearTimeout, sleep: async () => {},
  });
  vm.runInContext(`
    const TITLE_PREFIX = "\\u{1F440} ";
    const MARKER_INSTALL_SCRIPT = ${JSON.stringify(input.installScript)};
    const MARKER_REMOVE_SCRIPT = ${JSON.stringify(input.removeScript)};
    const PROTOCOL_VERSION = "1.3";
    ${input.wrapperRegion}
    ${input.workerMarkerRegion}
    const attachedTabs = new Set(${attached ? "[17]" : "[]"});
    function safeSend() {}
    ${input.detachTab}
  `, realm);
  return realm;
}

(async () => {
  const firstRealm = newWorkerRealm(true);
  await vm.runInContext("markTabAttached(17)", firstRealm);
  const titleAfterMark = document._rawTitle;

  // A service-worker update/reload rebuilds all extension globals. The page
  // and its injected marker survive, but markedTabs is now an empty Map.
  const secondRealm = newWorkerRealm(false);
  await vm.runInContext("detachTab(17)", secondRealm);

  process.stdout.write(JSON.stringify({
    titleAfterMark,
    titleAfterDetach: document._rawTitle,
    markerAfterDetach: !!page.window.__bdTitleMarker,
    callsAfterRealmRebuild: calls.slice(3),
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
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def _run_controlled_reload_probe(payload: dict[str, object]) -> list[dict]:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const calls = [];
const chrome = {
  debugger: {
    async sendCommand(target, method, params) {
      calls.push({ kind: "command", tabId: target.tabId, method, params });
      if (method === "Page.addScriptToEvaluateOnNewDocument") {
        if (input.markInFlight) {
          await new Promise((resolve) => setTimeout(resolve, 0));
        }
        return { identifier: "known-script" };
      }
      if (input.failRemove &&
          method === "Page.removeScriptToEvaluateOnNewDocument") {
        throw new Error("registration already gone");
      }
      if (input.hangRemove && method === "Runtime.evaluate" &&
          params.expression === input.removeScript) {
        return await new Promise(() => {});
      }
      return {};
    },
  },
  runtime: {
    reload() { calls.push({ kind: "reload" }); },
  },
};
const realm = vm.createContext({
  chrome,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  setTimeout,
  clearTimeout,
  console: { info() {}, warn() {}, error: console.error },
});
vm.runInContext(`
  const daemonVersion = "test";
  const TITLE_PREFIX = "\\u{1F440} ";
  const MARKER_INSTALL_SCRIPT = ${JSON.stringify(input.installScript)};
  const MARKER_REMOVE_SCRIPT = ${JSON.stringify(input.removeScript)};
  const attachedTabs = new Set([17]);
  ${input.workerMarkerRegion.replace(
    "const MARKER_RELOAD_CLEANUP_TIMEOUT_MS = 1500;",
    "const MARKER_RELOAD_CLEANUP_TIMEOUT_MS = 10;",
  )}
  ${input.handleDaemonMessage}
`, realm);
(async () => {
  let marking = null;
  if (input.markInFlight) {
    marking = vm.runInContext("markTabAttached(17)", realm);
  } else {
    vm.runInContext('markedTabs.set(17, "known-script")', realm);
  }
  await vm.runInContext(
    'handleDaemonMessage({type: "reloadExtension", reason: "test"})',
    realm,
  );
  if (marking) await marking;
  process.stdout.write(JSON.stringify(calls));
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
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def _run_node_probe(payload: dict[str, str]) -> dict[str, object]:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const PREFIX = "\u{1F440} ";
const EYE = "\u{1F440}";
const NBSP = "\u00A0";
const NARROW_NBSP = "\u202F";

function runMarkerScript(script, initialTitle) {
  class Document {}
  Object.defineProperty(Document.prototype, "title", {
    configurable: true,
    enumerable: true,
    get() {
      return this._rawTitle || "";
    },
    set(value) {
      this._rawTitle = String(value);
    },
  });

  class MutationObserver {
    constructor(callback) {
      this.callback = callback;
      this.disconnected = false;
    }
    observe() {
      this.callback();
    }
    disconnect() {
      this.disconnected = true;
    }
  }

  const document = new Document();
  document._rawTitle = initialTitle;
  document.head = { appendChild() {} };
  document.querySelector = () => null;
  document.createElement = () => ({ textContent: "" });
  document.addEventListener = () => {};

  const sandbox = {
    window: {},
    document,
    Document,
    HTMLDocument: Document,
    MutationObserver,
  };
  vm.runInNewContext(script, sandbox);
  return { sandbox, document };
}

vm.runInThisContext(
  `const TITLE_PREFIX = ${JSON.stringify(PREFIX)};\n${input.stripMarker}`,
);

const duplicateInstall = runMarkerScript(
  input.installScript,
  `${PREFIX}${PREFIX}Example`,
);
const cleanInstall = runMarkerScript(input.installScript, "Example");
const bareEyeInstall = runMarkerScript(input.installScript, `${EYE}Hello`);
const nbspEyeInstall = runMarkerScript(input.installScript, `${EYE}${NBSP}Hello`);
const narrowEyeInstall = runMarkerScript(
  input.installScript,
  `${EYE}${NARROW_NBSP}Hello`,
);
const setterProbe = runMarkerScript(input.installScript, "Example");
setterProbe.document.title = `${PREFIX}${PREFIX}Next`;
const bareEyeSetter = runMarkerScript(input.installScript, "Example");
bareEyeSetter.document.title = `${EYE}World`;
const nonStringSetter = runMarkerScript(input.installScript, "Example");
nonStringSetter.document.title = 42;
const duplicateRemove = runMarkerScript(
  input.removeScript,
  `${PREFIX}${PREFIX}${PREFIX}Example`,
);
const bareEyeRemove = runMarkerScript(input.removeScript, `${EYE}Hello`);
const nbspEyeRemove = runMarkerScript(input.removeScript, `${EYE}${NBSP}Hello`);
const lifecycle = runMarkerScript(input.installScript, `${PREFIX}${PREFIX}Example`);
lifecycle.document.title = `${EYE}${NBSP}Mid`;
vm.runInNewContext(input.removeScript, lifecycle.sandbox);
lifecycle.document.title = "After";

process.stdout.write(JSON.stringify({
  stripSamples: [
    stripMarker(`${PREFIX}${PREFIX}Example`),
    stripMarker(`${PREFIX}${PREFIX}${PREFIX}`),
    stripMarker("Plain"),
    stripMarker(null),
    stripMarker(`${EYE}Hello`),
    stripMarker(`${EYE} Hello`),
    stripMarker(`${EYE}${NBSP}Hello`),
    stripMarker(`${EYE}${NARROW_NBSP}Hello`),
    stripMarker(`${EYE}${EYE}Hello`),
    stripMarker(`${PREFIX}${EYE}Hello`),
    stripMarker(42),
    stripMarker(undefined),
    stripMarker(true),
  ],
  duplicateInstallTitle: {
    rawTitle: duplicateInstall.document._rawTitle,
    pageTitle: duplicateInstall.document.title,
  },
  cleanInstallTitle: {
    rawTitle: cleanInstall.document._rawTitle,
    pageTitle: cleanInstall.document.title,
  },
  bareEyeInstallTitle: {
    rawTitle: bareEyeInstall.document._rawTitle,
    pageTitle: bareEyeInstall.document.title,
  },
  nbspEyeInstallTitle: {
    rawTitle: nbspEyeInstall.document._rawTitle,
    pageTitle: nbspEyeInstall.document.title,
  },
  narrowEyeInstallTitle: {
    rawTitle: narrowEyeInstall.document._rawTitle,
    pageTitle: narrowEyeInstall.document.title,
  },
  setterTitle: {
    rawTitle: setterProbe.document._rawTitle,
    pageTitle: setterProbe.document.title,
  },
  bareEyeSetterTitle: {
    rawTitle: bareEyeSetter.document._rawTitle,
    pageTitle: bareEyeSetter.document.title,
  },
  nonStringSetterTitle: {
    rawTitle: nonStringSetter.document._rawTitle,
    pageTitle: nonStringSetter.document.title,
    stripped: stripMarker(nonStringSetter.document._rawTitle),
  },
  duplicateRemoveTitle: {
    rawTitle: duplicateRemove.document._rawTitle,
    pageTitle: duplicateRemove.document.title,
  },
  bareEyeRemoveTitle: {
    rawTitle: bareEyeRemove.document._rawTitle,
    pageTitle: bareEyeRemove.document.title,
  },
  nbspEyeRemoveTitle: {
    rawTitle: nbspEyeRemove.document._rawTitle,
    pageTitle: nbspEyeRemove.document.title,
  },
  lifecycleTitle: {
    rawTitle: lifecycle.document._rawTitle,
    pageTitle: lifecycle.document.title,
  },
}));
"""
    proc = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


#: How many raw title writes the marker is allowed before we call it a loop.
#: Settling costs one write; a second is slack for a normalization round-trip.
WRITE_BUDGET = 4

#: How many observer callbacks may be delivered before we call it a loop.
CALLBACK_BUDGET = 20


def _run_fixpoint_probe(payload: dict[str, str]) -> dict[str, object]:
    """Re-run the install script against a *spec-accurate* `document.title`.

    The DOM stub used by the probe above returns whatever was written, which is
    not what a browser does: per HTML, the `document.title` getter strips
    leading/trailing ASCII whitespace and collapses internal runs. That
    difference is the entire bug. The marker's prefix ends in a space, so on a
    page with an empty title it wrote `"<eye> "`, read back `"<eye>"`, judged the
    title wrong, and wrote again — forever. Each write mutates `<head>`, which
    re-fires the MutationObserver, whose callbacks are delivered *asynchronously*
    so the script's `normalizing` re-entry flag has already been released by the
    time one arrives. The result is an unbounded microtask loop that pins the
    renderer's main thread, which is why `chrome.debugger.sendCommand` stopped
    answering and the daemon reported `relay send failed: TimeoutError()`.

    So this stub models the two things that matter and the other stub does not:
    a normalizing getter, and observer callbacks queued rather than invoked
    inline. It then asserts the property the script must have — *the marked
    title is a fixpoint*: write it, read it back, and get the same string.
    """
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const WRITE_BUDGET = input.writeBudget;
const CALLBACK_BUDGET = input.callbackBudget;

// HTML's `document.title` getter: strip and collapse ASCII whitespace.
function normalize(value) {
  return String(value)
    .replace(/[\t\n\f\r ]+/g, " ")
    .replace(/^ /, "")
    .replace(/ $/, "");
}

function runMarkerScript(script, initialTitle, mutate) {
  const observers = [];
  const queue = [];
  let writes = 0;
  let overBudget = false;

  class Document {}
  Object.defineProperty(Document.prototype, "title", {
    configurable: true,
    enumerable: true,
    get() {
      return normalize(this._rawTitle || "");
    },
    set(value) {
      this._rawTitle = String(value);
      if (++writes > WRITE_BUDGET) {
        overBudget = true;
        throw new Error("title write budget exceeded");
      }
      // Writing the title mutates <head>. Real MutationObserver callbacks are
      // delivered as microtasks, i.e. AFTER the writer's stack (and its
      // `normalizing` guard) has unwound — queue, never call inline.
      for (const obs of observers) {
        if (!obs.disconnected) queue.push(() => obs.callback());
      }
    },
  });

  class MutationObserver {
    constructor(callback) {
      this.callback = callback;
      this.disconnected = false;
    }
    observe() {
      observers.push(this);
    }
    disconnect() {
      this.disconnected = true;
    }
  }

  const document = new Document();
  document._rawTitle = initialTitle;
  document.head = { appendChild() {} };
  document.querySelector = () => null;
  document.createElement = () => ({ textContent: "" });
  document.addEventListener = () => {};

  const sandbox = { window: {}, document, Document, HTMLDocument: Document,
                    MutationObserver };

  let converged = true;
  try {
    vm.runInNewContext(script, sandbox);
    if (mutate) mutate(document);
    let steps = 0;
    while (queue.length) {
      if (++steps > CALLBACK_BUDGET) { converged = false; break; }
      queue.shift()();
    }
  } catch (e) {
    converged = false;
  }
  return {
    converged: converged && !overBudget,
    writes,
    raw: document._rawTitle,
    // What the page itself sees through the marker's accessor.
    pageTitle: (() => { try { return document.title; } catch (e) { return null; } })(),
    // What the DOM would hand back for the value we actually stored — the
    // fixpoint check: these two must agree or the observer rewrites forever.
    readBack: normalize(document._rawTitle),
  };
}

const scenarios = {
  // about:blank / data: URLs / a page caught before it sets a title.
  emptyTitle: runMarkerScript(input.installScript, ""),
  // The ordinary case, which always worked.
  normalTitle: runMarkerScript(input.installScript, "Example"),
  // Whitespace-only is empty once the DOM normalizes it.
  whitespaceTitle: runMarkerScript(input.installScript, "   "),
  // A stale marker with nothing behind it.
  bareMarkerTitle: runMarkerScript(input.installScript, "\u{1F440}"),
  // The other write path: the page clearing its own title through the
  // accessor the marker installed.
  pageClearsTitle: runMarkerScript(input.installScript, "Example", (doc) => {
    doc.title = "";
  }),
  // ...and setting a real one.
  pageSetsTitle: runMarkerScript(input.installScript, "", (doc) => {
    doc.title = "Later";
  }),
};

process.stdout.write(JSON.stringify(scenarios));
"""
    proc = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps({**payload, "writeBudget": WRITE_BUDGET,
                          "callbackBudget": CALLBACK_BUDGET}),
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def test_title_marker_is_a_fixpoint_under_html_title_normalization() -> None:
    """The marked title must survive a DOM round-trip unchanged.

    If `read(write(x)) != write(x)` the MutationObserver that re-asserts the
    marker never stops firing, and the renderer's main thread never gets back to
    the event loop — the "random freeze" this file's other test could not see,
    because its DOM stub echoes writes back verbatim.
    """
    results = _run_fixpoint_probe(_marker_sources())

    for label, r in results.items():
        assert r["converged"], (
            f"[{label}] the title marker never settled (writes={r['writes']}, "
            f"raw={r['raw']!r}) — this is the rewrite loop that wedges the "
            f"renderer, i.e. the freeze"
        )
        assert r["raw"] == r["readBack"], (
            f"[{label}] the marker wrote {r['raw']!r} but the DOM hands back "
            f"{r['readBack']!r}; that asymmetry is what loops"
        )
        assert r["writes"] <= 2, (
            f"[{label}] settling took {r['writes']} title writes; one is "
            f"expected, more means the marker is arguing with the DOM"
        )
        assert "\U0001f440" in (r["raw"] or ""), (
            f"[{label}] the marker is missing — the user can no longer see "
            f"which tab the agent is driving: {r}"
        )

    # The visible outcome, unchanged for pages that have a title.
    assert results["normalTitle"]["raw"] == f"{PREFIX}Example"
    assert results["normalTitle"]["pageTitle"] == "Example"
    assert results["pageSetsTitle"]["raw"] == f"{PREFIX}Later"
    assert results["pageSetsTitle"]["pageTitle"] == "Later"
    # ...and for empty ones the marker is the bare eye, with no trailing space
    # for the DOM to strip back off.
    assert results["emptyTitle"]["raw"] == "\U0001f440"
    assert results["emptyTitle"]["pageTitle"] == ""
    assert results["pageClearsTitle"]["raw"] == "\U0001f440"
    assert results["pageClearsTitle"]["pageTitle"] == ""


def test_title_marker_helpers_normalize_and_remove_all_leading_markers() -> None:
    results = _run_node_probe(_marker_sources())

    assert results["stripSamples"] == [
        "Example",
        "",
        "Plain",
        "",
        "Hello",
        "Hello",
        "Hello",
        "Hello",
        "Hello",
        "Hello",
        "42",
        "",
        "true",
    ]
    assert results["duplicateInstallTitle"] == {
        "rawTitle": f"{PREFIX}Example",
        "pageTitle": "Example",
    }
    assert results["cleanInstallTitle"] == {
        "rawTitle": f"{PREFIX}Example",
        "pageTitle": "Example",
    }
    assert results["bareEyeInstallTitle"] == {
        "rawTitle": f"{PREFIX}Hello",
        "pageTitle": "Hello",
    }
    assert results["nbspEyeInstallTitle"] == {
        "rawTitle": f"{PREFIX}Hello",
        "pageTitle": "Hello",
    }
    assert results["narrowEyeInstallTitle"] == {
        "rawTitle": f"{PREFIX}Hello",
        "pageTitle": "Hello",
    }
    assert results["setterTitle"] == {
        "rawTitle": f"{PREFIX}Next",
        "pageTitle": "Next",
    }
    assert results["bareEyeSetterTitle"] == {
        "rawTitle": f"{PREFIX}World",
        "pageTitle": "World",
    }
    assert results["nonStringSetterTitle"] == {
        "rawTitle": f"{PREFIX}42",
        "pageTitle": "42",
        "stripped": "42",
    }
    assert results["duplicateRemoveTitle"] == {
        "rawTitle": "Example",
        "pageTitle": "Example",
    }
    assert results["bareEyeRemoveTitle"] == {
        "rawTitle": "Hello",
        "pageTitle": "Hello",
    }
    assert results["nbspEyeRemoveTitle"] == {
        "rawTitle": "Hello",
        "pageTitle": "Hello",
    }
    assert results["lifecycleTitle"] == {
        "rawTitle": "After",
        "pageTitle": "After",
    }


def test_detach_cleans_marker_after_service_worker_realm_rebuild() -> None:
    results = _run_service_worker_realm_rebuild_probe(
        _service_worker_marker_sources())

    assert results["titleAfterMark"] == f"{PREFIX}Legacy"
    assert results["titleAfterDetach"] == "Legacy"
    assert results["markerAfterDetach"] is False
    assert results["callsAfterRealmRebuild"] == [
        {
            "kind": "command",
            "method": "Runtime.evaluate",
            "params": {"expression": _marker_sources()["removeScript"]},
        },
        {"kind": "detach", "tabId": 17},
    ]


def test_controlled_reload_cleans_known_tabs_before_reloading() -> None:
    sources = _service_worker_marker_sources()
    assert _run_controlled_reload_probe(sources) == [
        {
            "kind": "command",
            "tabId": 17,
            "method": "Page.removeScriptToEvaluateOnNewDocument",
            "params": {"identifier": "known-script"},
        },
        {
            "kind": "command",
            "tabId": 17,
            "method": "Runtime.evaluate",
            "params": {"expression": sources["removeScript"]},
        },
        {"kind": "reload"},
    ]


def test_marker_cleanup_is_ordered_and_reload_remains_best_effort() -> None:
    sources: dict[str, object] = _service_worker_marker_sources()

    remove_failed = _run_controlled_reload_probe({
        **sources,
        "failRemove": True,
    })
    assert [call.get("method") or call["kind"] for call in remove_failed] == [
        "Page.removeScriptToEvaluateOnNewDocument",
        "Runtime.evaluate",
        "reload",
    ]

    remove_hung = _run_controlled_reload_probe({
        **sources,
        "hangRemove": True,
    })
    assert [call.get("method") or call["kind"] for call in remove_hung] == [
        "Page.removeScriptToEvaluateOnNewDocument",
        "Runtime.evaluate",
        "reload",
    ]

    mark_in_flight = _run_controlled_reload_probe({
        **sources,
        "markInFlight": True,
    })
    # Cleanup now invalidates the in-flight generation immediately. Waiting
    # for the full install before sending removal was the bug: the removal
    # deadline could expire, reload/detach would proceed, and the old install
    # continuation could then restore the marker. Page.enable may finish, but
    # no addScript/install-evaluate is allowed after invalidation; current-page
    # removal is queued immediately.
    assert [call.get("method") or call["kind"] for call in mark_in_flight] == [
        "Page.enable",
        "Runtime.evaluate",
        "reload",
    ]
    assert mark_in_flight[1]["params"]["expression"] == sources["removeScript"]


def test_marker_commands_cannot_block_debugger_detach() -> None:
    sources = _service_worker_marker_sources()
    stages = [
        "Page.enable",
        "Page.addScriptToEvaluateOnNewDocument",
        "installEvaluate",
        "Page.removeScriptToEvaluateOnNewDocument",
        "removeEvaluate",
    ]
    for stage in stages:
        result = _run_marker_detach_budget_probe(sources, stage)
        assert result["completed"] is True, stage
        assert result["attached"] is False, stage
        assert result["calls"][-1] == {"kind": "detach", "tabId": 17}, stage


def test_late_marker_install_cannot_resume_after_debugger_detach() -> None:
    sources = _service_worker_marker_sources()
    for stage in (
        "Page.enable",
        "Page.addScriptToEvaluateOnNewDocument",
        "installEvaluate",
    ):
        result = _run_marker_late_completion_probe(sources, stage)
        calls = result["calls"]
        detach_index = result["detachIndex"]
        assert detach_index >= 0, stage
        assert not any(
            call.get("stage") in {
                "Page.addScriptToEvaluateOnNewDocument", "installEvaluate"
            }
            for call in calls[detach_index + 1:]
        ), stage
        assert result["pageMarked"] is False, stage
        assert result["marked"] is False, stage
        assert result["marking"] is False, stage


def test_failed_tab_close_preserves_marker_and_attachment_state() -> None:
    result = _run_failed_close_marker_probe(_service_worker_marker_sources())

    assert result["attached"] is True
    assert result["marked"] is True
    assert [call["kind"] for call in result["calls"]] == ["remove", "response"]
    assert result["calls"][-1]["message"]["error"]["message"] == "close denied"
