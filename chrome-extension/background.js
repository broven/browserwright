// browserwright-daemon relay — Chrome extension service worker.
//
// Speaks the protocol defined in `src/browserwright/daemon/server/relay.py`.
// Wire shape (JSON text frames):
//
//   daemon → us (chrome.debugger requests):
//     {"type":"attach","id":N,"tabId":42}
//     {"type":"detach","id":N,"tabId":42}
//     {"type":"command","id":N,"tabId":42,"method":"Page.navigate","params":{...}}
//     {"type":"attachActive","id":N,"groupId":7,"groupName":"sess-1"}
//     {"type":"createTab","id":N,"url":"...","groupId":7,"groupName":"sess-1"}
//     {"type":"closeTab","id":N,"tabId":42}
//     {"type":"queryGroup","id":N,"groupId":7,"groupName":"sess-1"}
//
// Tab-group = session's browser (see docs/refactor-single-daemon.md): a
// session's durable identity is a Chrome tab GROUP. The daemon binds to the
// numeric `groupId` (passed back on every op); `groupName` (= session name) is
// only the human-visible title applied when a new group is created. Live group
// membership (`chrome.tabs.query({groupId})`) is the single source of truth
// for what is "in" the session; a tab dragged out of the group leaves the
// session (we detach it and emit `detached`).
//
// A group belongs to a session iff its TITLE is that session's `<name>-BW<sid>`
// (ADR-0009). We write that title when we create the group and Chrome restores
// it with the group, which makes it the one anchor that is both ours and
// survives a browser restart. The numeric groupId is Chrome's handle, never the
// identity.
//
//   us → daemon:
//     {"type":"hello","installId":"...","browser":"chrome","version":"..."}
//     {"type":"response","id":N,"result":{...}}
//     {"type":"response","id":N,"error":{"code":-32000,"message":"..."}}
//     {"type":"attached","tabId":42,"targetInfo":{"url":"...","title":"..."}}
//     {"type":"detached","tabId":42}
//     {"type":"event","tabId":42,"method":"Page.frameNavigated","params":{...}}
//
// Design notes:
//
// - MV3 service worker model: Chrome terminates idle SWs after ~30s. The
//   `maintainLoop` below — `while(true) await sleep(...)` — keeps a pending
//   async timer alive at all times, which prevents idle termination
//   (playwriter uses the same trick; verified in Chrome 120+). The setTimeout
//   inside `sleep` is the lifeline: as long as it's pending, SW stays alive.
//   `chrome.runtime.onStartup` + `onInstalled` handle cold-spawn after browser
//   restart / extension install — Chrome won't run the SW top-level on its
//   own there without an event subscription.
// - We DON'T auto-attach all tabs (spec §8.4: user manual attach model).
//   The popup explicitly drives `chrome.debugger.attach`.
// - chrome.debugger events from attached tabs are funneled through `onEvent`
//   straight to the ws. Child-session events are handled inside the extension:
//   they exist only to resume Chromium-paused OOPIF/worker targets.

const RELAY_URL = "ws://127.0.0.1:19989/";
const BROWSERWRIGHT_EXTENSION_PROTOCOL_VERSION = "2";  // ADR-0009: title binding
const PROTOCOL_VERSION = "1.3";  // chrome.debugger.attach signature
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000, 10000];

let ws = null;
let reconnectIdx = 0;
let installId = null;
const attachedTabs = new Set();

const PING_INTERVAL_MS = 20000;
const SERVER_PONG_STALE_MS = 25000;
const LEGACY_PONG_STALE_MS = 45000;
let lastPongTs = 0;
let lastInboundFrameTs = 0;
let seenServerPing = false;
let daemonVersion = null;

// ---- install id (stable across reloads) -----------------------------------

async function getInstallId() {
  if (installId) return installId;
  const v = await chrome.storage.local.get(["installId"]);
  if (v.installId) {
    installId = v.installId;
    return installId;
  }
  installId =
    "bd-" +
    Array.from(crypto.getRandomValues(new Uint8Array(8)))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  await chrome.storage.local.set({ installId });
  return installId;
}

// ---- ws lifecycle ---------------------------------------------------------

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  try {
    ws = new WebSocket(RELAY_URL);
  } catch (e) {
    console.warn("[bd-relay] WebSocket construct failed:", e);
    // maintainLoop sees ws === null on next tick and retries.
    return;
  }

  ws.onopen = async () => {
    try {
      reconnectIdx = 0;
      lastPongTs = Date.now();
      lastInboundFrameTs = lastPongTs;
      const id = await getInstallId();
      const manifest = chrome.runtime.getManifest();
      safeSend({
        type: "hello",
        installId: id,
        browser: "chrome",
        version: manifest.version,
        browserwrightVersion: manifest.version,
        extensionProtocolVersion: BROWSERWRIGHT_EXTENSION_PROTOCOL_VERSION,
      });
      // Re-announce currently-attached tabs so the daemon's ghost table
      // recovers after a reconnect.
      for (const tabId of attachedTabs) {
        announceAttached(tabId).catch((e) =>
          console.warn("[bd-relay] re-announce failed:", e),
        );
      }
    } catch (e) {
      // If `getInstallId()` (or anything else here) rejects, the ws is
      // OPEN but we never sent `hello` — the daemon's `wait_ready` then
      // hits its timeout. Force-close so `onclose` fires and the
      // `maintainLoop` retries cleanly.
      console.warn("[bd-relay] onopen failed:", e);
      try { ws?.close(1011, "hello failed"); } catch {}
    }
  };

  ws.onmessage = (ev) => {
    lastInboundFrameTs = Date.now();
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleDaemonMessage(msg).catch((e) => {
      console.warn("[bd-relay] handler failed:", e);
      if (typeof msg?.id === "number") {
        safeSend({
          type: "response",
          id: msg.id,
          error: { code: -32603, message: String(e) },
        });
      }
    });
  };

  ws.onclose = () => {
    ws = null;
    lastPongTs = 0;
    lastInboundFrameTs = 0;
    // maintainLoop will retry; no setTimeout here (would die when SW idles).
  };

  ws.onerror = (ev) => {
    console.debug("[bd-relay] ws error:", ev);
  };
}

function wsLooksHealthy() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  const now = Date.now();
  const staleMs = seenServerPing ? SERVER_PONG_STALE_MS : LEGACY_PONG_STALE_MS;
  return (now - Math.max(lastPongTs, lastInboundFrameTs)) <= staleMs;
}

function forceReconnect(reason) {
  const old = ws;
  ws = null;
  lastPongTs = 0;
  lastInboundFrameTs = 0;
  try {
    old && old.close(1011, reason || "stale relay connection");
  } catch (_e) {}
  connect();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function maintainLoop() {
  // Perpetual reconnect loop. Note: contrary to a common claim, an
  // `await sleep(...)` chain does NOT by itself keep an MV3 SW alive in
  // Chrome 116+. The real keepalive is `pingLoop` below, which drives
  // app-level ws frames every 20s — Chrome's reaper resets only on
  // ws onmessage/send events, not on setTimeout callbacks or on the
  // protocol-level PING the daemon's `websockets` lib emits.
  while (true) {
    const state = ws ? ws.readyState : WebSocket.CLOSED;
    if (state === WebSocket.OPEN) {
      if (!wsLooksHealthy()) {
        forceReconnect("server heartbeat stale");
      }
      await sleep(1000);
      continue;
    }
    if (state === WebSocket.CONNECTING) {
      await sleep(1000);
      continue;
    }
    ws = null;
    // Defensive: `connect()` is synchronous today (no awaits in its
    // body), but `new WebSocket(URL)` can throw synchronously on a
    // malformed URL. Catch so the SW lifetime loop never dies.
    try {
      connect();
    } catch (e) {
      console.warn("[bd-relay] connect threw:", e);
    }
    const delay = RECONNECT_DELAYS_MS[
      Math.min(reconnectIdx, RECONNECT_DELAYS_MS.length - 1)
    ];
    reconnectIdx += 1;
    await sleep(delay);
  }
}

// MV3 SW lifetime keepalive. Send an app-level ping on the open ws every
// 20s; the daemon's relay echoes a `pong`. Each send resets the SW's idle
// reaper (Chrome 116+ counts ws sends as activity); each incoming pong's
// `onmessage` does the same. With <30s between events, the SW stays alive
// indefinitely. `chrome.alarms` below is a recovery net for the case
// where the SW dies anyway (memory pressure, browser update, etc.).
async function pingLoop() {
  while (true) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      safeSend({ type: "ping", ts: Date.now() });
    }
    await sleep(PING_INTERVAL_MS);
  }
}

