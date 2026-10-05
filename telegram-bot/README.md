# Perchance Telegram bot (PHP)

A Telegram chat bot, plus a small JSON chat API, that answers with Perchance's free AI. It runs on ordinary PHP hosting such as Hostinger.

```
Telegram ──webhook──▶ webhook.php ──▶ Perchance text generator
                          │           (history per chat in data/)
                          └──▶ reply via the Telegram Bot API
```

## Files

| File | What it does |
|---|---|
| `webhook.php` | Telegram sends every message here. It only accepts requests carrying `TELEGRAM_WEBHOOK_SECRET`. |
| `api.php` | JSON chat API, protected by `API_KEY` |
| `setup.php` | Checks the server and connects the bot to Telegram. It needs `?secret=<TELEGRAM_WEBHOOK_SECRET>`. |
| `lib/` | Shared code. It is a port of `perchance.py`. |
| `data/` | Conversations, the bot owner and a log (`bot.log`). It is created automatically. |
| `.env` | Settings and secrets |
| `.htaccess` | Blocks web access to `.env`, `lib/` and `data/` |

## Deploy on Hostinger

1. **Bot token.** Put the token from @BotFather in `.env` on the `TELEGRAM_BOT_TOKEN=` line.
2. **PHP version.** In hPanel, choose PHP 8.x (Websites > your site > Advanced > PHP Configuration).
3. **Upload.** In File Manager, open `public_html`, upload `telegram-bot.zip` and extract it. You get `public_html/telegram-bot/`, which you can rename if you like. Check that the hidden files `.env` and `.htaccess` are there.
4. **Check the server.** Open `https://YOUR-DOMAIN/telegram-bot/setup.php?secret=TELEGRAM_WEBHOOK_SECRET`, using the value from `.env`. Every line should say `[ OK ]`. The most important line is "Perchance text generation works from this server" (see [Limits](#limits)).
5. **Connect the webhook.** Open the same address with `&action=webhook` added. This registers `webhook.php` with Telegram.
6. **Claim the bot.** Message your bot. The first person who writes to it becomes its owner, and everyone else is refused.

## Using the bot

Just write to it. It remembers the conversation until you send `/new`.

| Command | What it does |
|---|---|
| `/new` | Start a new conversation |
| `/image <description>` | Make a picture. Start with `wide` or `tall` for 768×512 or 512×768. |
| `/id` | Show your Telegram user ID and the chat ID |
| `/help` | List the commands |

In groups, the bot only answers `/ask <question>` and replies to its own messages.

To let other people use the bot, add their user IDs (they can get them with `/id`) or @usernames to `TELEGRAM_ALLOWED_USERS`, comma-separated. Use `*` to let everyone use it.

## Chat API

```bash
curl -X POST https://YOUR-DOMAIN/telegram-bot/api.php \
  -H "X-API-Key: API_KEY_FROM_ENV" -H "Content-Type: application/json" \
  -d '{"message": "Hi! What can you do?"}'
# {"reply": "...", "conversation_id": "3f9c..."}
```

To continue the same conversation, send `"conversation_id"` back with the next message. Add `"reset": true` to start that conversation over.

## Settings (`.env`)

| Setting | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot's token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Random secret. Telegram sends it with every update, and `setup.php` asks for it. |
| `TELEGRAM_ALLOWED_USERS` | User IDs or @usernames, comma-separated, or `*`. If empty, the first user to message the bot becomes its owner. |
| `API_KEY` | Key for `api.php`. If empty, the API is off. |
| `BOT_SYSTEM_PROMPT` | How the bot should behave |
| `PERCHANCE_*` | The Perchance session from your browser: the same values as the main project's `.env` |
| `PERCHANCE_MAX_CONTINUES` | Extra requests for replies longer than Perchance's 1,024-token limit. Default 3. |
| `HISTORY_MAX_CHARS` | How much of the conversation is sent with each message. Default 12000. |
| `PERCHANCE_IMAGE_ALLOW_NSFW` | `true` sends images Perchance flags as possibly NSFW. The site hides these by default, and so does the bot. |

## When something goes wrong

- **The bot doesn't answer.** Open `setup.php?secret=...`. At the bottom it shows Telegram's last delivery error for the webhook. `data/bot.log` (open it in File Manager) lists errors.
- **"Cloudflare blocked the request" or "Unexpected reply from Perchance".** The Perchance values in `.env` have expired, or Perchance doesn't accept them from this server. Copy fresh ones from your browser.
- **"Perchance stayed busy".** Perchance runs one request per key at a time. If the same key is also used elsewhere (for example by the local proxy for Cline), requests wait for each other.

## Limits

- **The Perchance session may not work from the server.** It comes from your browser: `cf_clearance` is tied to the IP address and browser it was issued to, and the `userKey` may be too. The "Perchance text generation works from this server" check in `setup.php` tells you whether this server is accepted.
- **Replies are capped.** Each Perchance request returns at most 1,024 tokens, and the bot continues a long reply up to 3 times.
- **Perchance is a free, ad-funded site.** Heavy automated use can get the key blocked.
