// Perchance Bridge, page part: runs in the Perchance generator page, asks the extension for work and
// runs it with the page's own text-generation frame, the same way Perchance's ai-text-plugin does
// (startStream / streamKeepAlive messages). That frame handles the key and verification itself.
(() => {
  const EMBED_ORIGIN = "https://text-generation.perchance.org";
  const KEEPALIVE_MS = 2000;      // the frame drops a request after 10 s without a keep-alive
  const JOB_TIMEOUT_MS = 170000;

  // Timers that keep their pace in background tabs, where Chrome slows normal timers to once a
  // minute; Perchance's own hiddenSafeWait uses the same Web Worker trick.
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
    } catch (e) {
      worker = null;   // fall back to normal timers
    }
    return (ms) => new Promise((resolve) => {
      if (!worker) return setTimeout(resolve, ms);
      const id = ++seq;
      waiting[id] = resolve;
      worker.postMessage({ id, ms });
    });
  })();

  const send = (message) => chrome.runtime.sendMessage(message);
  const findEmbed = () => document.querySelector(`iframe[src^="${EMBED_ORIGIN}/"]`);

  async function runJob(job, embedFrame) {
    const started = await send({ type: "start", id: job.id });
    if (!started || !started.ok) return;   // another tab took it, or it expired
    const requestId = `bridge-${job.id}-${Math.random().toString(36).slice(2)}`;
    const target = embedFrame.contentWindow;
    let text = "";
    let stopReason = null;
    let error = null;
    let over = false;
    let finish;
    const finished = new Promise((resolve) => { finish = resolve; });
    const onMessage = (e) => {
      if (e.origin !== EMBED_ORIGIN || !e.data || e.data.requestId !== requestId) return;
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
    // Only the generator's own frame contains the text-generation frame; give the page time to make it.
    let embed = null;
    for (let i = 0; i < 120 && !(embed = findEmbed()); i++) await wait(500);
    if (!embed) return;
    while (true) {
      try {
        // Waits until the server has a job (up to ~20 s), so this loop needs no timers.
        const response = await send({ type: "poll", state: { page: location.href } });
        if (response && response.job) await runJob(response.job, findEmbed() || embed);
        else if (!response || response.error) await wait(5000);
      } catch (e) {
        if (!chrome.runtime || !chrome.runtime.id) return;   // the extension was reloaded: this copy is stale
        await wait(5000);
      }
    }
  }

  main();
})();