function safeSend(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try {
    ws.send(JSON.stringify(obj));
    return true;
  } catch (e) {
    console.warn("[bd-relay] send failed:", e);
    return false;
  }
}

// ---- userscripts: store + chrome.userScripts registration ------------------

const US_KEY = "userscripts";
const US_MASTER = "userscriptsMasterEnabled";
const US_LOG = "userscriptLog";
const US_LOG_CAP = 500;

async function usGetAll() {
  const v = await chrome.storage.local.get([US_KEY, US_MASTER]);
  return { scripts: v[US_KEY] || {}, master: v[US_MASTER] !== false };
}

async function usPutRecord(rec) {
  const { scripts } = await usGetAll();
  const now = Date.now();
  scripts[rec.id] = {
    ...rec,
    enabled: rec.enabled !== false,
    updatedAt: now,
    createdAt: scripts[rec.id]?.createdAt || now,
  };
  await chrome.storage.local.set({ [US_KEY]: scripts });
  return scripts[rec.id];
}

// Resolve a caller-supplied key (script id OR identity) to the script id.
function usResolveId(scripts, key) {
  return scripts[key] ? key
    : Object.values(scripts).find((script) => script.identity === key)?.id;
}

async function usDelete(key) {
  const { scripts } = await usGetAll();
  const id = usResolveId(scripts, key);
  if (id) {
    delete scripts[id];
    await chrome.storage.local.set({ [US_KEY]: scripts });
  }
  return id || null;
}

async function usAppendLog(entry) {
  const row = { ts: Date.now(), ...entry };
  const v = await chrome.storage.local.get([US_LOG]);
  const log = v[US_LOG] || [];
  log.push(row);
  if (log.length > US_LOG_CAP) log.splice(0, log.length - US_LOG_CAP);
  await chrome.storage.local.set({ [US_LOG]: log });
}

function usWrapCode(rec) {
  return (
    "try{chrome.runtime.sendMessage({type:'userscript.injected',id:" +
    JSON.stringify(rec.id) + ",url:location.href});}catch(e){}\n" +
    "(function(){\n" + rec.code + "\n})();"
  );
}


function usToRegistration(rec) {
  const registration = {
    id: rec.id,
    matches: rec.matches,
    js: [{ code: usWrapCode(rec) }],
    runAt: rec.runAt || "document_idle",
    world: "USER_SCRIPT",
    allFrames: false,
  };
  if (rec.excludeMatches && rec.excludeMatches.length) {
    registration.excludeMatches = rec.excludeMatches;
  }
  return registration;
}

async function usSyncAll() {
  if (!chrome.userScripts) {
    console.warn("[bd-relay] chrome.userScripts unavailable (enable 'Allow user scripts')");
    return { ok: false, registered: 0, failed: [], reason: "userScripts API unavailable" };
  }
  try { await chrome.userScripts.configureWorld({ messaging: true }); } catch (e) {}
  const { scripts, master } = await usGetAll();
  const existing = await chrome.userScripts.getScripts({});
  if (existing.length) {
    await chrome.userScripts.unregister({ ids: existing.map((script) => script.id) });
  }
  if (!master) return { ok: true, registered: 0, failed: [] };
  const enabled = Object.values(scripts).filter((script) => script.enabled !== false);
  let registered = 0;
  const failed = [];
  // Register one script at a time: chrome.userScripts.register rejects the
  // entire batch if any single match pattern is invalid, which (after the
  // unregister above) would leave every resident script disabled. Per-script
  // registration contains the blast radius to the offending script.
  for (const rec of enabled) {
    try {
      await chrome.userScripts.register([usToRegistration(rec)]);
      registered += 1;
      await usAppendLog({ event: "registered", id: rec.id, identity: rec.identity });
    } catch (e) {
      failed.push({ id: rec.id, identity: rec.identity, error: errMessage(e) });
      await usAppendLog({ event: "register_failed", id: rec.id, identity: rec.identity, error: errMessage(e) });
    }
  }
  return { ok: failed.length === 0, registered, failed };
}

