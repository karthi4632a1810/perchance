// Perchance Telegram Bridge, page part: runs in the Perchance generator's frame, adds Perchance's
// text-generation frame the same way the ai-text-plugin does, asks the extension for work and runs it
// there (startStream / streamKeepAlive messages). That frame handles the key and verification itself.
(() => {
  const EMBED_ORIGIN = "https://text-generation.perchance.org";
  const GENERATOR_PATH = "/ai-code-generator";
  const KEEPALIVE_MS = 2000;      // the frame drops a request after 10 s without a keep-alive
  const JOB_TIMEOUT_MS = 170000;

  // The text-generation frame only takes requests from a *.perchance.org parent, which is the
  // generator's own frame (perchance.org itself is the outer page).
  if (window === window.top || location.hostname === "perchance.org" || !location.pathname.startsWith(GENERATOR_PATH)) return;

  // Timers that keep their pace in background tabs, where Chrome slows normal timers to once a
  // minute; Perchance's own hiddenSafeWait uses the same Web Worker trick. A normal timer runs
  // alongside, in case the page doesn't allow the worker.
  const wait = (() => {
    let worker = null;
    let seq = 0;
    const waiting = {};
    try {
      const source = "onmessage = (e) => setTimeout(() => postMessage(e.data.id), e.data.ms);";
      worker = new Worker(URL.createObjectURL(new Blob([source], { type: "text/javascript" })));
      worker.onmessage = (e) => {
        const resolve = waiting[e.data];
        delete waiting[e.data];
        if (resolve) resolve();
      };
      worker.onerror = () => { worker = null; };
    } catch (e) {
      worker = null;
    }
    return (ms) => new Promise((resolve) => {
      if (!worker) return setTimeout(resolve, ms);
      setTimeout(resolve, ms + 1000);   // in case the worker stops answering
      const id = ++seq;
      waiting[id] = resolve;
      worker.postMessage({ id, ms });
    });
  })();

  const send = (message) => chrome.runtime.sendMessage(message);
  const status = (text) => send({ type: "status", text }).catch(() => {});

  /** Adds the text-generation frame like the ai-text-plugin does; resolves once it says it's ready. */
  function addEmbed() {
    const frame = document.createElement("iframe");
    frame.id = "perchanceBridgeEmbedIframe";
    frame.src = `${EMBED_ORIGIN}/embed`;
    frame.style.cssText = "display:none; position:fixed; top:0.5rem; right:0.5rem; height:3rem; width:11rem; "
      + "background:#333; border:none; border-radius:3px; z-index:10000";
    let ready = false;
    const isReady = new Promise((resolve) => {
      window.addEventListener("message", (e) => {
        if (e.source !== frame.contentWindow || e.origin !== EMBED_ORIGIN || !e.data) return;
        if (e.data.type === "embedIsReady" && !ready) {
          ready = true;
          frame.contentWindow.postMessage({ type: "verifyUser" }, EMBED_ORIGIN);
          resolve(frame);
        } else if (e.data.type === "verifying") {
          frame.style.display = "";   // in case Perchance's human check wants a click
          status("Perchance is checking that you're human. If it doesn't finish, open the Perchance tab.");
        } else if (e.data.type === "verified") {
          frame.style.display = "none";
          status("Ready: waiting for messages.");
        }
      });
    });
    document.body.appendChild(frame);
    setTimeout(() => {   // the plugin retries a slow frame the same way
      if (!ready) frame.src = `${EMBED_ORIGIN}/embed?__cacheBust=${Math.random()}`;
    }, 15000);
    return isReady;
  }

  async function runJob(job, frame) {
    const started = await send({ type: "start", id: job.id });
    if (!started || !started.ok) return;   // another tab took it, or it expired
    const requestId = `bridge-${job.id}-${Math.random().toString(36).slice(2)}`;
    const target = frame.contentWindow;
    let text = "";
    let stopReason = null;
    let error = null;
    let over = false;
    let finish;
    const finished = new Promise((resolve) => { finish = resolve; });
    const onMessage = (e) => {
      if (e.source !== target || e.origin !== EMBED_ORIGIN || !e.data || e.data.requestId !== requestId) return;
      if (e.data.type === "streamData") {
        const value = e.data.value || {};
        if (typeof value.text === "string") text += value.text;
        if (value.stopReason) stopReason = value.stopReason;
      } else if (e.data.type === "streamEnd" || e.data.type === "streamError") {
        if (e.data.type === "streamError") error = e.data.status || "stream error";
        over = true;
        finish();
      }
    };
    window.addEventListener("message", onMessage);
    target.postMessage({
      type: "startStream",
      requestId,
      postData: {
        instruction: job.instruction,
        startWith: job.startWith || "",
        stopSequences: job.stopSequences || [],
        generatorName: job.generatorName || "ai-code-generator",
      },
    }, EMBED_ORIGIN);
    const startedAt = Date.now();
    while (!over) {
      await Promise.race([finished, wait(KEEPALIVE_MS)]);
      if (over) break;
      target.postMessage({ type: "streamKeepAlive", requestId }, EMBED_ORIGIN);
      if (Date.now() - startedAt > JOB_TIMEOUT_MS) {
        error = `no answer from Perchance after ${JOB_TIMEOUT_MS / 1000} seconds`;
        target.postMessage({ type: "stopStream", requestId }, EMBED_ORIGIN);
        break;
      }
    }
    window.removeEventListener("message", onMessage);
    await send({ type: "result", result: error ? { id: job.id, error, text } : { id: job.id, text, stop_reason: stopReason } });
  }

  async function main() {
    const hello = await send({ type: "hello" }).catch(() => null);
    if (!hello || !hello.bridge) return;   // not the bridge tab
    status("Starting Perchance's text generator...");
    const frame = await Promise.race([addEmbed(), wait(45000).then(() => null)]);
    if (!frame) {
      status("Perchance's text generator didn't start; reloading the tab.");
      location.reload();
      return;
    }
    status("Ready: waiting for messages.");
    while (true) {
      try {
        // Waits until the server has a job (up to ~20 s), so this loop needs no timers.
        const response = await send({ type: "poll", state: { page: location.href } });
        if (response && response.job) await runJob(response.job, frame);
        else if (!response || response.error) {
          if (response && response.error) status(`Problem talking to your server: ${response.error}`);
          await wait(5000);
        }
      } catch (e) {
        if (!chrome.runtime || !chrome.runtime.id) return;   // the extension was reloaded: this copy is stale
        await wait(5000);
      }
    }
  }

  main();
})();
