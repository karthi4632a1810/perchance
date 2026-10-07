// Perchance Bridge content script:
// Runs on https://perchance.org/minimal#edit to automate the Perchance AI Agent for Cline.
(() => {
  const KEEPALIVE_MS = 2000;
  const JOB_TIMEOUT_MS = 180000; // 3 minutes

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const send = (message) => chrome.runtime.sendMessage(message);
  const status = (text) => send({ type: "status", text }).catch(() => {});

  function isAgentPage() {
    return (
      location.hostname === "perchance.org" &&
      (location.hash.startsWith("#edit") || location.pathname.includes("minimal"))
    );
  }

  async function waitForAgentUI(timeoutMs = 60000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const input = document.querySelector("#aiAgentInputEl");
      const sendBtn = document.querySelector("#aiAgentSendBtn");
      const msgs = document.querySelector("#aiAgentMsgsEl");
      if (input && sendBtn && msgs) {
        return { input, sendBtn, msgs };
      }
      // If we are on minimal without #edit, add #edit
      if (location.hostname === "perchance.org" && !location.hash.startsWith("#edit")) {
        location.hash = "#edit";
      }
      await wait(500);
    }
    return null;
  }

  function cleanWelcomeText(str) {
    if (!str) return "";
    return str
      .replace(/^👋\s*I can edit the code and test it live\.[^\n]*\n*/i, "")
      .trim();
  }

  async function runAgentJob(job, ui) {
    const started = await send({ type: "start", id: job.id });
    if (!started || !started.ok) return;

    const { input, sendBtn, msgs } = ui;

    // Start fresh chat if requested
    if (job.new_chat) {
      const newChatBtn = document.querySelector("#aiAgentNewChatBtn");
      if (newChatBtn) {
        newChatBtn.click();
        await wait(600);
      }
    }

    const initialAssistantCount = msgs.querySelectorAll(".aa-md").length;
    const initialFoldsCount = msgs.querySelectorAll(".aa-fold").length;

    // Fill in the input
    input.value = job.prompt;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await wait(200);

    // Send
    sendBtn.click();

    // Wait for generation to start
    let generating = false;
    const startDeadline = Date.now() + 15000;
    while (Date.now() < startDeadline) {
      const stopBtn = document.querySelector("#aiAgentStopBtn");
      if (stopBtn && !stopBtn.hidden && stopBtn.offsetParent !== null) {
        generating = true;
        break;
      }
      if (msgs.querySelectorAll(".aa-md").length > initialAssistantCount) {
        generating = true;
        break;
      }
      await wait(100);
    }

    let lastContent = "";
    let lastReasoning = "";
    let text = "";
    const jobStart = Date.now();

    while (Date.now() - jobStart < JOB_TIMEOUT_MS) {
      await wait(150);

      const stopBtn = document.querySelector("#aiAgentStopBtn");
      const isStillGenerating = stopBtn && !stopBtn.hidden && stopBtn.offsetParent !== null;

      // Extract current assistant content:
      // CRITICAL: Only inspect assistant elements created strictly AFTER this job started!
      const assistantEls = Array.from(msgs.querySelectorAll(".aa-md"));
      let curContent = "";
      if (assistantEls.length > initialAssistantCount) {
        const curAssistant = assistantEls[assistantEls.length - 1];
        const rawText = (curAssistant._raw || curAssistant.innerText || curAssistant.textContent || "").trim();
        curContent = cleanWelcomeText(rawText);
      }

      // Extract reasoning content (.aa-fold)
      const foldEls = Array.from(msgs.querySelectorAll(".aa-fold"));
      let curReasoning = "";
      if (foldEls.length > initialFoldsCount) {
        const latestFold = foldEls[foldEls.length - 1];
        curReasoning = (latestFold._raw || latestFold.innerText || latestFold.textContent || "").trim();
      }

      // Stream content delta
      if (curContent.length > lastContent.length) {
        const delta = curContent.slice(lastContent.length);
        lastContent = curContent;
        await send({
          type: "chunk",
          id: job.id,
          delta: { content: delta }
        }).catch(() => {});
      }

      // Stream reasoning delta
      if (curReasoning.length > lastReasoning.length) {
        const delta = curReasoning.slice(lastReasoning.length);
        lastReasoning = curReasoning;
        await send({
          type: "chunk",
          id: job.id,
          delta: { reasoning: delta }
        }).catch(() => {});
      }

      // Detect end of generation
      if (generating && !isStillGenerating) {
        await wait(500); // Wait for final DOM settlement
        break;
      }
    }

    // Final extract
    const assistantEls = Array.from(msgs.querySelectorAll(".aa-md"));
    if (assistantEls.length > initialAssistantCount) {
      const curAssistant = assistantEls[assistantEls.length - 1];
      const rawText = (curAssistant._raw || curAssistant.innerText || curAssistant.textContent || "").trim();
      text = cleanWelcomeText(rawText);
    } else {
      text = lastContent;
    }

    await send({
      type: "done",
      result: {
        id: job.id,
        text: text || "(Empty reply from agent)"
      }
    });
  }

  async function main() {
    const hello = await send({ type: "hello" }).catch(() => null);
    if (!hello || !hello.bridge) return;

    if (isAgentPage()) {
      status("Initializing Perchance AI Agent interface...");
      const ui = await waitForAgentUI();
      if (!ui) {
        status("Could not locate AI Agent elements; reload the page.");
        return;
      }
      status("🟢 Online: Perchance AI Agent ready for Cline");

      while (true) {
        try {
          const response = await send({ type: "poll", state: { page: location.href, mode: "agent" } });
          if (response && response.job) {
            status(`Working on request ${response.job.id}...`);
            await runAgentJob(response.job, ui);
            status("🟢 Ready: waiting for messages");
          } else if (!response || response.error) {
            await wait(3000);
          }
        } catch (e) {
          if (!chrome.runtime || !chrome.runtime.id) return;
          await wait(5000);
        }
      }
    }
  }

  main();
})();
