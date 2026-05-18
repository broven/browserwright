// popup.js — drives the popup UI by messaging the background service
// worker. The background worker owns the relay ws + chrome.debugger; the
// popup is just a thin remote.

const dot = document.getElementById("dot");
const primary = document.getElementById("status-primary");
const secondary = document.getElementById("status-secondary");
const attachBtn = document.getElementById("attach-btn");
const detachBtn = document.getElementById("detach-btn");
const list = document.getElementById("attached-list");

async function refresh() {
  const status = await new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "status" }, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ connected: false, attachedTabs: [], error: chrome.runtime.lastError.message });
      } else {
        resolve(resp || {});
      }
    });
  });

  if (status.error) {
    dot.className = "dot disconnected";
    primary.textContent = "Background worker error";
    secondary.textContent = status.error;
    attachBtn.disabled = true;
    detachBtn.disabled = true;
    list.innerHTML = "";
    return;
  }

  const connected = !!status.connected;
  dot.className = "dot " + (connected ? "connected" : "disconnected");
  primary.textContent = connected ? "Connected to daemon" : "Disconnected";
  secondary.textContent = connected
    ? `install ${(status.installId || "").slice(0, 12)}…`
    : "Run `browser-daemon serve --backend extension` and reopen this popup";
  attachBtn.disabled = !connected;
  detachBtn.disabled = !connected || (status.attachedTabs || []).length === 0;

  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  list.innerHTML = "";
  for (const tabId of status.attachedTabs || []) {
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch {
      continue;
    }
    const row = document.createElement("div");
    row.className = "row";
    const tag = activeTab && activeTab.id === tabId ? "✱ " : "  ";
    row.textContent = `${tag}${tab.title || "(untitled)"}`;
    row.title = tab.url || "";
    list.appendChild(row);
  }
}

attachBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "attachActive" }, () => {
    setTimeout(refresh, 100);
  });
});

detachBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "detachActive" }, () => {
    setTimeout(refresh, 100);
  });
});

refresh();
// Light auto-refresh while the popup is open.
const handle = setInterval(refresh, 1000);
window.addEventListener("unload", () => clearInterval(handle));
