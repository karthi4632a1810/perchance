"""
Client for Perchance's free text generator (text-generation.perchance.org/api/generate) and its
text-to-image plugin (image-generation.perchance.org/api/generate).

It reuses the session from your own browser: the userKey (and, for images, the adAccessCode) from a
generate request and the cf_clearance cookie, all copied from Chrome DevTools into .env.
cf_clearance only works with the same User-Agent (and usually the same IP) it was issued to.

Quick tests:  python3 perchance.py "Write a Python function that reverses a string"
              python3 perchance.py --image "a red apple on a wooden table"
"""

import argparse
import json
import os
import random
import sys
import time

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
    "PERCHANCE_IMAGE_BASE_URL": "https://image-generation.perchance.org",
    "PERCHANCE_IMAGE_USER_KEY": "",
    "PERCHANCE_AD_ACCESS_CODE": "",
    # Like the site's default safety setting, images Perchance flags as maybe NSFW are not returned.
    "PERCHANCE_IMAGE_ALLOW_NSFW": "false",
}

REFRESH_HINT = (
    "Open https://perchance.org/ai-code-generator in Chrome, generate once, then copy the "
    "userKey (Network > generate request > Payload) and the cf_clearance cookie into .env."
)
IMAGE_REFRESH_HINT = (
    "Open https://perchance.org/text-to-image-plugin in Chrome, generate one image, then copy the userKey "
    "and adAccessCode (Network > generate request > Payload) into PERCHANCE_IMAGE_USER_KEY and "
    "PERCHANCE_AD_ACCESS_CODE in .env, and the cf_clearance cookie into PERCHANCE_CF_CLEARANCE."
)
IMAGES_DIR = os.path.join(SCRIPT_DIR, "images")


class PerchanceError(RuntimeError):
    pass


class PerchanceBusy(PerchanceError):
    """Perchance is still generating an earlier request for the same userKey (it runs one at a time)."""


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
            text = resp.text[:300]
            # HTTP 203 {"status":"waiting_for_prev_request_to_finish",...}: another request is still running.
            if "waiting_for_prev_request_to_finish" in text:
                raise PerchanceBusy(text)
            # HTTP 400 {"status":"invalid_key"}: the key expired, or Perchance cancelled it (it also does this
            # when the same key is used from more than one IP address).
            if "invalid_key" in text:
                raise PerchanceError("Perchance no longer accepts the userKey (invalid_key). " + REFRESH_HINT)
            raise PerchanceError(f"Perchance returned HTTP {resp.status_code}: {text!r}. " + REFRESH_HINT)
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


