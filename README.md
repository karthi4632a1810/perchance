# Unlimited: Perchance's free model as an OpenAI-compatible agent API

Uses the free model behind https://perchance.org/ai-code-generator through your own browser session. It works with Cline, Roo Code, Continue, or any OpenAI client.

It can also generate images through https://perchance.org/text-to-image-plugin (see [Image generation](#image-generation)).

For a Telegram chat bot that runs on ordinary PHP hosting such as Hostinger, see [telegram-bot/](telegram-bot/README.md). It reaches Perchance through your own Chrome with the [Perchance Bridge extension](perchance-bridge-extension/README.md), because Perchance only accepts a key from the browser that verified it.

```
Cline ──/v1/chat/completions + tools──▶ server.py :8010 ──one prompt──▶ text-generation.perchance.org
  ▲   runs the tool calls on your machine     │  parses ```tool_call blocks from the reply
  └──────────── real OpenAI tool_calls ◀──────┘
```

Perchance has no native tool calling. `agent.py` (ported from `GPT-6-astra/proxy.py`) describes Cline's tools in the prompt and turns the model's ` ```tool_call ` blocks back into real `tool_calls`.

How the proxy deals with Perchance's quirks:

- **Raw file blocks.** New files can come as raw ` ```editor /abs/path ` blocks, so the model never has to escape a whole HTML file into JSON. A block is only used once its closing fence has arrived, so a cut-off reply never writes half a file.
- **Big files.** Cline CLI's `editor` rejects more than 6,000 characters per call. A bigger new file is split into a create call plus appends, which Cline runs in order. Each append replaces the file's unique last lines with those lines plus the next piece, so if the file already existed, nothing matches and it stays untouched.
- **Replies longer than 1,024 tokens.** Perchance ends every reply after at most 1,024 tokens. The proxy continues it the way the site's "continue" button does, until the reply is done or Perchance's context (about 52,000 characters of prompt plus reply) is nearly full.
- **Broken tool calls.** Small JSON slips are repaired: missing closing brackets and a stray `\n` between tokens. If some blocks still can't be parsed, the valid ones run and the model is told which ones to resend. Only when nothing in a reply is usable is the model asked to redo it, because a resend takes Perchance minutes.
- **Smaller prompts.** Files the model wrote earlier are shortened in the history it sees, which keeps prompts small and fast.

## Setup

1. `cp .env.example .env` and fill it in from Chrome DevTools on the Perchance page: Network tab, then the `generate?...` request.
   - `PERCHANCE_USER_KEY` and `PERCHANCE_BROWSER_ID`: Payload > Query string parameters
   - `PERCHANCE_CF_CLEARANCE`: Cookies tab
   - `PERCHANCE_USER_AGENT`: must match the Chrome that got the cookie
2. Test: `python3 perchance.py "Write a Python function that reverses a string"`
3. Run: `python3 server.py` (options: `--host`, `--port`, default `127.0.0.1:8010`)

## Use in Cline

In Cline's settings (gear icon):

| Setting | Value |
|---|---|
| API Provider | `OpenAI Compatible` |
| Base URL | `http://127.0.0.1:8010/v1` |
| API Key | any string, e.g. `sk-local` |
| Model ID | `perchance` |

Turn on native tool calling if your Cline version asks. Keep `python3 server.py` running while you use Cline. Its terminal shows each step, for example `[<] Sent 2 tool call(s): run_commands, read_files`.

This works with both the VS Code extension and the `cline` CLI.

Most steps take about 10–15 seconds. Writing a big file takes a few minutes, because Perchance hands it over in pieces and the log shows `[~] Perchance cut the reply at N chars; continuing (n/24)`. For example, a 12k-character portfolio page took about 3 minutes over 8 requests. While the model writes, its text streams into Cline as "thinking".

