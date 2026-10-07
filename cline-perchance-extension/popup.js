const AGENT_PAGE = "https://perchance.org/minimal#edit";
const ago = (time) => (time ? `${Math.round((Date.now() - time) / 1000)} s ago` : "never");

chrome.storage.local.get(["lastPoll", "done", "lastJob", "lastJobError", "lastError", "lastErrorTime", "frameStatus", "frameStatusTime"]).then((s) => {
  const online = s.lastPoll && Date.now() - s.lastPoll < 60000;
  document.getElementById("status").textContent = online
    ? "🟢 Online: Ready for Cline requests"
    : "🔴 Offline: Open minimal#edit tab in Chrome";
  if (s.frameStatus) document.getElementById("tab").textContent = `Status (${ago(s.frameStatusTime)}): ${s.frameStatus}`;
  document.getElementById("jobs").textContent = `Replies made: ${s.done || 0} (last ${ago(s.lastJob)})`
    + (s.lastJobError ? `. Last one failed: ${s.lastJobError}` : "");
  if (s.lastError && Date.now() - (s.lastErrorTime || 0) < 10 * 60000) {
    document.getElementById("error").textContent = `Problem ${ago(s.lastErrorTime)}: ${s.lastError}`;
  }
});

document.getElementById("open").onclick = async () => {
  const [tab] = await chrome.tabs.query({ url: "https://perchance.org/minimal#edit*" });
  if (tab) chrome.tabs.update(tab.id, { active: true });
  else chrome.tabs.create({ url: AGENT_PAGE, pinned: true });
};
