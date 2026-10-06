// Perchance Bridge, background part: talks to bridge.php on your server and keeps a Perchance tab
// open, in which content.js runs the actual generations.
importScripts("config.js");   // BRIDGE_URL and BRIDGE_KEY

const BRIDGE_PAGE = "https://perchance.org/ai-code-generator";
const STALE_MS = 3 * 60 * 1000;   // no poll from the tab for this long: reload it

async function callBridge(action, body, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${BRIDGE_URL}?action=${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Key": BRIDGE_KEY },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

// Status for the popup.
const note = (fields) => chrome.storage.local.set(fields);

async function handle(message, sender) {
  const url = (sender.tab && sender.tab.url) || "";
  if (message.type === "hello") return { bridge: url.startsWith(BRIDGE_PAGE) };
  if (!url.startsWith(BRIDGE_PAGE)) return { error: "not the bridge tab" };
  if (message.type === "poll") {
    await note({ lastPoll: Date.now() });
    const data = await callBridge("poll", { wait: 20, state: message.state || {} }, 35000);
    await note({ lastPoll: Date.now(), lastError: "" });
    return data;
  }
  if (message.type === "start") return callBridge("start", { id: message.id }, 15000);
  if (message.type === "result") {
    const data = await callBridge("result", message.result, 15000);
    const { done = 0 } = await chrome.storage.local.get("done");
    await note({ done: done + 1, lastJob: Date.now(), lastJobError: message.result.error || "" });
    return data;
  }
  return { error: `unknown message ${message.type}` };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handle(message, sender).then(sendResponse, (error) => {
    const text = String((error && error.message) || error);
    note({ lastError: text, lastErrorTime: Date.now() });
    sendResponse({ error: text });
  });
  return true;   // the answer comes asynchronously
});

// Keeps the Perchance tab around: opens it (pinned, in the background) if it's missing, and reloads it
// if Chrome discarded or froze it, or if it stopped asking for work.
async function watchdog() {
  const tabs = await chrome.tabs.query({ url: BRIDGE_PAGE + "*" });
  const { lastPoll = 0, lastTabAction = 0 } = await chrome.storage.local.get(["lastPoll", "lastTabAction"]);
  if (tabs.length === 0) {
    await chrome.tabs.create({ url: BRIDGE_PAGE, pinned: true, active: false });
    await note({ lastTabAction: Date.now() });
  } else if (tabs[0].discarded || Date.now() - Math.max(lastPoll, lastTabAction) > STALE_MS) {
    await chrome.tabs.reload(tabs[0].id);
    await note({ lastTabAction: Date.now() });
  }
}

// Creating an alarm that exists already would restart its countdown every time the worker wakes up.
chrome.alarms.get("watchdog").then((alarm) => {
  if (!alarm) chrome.alarms.create("watchdog", { periodInMinutes: 1 });
});
chrome.alarms.onAlarm.addListener((alarm) => (alarm.name === "watchdog" ? watchdog() : undefined));
chrome.runtime.onStartup.addListener(watchdog);
chrome.runtime.onInstalled.addListener(watchdog);