function usPatternMatchesUrl(pattern, url) {
  if (!pattern || pattern === "<all_urls>") return true;
  try {
    const parsed = new URL(url);
    const match = pattern.match(/^([^:]+):\/\/([^/]+)(\/.*)$/);
    if (match) {
      const scheme = match[1];
      const host = match[2];
      const path = match[3];
      if (scheme !== "*" && scheme !== parsed.protocol.slice(0, -1)) return false;
      // Chrome host semantics: "*" = any host; "*.example.com" = example.com
      // OR any subdomain; otherwise an exact host match.
      if (host !== "*") {
        if (host.startsWith("*.")) {
          const base = host.slice(2);
          if (parsed.hostname !== base && !parsed.hostname.endsWith("." + base)) {
            return false;
          }
        } else if (host !== parsed.hostname && host !== parsed.host) {
          return false;
        }
      }
      const pathRegex = new RegExp("^" + path
        .replace(/[.+?^${}()|[\]\\]/g, "\\$&")
        .replace(/\*/g, ".*") + "$");
      return pathRegex.test(parsed.pathname + parsed.search + parsed.hash);
    }
  } catch (_e) {
    // Fall through to the broad string matcher below.
  }
  const escaped = pattern
    .replace(/[.+?^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*");
  return new RegExp("^" + escaped + "$").test(url);
}

function usRecordMatchesSite(rec, site) {
  if (!site) return true;
  return (rec.matches || []).some((pattern) => usPatternMatchesUrl(pattern, site)) &&
    !(rec.excludeMatches || []).some((pattern) => usPatternMatchesUrl(pattern, site));
}

async function doUserscriptInstall(id, script) {
  try {
    if (!script?.id || !Array.isArray(script.matches) || !script.code) {
      throw new Error("userscript.install requires script {id,matches,code}");
    }
    const rec = await usPutRecord(script);
    const sync = await usSyncAll();
    safeSend({
      type: "response",
      id,
      result: { ok: true, id: rec.id, identity: rec.identity, warnings: rec.warnings || [], sync },
    });
  } catch (e) {
    safeSend({ type: "response", id, error: { code: -32000, message: errMessage(e) } });
  }
}

async function doUserscriptList(id, site) {
  try {
    const { scripts, master } = await usGetAll();
    const rows = Object.values(scripts)
      .filter((script) => usRecordMatchesSite(script, site))
      .map((script) => ({ ...script, enabled: script.enabled !== false }))
      .sort((a, b) => (a.identity || a.id).localeCompare(b.identity || b.id));
    safeSend({ type: "response", id, result: { scripts: rows, master } });
  } catch (e) {
    safeSend({ type: "response", id, error: { code: -32000, message: errMessage(e) } });
  }
}

async function doUserscriptRemove(id, key) {
  try {
    const removed = await usDelete(key);
    const sync = await usSyncAll();
    safeSend({ type: "response", id, result: { ok: true, removed, sync } });
  } catch (e) {
    safeSend({ type: "response", id, error: { code: -32000, message: errMessage(e) } });
  }
}

async function doUserscriptToggle(id, key, enabled) {
  try {
    const { scripts } = await usGetAll();
    const scriptId = usResolveId(scripts, key);
    if (!scriptId) throw new Error("userscript not found: " + key);
    scripts[scriptId].enabled = !!enabled;
    scripts[scriptId].updatedAt = Date.now();
    await chrome.storage.local.set({ [US_KEY]: scripts });
    const sync = await usSyncAll();
    safeSend({ type: "response", id, result: { ok: true, id: scriptId, enabled: !!enabled, sync } });
  } catch (e) {
    safeSend({ type: "response", id, error: { code: -32000, message: errMessage(e) } });
  }
}

async function doUserscriptLogs(id, msg) {
  try {
    const limit = Number.isFinite(msg?.limit) ? msg.limit : 50;
    const v = await chrome.storage.local.get([US_LOG]);
    let log = v[US_LOG] || [];
    // The RPC envelope's own `id` is the request id, so the script-id filter
    // arrives under `scriptId` (see daemon CLI logs construction).
    if (msg?.scriptId) log = log.filter((entry) => entry.id === msg.scriptId);
    safeSend({ type: "response", id, result: { logs: log.slice(-limit) } });
  } catch (e) {
    safeSend({ type: "response", id, error: { code: -32000, message: errMessage(e) } });
  }
}

// ---- daemon command dispatch ----------------------------------------------

async function handleDaemonMessage(msg) {
  const { type, id } = msg || {};
  switch (type) {
    case "helloAck":
      daemonVersion = msg.daemonVersion || null;
      return;
    case "reloadExtension":
      console.info(
        "[bd-relay] reloading extension",
        msg.reason || "manual",
        msg.expectedVersion || daemonVersion || "",
      );
      await cleanupMarkersBeforeReload();
      chrome.runtime.reload();
      return;
    case "attach":
      return await doAttach(id, msg.tabId);
    case "detach":
      return await doDetach(id, msg.tabId);
    case "command":
      return await doCommand(id, msg.tabId, msg.method, msg.params || {});
    case "attachActive":
      return await doAttachActive(id, msg.groupName);
    case "createTab":
      return await doCreateTab(
        id, msg.url, msg.groupName, msg.background,
        msg.skipPostAttachCommands);
    case "closeTab":
      return await doCloseTab(id, msg.tabId);
    case "queryGroup":
      return await doQueryGroup(id, msg.groupName);
    case "userscript.install":
      return await doUserscriptInstall(id, msg.script);
    case "userscript.list":
      return await doUserscriptList(id, msg.site);
    case "userscript.remove":
      return await doUserscriptRemove(id, msg.key);
    case "userscript.toggle":
      return await doUserscriptToggle(id, msg.key, msg.enabled);
    case "userscript.logs":
      return await doUserscriptLogs(id, msg);
    case "ping":
      seenServerPing = true;
      safeSend({ type: "pong", ts: msg.ts || Date.now() });
      return;
    case "pong":
      // App-level keepalive reply (see pingLoop). The mere fact that this
      // onmessage fired is enough to reset Chrome's SW idle reaper — no
      // further bookkeeping needed for the reaper, but lastPongTs lets the
      // recovery net distrust a stale WebSocket that still claims OPEN.
      lastPongTs = Date.now();
      return;
    default:
      console.warn("[bd-relay] unknown message type:", type);
  }
}

// ---- bounded chrome.debugger calls ---------------------------------------
//
// Every chrome.debugger call in this file is awaited through one of the
// wrappers below. Chrome can leave a chrome.debugger promise unsettled
// forever (the OOPIF throttle, a renderer pinned by an infinite loop), and
// an unbounded await means the extension never sends its `response` frame:
// the daemon's `_ExtensionConn.pending` future then never resolves and the
// caller eats the full `_request(timeout)` while this side leaks.
//
// Budgets are deliberately SMALLER than the daemon's matching `_request`
// timeout (relay.py: send_cdp 10.0s, attach_tab/detach_tab 5.0s) so the
// extension always answers FIRST with a distinguishable error frame; the
// daemon's own timeout stays as a last-resort net for a wedged extension.
// The agreement is locked by a test that parses both files
// (test_extension_debugger_timeout_unit.py).
//
// Timeout semantics: the budget error carries code -32001 (CDP's
// implementation-defined range -32000..-32099; Chrome itself never sends
// it) and a `timedOut` flag, so the daemon surfaces it as a `_CommandError`
// distinguishable from a genuine CDP error (-32000) and from its own bare
// `TimeoutError` (-32603). The underlying promise is NOT cancelled — the
// command may still land in Chrome later — but `Promise.race` settles the
// wrapper at the budget, so a late completion is structurally discarded
// (no second response frame, no state mutation). For the multi-await
// attach/detach pipelines, where the race alone cannot cover the
// "completes after detach" window, `tabEpochs` below guards the steps in
// between.
//
// The title-marker path predates this and stays separate on purpose:
// `markerCommandBefore` shares ONE deadline across the phases of a marker
// install/remove (so hung calls cannot multiply the delay) and is guarded
// by its own per-tab tokens. Do not merge the two without re-running the
// marker unit tests.

const DEBUGGER_COMMAND_TIMEOUT_MS = 9000;  // daemon send_cdp: 10.0s
const DEBUGGER_ATTACH_TIMEOUT_MS = 3000;   // daemon attach paths: >= 5.0s
const DEBUGGER_DETACH_TIMEOUT_MS = 3000;   // daemon detach_tab: 5.0s
// Shared budget for Target.setDiscoverTargets + Target.setAutoAttach after
// an attach. Arming is re-done by Playwright anyway, and it runs inside the
// attach RPC's response path, so it must never hold the attach response
// hostage: a wedged renderer must not push the attach reply past the
// daemon's 5.0s wait.
const DEBUGGER_ARM_TIMEOUT_MS = 1500;

// The code the budget error carries. CDP implementation-defined range
// (-32000..-32099); Chrome never emits it, so any -32001 crossing the relay
// is by construction an extension-side timeout.
const DEBUGGER_TIMEOUT_CODE = -32001;

function debuggerTimeoutError(op, detail, timeoutMs) {
  const err = new Error(
    "chrome.debugger." + op + " timed out after " + timeoutMs + "ms (" +
    detail + "); the command may still land in Chrome");
  err.code = DEBUGGER_TIMEOUT_CODE;
  err.timedOut = true;
  return err;
}

function isDebuggerTimeout(e) {
  return !!e && e.timedOut === true;
}

// Error-code passthrough for response frames: a budget timeout carries its
// own -32001; everything else stays the extension's -32000.
function errorCode(e) {
  return (e && typeof e.code === "number") ? e.code : -32000;
}

// Race `start()` (a chrome.debugger call) against a budget timer. The timer
// keeps the MV3 SW alive for the wait, like the maintainLoop trick, and is
// cleared as soon as either side wins.
function boundedDebuggerCall(start, { timeoutMs, op, detail }) {
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(debuggerTimeoutError(op, detail, timeoutMs)),
      Math.max(1, timeoutMs));
  });
  // Defer the call so a synchronous throw becomes a rejection instead of
  // escaping before the race exists (which would leak the guard timer and
  // its eventual unhandled rejection).
  const call = Promise.resolve().then(start);
  return Promise.race([call, guard]).finally(() => {
    if (timer !== null) clearTimeout(timer);
  });
}

function debuggerCommand(target, method, params, { timeoutMs = DEBUGGER_COMMAND_TIMEOUT_MS } = {}) {
  return boundedDebuggerCall(
    () => chrome.debugger.sendCommand(target, method, params || {}),
    { timeoutMs, op: "sendCommand", detail: method + " tabId=" + target.tabId });
}

function debuggerAttach(tabId) {
  return boundedDebuggerCall(
    () => chrome.debugger.attach({ tabId }, PROTOCOL_VERSION),
    { timeoutMs: DEBUGGER_ATTACH_TIMEOUT_MS, op: "attach", detail: "tabId=" + tabId });
}

function debuggerDetach(tabId) {
  return boundedDebuggerCall(
    () => chrome.debugger.detach({ tabId }),
    { timeoutMs: DEBUGGER_DETACH_TIMEOUT_MS, op: "detach", detail: "tabId=" + tabId });
}

// tabId → opaque epoch token for the attach/detach lifecycle. Bumped on
// every attach attempt and every detach-path event (detachTab, onDetach,
// onRemoved, doCloseTab); a continuation that would mutate tab bookkeeping
// after a chrome.debugger call re-checks the token it captured at call
// start. A step whose epoch went stale is abandoned, so a completion that
// lands after a detach cannot resurrect bookkeeping for a tab we no longer
// drive — and a detach that lands after a re-attach cannot tear down the
// fresh attachment's bookkeeping (the ABA case).
const tabEpochs = new Map();

function bumpTabEpoch(tabId) {
  const token = {};
  tabEpochs.set(tabId, token);
  return token;
}

function isTabEpochCurrent(tabId, token) {
  // `undefined` = no epoch recorded (legacy path, e.g. re-announce on
  // reconnect): nothing to compare against, treat as current.
  return token === undefined || tabEpochs.get(tabId) === token;
}

