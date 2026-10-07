# Perchance Telegram Bridge (Chrome extension)

The extension lets the Telegram bot on your server (`telegram-bot/`) use Perchance through your own Chrome. It is separate from the Cline extension (`perchance-bridge-extension/`), and both can be installed at the same time. Perchance only accepts a key from the browser and IP address that verified it, so the server itself can't use it; requests from a second IP even get the key cancelled.

```
Telegram ─▶ webhook.php on your server ─▶ job waits in data/bridge/
                                               ▲          │
        Chrome + this extension ───asks for work┘          ▼
        (runs the job in a Perchance tab) ───────▶ result ─▶ reply on Telegram
```

The extension keeps a pinned tab with https://perchance.org/ai-code-generator open in the background. It runs each job through that page's own text generator, the same way Perchance's ai-text-plugin does. So Perchance handles its key and verification itself, and you never copy a key again.

It keeps working while the tab is in the background:
- **No throttling.** The tab's timers use a Web Worker, so Chrome's background-tab slowdown doesn't affect them.
- **Self-healing tab.** Every minute, the extension reopens the tab if you closed it. It reloads the tab if Chrome discarded it or it stopped asking for work.

## Install

1. In Chrome, open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and choose this folder (`perchance-telegram-extension`).
3. A pinned Perchance tab opens within a minute. Leave it there; it can stay in the background.
4. Stop Chrome from unloading the tab. Go to Settings → Performance → Memory Saver → **Always keep these sites active** → Add `perchance.org`.

Click the extension's icon to see whether it's online and how many replies it has made.

The bot only answers while Chrome is running on this computer. When Chrome is closed, the bot replies that the Perchance Bridge is offline.

## Settings

`config.js` holds the server address (`BRIDGE_URL`) and `BRIDGE_KEY`, which must match `BRIDGE_KEY` in the bot's `.env`. If your server is on a different domain, also change it in `host_permissions` in `manifest.json`, then click the reload button on `chrome://extensions`. `config.js` is private; `config.example.js` is the template.