## Other clients

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8010/v1", api_key="x")
print(client.chat.completions.create(model="perchance", messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)
```

Perchance runs one request per key at a time. If a second one arrives, for example from another Cline task, the proxy waits for the first to finish. The log then shows `waiting_for_prev_request_to_finish`.

## Image generation

The image generator has its own key. On https://perchance.org/text-to-image-plugin, generate one image with DevTools open. Then copy two values from the `generate?...` request (Payload) into `.env`:
- `userKey` into `PERCHANCE_IMAGE_USER_KEY`
- `adAccessCode` into `PERCHANCE_AD_ACCESS_CODE`

`PERCHANCE_CF_CLEARANCE` is shared with the text side.

The `adAccessCode` is how Perchance ties free image generation to the ads on its page. When it stops being accepted, you get an error saying so. Generate an image on the site again and copy the new code.

From the command line:

```bash
python3 perchance.py --image "a lighthouse on a rocky coast at sunset" --size 768x512
```

Through the proxy, from any OpenAI client:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8010/v1", api_key="x")
image = client.images.generate(model="perchance-image", prompt="a red apple on a wooden table", size="1024x1024")
print(image.data[0].url)   # http://127.0.0.1:8010/images/<file>.jpeg
```

Request options:
- **`size`:** square sizes become 512×512, portrait 512×768, and landscape 768×512.
- **`n`:** up to 4 images, generated one after another.
- **`response_format`:** `"url"` (default) or `"b64_json"`.
- **Perchance's own options:** `negative_prompt`, `seed` and `guidance_scale` (default 7).

Every image is also saved in `images/`. One image takes about 3–7 seconds.

Perchance flags some images as possibly not safe for work, and the site hides those by default. The proxy does the same: it returns an error instead of the image. Setting `PERCHANCE_IMAGE_ALLOW_NSFW=true` in `.env` turns this off, like changing the site's safety setting.

## When it stops working

Cline shows an error with "re-verified" or "Unexpected reply". This means the userKey or `cf_clearance` has expired.

To fix it:
1. Open the Perchance page in Chrome and generate once.
2. Copy the new values into `.env`. The server re-reads `.env` on every request, so you don't need to restart it.
3. Press Retry in Cline.

If the model does something odd, check `last_prompt.txt` and `last_reply.txt` to see exactly what it got and wrote.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PORT` / `HOST` | `8010` / `127.0.0.1` | Where the proxy listens |
| `PERCHANCE_MAX_CONTINUES` (also in `.env`) | `24` | Extra requests allowed to finish one long reply |
| `PERCHANCE_IMAGE_ALLOW_NSFW` (also in `.env`) | `false` | Return images Perchance flags as maybe NSFW (the site hides them by default) |
| `PERCHANCE_MAX_INPUT_CHARS` (also in `.env`) | `46000` | Prompt plus reply so far allowed in one request. Perchance returned `"error": true` at about 52,500. |
| `MAX_PROMPT_CHARS` | `34000` | Max prompt size. Older tool output is shortened to fit, which leaves room for the reply. |
| `MAX_TOOL_RESULT_CHARS` | `12000` | Max characters kept per tool result |
| `WRITE_CHUNK_CHARS` | `5000` | Size of each piece when a big new file is split (Cline's `editor` limit is 6000) |
| `HISTORY_FILE_CHARS` | `1500` | How much of each file the model wrote earlier it sees again |
| `MAX_REPAIR_ROUNDS` | `2` | Attempts to get a reply fixed when none of its tool calls can be parsed |
| `TOOL_EXCLUDE` | `^(team_\|spawn_agent$)` | Regex of tools hidden from the model. Cline's team and sub-agent tools are hidden to keep prompts small. |
| `STREAM_PROGRESS` | `true` | Stream the model's live text to Cline as thinking |
| `DEBUG_DUMPS` | `true` | Write `last_request.json`, `last_prompt.txt` and `last_reply.txt` |

## Limits

- **No image input.** The text model can't see images; it only gets "[image attached]". Image generation is separate (see above).
- **Small context.** Perchance holds only about 52,000 characters (roughly 15k tokens), and the conversation is resent on every step. In long tasks, old tool output gets shortened and the oldest messages are dropped.
- **The session expires.** `cf_clearance` is tied to your IP and User-Agent.
- **Perchance is a free, ad-funded site.** Heavy automated use can get the key blocked.
# perchance