// Shared attach sequence: chrome.debugger.attach → attachedTabs.add →
// armAutoAttach → [announceAttached]. Options:
//   announce (default true) — emit the `attached` ghost-target event. doAttach
//     deliberately passes false: the daemon builds the ghost from the RPC
//     response instead.
//   skipIfAttached (default false) — skip the attach core when we already
//     drive this tab (doAttachActive re-adopt path); announce still fires.
// Returns true when the tab is tracked as attached afterwards; false when the
// sequence was abandoned mid-flight (a detach raced in, or the attach budget
// expired). Callers use that to skip post-attach cosmetics for a tab we no
// longer drive.
async function attachTab(tabId, { announce = true, skipIfAttached = false } = {}) {
  if (skipIfAttached && attachedTabs.has(tabId)) {
    if (announce) await announceAttached(tabId);
    return true;
  }
  const epoch = bumpTabEpoch(tabId);
  await debuggerAttach(tabId);
  // A detach raced in while the attach was in flight (drag-out, DevTools,
  // daemon-initiated). The attach may still have landed in Chrome, but we
  // no longer drive this tab — abandon, and let onDetach clean up when
  // Chrome eventually tears the session down.
  if (!isTabEpochCurrent(tabId, epoch)) return false;
  attachedTabs.add(tabId);
  await armAutoAttach(tabId, epoch);
  if (!isTabEpochCurrent(tabId, epoch)) return false;
  if (announce) await announceAttached(tabId);
  return true;
}

// Post-attach niceties, deliberately NOT awaited by callers. Called at each
// site's original position (after/before the RPC response varies per site).
function postAttachCosmetics(tabId) {
  markTabAttached(tabId);  // fire-and-forget; cosmetic
  keepTabRendered(tabId);  // fire-and-forget; keep off-screen tab rendering
}

// Shared detach cleanup: strip title marker → chrome.debugger.detach →
// attachedTabs.delete → [announce `detached`]. Options:
//   announceReason — when set, emit {type:"detached", reason} to the daemon.
//   ignoreDetachError (default false) — swallow chrome.debugger.detach errors
//     (already detached / tab gone) instead of throwing.
async function detachTab(tabId, { announceReason = null, ignoreDetachError = false } = {}) {
  const epoch = bumpTabEpoch(tabId);
  await unmarkTabBeforeDetach(tabId);
  try {
    await debuggerDetach(tabId);
  } catch (e) {
    if (!ignoreDetachError) throw e;
  }
  // A re-attach raced in while the detach was in flight: leave the fresh
  // attachment's bookkeeping alone (and skip the `detached` announce — the
  // daemon would drop a ghost that just re-joined).
  if (!isTabEpochCurrent(tabId, epoch)) return;
  attachedTabs.delete(tabId);
  if (announceReason) {
    safeSend({ type: "detached", tabId, reason: announceReason });
  }
}

async function doAttach(id, tabId) {
  try {
    // No announceAttached here — the daemon builds the ghost target from
    // this RPC response itself.
    const attached = await attachTab(tabId, { announce: false });
    const tab = await chrome.tabs.get(tabId);
    safeSend({
      type: "response",
      id,
      result: {
        targetInfo: {
          url: tab.url || "",
          title: stripMarker(tab.title),
        },
      },
    });
    if (attached) postAttachCosmetics(tabId);
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: errorCode(e), message: errMessage(e) },
    });
  }
}

async function doAttachActive(id, groupName) {
  // Adopt the user's focused-window active tab INTO this session's tab group
  // (docs C1: adopt, not borrow). The tab becomes a regular group member and
  // closes with the group on endSession like any other member — there is no
  // separate "borrowed" flag.
  //
  // Refuse-on-conflict: if the focused tab already lives in ANY tab group
  // other than ours (a real group whose id differs from ours — the user's own
  // manual groups count as occupied too, since a tab group is the isolation
  // unit between sessions), we refuse and do NOT steal it out of that group.
  // Ungrouped tabs (groupId == -1) and tabs already in our group are fine to
  // adopt.
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) {
      safeSend({
        type: "response",
        id,
        error: { code: -32000, message: "no active tab in focused window" },
      });
      return;
    }
    // Resolve this session's destination group (existing groupId if still
    // live, else create a fresh one on the tab).
    const ourGroupId = await _resolveSessionGroup(groupName);
    const tabGroup = typeof tab.groupId === "number" ? tab.groupId : -1;
    const inAGroup = tabGroup >= 0;  // -1 == chrome.tabGroups.TAB_GROUP_ID_NONE
    if (inAGroup && tabGroup !== ourGroupId) {
      safeSend({
        type: "response",
        id,
        error: {
          code: -32000,
          message: "focused tab is in a tab group (groupId=" + tabGroup +
            "); refusing to take it over. Drag the tab out of the group " +
            "first, then retry.",
        },
      });
      return;
    }
    // Move the tab into our group (idempotent if already a member). When we
    // had no live group, chrome.tabs.group({tabIds}) creates one and we name
    // it with the session name for human-readable Chrome UI.
    const finalGroupId = await _ensureTabInGroup(
      tab.id, groupName, ourGroupId, tab.windowId);
    const attached = await attachTab(tab.id, { skipIfAttached: true });
    safeSend({
      type: "response",
      id,
      result: {
        tabId: tab.id,
        url: tab.url || "",
        title: stripMarker(tab.title),
        groupId: finalGroupId,
      },
    });
    if (attached) postAttachCosmetics(tab.id);
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: errorCode(e), message: errMessage(e) },
    });
  }
}

async function _resolveSessionGroup(groupName) {
  // Return the live groupId for this session, or -1 if none exists yet.
  //
  // ADR-0009: the group TITLE is the session's only binding (`<name>-BW<sid>`).
  // We write it at group creation and Chrome restores it along with the group,
  // which makes it the one anchor that is both ours and survives a browser
  // restart. The numeric groupId is Chrome's handle — recycled across restarts
  // and dropped the moment the group goes empty — so it is never the identity.
  // Never creates a group here.
  //
  // Enumerate and compare exactly rather than `chrome.tabGroups.query({title})`:
  // that field is documented as "Match group titles against a pattern" with an
  // unpublished pattern syntax, so a session name containing `*` or `?` would
  // silently change what matches.
  if (typeof groupName !== "string" || !groupName) return -1;
  try {
    const groups = await chrome.tabGroups.query({});
    for (const g of Array.isArray(groups) ? groups : []) {
      if (g && g.title === groupName) return g.id;
    }
  } catch (_e) {
    // No tabGroups access, or a transient failure — same answer as "no group".
  }
  return -1;
}

async function doCreateTab(
  id, url, groupName, background, skipPostAttachCommands) {
  try {
    if (typeof url !== "string" || !url) {
      throw new Error("createTab requires a url");
    }
    // active:false (the default) keeps the user's focus tab; the daemon sends
    // background:false only when the caller explicitly asked for a foreground
    // tab (open(url, background=False)).
    const active = background === false;
    const tab = await chrome.tabs.create({ url, active });
    let groupId = -1;
    // Bind the tab to the session's group: join the one whose title is this
    // session's, or create it under that title when none exists yet (ADR-0009).
    if (typeof groupName === "string" && groupName) {
      const resolved = await _resolveSessionGroup(groupName);
      groupId = await _ensureTabInGroup(
        tab.id, groupName, resolved, tab.windowId);
    }
    const attached = await attachTab(tab.id);
    let title = tab.title || "";
    let actualUrl = tab.url || url;
    try {
      const refreshed = await chrome.tabs.get(tab.id);
      title = refreshed.title || title;
      actualUrl = refreshed.url || actualUrl;
    } catch (_e) {
      // Tab vanished between create and refresh — keep what we have.
    }
    safeSend({
      type: "response",
      id,
      result: { tabId: tab.id, url: actualUrl, title: stripMarker(title), groupId },
    });
    if (!skipPostAttachCommands && attached) {
      postAttachCosmetics(tab.id);
    }
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: errorCode(e), message: errMessage(e) },
    });
  }
}

