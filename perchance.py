"""
Client for Perchance's free text generator (text-generation.perchance.org/api/generate).

It reuses the session from your own browser: the userKey from a generate request and the
cf_clearance cookie, both copied from Chrome DevTools into .env. cf_clearance only works
with the same User-Agent (and usually the same IP) it was issued to.

Quick test:  python3 perchance.py "Write a Python function that reverses a string"
"""

import json
import os
import random
import sys

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

DEFAULTS = {
    "PERCHANCE_BASE_URL": "https://text-generation.perchance.org",
    "PERCHANCE_USER_KEY": "",
    "PERCHANCE_CF_CLEARANCE": "",
    "PERCHANCE_USER_AGENT": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "PERCHANCE_BROWSER_ID": "",
    "PERCHANCE_GENERATOR": "ai-code-generator",
    "PERCHANCE_TIMEOUT": "300",
    # Each request returns at most 1024 tokens (often less); longer replies take extra requests.
    "PERCHANCE_MAX_CONTINUES": "24",
    # Perchance fails ("error": true) past roughly 52,000 characters of instruction + reply so far.
    "PERCHANCE_MAX_INPUT_CHARS": "46000",
}

REFRESH_HINT = (
    "Open https://perchance.org/ai-code-generator in Chrome, generate once, then copy the "
    "userKey (Network > generate request > Payload) and the cf_clearance cookie into .env."
)


class PerchanceError(RuntimeError):
    pass


def load_config():
    """Settings from the environment, falling back to .env. Re-read on every request, so a
    refreshed userKey in .env takes effect without a restart."""
    config = dict(DEFAULTS)
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    for key in DEFAULTS:
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def approx_tokens(text):
    # The site counts with a DeepSeek tokenizer; ~3.5 characters per token is close enough.
    return (len(text) * 2 + 6) // 7


def _parse(line):
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise PerchanceError(f"Unreadable reply from Perchance: {line[:300]!r}") from e


def _request(instruction, start_with, stop, config, result):
    """One request. Yields the text chunks and stores Perchance's stopReason in result.

    The endpoint answers with lines like  t:"chunk"  and ends with
    data:{"text":"","final":true,"stopReason":"natural"}  (the final text is the last token).
    """
    params = {
        "browserId": config["PERCHANCE_BROWSER_ID"],
        "userKey": config["PERCHANCE_USER_KEY"],
        "thread": 0,
        "requestId": f"aiTextCompletion{random.randrange(10**16, 10**17)}",
        "__cacheBust": random.random(),
    }
    body = {
        "instruction": instruction,
        "startWith": start_with,
        "stopSequences": stop,
        "generatorName": config["PERCHANCE_GENERATOR"],
        "instructionTokenCount": approx_tokens(instruction),
        "startWithTokenCount": approx_tokens(start_with),
    }
    headers = {
        "User-Agent": config["PERCHANCE_USER_AGENT"],
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://text-generation.perchance.org",
        "Referer": "https://text-generation.perchance.org/embed",
    }
    if config["PERCHANCE_CF_CLEARANCE"]:
        headers["Cookie"] = "cf_clearance=" + config["PERCHANCE_CF_CLEARANCE"]

    try:
        resp = requests.post(
            config["PERCHANCE_BASE_URL"].rstrip("/") + "/api/generate",
            params=params,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            stream=True,
            timeout=(15, float(config["PERCHANCE_TIMEOUT"])),
        )
    except requests.RequestException as e:
        raise PerchanceError(f"Could not reach Perchance: {e}") from e

    with resp:
        if resp.status_code != 200:
            raise PerchanceError(
                f"Perchance returned HTTP {resp.status_code}: {resp.text[:300]!r}. " + REFRESH_HINT
            )
        # Seen in the browser when the key is missing or no longer verified.
        reverify = bool(resp.headers.get("X-Should-Reverify"))
        got_text = False
        resp.encoding = "utf-8"
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("t:"):
                chunk = _parse(line[2:])
                if chunk:
                    got_text = True
                    yield chunk
            elif line.startswith("data:"):
                event = _parse(line[5:])
                if event.get("error"):
                    raise PerchanceError(f"Perchance returned an error: {json.dumps(event)[:500]}")
                if event.get("text"):
                    got_text = True
                    yield event["text"]
                if event.get("final"):
                    result["stop_reason"] = event.get("stopReason")
                    break
            else:
                raise PerchanceError(f"Unexpected reply from Perchance: {line[:300]!r}. " + REFRESH_HINT)
        if reverify and not got_text:
            raise PerchanceError("Perchance wants the userKey re-verified. " + REFRESH_HINT)


def _ends_in_stop_sequence(text, stop):
    # Perchance keeps the matched stop sequence (and at most the rest of its last token) in the text.
    return any(s in text[-(len(s) + 20):] for s in stop)


def generate(instruction, start_with="", stop=None, config=None, on_continue=None, info=None):
    """Yield the reply text in chunks as Perchance streams it.

    Perchance ends every reply after 1024 tokens with stopReason "artificial", the same reason it
    gives when a stop sequence matches. A reply cut by the limit is continued like the site's
    "continue" button does it: the same instruction again, with startWith set to the text so far.
    on_continue(n, max_continues, chars_so_far) is called before each continuation. If the reply
    can't be finished (context full, too many continuations, a failed continuation), the text so far
    is kept and info["truncated"] says why.
    """
    config = config or load_config()
    if not config["PERCHANCE_USER_KEY"]:
        raise PerchanceError("PERCHANCE_USER_KEY is not set. " + REFRESH_HINT)
    max_input = int(config["PERCHANCE_MAX_INPUT_CHARS"])
    if len(instruction) + len(start_with) > max_input:
        raise PerchanceError(f"The prompt is too long for Perchance ({len(instruction) + len(start_with)} "
                             f"characters, limit {max_input}).")
    stop = [s for s in stop or [] if s]
    max_continues = int(config["PERCHANCE_MAX_CONTINUES"])
    info = {} if info is None else info
    reply = ""
    for n in range(max_continues + 1):
        if n:
            if len(instruction) + len(start_with) + len(reply) > max_input:
                info["truncated"] = "Perchance's context is full"
                return
            if on_continue:
                on_continue(n, max_continues, len(reply))
        result, before = {}, len(reply)
        try:
            for chunk in _request(instruction, start_with + reply, stop, config, result):
                reply += chunk
                yield chunk
        except PerchanceError as e:
            if not reply:
                raise
            info["truncated"] = f"a continuation failed ({e})"
            return
        cut_by_limit = result.get("stop_reason") == "artificial" and not _ends_in_stop_sequence(reply, stop)
        if not cut_by_limit or len(reply) == before:
            return
    info["truncated"] = f"it needed more than {max_continues} continuations"


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or sys.stdin.read()
    try:
        for piece in generate(prompt):
            print(piece, end="", flush=True)
        print()
    except PerchanceError as e:
        sys.exit(f"\n{e}")
