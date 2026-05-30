// browserwright-daemon relay — Chrome extension service worker.
//
// Speaks the protocol defined in `src/browserwright/daemon/server/relay.py`.
// Wire shape (JSON text frames):
//
//   daemon → us (chrome.debugger requests):
//     {"type":"attach","id":N,"tabId":42}
//     {"type":"detach","id":N,"tabId":42}
//     {"type":"command","id":N,"tabId":42,"method":"Page.navigate","params":{...}}
//     {"type":"queryActiveTab","id":N}
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
//   us → daemon:
//     {"type":"hello","installId":"...","browser":"chrome","version":"..."}
//     {"type":"response","id":N,"result":{...}}
//     {"type":"response","id":N,"error":{"code":-32000,"message":"..."}}
//     {"type":"attached","tabId":42,"targetInfo":{"url":"...","title":"..."}}
//     {"type":"detached","tabId":42}
//     {"type":"event","tabId":42,"method":"Page.frameNavigated","params":{...}}
//     {"type":"activeTab","id":N,"tabId":42,"url":"...","title":"..."}
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
//   straight to the ws. The daemon's relay then routes them per-session.

const RELAY_URL = "ws://127.0.0.1:19989/";
const BROWSERWRIGHT_EXTENSION_PROTOCOL_VERSION = "1";
const PROTOCOL_VERSION = "1.3";  // chrome.debugger.attach signature
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000, 10000];

let ws = null;
let reconnectIdx = 0;
let installId = null;
const attachedTabs = new Set();
let userscriptMemoryLog = [];

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
    // maintainLoop will retry; no setTimeout here (would die when SW idles).
  };

  ws.onerror = (ev) => {
    console.debug("[bd-relay] ws error:", ev);
  };
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
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
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
const PING_INTERVAL_MS = 20000;
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

async function usDelete(key) {
  const { scripts } = await usGetAll();
  const id = scripts[key] ? key
    : Object.values(scripts).find((script) => script.identity === key)?.id;
  if (id) {
    delete scripts[id];
    await chrome.storage.local.set({ [US_KEY]: scripts });
  }
  return id || null;
}

async function usAppendLog(entry) {
  const row = { ts: Date.now(), ...entry };
  userscriptMemoryLog.push(row);
  if (userscriptMemoryLog.length > US_LOG_CAP) {
    userscriptMemoryLog.splice(0, userscriptMemoryLog.length - US_LOG_CAP);
  }
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
  return { ok: failed.length === 0, registered, failed, logCount: userscriptMemoryLog.length };
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
    const scriptId = scripts[key] ? key
      : Object.values(scripts).find((script) => script.identity === key)?.id;
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
    const persisted = v[US_LOG] || [];
    let log = persisted.concat(userscriptMemoryLog);
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
    case "attach":
      return await doAttach(id, msg.tabId);
    case "detach":
      return await doDetach(id, msg.tabId);
    case "command":
      return await doCommand(id, msg.tabId, msg.method, msg.params || {});
    case "queryActiveTab":
      return await doQueryActiveTab(id);
    case "attachActive":
      return await doAttachActive(id, msg.groupId, msg.groupName);
    case "createTab":
      return await doCreateTab(id, msg.url, msg.groupName, msg.groupId, msg.background);
    case "closeTab":
      return await doCloseTab(id, msg.tabId);
    case "queryGroup":
      return await doQueryGroup(id, msg.groupName, msg.groupId);
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
    case "pong":
      // App-level keepalive reply (see pingLoop). The mere fact that this
      // onmessage fired is enough to reset Chrome's SW idle reaper — no
      // further bookkeeping needed.
      return;
    default:
      console.warn("[bd-relay] unknown message type:", type);
  }
}