async function _ensureTabInGroup(tabId, groupName, resolvedGroupId, windowId) {
  // Move `tabId` into the session's group and return the live groupId.
  //
  // groupId-first (the durable binding): if the caller already resolved a
  // live groupId (via _resolveSessionGroup), join it directly. Otherwise
  // create a fresh group (chrome.tabs.group both creates and assigns) and
  // name it for human-readable Chrome UI.
  if (typeof resolvedGroupId === "number" && resolvedGroupId >= 0) {
    await chrome.tabs.group({ groupId: resolvedGroupId, tabIds: [tabId] });
    return resolvedGroupId;
  }
  // Pin the new group to the tab's OWN window. Without createProperties.windowId
  // chrome.tabs.group() creates the group in the "current" window, which — when
  // several browser windows are open — is often NOT the window the tab lives in.
  // Chrome then tries to move the tab across windows and throws the misleading
  // "Tabs can only be moved to and from normal windows." Binding the group to
  // the tab's window keeps the operation in-window and avoids that failure.
  const groupArgs = { tabIds: [tabId] };
  if (typeof windowId === "number" && windowId >= 0) {
    groupArgs.createProperties = { windowId };
  }
  const newGroupId = await chrome.tabs.group(groupArgs);
  if (typeof groupName === "string" && groupName) {
    try {
      await chrome.tabGroups.update(newGroupId, {
        title: groupName,
        collapsed: false,
      });
    } catch (_e) {
      // Title race; group still exists.
    }
  }
  return newGroupId;
}

async function doCloseTab(id, tabId) {
  try {
    await chrome.tabs.remove(tabId);
    // Chrome removed the tab and tears down its debugger session as part of
    // that operation. Commit our bookkeeping only after that confirmation;
    // otherwise a failed remove would leave a visible marker on a tab we had
    // already forgotten and detached.
    bumpTabEpoch(tabId);
    invalidateMarkerInstall(tabId);
    markedTabs.delete(tabId);
    attachedTabs.delete(tabId);
    safeSend({ type: "response", id, result: { ok: true, tabId } });
  } catch (e) {
    const msg = String(e?.message || e || "").toLowerCase();
    if (msg.includes("no tab with id")) {
      // Already gone — caller wanted it closed, success-equivalent.
      bumpTabEpoch(tabId);
      invalidateMarkerInstall(tabId);
      markedTabs.delete(tabId);
      attachedTabs.delete(tabId);
        safeSend({ type: "response", id, result: { ok: true, tabId } });
      return;
    }
    safeSend({
      type: "response",
      id,
      error: { code: errorCode(e), message: errMessage(e) },
    });
  }
}

async function doDetach(id, tabId) {
  try {
    // No `detached` announcement — the daemon initiated this detach and
    // updates its own state from the RPC response.
    await detachTab(tabId);
    safeSend({ type: "response", id, result: {} });
  } catch (e) {
    // "Debugger is not attached to the tab with id X" — surface as a
    // benign result rather than an error. Daemon already detached us.
    const msg = String(e?.message || "").toLowerCase();
    if (msg.includes("not attached") || isDebuggerTimeout(e)) {
      // Timeout included: the detach decision stands on both sides (the
      // daemon pops its ghost regardless of the outcome), and the tab may
      // still be tearing down — report success so teardown isn't noisier.
      // If Chrome detaches late, onDetach fires and cleans up.
      attachedTabs.delete(tabId);
      safeSend({ type: "response", id, result: {} });
      return;
    }
    safeSend({
      type: "response",
      id,
      error: { code: errorCode(e), message: errMessage(e) },
    });
  }
}

async function doCommand(id, tabId, method, params) {
  try {
    const result = await debuggerCommand({ tabId }, method, params);
    safeSend({ type: "response", id, result: result || {} });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: errorCode(e), message: errMessage(e) },
    });
  }
}

async function armAutoAttach(tabId, epoch) {
  // With chrome.debugger attached, Chromium can pause new child targets
  // (OOPIFs/workers/prerenders) until the debugger resumes them. Arm
  // discovery + auto-attach before callers can navigate the tab. Chromium only
  // surfaces OOPIF child targets reliably after discovery is enabled.
  // Playwright may later send the same auto-attach command for its page
  // session; that is fine and keeps Chrome's page-session contract satisfied.
  //
  // Both commands share one budget (DEBUGGER_ARM_TIMEOUT_MS): arming runs
  // inside the attach RPC's response path and must not push the reply past
  // the daemon's wait. A hung first command skips the second — the deadline
  // is gone either way.
  const deadline = Date.now() + DEBUGGER_ARM_TIMEOUT_MS;
  const remaining = () => Math.max(1, deadline - Date.now());
  try {
    await debuggerCommand(
      { tabId }, "Target.setDiscoverTargets", { discover: true, filter: [{}] },
      { timeoutMs: remaining() });
    if (!isTabEpochCurrent(tabId, epoch)) return;
    await debuggerCommand(
      { tabId }, "Target.setAutoAttach", {
        autoAttach: true,
        waitForDebuggerOnStart: true,
        flatten: true,
        filter: [{}],
      },
      { timeoutMs: remaining() });
    await sleep(50);
  } catch (e) {
    console.warn("[bd-relay] auto-attach arm(" + tabId + ") failed:", e);
  }
}

