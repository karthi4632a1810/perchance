// Perchance Bridge, background service worker:
// Mediates between server.py (http://127.0.0.1:8010/bridge) and the Perchance AI Agent tab.
importScripts("config.js");

const TARGET_PAGE = typeof AGENT_PAGE !== "undefined" ? AGENT_PAGE : "https://perchance.org/minimal#edit";
const STALE_MS = 3 * 60 * 1000;

async function callBridge(action, body, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${BRIDGE_URL}?action=${action}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-Key": BRIDGE_KEY || "",
      },
      body: JSON.stringify(body || {}),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

const note = (fields) => chrome.storage.local.set(fields);

async function handle(message, sender) {
  const url = (sender.tab && sender.tab.url) || "";

  if (message.type === "hello") {
    const isPerchance = url.includes("perchance.org");
    return { bridge: isPerchance, url };
  }

  if (message.type === "status") {
    await note({ frameStatus: message.text, frameStatusTime: Date.now() });
    try { await callBridge("status", { text: message.text }, 5000); } catch (_) {}
    return { ok: true };
  }

  if (message.type === "poll") {
    await note({ lastPoll: Date.now() });
    const data = await callBridge("poll", { wait: 20, state: message.state || {} }, 35000);
    await note({ lastPoll: Date.now(), lastError: "" });
    return data;
  }

  if (message.type === "start") {
    return callBridge("start", { id: message.id }, 15000);
  }

  if (message.type === "chunk") {
    return callBridge("chunk", message, 10000);
  }

  if (message.type === "done" || message.type === "result") {
    const payload = message.result || message;
    const data = await callBridge("done", payload, 15000);
    const { done = 0 } = await chrome.storage.local.get("done");
    await note({ done: done + 1, lastJob: Date.now(), lastJobError: payload.error || "" });
    return data;
  }

  if (message.type === "error") {
    return callBridge("error", message, 15000);
  }

  return { error: `unknown message ${message.type}` };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handle(message, sender).then(sendResponse, (error) => {
    const text = String((error && error.message) || error);
    note({ lastError: text, lastErrorTime: Date.now() });
    sendResponse({ error: text });
  });
  return true;
});

async function watchdog() {
  const tabs = await chrome.tabs.query({ url: "https://perchance.org/*" });
  const agentTab = tabs.find((t) => t.url && t.url.includes("minimal#edit")) || tabs[0];
  const { lastPoll = 0, lastTabAction = 0, frameStatusTime = 0 } =
    await chrome.storage.local.get(["lastPoll", "lastTabAction", "frameStatusTime"]);

  if (!agentTab) {
    await chrome.tabs.create({ url: TARGET_PAGE, pinned: true, active: false });
    await note({ lastTabAction: Date.now() });
  } else if (agentTab.discarded || Date.now() - Math.max(lastPoll, lastTabAction, frameStatusTime) > STALE_MS) {
    await chrome.tabs.reload(agentTab.id);
    await note({ lastTabAction: Date.now() });
  }
}

chrome.alarms.get("watchdog").then((alarm) => {
  if (!alarm) chrome.alarms.create("watchdog", { periodInMinutes: 1 });
});
chrome.alarms.onAlarm.addListener((alarm) => (alarm.name === "watchdog" ? watchdog() : undefined));
chrome.runtime.onStartup.addListener(watchdog);
chrome.runtime.onInstalled.addListener(watchdog);
