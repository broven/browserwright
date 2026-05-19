// browser-daemon relay — Chrome extension service worker.
//
// Speaks the protocol defined in `src/browser_daemon/server/relay.py`.
// Wire shape (JSON text frames):
//
//   daemon → us (chrome.debugger requests):
//     {"type":"attach","id":N,"tabId":42}
//     {"type":"detach","id":N,"tabId":42}
//     {"type":"command","id":N,"tabId":42,"method":"Page.navigate","params":{...}}
//     {"type":"queryActiveTab","id":N}
//     {"type":"attachActive","id":N}
//     {"type":"createTab","id":N,"url":"...","groupName":"Agent"}
//     {"type":"closeTab","id":N,"tabId":42}
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
const PROTOCOL_VERSION = "1.3";  // chrome.debugger.attach signature
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000, 10000];

let ws = null;
let reconnectIdx = 0;
let installId = null;
const attachedTabs = new Set();

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
  // Perpetual reconnect + keepalive. The pending setTimeout from `sleep`
  // is what stops Chrome from idling the service worker.
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
      return await doAttachActive(id);
    case "createTab":
      return await doCreateTab(id, msg.url, msg.groupName);
    case "closeTab":
      return await doCloseTab(id, msg.tabId);
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
          title: tab.title || "",
        },
      },
    });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function doAttachActive(id) {
  // Daemon-driven equivalent of the popup's "Attach this tab" click — picks
  // the currently-focused-window active tab, attaches the debugger, and
  // announces so the daemon's ghost-target table is populated identically
  // to the popup path.
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
    await chrome.debugger.attach({ tabId: tab.id }, PROTOCOL_VERSION);
    attachedTabs.add(tab.id);
    await announceAttached(tab.id);
    safeSend({
      type: "response",
      id,
      result: {
        tabId: tab.id,
        url: tab.url || "",
        title: tab.title || "",
      },
    });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function doCreateTab(id, url, groupName) {
  try {
    if (typeof url !== "string" || !url) {
      throw new Error("createTab requires a url");
    }
    // active:false is critical: we don't want to steal user focus.
    const tab = await chrome.tabs.create({ url, active: false });
    let groupId = -1;
    if (typeof groupName === "string" && groupName) {
      try {
        groupId = await _ensureTabInGroup(tab.id, groupName);
      } catch (ge) {
        console.warn("[bd-relay] grouping failed:", ge);
        groupId = -1;
      }
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
      result: { tabId: tab.id, url: actualUrl, title, groupId },
    });
  } catch (e) {
    safeSend({
      type: "response",
      id,
      error: { code: -32000, message: errMessage(e) },
    });
  }
}

async function _ensureTabInGroup(tabId, groupName) {
  // Reuse a deterministic existing group with matching title; otherwise
  // create a fresh group (chrome.tabs.group both creates and assigns).
  let groupId = -1;
  try {
    const matching = await chrome.tabGroups.query({ title: groupName });
    if (Array.isArray(matching) && matching.length > 0) {
      matching.sort((a, b) => a.id - b.id);
      groupId = matching[0].id;
    }
  } catch (_e) {
    groupId = -1;
  }
  if (groupId >= 0) {
    await chrome.tabs.group({ groupId, tabIds: [tabId] });
    return groupId;
  }
  const newGroupId = await chrome.tabs.group({ tabIds: [tabId] });
  try {
    await chrome.tabGroups.update(newGroupId, {
      title: groupName,
      collapsed: false,
    });
  } catch (_e) {
    // Title race; group still exists.
  }
  return newGroupId;
}

async function doCloseTab(id, tabId) {
  // Best-effort detach first so chrome.debugger doesn't try to talk to the
  // doomed tab as we tear it down. Failures here are silent.
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
            title: tab.title || "",
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

async function announceAttached(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    safeSend({
      type: "attached",
      tabId,
      targetInfo: { url: tab.url || "", title: tab.title || "" },
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
    safeSend({ type: "detached", tabId: source.tabId, reason });
  }
});

// ---- popup → background message bridge ------------------------------------
//
// The popup script can't open a ws directly (would also work, but we
// centralize the connection here). It sends `chrome.runtime.sendMessage`s
// for "attach this tab" / "detach this tab" / "status".

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
        await chrome.debugger.detach({ tabId: tab.id });
        attachedTabs.delete(tab.id);
        safeSend({ type: "detached", tabId: tab.id, reason: "popup_request" });
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: errMessage(e) });
      }
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

connect();
maintainLoop();