async function doQueryGroup(id, groupName) {
  // Live group membership = the single source of truth for "what's in this
  // session's browser" (docs invariant 2). The daemon asks for the tabs of
  // the session's group; we resolve it by TITLE (ADR-0009).
  // Returns groupId -1 / [] when no group matches — the session's browser
  // currently has no tabs.
  try {
    const groupId = await _resolveSessionGroup(groupName);
    if (groupId < 0) {
      safeSend({ type: "response", id, result: { groupId: -1, tabs: [] } });
      return;
    }
    // Chrome deletes a group the moment its last tab goes, so the user closing
    // or dragging out that tab between the resolve above and these two calls
    // makes them reject. That is the documented empty-group state, not a
    // failure: reporting -32000 here makes enumeration and teardown fail and
    // keeps the ledger row for a retry that has nothing left to do.
    let group;
    try {
      group = await chrome.tabGroups.get(groupId);
    } catch (_e) {
      safeSend({ type: "response", id, result: { groupId: -1, tabs: [] } });
      return;
    }
    let tabs;
    try {
      tabs = await chrome.tabs.query({ groupId });
    } catch (_e) {
      safeSend({ type: "response", id, result: { groupId: -1, tabs: [] } });
      return;
    }
    const out = (Array.isArray(tabs) ? tabs : []).map((tab) => ({
      tabId: tab.id,
      url: tab.url || "",
      title: stripMarker(tab.title),
      active: !!tab.active,
      lastAccessed: tab.lastAccessed || 0,
    }));
    // Most-recently-accessed first so the daemon can pick a representative tab.
    out.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    safeSend({
      type: "response",
      id,
      result: { groupId, groupTitle: group?.title || "", tabs: out },
    });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function announceAttached(tabId) {
  const epoch = tabEpochs.get(tabId);
  try {
    const tab = await chrome.tabs.get(tabId);
    // Detached while we were reading the tab — a late `attached` frame
    // after the `detached` one would recreate the daemon's ghost. Drop it.
    if (!isTabEpochCurrent(tabId, epoch)) return;
    safeSend({
      type: "attached",
      tabId,
      targetInfo: { url: tab.url || "", title: stripMarker(tab.title) },
    });
  } catch (e) {
    // Tab was closed before we could read it — silently drop.
    attachedTabs.delete(tabId);
  }
}

function errMessage(e) {
  if (!e) return "unknown error";
  return e.message || String(e);
}

// ---- title marker: 👀 prefix on AI-attached tabs --------------------------
//
// Prepend 👀 to document.title on every attached tab so the user can see in
// their browser tab strip which tab the agent is driving. Survives same-doc
// title mutations (React/Next.js routes, jQuery, etc.) via MutationObserver
// on document.head, and survives navigations via
// Page.addScriptToEvaluateOnNewDocument. On graceful detach we strip the
// prefix and disconnect the observer; on unexpected detach (DevTools steals
// the session) the prefix persists until the user reloads — acceptable.

// The marker written in front of a real title: eye + separating space.
//
// There are TWO marker forms on purpose, and the injected side (PREFIX /
// PREFIX_BARE in MARKER_STRIP_PREFIX_SRC) is what decides between them. An
// empty title gets the *bare* eye, because HTML's `document.title` getter
// strips trailing ASCII whitespace: `"👀 "` can never be read back as `"👀 "`.
// The injected observer re-asserts the marker on every <head> mutation, so a
// value that never reads back as what was written is rewritten forever — a
// microtask loop that pins the renderer's main thread. `chrome.debugger`
// commands then never resolve and the daemon reports
// `relay send failed: TimeoutError()`. Only empty-titled pages (about:blank,
// data: URLs, pages caught before their title is set) could reach it, which is
// what made the freeze look random.
//
// This constant is the read side: `stripMarker()` below must keep accepting
// both forms. There is no bare twin here because the SW never writes titles.
const TITLE_PREFIX = "\u{1F440} ";  // 👀 + space

// ---- shared injected-source fragments --------------------------------------
//
// Both MARKER_INSTALL_SCRIPT and MARKER_REMOVE_SCRIPT are assembled from
// these canonical fragments so the prefix-stripping / title-reading logic
// can't drift between the two. The SW-side stripMarker() below mirrors
// MARKER_STRIP_PREFIX_SRC but can't be generated from it (MV3 extension CSP
// forbids eval/new Function) — keep them in sync by hand.
//
// Footgun: everything below lives inside template literals, so a backtick
// anywhere in them — including in a // comment — ends the string early and
// turns background.js into a syntax error. Quote identifiers in these comments
// with plain words, not backticks.

// Defines PREFIX / PREFIX_BARE + stripPrefix(value) + markedTitle(value) in the
// injected scope. Mirrors TITLE_PREFIX / TITLE_PREFIX_BARE / stripMarker() on
// the SW side.
const MARKER_STRIP_PREFIX_SRC = `
  const PREFIX = '\u{1F440} ';
  const PREFIX_BARE = '\u{1F440}';

  function stripPrefix(value) {
    let title = String(value ?? '');
    while (title.startsWith(PREFIX)) {
      title = title.slice(PREFIX.length);
    }
    while (title.length > 0 && title.codePointAt(0) === 0x1F440) {
      title = title.slice(2);
      if (title.length > 0 && /\\s/.test(title[0])) {
        title = title.slice(1);
      }
    }
    return title;
  }

  // The one place that decides what a marked title looks like. Used by BOTH
  // writers (the observer's re-assert and the document.title setter) so they
  // cannot disagree about the empty case — the case that used to loop.
  //
  // Invariant: markedTitle(x) must survive a DOM round-trip unchanged, i.e.
  // reading back what it wrote yields the same string. With a trailing space
  // and an empty clean title it does not, and the observer never settles.
  // stripPrefix() accepts both the spaced and the bare form, so titles marked
  // by an older build are still cleaned up correctly on detach.
  function markedTitle(value) {
    const clean = stripPrefix(value);
    return clean ? PREFIX + clean : PREFIX_BARE;
  }
`;

// Defines rawTitle(doc) in the injected scope. Reads the real document title
// via a `titleDescriptor` that must already be in scope (each script derives
// its own), falling back to the <title> element. writeRawTitle is NOT shared:
// the install and remove scripts intentionally differ in which descriptor
// they trust for writing.
const MARKER_RAW_TITLE_SRC = `
  function rawTitle(doc) {
    doc = doc || document;
    if (titleDescriptor && titleDescriptor.get) {
      return titleDescriptor.get.call(doc) || '';
    }
    const el = doc.querySelector && doc.querySelector('title');
    return el ? (el.textContent || '') : '';
  }
`;

const MARKER_INSTALL_SCRIPT = `
(function() {
` + MARKER_STRIP_PREFIX_SRC + `
  const previousMarker = window.__bdTitleMarker || null;
  try {
    previousMarker && previousMarker.obs && previousMarker.obs.disconnect();
  } catch (e) {}
  try {
    previousMarker && previousMarker.restoreTitleAccessor &&
      previousMarker.restoreTitleAccessor();
  } catch (e) {}

  function findTitleOwner() {
    if (typeof Document !== 'undefined' &&
        Object.getOwnPropertyDescriptor(Document.prototype, 'title')) {
      return Document.prototype;
    }
    if (typeof HTMLDocument !== 'undefined' &&
        Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'title')) {
      return HTMLDocument.prototype;
    }
    return typeof Document !== 'undefined' ? Document.prototype : null;
  }

  const titleOwner = findTitleOwner();
  const titleDescriptor = titleOwner
    ? Object.getOwnPropertyDescriptor(titleOwner, 'title')
    : null;
` + MARKER_RAW_TITLE_SRC + `
  function writeRawTitle(doc, title) {
    doc = doc || document;
    if (titleDescriptor && titleDescriptor.set) {
      titleDescriptor.set.call(doc, title);
      return;
    }
    let el = doc.querySelector && doc.querySelector('title');
    if (!el && doc.head && doc.createElement) {
      el = doc.createElement('title');
      doc.head.appendChild(el);
    }
    if (el) el.textContent = title;
  }

  let normalizing = false;
  // The value we last asked the DOM to store, cleared once it sticks.
  //
  // The normalizing flag is not a guard against self-feeding writes:
  // ensurePrefix runs from a MutationObserver, whose callbacks are delivered
  // asynchronously, so the finally below has always released the flag before
  // the next one arrives. markedTitle() is a fixpoint, so this latch never
  // fires in practice — but if a write ever fails to round-trip again (a future
  // DOM normalization rule, another script fighting us for the title) it caps
  // the argument at one wasted write instead of an unbounded microtask loop
  // that freezes the tab.
  let lastWritten = null;
  function ensurePrefix() {
    if (normalizing) return;
    normalizing = true;
    try {
      const current = rawTitle();
      const marked = markedTitle(current);
      if (current === marked) {
        lastWritten = null;  // settled; a later title change may re-mark
        return;
      }
      if (marked === lastWritten) return;  // already written, it didn't stick
      lastWritten = marked;
      writeRawTitle(document, marked);
    } finally {
      normalizing = false;
    }
  }

  function installTitleAccessor(target) {
    if (!target) return;
    Object.defineProperty(target, 'title', {
      configurable: true,
      enumerable: true,
      get: function() {
        return stripPrefix(rawTitle(this));
      },
      set: function(value) {
        // Same fixpoint rule as ensurePrefix — a page clearing its own title
        // must not store a value the getter will normalize into a mismatch.
        writeRawTitle(this, markedTitle(value));
      },
    });
  }

  function restoreTitleAccessor() {
    try { delete document.title; } catch (e) {}
    if (!titleOwner) return;
    try {
      if (titleDescriptor) {
        Object.defineProperty(titleOwner, 'title', titleDescriptor);
      } else {
        delete titleOwner.title;
      }
    } catch (e) {}
  }

  try {
    installTitleAccessor(titleOwner);
    installTitleAccessor(document);
  } catch (e) {}

  const obs = new MutationObserver(ensurePrefix);
  function attachObs() {
    if (!document.head) return;
    obs.observe(document.head, { childList: true, characterData: true, subtree: true });
    ensurePrefix();
  }
  if (document.head) {
    attachObs();
  } else {
    document.addEventListener('DOMContentLoaded', attachObs, { once: true });
  }
  window.__bdTitleMarker = {
    obs,
    ensurePrefix,
    restoreTitleAccessor,
    nativeTitleDescriptor: titleDescriptor,
    nativeTitleOwner: titleOwner,
  };
})();
`;

const MARKER_REMOVE_SCRIPT = `
(function() {
` + MARKER_STRIP_PREFIX_SRC + `
  const marker = window.__bdTitleMarker || null;
  const nativeTitleDescriptor =
    marker && marker.nativeTitleDescriptor;
  const titleDescriptor =
    nativeTitleDescriptor ||
    Object.getOwnPropertyDescriptor(Document.prototype, 'title') ||
    (typeof HTMLDocument !== 'undefined'
      ? Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'title')
      : null);
` + MARKER_RAW_TITLE_SRC + `
  function writeRawTitle(doc, title) {
    doc = doc || document;
    if (nativeTitleDescriptor && nativeTitleDescriptor.set) {
      nativeTitleDescriptor.set.call(doc, title);
      return;
    }
    if (titleDescriptor && titleDescriptor.set &&
        !(marker && marker.restoreTitleAccessor)) {
      titleDescriptor.set.call(doc, title);
      return;
    }
    let el = doc.querySelector && doc.querySelector('title');
    if (!el && doc.head && doc.createElement) {
      el = doc.createElement('title');
      doc.head.appendChild(el);
    }
    if (el) el.textContent = title;
  }
  try { window.__bdTitleMarker && window.__bdTitleMarker.obs.disconnect(); } catch (e) {}
  const clean = stripPrefix(rawTitle());
  try {
    marker && marker.restoreTitleAccessor && marker.restoreTitleAccessor();
  } catch (e) {
    try { delete document.title; } catch (_e) {}
  }
  delete window.__bdTitleMarker;
  writeRawTitle(document, clean);
})();
`;

// tabId → scriptIdentifier returned by Page.addScriptToEvaluateOnNewDocument
// (needed to remove the per-document hook on detach).
const markedTabs = new Map();
// tabId → {token, promise} for the in-flight installation. The unique token is
// the cancellation/ABA guard: detach invalidates it, and a later re-attach gets
// a different token that an old catch/finally cannot erase.
const markingTabs = new Map();
// tabId → current opaque generation token. Object identity (not a resettable
// integer) stays safe if Chrome later recycles a numeric tab id.
const markerTokens = new Map();
const MARKER_RELOAD_CLEANUP_TIMEOUT_MS = 1500;
// Marker CDP is cosmetic and must never hold debugger detach hostage. Each
// phase gets one absolute budget shared by all of its sendCommand calls, so a
// sequence of hung calls cannot multiply the delay.
const MARKER_INSTALL_TIMEOUT_MS = 1500;
const MARKER_REMOVE_TIMEOUT_MS = 1000;

function invalidateMarkerInstall(tabId) {
  const pending = markingTabs.get(tabId)?.promise;
  markerTokens.set(tabId, {});
  markingTabs.delete(tabId);
  return pending;
}

async function markerCommandBefore(deadline, tabId, method, params) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    throw new Error("marker " + method + " exceeded its deadline");
  }
  let timer = null;
  try {
    return await Promise.race([
      chrome.debugger.sendCommand({ tabId }, method, params),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(
          "marker " + method + " timed out")), remaining);
      }),
    ]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

