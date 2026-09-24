// Verbatim ws-lifecycle functions from chrome-extension/background.js at v0.15.0
// (the Chrome Web Store build that predates the GH#79 socket-identity fix).
// Used by test_issue106_legacy_supersede_livelock.py -- do not edit.

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