def generate(instruction, start_with="", stop=None, config=None, on_continue=None, info=None, on_wait=None):
    """Yield the reply text in chunks as Perchance streams it.

    Perchance ends every reply after 1024 tokens with stopReason "artificial", the same reason it
    gives when a stop sequence matches. A reply cut by the limit is continued like the site's
    "continue" button does it: the same instruction again, with startWith set to the text so far.
    on_continue(n, max_continues, chars_so_far) is called before each continuation. If the reply
    can't be finished (context full, too many continuations, a failed continuation), the text so far
    is kept and info["truncated"] says why. While Perchance is still busy with an earlier request for
    the same key, it waits and asks again; on_wait(status) is called each time.
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
        busy_until = time.time() + float(config["PERCHANCE_TIMEOUT"])
        while True:
            try:
                for chunk in _request(instruction, start_with + reply, stop, config, result):
                    reply += chunk
                    yield chunk
                break
            except PerchanceBusy:
                if time.time() > busy_until:
                    raise PerchanceError("Perchance stayed busy with an earlier request for this userKey "
                                         "(is another Cline task or script using the same key?).")
                if on_wait:
                    on_wait("waiting_for_prev_request_to_finish")
                time.sleep(2 + random.random() * 2)
            except PerchanceError as e:
                if not reply:
                    raise
                info["truncated"] = f"a continuation failed ({e})"
                return
        cut_by_limit = result.get("stop_reason") == "artificial" and not _ends_in_stop_sequence(reply, stop)
        if not cut_by_limit or len(reply) == before:
            return
    info["truncated"] = f"it needed more than {max_continues} continuations"


def _image_headers(config):
    headers = {
        "User-Agent": config["PERCHANCE_USER_AGENT"],
        "Accept": "*/*",
        "Origin": "https://image-generation.perchance.org",
        "Referer": "https://image-generation.perchance.org/embed",
    }
    if config["PERCHANCE_CF_CLEARANCE"]:
        headers["Cookie"] = "cf_clearance=" + config["PERCHANCE_CF_CLEARANCE"]
    return headers


def _looks_like_image(data):
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def generate_image(prompt, negative_prompt="", resolution="512x512", seed=-1, guidance_scale=7, config=None, on_wait=None):
    """Generate one image with Perchance's text-to-image plugin.

    Follows the site's own client (image-generation.perchance.org/embed): one requestId per image,
    asking again while the queue is busy, then downloading the result from imageDownloadUrl.
    Returns {"data", "extension", "seed", "width", "height"}.
    """
    config = config or load_config()
    key, code = config["PERCHANCE_IMAGE_USER_KEY"], config["PERCHANCE_AD_ACCESS_CODE"]
    if not key or not code:
        raise PerchanceError("PERCHANCE_IMAGE_USER_KEY and PERCHANCE_AD_ACCESS_CODE are not set. " + IMAGE_REFRESH_HINT)
    base = config["PERCHANCE_IMAGE_BASE_URL"].rstrip("/")
    headers = _image_headers(config)
    request_id = str(random.random())
    body = {"prompt": prompt, "negativePrompt": negative_prompt, "seed": seed, "resolution": resolution,
            "guidanceScale": guidance_scale, "channel": "text-to-image-plugin", "subChannel": "public",
            "userKey": key, "adAccessCode": code, "requestId": request_id}
    deadline = time.time() + float(config["PERCHANCE_TIMEOUT"])
    while True:
        params = {"userKey": key, "requestId": request_id, "adAccessCode": code, "__cacheBust": random.random()}
        try:
            resp = requests.post(base + "/api/generate", params=params, data=json.dumps(body).encode("utf-8"),
                                 headers={**headers, "Content-Type": "text/plain;charset=UTF-8"}, timeout=(15, 120))
        except requests.RequestException as e:
            raise PerchanceError(f"Could not reach Perchance's image generator: {e}") from e
        try:
            result = resp.json()
        except ValueError:
            raise PerchanceError(f"Perchance's image generator returned HTTP {resp.status_code}: "
                                 f"{resp.text[:300]!r}. " + IMAGE_REFRESH_HINT)
        status = result.get("status")
        if status == "success":
            break
        # The site waits 2-4 seconds and asks again in these cases.
        if status in ("waiting_for_prev_request_to_finish", "network_busy") and time.time() < deadline:
            if on_wait:
                on_wait(status)
            time.sleep(2 + random.random() * 2)
            continue
        hint = " " + IMAGE_REFRESH_HINT if status in ("invalid_key", "invalid_ad_access_code") else ""
        raise PerchanceError(f"Perchance image generation failed: {status or json.dumps(result)[:300]}.{hint}")

    if result.get("maybeNsfw") and config["PERCHANCE_IMAGE_ALLOW_NSFW"].strip().lower() not in ("1", "true", "yes", "on"):
        raise PerchanceError("Perchance flagged this image as possibly not safe for work, so it was not returned "
                             "(the site hides these by default too; PERCHANCE_IMAGE_ALLOW_NSFW=true in .env changes that).")
    url = result.get("imageDownloadUrl") or f"/api/downloadTemporaryImage?imageId={result.get('imageId')}"
    url = url if url.startswith("http") else base + url
    problem = "no attempt made"
    for attempt in range(3):
        if attempt:
            time.sleep(2)
        try:
            image = requests.get(url, headers=headers, timeout=(15, 60))
        except requests.RequestException as e:
            problem = str(e)
            continue
        if image.status_code == 200 and _looks_like_image(image.content):
            return {"data": image.content, "extension": result.get("fileExtension") or "jpeg",
                    "seed": result.get("seed"), "width": result.get("width"), "height": result.get("height")}
        problem = f"HTTP {image.status_code}, {len(image.content)} bytes"
    raise PerchanceError(f"The image was generated but could not be downloaded ({problem}).")


def save_image(image, prompt=""):
    """Saves a generate_image() result under images/ and returns the file path."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    words = "-".join("".join(c for c in w if c.isalnum()) for w in prompt.lower().split()[:6]).strip("-")
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{words or 'image'}-{image['seed']}"
    for n in range(1, 1000):
        path = os.path.join(IMAGES_DIR, f"{stem}{f'-{n}' if n > 1 else ''}.{image['extension']}")
        try:
            with open(path, "xb") as f:   # "x": never overwrite an image saved in the same second
                f.write(image["data"])
            return path
        except FileExistsError:
            continue
    raise PerchanceError(f"Could not find a free file name for {stem} in {IMAGES_DIR}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Try Perchance's text or image generator from the command line.")
    parser.add_argument("prompt", nargs="*", help="the prompt (read from stdin if empty)")
    parser.add_argument("--image", action="store_true", help="generate an image and save it under images/")
    parser.add_argument("--size", default="512x512", help="image resolution, e.g. 512x512, 512x768, 768x512")
    parser.add_argument("--negative", default="", help="things the image should not contain")
    args = parser.parse_args()
    prompt = " ".join(args.prompt) or sys.stdin.read()
    try:
        if args.image:
            print(save_image(generate_image(prompt, args.negative, args.size), prompt))
        else:
            for piece in generate(prompt):
                print(piece, end="", flush=True)
            print()
    except PerchanceError as e:
        sys.exit(f"\n{e}")