// SW-side twin of the injected stripPrefix — see MARKER_STRIP_PREFIX_SRC
// above. Can't be generated from that fragment (MV3 CSP bans eval); if you
// change one, change both.
//
// Handles BOTH marker forms, and must keep doing so: the first loop eats
// TITLE_PREFIX (`"👀 Foo"`), the second eats a bare TITLE_PREFIX_BARE plus one
// optional following whitespace char (`"👀"`, `"👀Foo"`). That is not just
// tidiness — tabs marked by an older build carry the spaced form, and a user's
// long-lived Chrome still has those tabs open across an extension update, so a
// stripper that only knew the new form would leave 👀 stuck in their tab strip.
// There is deliberately no markedTitle() twin here: the SW only ever *reads*
// titles (to report them upstream), it never writes one.
function stripMarker(title) {
  let clean = String(title ?? "");
  while (clean.startsWith(TITLE_PREFIX)) {
    clean = clean.slice(TITLE_PREFIX.length);
  }
  while (clean.length > 0 && clean.codePointAt(0) === 0x1F440) {
    clean = clean.slice(2);
    if (clean.length > 0 && /\s/.test(clean[0])) {
      clean = clean.slice(1);
    }
  }
  return clean;
}

async function keepTabRendered(tabId) {
  // Make a backgrounded tab behave as if the user is viewing it, so pages
  // that only render/advance when focused+visible keep working off-screen:
  // unthrottles requestAnimationFrame, keeps document.hasFocus() true, and
  // stops Chrome from freezing/discarding the tab. fire-and-forget; each
  // command is independently guarded so an unsupported one doesn't sink the
  // other, and a failure never fails the attach (cosmetic-ish, like markTab).
  try {
    await debuggerCommand(
      { tabId }, "Emulation.setFocusEmulationEnabled", { enabled: true });
  } catch (e) {
    console.warn("[bd-relay] setFocusEmulationEnabled(" + tabId + ") failed:", e);
  }
  try {
    await debuggerCommand(
      { tabId }, "Page.setWebLifecycleState", { state: "active" });
  } catch (e) {
    console.warn("[bd-relay] setWebLifecycleState(" + tabId + ") failed:", e);
  }
}

async function markTabAttached(tabId) {
  const pending = markingTabs.get(tabId);
  if (pending && markerTokens.get(tabId) === pending.token) {
    await pending.promise;
    return;
  }
  if (markedTabs.has(tabId)) return;
  const token = {};
  markerTokens.set(tabId, token);
  const isCurrent = () => markerTokens.get(tabId) === token;
  const install = (async () => {
    const deadline = Date.now() + MARKER_INSTALL_TIMEOUT_MS;
    // Reserve the slot up-front so concurrent markTabAttached(tabId) calls
    // (e.g. popup-attach racing daemon attach-active) coalesce.
    markedTabs.set(tabId, "");
    try {
      // Page domain may not be enabled yet on a fresh chrome.debugger session;
      // enabling is idempotent so this is safe to call repeatedly.
      await markerCommandBefore(deadline, tabId, "Page.enable", {});
      if (!isCurrent()) return;
      const reg = await markerCommandBefore(
        deadline,
        tabId,
        "Page.addScriptToEvaluateOnNewDocument",
        { source: MARKER_INSTALL_SCRIPT },
      );
      if (!isCurrent()) {
        // Best effort for a registration that completed as detach invalidated
        // us. Detach itself clears debugger-session registrations; this covers
        // implementations where the completion won that race. Bounded like
        // every other chrome.debugger call here.
        if (reg?.identifier) {
          debuggerCommand(
            { tabId },
            "Page.removeScriptToEvaluateOnNewDocument",
            { identifier: reg.identifier },
          ).catch(() => {});
        }
        return;
      }
      markedTabs.set(tabId, reg?.identifier || "");
      // The above fires only on new documents; inject into the current one too.
      await markerCommandBefore(
        deadline,
        tabId,
        "Runtime.evaluate",
        { expression: MARKER_INSTALL_SCRIPT },
      );
      if (!isCurrent()) return;
    } catch (e) {
      // Tab might have closed mid-attach, or chrome.debugger session is gone —
      // not worth failing the whole attach over a cosmetic marker.
      console.warn("[bd-relay] markTabAttached(" + tabId + ") failed:", e);
      if (isCurrent()) markedTabs.delete(tabId);
    }
  })();
  const record = { token, promise: install };
  markingTabs.set(tabId, record);
  try {
    await install;
  } finally {
    if (markingTabs.get(tabId) === record) markingTabs.delete(tabId);
  }
}

async function unmarkTabBeforeDetach(tabId) {
  const deadline = Date.now() + MARKER_REMOVE_TIMEOUT_MS;
  const pending = invalidateMarkerInstall(tabId);
  // Invalidate before any wait. Every continuation of the old installation
  // checks its opaque token before issuing the next marker command.
  const identifier = markedTabs.get(tabId);
  markedTabs.delete(tabId);
  // Dispatch both cleanup commands immediately. They queue behind any marker
  // command Chrome is already processing; notably, current-page removal then
  // runs after a late install Runtime.evaluate instead of detach racing ahead
  // and leaving the visible marker behind. Awaiting one cleanup before sending
  // the other would let it consume the whole shared deadline.
  const removals = [];
  if (identifier) {
    removals.push(markerCommandBefore(
        deadline,
        tabId,
        "Page.removeScriptToEvaluateOnNewDocument",
        { identifier },
      ).catch(() => {}));
  }
  removals.push(markerCommandBefore(
      deadline,
      tabId,
      "Runtime.evaluate",
      { expression: MARKER_REMOVE_SCRIPT },
    ).catch(() => {}));
  await Promise.allSettled(removals);
  if (pending) {
    // Still bounded: a wedged renderer must never hold debugger detach hostage.
    await Promise.race([
      pending.catch(() => {}),
      sleep(Math.max(0, deadline - Date.now())),
    ]);
  }
}

async function cleanupMarkersBeforeReload() {
  // A controlled extension reload destroys this service-worker realm and both
  // in-memory sets. Strip markers while chrome.debugger is still usable. The
  // union also covers a mark still in flight (attached, but no identifier yet)
  // and any bookkeeping drift between the two collections.
  const knownTabs = new Set([...attachedTabs, ...markedTabs.keys()]);
  const cleanup = Promise.allSettled(
    [...knownTabs].map((tabId) => unmarkTabBeforeDetach(tabId)),
  );
  const completed = await Promise.race([
    cleanup.then(() => true),
    sleep(MARKER_RELOAD_CLEANUP_TIMEOUT_MS).then(() => false),
  ]);
  if (!completed) {
    console.warn("[bd-relay] marker cleanup timed out; reloading anyway");
  }
}