async function doAttach(id, tabId) {
  try {
    await chrome.debugger.attach({ tabId }, PROTOCOL_VERSION);
    attachedTabs.add(tabId);
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
    markTabAttached(tabId);  // fire-and-forget; cosmetic
    keepTabRendered(tabId);  // fire-and-forget; keep off-screen tab rendering
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function doAttachActive(id, groupId, groupName) {
  // Adopt the user's focused-window active tab INTO this session's tab group
  // (docs C1: adopt, not borrow). The tab becomes a regular group member and
  // closes with the group on endSession like any other member — there is no
  // separate "borrowed" flag.
  //
  // Refuse-on-conflict: if the focused tab already lives in ANOTHER session's
  // group (a real group whose id differs from ours), we refuse and do NOT
  // steal it out of that group. Ungrouped tabs (groupId == -1) and tabs
  // already in our group are fine to adopt.
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
    const ourGroupId = await _resolveSessionGroup(groupId, groupName);
    const tabGroup = typeof tab.groupId === "number" ? tab.groupId : -1;
    const inAGroup = tabGroup >= 0;  // -1 == chrome.tabGroups.TAB_GROUP_ID_NONE
    if (inAGroup && ourGroupId >= 0 && tabGroup !== ourGroupId) {
      safeSend({
        type: "response",
        id,
        error: {
          code: -32000,
          message: "focused tab belongs to another session's tab group " +
            "(groupId=" + tabGroup + "); refusing to steal it. Drag it out " +
            "of that group first, or adopt it from its owning session.",
        },
      });
      return;
    }
    // Move the tab into our group (idempotent if already a member). When we
    // had no live group, chrome.tabs.group({tabIds}) creates one and we name
    // it with the session name for human-readable Chrome UI.
    const finalGroupId = await _ensureTabInGroup(
      tab.id, groupName, ourGroupId, tab.windowId);
    if (!attachedTabs.has(tab.id)) {
      await chrome.debugger.attach({ tabId: tab.id }, PROTOCOL_VERSION);
      attachedTabs.add(tab.id);
    }
    await announceAttached(tab.id);
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
    markTabAttached(tab.id);  // fire-and-forget; cosmetic
    keepTabRendered(tab.id);  // fire-and-forget; keep off-screen tab rendering
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function _resolveSessionGroup(groupId, groupName) {
  // Return the live groupId for this session, or -1 if none exists yet.
  // Primary key = the numeric groupId the daemon remembers; if that group
  // still exists, use it. Do not query by groupName because titles are
  // user-editable and not unique. Never creates a group here.
  if (typeof groupId === "number" && groupId >= 0) {
    try {
      await chrome.tabGroups.get(groupId);
      return groupId;  // still live
    } catch (_e) {
      // groupId went invalid (empty group auto-deleted, etc.).
    }
  }
  return -1;
}

async function doCreateTab(id, url, groupName, sessionGroupId, background) {
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
    // Bind the tab to the session's group. Prefer the daemon-remembered
    // numeric groupId; create a fresh group named with the session name when
    // no live id is available.
    if ((typeof sessionGroupId === "number" && sessionGroupId >= 0) ||
        (typeof groupName === "string" && groupName)) {
      const resolved = await _resolveSessionGroup(sessionGroupId, groupName);
      groupId = await _ensureTabInGroup(
        tab.id, groupName, resolved, tab.windowId);
    }
    await chrome.debugger.attach({ tabId: tab.id }, PROTOCOL_VERSION);
    attachedTabs.add(tab.id);
    await announceAttached(tab.id);
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
    markTabAttached(tab.id);  // fire-and-forget; cosmetic
    keepTabRendered(tab.id);  // fire-and-forget; keep off-screen tab rendering
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
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
  // Best-effort detach first so chrome.debugger doesn't try to talk to the
  // doomed tab as we tear it down. Failures here are silent.
  markedTabs.delete(tabId);  // skip strip-prefix — tab is dying anyway
  try {
    await chrome.debugger.detach({ tabId });
  } catch (_e) {
    // not attached, or mid-close — ignore.
  }
  attachedTabs.delete(tabId);
  try {
    await chrome.tabs.remove(tabId);
    safeSend({ type: "response", id, result: { ok: true, tabId } });
  } catch (e) {
    const msg = String(e?.message || e || "").toLowerCase();
    if (msg.includes("no tab with id")) {
      // Already gone — caller wanted it closed, success-equivalent.
      safeSend({ type: "response", id, result: { ok: true, tabId } });
      return;
    }
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function doDetach(id, tabId) {
  try {
    await unmarkTabBeforeDetach(tabId);
    await chrome.debugger.detach({ tabId });
    attachedTabs.delete(tabId);
    safeSend({ type: "response", id, result: {} });
  } catch (e) {
    // "Debugger is not attached to the tab with id X" — surface as a
    // benign result rather than an error. Daemon already detached us.
    if (String(e?.message || "").toLowerCase().includes("not attached")) {
      attachedTabs.delete(tabId);
      safeSend({ type: "response", id, result: {} });
      return;
    }
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function doCommand(id, tabId, method, params) {
  try {
    const result = await chrome.debugger.sendCommand({ tabId }, method, params);
    safeSend({ type: "response", id, result: result || {} });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function doQueryActiveTab(id) {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    safeSend({
      type: "response",
      id,
      result: tab
        ? {
            tabId: tab.id,
            url: tab.url || "",
            title: stripMarker(tab.title),
          }
        : null,
    });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function doQueryGroup(id, groupName, sessionGroupId) {
  // Live group membership = the single source of truth for "what's in this
  // session's browser" (docs invariant 2). The daemon asks for the tabs of
  // the session's group; we resolve only by the durable numeric groupId.
  // Returns groupId -1 / [] when no group matches — the session's browser
  // currently has no tabs.
  try {
    const groupId = await _resolveSessionGroup(sessionGroupId, groupName);
    if (groupId < 0) {
      safeSend({ type: "response", id, result: { groupId: -1, tabs: [] } });
      return;
    }
    const tabs = await chrome.tabs.query({ groupId });
    const out = (Array.isArray(tabs) ? tabs : []).map((tab) => ({
      tabId: tab.id,
      url: tab.url || "",
      title: stripMarker(tab.title),
      active: !!tab.active,
      lastAccessed: tab.lastAccessed || 0,
    }));
    // Most-recently-accessed first so the daemon can pick a representative tab.
    out.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    safeSend({ type: "response", id, result: { groupId, tabs: out } });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function announceAttached(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
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

const TITLE_PREFIX = "\u{1F440} ";  // 👀 + space

const MARKER_INSTALL_SCRIPT = `
(function() {
  const PREFIX = '\u{1F440} ';

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

  function rawTitle(doc) {
    doc = doc || document;
    if (titleDescriptor && titleDescriptor.get) {
      return titleDescriptor.get.call(doc) || '';
    }
    const el = doc.querySelector && doc.querySelector('title');
    return el ? (el.textContent || '') : '';
  }

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
  function ensurePrefix() {
    if (normalizing) return;
    normalizing = true;
    try {
      const current = rawTitle();
      const clean = stripPrefix(current);
      const marked = PREFIX + clean;
      if (stripPrefix(current) !== clean || current !== marked) {
        writeRawTitle(document, marked);
      }
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
        writeRawTitle(this, PREFIX + stripPrefix(value));
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
  const PREFIX = '\u{1F440} ';
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
  const marker = window.__bdTitleMarker || null;
  const nativeTitleDescriptor =
    marker && marker.nativeTitleDescriptor;
  const titleDescriptor =
    nativeTitleDescriptor ||
    Object.getOwnPropertyDescriptor(Document.prototype, 'title') ||
    (typeof HTMLDocument !== 'undefined'
      ? Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'title')
      : null);
  function rawTitle(doc) {
    doc = doc || document;
    if (titleDescriptor && titleDescriptor.get) {
      return titleDescriptor.get.call(doc) || '';
    }
    const el = doc.querySelector && doc.querySelector('title');
    return el ? (el.textContent || '') : '';
  }
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
    await chrome.debugger.sendCommand(
      { tabId }, "Emulation.setFocusEmulationEnabled", { enabled: true });
  } catch (e) {
    console.warn("[bd-relay] setFocusEmulationEnabled(" + tabId + ") failed:", e);
  }
  try {
    await chrome.debugger.sendCommand(
      { tabId }, "Page.setWebLifecycleState", { state: "active" });
  } catch (e) {
    console.warn("[bd-relay] setWebLifecycleState(" + tabId + ") failed:", e);
  }
}

async function markTabAttached(tabId) {
  if (markedTabs.has(tabId)) return;
  // Reserve the slot up-front so concurrent markTabAttached(tabId) calls
  // (e.g. popup-attach racing daemon attach-active) coalesce.
  markedTabs.set(tabId, "");
  try {
    // Page domain may not be enabled yet on a fresh chrome.debugger session;
    // enabling is idempotent so this is safe to call repeatedly.
    await chrome.debugger.sendCommand({ tabId }, "Page.enable", {});
    const reg = await chrome.debugger.sendCommand(
      { tabId },
      "Page.addScriptToEvaluateOnNewDocument",
      { source: MARKER_INSTALL_SCRIPT },
    );
    markedTabs.set(tabId, reg?.identifier || "");
    // The above fires only on *new* documents; inject into the current one too.
    await chrome.debugger.sendCommand(
      { tabId },
      "Runtime.evaluate",
      { expression: MARKER_INSTALL_SCRIPT },
    );
  } catch (e) {
    // Tab might have closed mid-attach, or chrome.debugger session is gone —
    // not worth failing the whole attach over a cosmetic marker.
    console.warn("[bd-relay] markTabAttached(" + tabId + ") failed:", e);
    markedTabs.delete(tabId);
  }
}

async function unmarkTabBeforeDetach(tabId) {
  const identifier = markedTabs.get(tabId);
  markedTabs.delete(tabId);
  if (identifier === undefined) return;
  try {
    if (identifier) {
      await chrome.debugger.sendCommand(
        { tabId },
        "Page.removeScriptToEvaluateOnNewDocument",
        { identifier },
      );
    }
    await chrome.debugger.sendCommand(
      { tabId },
      "Runtime.evaluate",
      { expression: MARKER_REMOVE_SCRIPT },
    );
  } catch (e) {
    // Tab closing or session already torn down — safe to ignore.
  }
}

// ---- chrome.debugger event fan-out ----------------------------------------

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source || typeof source.tabId !== "number") return;
  safeSend({
    type: "event",
    tabId: source.tabId,
    method,
    params: params || {},
  });
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source && typeof source.tabId === "number") {
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
  attachedTabs.delete(tabId);
  await unmarkTabBeforeDetach(tabId);
  try {
    await chrome.debugger.detach({ tabId });
  } catch (_e) {
    // Already detached / tab gone — onDetach (if any) handles the rest.
  }
  safeSend({ type: "detached", tabId, reason });
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

// A whole group being removed (last tab dragged out / group dissolved) — drop
// any attached tabs that belonged to it. Chrome auto-deletes a group when its
// last tab leaves, so this also covers "session group emptied".
if (chrome.tabGroups && chrome.tabGroups.onRemoved) {
  chrome.tabGroups.onRemoved.addListener((group) => {
    // The tabs are already out of the group by the time this fires; the
    // per-tab onUpdated above handles the detach. This listener exists so a
    // future daemon-side "group gone" notification has a hook — kept minimal.
    console.debug("[bd-relay] tab group removed:", group?.id);
  });
}

// onRemoved fires when a tab is closed outright. If we were driving it, tell
// the daemon so its ghost-target table stays in sync even when chrome.debugger
// onDetach didn't fire first (rare close-ordering races).
chrome.tabs.onRemoved.addListener((tabId) => {
  if (!attachedTabs.has(tabId)) return;
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
        await chrome.debugger.attach({ tabId: tab.id }, PROTOCOL_VERSION);
        attachedTabs.add(tab.id);
        await announceAttached(tab.id);
        markTabAttached(tab.id);  // fire-and-forget; cosmetic
        keepTabRendered(tab.id);  // fire-and-forget; keep off-screen tab rendering
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
        await unmarkTabBeforeDetach(tab.id);
        await chrome.debugger.detach({ tabId: tab.id });
        attachedTabs.delete(tab.id);
        safeSend({ type: "detached", tabId: tab.id, reason: "popup_request" });
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: errMessage(e) });
      }
      return;
    }
    if (msg?.type === "userscript.injected") {
      await usAppendLog({ id: msg.id, url: msg.url, event: "injected" });
      sendResponse({ ok: true });
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
  if (alarm.name === "bd-relay-keepalive" && !ws) {
    connect();
  }
});

usSyncAll().catch((e) => console.warn("[bd-relay] usSyncAll on init failed:", e));
connect();
maintainLoop();
pingLoop();
