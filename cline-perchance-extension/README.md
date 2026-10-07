# Perchance AI Agent Bridge (Chrome Extension)

This extension connects **Perchance's new AI Agent** (`https://perchance.org/minimal#edit`) directly to your local proxy (`server.py`) so you can use it in **Cline**, **Roo Code**, **Continue**, or any OpenAI-compatible client.

```
Cline / Roo Code ─▶ http://127.0.0.1:8010/v1 ─▶ server.py (local proxy)
                                                     │   ▲
                  Chrome + this extension ───polls───┘   │ (streams reasoning
                  (runs in https://perchance.org/minimal#edit)   & content deltas)
```

### Why use the Chrome Bridge?
Perchance's AI Agent runs with advanced Cloudflare challenge and session verification (`AS-0017`). Direct standalone requests from scripts get challenged. By running in your browser tab:
- Perchance's Cloudflare verification is 100% native.
- You get full Claude 3.5 Sonnet level agent responses with reasoning/thinking.
- Real-time streaming chunks (`reasoning_content` and `content`) go straight to Cline.

---

## Quick Setup

1. **Start the local proxy** (if not already running):
   ```bash
   python3 -u server.py
   ```
   Proxy listens on `http://127.0.0.1:8010/v1`.

2. **Load the Extension in Chrome**:
   - Open `chrome://extensions` in Google Chrome.
   - Turn on **Developer mode** (top right toggle).
   - Click **Load unpacked** and select the `cline-perchance-extension` folder.
   - If you already loaded it earlier, click the **Reload (↻)** button on the extension card.

3. **Open the Agent tab**:
   - The extension will automatically open and pin `https://perchance.org/minimal#edit`.
   - Click the extension icon in Chrome toolbar: it will show `🟢 Online: Ready for Cline requests`.

4. **Configure Cline**:
   In Cline's settings (or Roo Code):
   - **API Provider**: `OpenAI Compatible`
   - **Base URL**: `http://127.0.0.1:8010/v1`
   - **API Key**: any text (e.g. `perchance`)
   - **Model ID**: `perchance-agent` (or `perchance`)

---

## Status Check
- In your browser: visit `http://127.0.0.1:8010/bridge/status` or `http://127.0.0.1:8010/health`.
- `bridge_online: true` indicates Chrome is connected and ready.