// ---- chrome.debugger event fan-out ----------------------------------------

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source || typeof source.tabId !== "number") return;
  if (method === "Target.attachedToTarget"
      && typeof params?.sessionId === "string"
      && params.sessionId) {
    if (params.waitingForDebugger) {
      const debuggerSession = { ...source, sessionId: params.sessionId };
      debuggerCommand(
        debuggerSession,
        "Runtime.runIfWaitingForDebugger",
        {},
      ).catch((e) => {
        console.warn(
          "[bd-relay] runIfWaitingForDebugger(" + source.tabId + ") failed:",
          e,
        );
      });
    }
    return;
  }
  if (method === "Target.detachedFromTarget"
      && typeof params?.sessionId === "string"
      && params.sessionId) {
    return;
  }
  if (typeof source.sessionId === "string" && source.sessionId) {
    return;
  }
  safeSend({
    type: "event",
    tabId: source.tabId,
    method,
    params: params || {},
  });
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source && typeof source.tabId === "number") {
    // A detach invalidates every in-flight attach/arm continuation for this
    // tab: whatever Chrome settles from here on is stale by construction.
    bumpTabEpoch(source.tabId);
    invalidateMarkerInstall(source.tabId);
    attachedTabs.delete(source.tabId);
    // Unexpected detach (DevTools steals the session, tab crashes, etc.) —
    // we can no longer run CDP commands, so the page-side observer keeps the
    // 👀 prefix on the current document. It clears naturally on next
    // navigation (addScriptToEvaluateOnNewDocument is no longer registered).
    markedTabs.delete(source.tabId);
    safeSend({ type: "detached", tabId: source.tabId, reason });
  }
});

// ---- group-membership = session membership --------------------------------
//
// docs invariant 3: entering/leaving the session's tab group == entering/
// leaving the session's browser. A tab the agent drives that the user drags
// OUT of the group (or that gets removed/ungrouped) must leave the session:
// we detach chrome.debugger and emit `detached` so the daemon drops it from
// its ghost-target table. We only act on tabs WE attached (`attachedTabs`),
// so unrelated user tab/group activity is ignored.
//
// Membership truth is always re-derived from chrome.tabs.query({groupId});
// these events are just the trigger to re-check an attached tab's group.

async function _detachAttachedTab(tabId, reason) {
  if (!attachedTabs.has(tabId)) return;
  // Delete up-front so a concurrent re-trigger can't double-detach.
  attachedTabs.delete(tabId);
  // ignoreDetachError: already detached / tab gone — onDetach (if any)
  // handles the rest.
  await detachTab(tabId, { announceReason: reason, ignoreDetachError: true });
}

// onUpdated fires with changeInfo.groupId when a tab is dragged into/out of a
// group. groupId === -1 (TAB_GROUP_ID_NONE) means it left its group entirely;
// any other value means it moved to a different group. Either way the tab is
// no longer in the session's group, so it leaves the session.
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!("groupId" in changeInfo)) return;
  if (!attachedTabs.has(tabId)) return;
  // The tab moved out of (or between) groups. We can't cheaply know which
  // session's group it should still be in here, but an attached agent tab
  // that the user pulls out of its group is, by the locked model, leaving the
  // session — detach it. (Re-adopting requires an explicit attach_active.)
  _detachAttachedTab(tabId, "dragged_out_of_group").catch((e) =>
    console.warn("[bd-relay] drag-out detach failed:", e));
});

// Note: no chrome.tabGroups.onRemoved listener is needed — when a group
// dissolves, its tabs fire per-tab onUpdated (groupId change) above, which
// handles the detach.

// onRemoved fires when a tab is closed outright. If we were driving it, tell
// the daemon so its ghost-target table stays in sync even when chrome.debugger
// onDetach didn't fire first (rare close-ordering races).
chrome.tabs.onRemoved.addListener((tabId) => {
  if (!attachedTabs.has(tabId)) return;
  bumpTabEpoch(tabId);
  invalidateMarkerInstall(tabId);
  attachedTabs.delete(tabId);
  markedTabs.delete(tabId);
  safeSend({ type: "detached", tabId, reason: "tab_closed" });
});

// ---- popup → background message bridge ------------------------------------
//
// The popup script can't open a ws directly (would also work, but we
// centralize the connection here). It sends `chrome.runtime.sendMessage`s
// for "attach this tab" / "detach this tab" / "status".

if (chrome.runtime.onUserScriptMessage) {
  chrome.runtime.onUserScriptMessage.addListener((msg, _sender, sendResponse) => {
    (async () => {
      if (msg?.type === "userscript.injected") {
        await usAppendLog({ id: msg.id, url: msg.url, event: "injected" });
        sendResponse({ ok: true });
        return;
      }
      sendResponse({ ok: false, error: "unknown user script message type" });
    })();
    return true;
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "status") {
      sendResponse({
        connected: !!ws && ws.readyState === WebSocket.OPEN,
        attachedTabs: Array.from(attachedTabs),
        installId: await getInstallId(),
      });
      return;
    }
    if (msg?.type === "attachActive") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) {
        sendResponse({ ok: false, error: "no active tab" });
        return;
      }
      try {
        await attachTab(tab.id);
        postAttachCosmetics(tab.id);
        sendResponse({ ok: true, tabId: tab.id });
      } catch (e) {
        sendResponse({ ok: false, error: errMessage(e) });
      }
      return;
    }
    if (msg?.type === "detachActive") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) {
        sendResponse({ ok: false, error: "no active tab" });
        return;
      }
      try {
        await detachTab(tab.id, { announceReason: "popup_request" });
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: errMessage(e) });
      }
      return;
    }
    if (msg?.type === "userscript.popupList") {
      const { scripts, master } = await usGetAll();
      sendResponse({
        master,
        scripts: Object.values(scripts)
          .filter((script) => usRecordMatchesSite(script, msg.url))
          .map((script) => ({ id: script.id, name: script.name, enabled: script.enabled !== false })),
      });
      return;
    }
    if (msg?.type === "userscript.popupToggle") {
      const { scripts } = await usGetAll();
      if (scripts[msg.id]) {
        scripts[msg.id].enabled = !!msg.enabled;
        scripts[msg.id].updatedAt = Date.now();
        await chrome.storage.local.set({ [US_KEY]: scripts });
      }
      const sync = await usSyncAll();
      sendResponse({ ok: true, sync });
      return;
    }
    if (msg?.type === "userscript.popupMaster") {
      await chrome.storage.local.set({ [US_MASTER]: !!msg.enabled });
      const sync = await usSyncAll();
      sendResponse({ ok: true, sync });
      return;
    }
    sendResponse({ ok: false, error: "unknown message type" });
  })();
  return true; // async response
});

// ---- boot ------------------------------------------------------------------

// Cold-wake hooks: Chrome doesn't run the SW top-level on its own after
// browser restart or extension install/update — these handlers force it.
chrome.runtime.onStartup.addListener(() => {
  if (!ws) connect();
});
chrome.runtime.onInstalled.addListener(() => {
  if (!ws) connect();
});

// Belt-and-suspenders: chrome.alarms wakes the SW every 30s even after
// Chrome has fully terminated it (maintainLoop only protects an already-
// running SW). 30s is the MV3 minimum periodInMinutes (0.5). If the SW
// gets terminated despite maintainLoop (memory pressure, long uptime,
// etc.) this alarm will respawn it within 30s and reconnect.
chrome.alarms.create("bd-relay-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "bd-relay-keepalive") return;
  if (!ws) {
    connect();
    return;
  }
  if (ws.readyState !== WebSocket.OPEN && ws.readyState !== WebSocket.CONNECTING) {
    forceReconnect("alarm found closed relay socket");
    return;
  }
  if (ws.readyState === WebSocket.OPEN && !wsLooksHealthy()) {
    forceReconnect("alarm detected stale relay heartbeat");
  }
});

usSyncAll().catch((e) => console.warn("[bd-relay] usSyncAll on init failed:", e));
connect();
maintainLoop();
pingLoop();
