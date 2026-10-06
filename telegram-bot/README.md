# Perchance Telegram bot (PHP)

A Telegram chat bot, plus a small JSON chat API, that answers with Perchance's free AI. It runs on ordinary PHP hosting such as Hostinger.

```
Telegram ──webhook──▶ webhook.php ──▶ job in data/bridge/ ◀──asks for work── Perchance Bridge (Chrome extension)
                          │                  (history per chat in data/)        runs the job in your browser
                          └──▶ reply via the Telegram Bot API ◀────────── result ──┘
```

## How it reaches Perchance

Perchance only accepts a key from the browser and IP address that verified it. A key copied to a server stops working, and using it from a second IP address gets it cancelled altogether. So this bot has two modes, set with `PERCHANCE_VIA` in `.env`:

- **`bridge` (use this on Hostinger).** The **Perchance Bridge** Chrome extension (`../perchance-bridge-extension/`) does the Perchance requests in your own Chrome, and the server only passes messages. It needs no keys and never needs refreshing, but the bot only answers while Chrome is running on your computer. Images (`/image`) aren't available in this mode yet.
- **`direct`.** The server calls Perchance itself with the `PERCHANCE_*` values in `.env`. This only works on the computer whose browser those values came from, for example when running `php poll.php` there.

## Files

| File | What it does |
|---|---|
| `webhook.php` | Telegram sends every message here. It only accepts requests carrying `TELEGRAM_WEBHOOK_SECRET`. |
| `bridge.php` | Where the Chrome extension asks for work and returns results. It needs `BRIDGE_KEY`. |
| `api.php` | JSON chat API, protected by `API_KEY` |
| `setup.php` | Checks the server and connects the bot to Telegram. It needs `?secret=<TELEGRAM_WEBHOOK_SECRET>`. |
| `poll.php` | Runs the bot from a command line without a webhook (`php poll.php`) |
| `lib/` | Shared code. It is a port of `perchance.py`, plus the bridge queue. |
| `knowledge/` | Your notes about yourself, which the bot uses in your private chat (see below) |
| `data/` | Conversations, bridge jobs, the bot owner and a log (`bot.log`). It is created automatically. |
| `.env` | Settings and secrets |
| `.htaccess` | Blocks web access to `.env`, `lib/` and `data/` |

## Deploy on Hostinger

1. **Settings.** In `.env`, put your bot token from @BotFather on `TELEGRAM_BOT_TOKEN=`. Keep `PERCHANCE_VIA=bridge`. `BRIDGE_KEY` must match `BRIDGE_KEY` in the extension's `config.js`.
2. **Upload.** In hPanel's File Manager, open `public_html`, upload `telegram-bot.zip` and extract it. Then delete the zip, because it contains your secrets. Check that the hidden files `.env` and `.htaccess` are in `public_html/telegram-bot/`. Use PHP 8.x (Advanced > PHP Configuration).
3. **Install the extension in Chrome.** Follow `perchance-bridge-extension/README.md`. A pinned Perchance tab opens and starts asking your server for work.
4. **Check.** Open `https://YOUR-DOMAIN/telegram-bot/setup.php?secret=TELEGRAM_WEBHOOK_SECRET`, using the value from `.env`. Every line should say `[ OK ]`, including "Perchance Bridge extension is online" and "Perchance text generation works through the Perchance Bridge".
5. **Connect the webhook.** Open the same address with `&action=webhook` added.
6. **Claim the bot.** Message your bot. The first person who writes to it becomes its owner, and everyone else is refused.

## Using the bot

Just write to it. It remembers the conversation until you send `/new`.

| Command | What it does |
|---|---|
| `/new` | Start a new conversation |
| `/image <description>` | Make a picture (`direct` mode only). Start with `wide` or `tall` for 768×512 or 512×768. |
| `/id` | Show your Telegram user ID and the chat ID |
| `/help` | List the commands |

In groups, the bot only answers `/ask <question>` and replies to its own messages.

To let other people use the bot, add their user IDs (they can get them with `/id`) or @usernames to `TELEGRAM_ALLOWED_USERS`, comma-separated. Use `*` to let everyone use it.

## Notes about you (`knowledge/`)

Put Markdown or text files about yourself in `knowledge/`, for example `knowledge/PERSONAL.md`. In your private chat, the bot uses them to answer questions like "what's my backend stack?". It says it doesn't know when something isn't in your notes.

Your notes are only used for you, the owner: the first user in `TELEGRAM_ALLOWED_USERS`, or whoever claimed the bot. Other users and groups never get them. `api.php` only uses them when a request includes `"knowledge": true`. The folder is blocked from the web and kept out of git. Every message that uses your notes sends them to Perchance as part of the prompt.

## Chat API

```bash
curl -X POST https://YOUR-DOMAIN/telegram-bot/api.php \
  -H "X-API-Key: API_KEY_FROM_ENV" -H "Content-Type: application/json" \
  -d '{"message": "Hi! What can you do?"}'
# {"reply": "...", "conversation_id": "3f9c..."}
```

To continue the same conversation, send `"conversation_id"` back with the next message. Add `"reset": true` to start that conversation over, or `"knowledge": true` to include your notes from `knowledge/`.

## Settings (`.env`)

| Setting | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot's token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Random secret. Telegram sends it with every update, and `setup.php` asks for it. |
| `TELEGRAM_ALLOWED_USERS` | User IDs or @usernames, comma-separated, or `*`. If empty, the first user to message the bot becomes its owner. |
| `API_KEY` | Key for `api.php`. If empty, the API is off. |
| `BOT_SYSTEM_PROMPT` | How the bot should behave |
| `PERCHANCE_VIA` | `bridge` (through the Chrome extension) or `direct` (see above) |
| `BRIDGE_KEY` | The extension's key. It must match `config.js` in the extension. |
| `BRIDGE_TIMEOUT` | Seconds to wait for the extension's answer. Default 180. |
| `PERCHANCE_*` | `direct` mode only: the Perchance session from your browser |
| `PERCHANCE_MAX_CONTINUES` | Extra requests for replies longer than Perchance's 1,024-token limit. Default 3. |
| `KNOWLEDGE_MAX_CHARS` | How much of your notes is used. Default 15000. |
| `HISTORY_MAX_CHARS` | How much of the conversation is sent with each message. Default 12000. |
| `PERCHANCE_IMAGE_ALLOW_NSFW` | `true` sends images Perchance flags as possibly NSFW. The site hides these by default, and so does the bot. |

## When something goes wrong

- **"The Perchance Bridge is offline".** Chrome isn't running on your computer, or the Perchance tab stopped. Open Chrome; the extension reopens the tab within a minute. Its icon shows whether it's online.
- **The bot doesn't answer at all.** Open `setup.php?secret=...`. At the bottom it shows Telegram's last delivery error for the webhook. `data/bot.log` (open it in File Manager) lists errors.
- **"Perchance no longer accepts the userKey".** This only happens in `direct` mode. Copy fresh values from the browser on that same computer.

## Limits

- **Bridge mode needs your Chrome.** The bot only answers while Chrome is running with the extension.
- **Replies are capped.** Each Perchance request returns at most 1,024 tokens, and the bot continues a long reply up to 3 times.
- **Perchance is a free, ad-funded site.** Heavy automated use can get your key blocked.
