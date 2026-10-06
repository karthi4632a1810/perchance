# knowledge/

Notes the bot uses to answer questions about you. Put Markdown (`.md`) or text (`.txt`) files here, for example `PERSONAL.md`. All files except this README are added to the bot's instructions, up to `KNOWLEDGE_MAX_CHARS` characters in total (default 15000).

Where your notes are used:
- **Telegram:** only in private chats with the bot's owner. The owner is the first user in `TELEGRAM_ALLOWED_USERS`, or whoever claimed the bot. Other users and group chats never get them.
- **`api.php`:** only for requests that include `"knowledge": true`.

The bot is told to answer from these notes and to say it doesn't know when something isn't in them.

Keep this folder private. `.htaccess` blocks web access to it, and git ignores it so your notes don't end up on GitHub. With every message that uses your notes, they are sent to Perchance as part of the prompt.
