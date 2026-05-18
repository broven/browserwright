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
// - MV3 service worker model: the worker can be terminated when idle. The ws
//   reconnect loop is what keeps it alive while attached — Chrome considers
//   an open ws "active". When the user has zero tabs attached AND zero
//   pending commands, letting the worker sleep is fine; the next popup click
//   re-spins it up.
// - We DON'T auto-attach all tabs (spec §8.4: user manual attach model).
//   The popup explicitly drives `chrome.debugger.attach`.
// - chrome.debugger events from attached tabs are funneled through `onEvent`
//   straight to the ws. The daemon's relay then routes them per-session.

const RELAY_URL = "ws://127.0.0.1:19988/";
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

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  try {
    ws = new WebSocket(RELAY_URL);
  } catch (e) {
    console.warn("[bd-relay] WebSocket construct failed:", e);
    scheduleReconnect();
    return;
  }

  ws.onopen = async () => {
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
    scheduleReconnect();
  };

  ws.onerror = (ev) => {
    console.debug("[bd-relay] ws error:", ev);
  };
}

function scheduleReconnect() {
  const delay = RECONNECT_DELAYS_MS[Math.min(reconnectIdx, RECONNECT_DELAYS_MS.length - 1)];
  reconnectIdx += 1;
  setTimeout(connect, delay);
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

connect();
