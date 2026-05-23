// popup.js — drives the popup UI by messaging the background service
// worker. The background worker owns the relay ws + chrome.debugger; the
// popup is just a thin remote.

const dot = document.getElementById("dot");
const primary = document.getElementById("status-primary");
const secondary = document.getElementById("status-secondary");
const attachBtn = document.getElementById("attach-btn");
const detachBtn = document.getElementById("detach-btn");
const list = document.getElementById("attached-list");
const usMaster = document.getElementById("us-master");
const usList = document.getElementById("us-list");
const usEmpty = document.getElementById("us-empty");

async function refreshUserscripts() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return;
  const resp = await new Promise((resolve) =>
    chrome.runtime.sendMessage({ type: "userscript.popupList", url: tab.url },
      (value) => resolve(value || { scripts: [], master: true })));
  usMaster.checked = resp.master !== false;
  usList.innerHTML = "";
  const scripts = resp.scripts || [];
  usEmpty.style.display = scripts.length ? "none" : "block";
  for (const script of scripts) {
    const li = document.createElement("li");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = script.enabled !== false;
    cb.addEventListener("change", () =>
      chrome.runtime.sendMessage({
        type: "userscript.popupToggle",
        id: script.id,
        enabled: cb.checked,
      }));
    const label = document.createElement("span");
    label.textContent = " " + script.name;
    li.appendChild(cb);
    li.appendChild(label);
    usList.appendChild(li);
  }
}

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
    : "Run `browserwright-daemon serve --backend extension` and reopen this popup";
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
  await refreshUserscripts();
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

usMaster.addEventListener("change", (event) => {
  chrome.runtime.sendMessage({
    type: "userscript.popupMaster",
    enabled: event.target.checked,
  }, () => setTimeout(refreshUserscripts, 100));
});

refresh();
// Light auto-refresh while the popup is open.
const handle = setInterval(refresh, 1000);
window.addEventListener("unload", () => clearInterval(handle));
