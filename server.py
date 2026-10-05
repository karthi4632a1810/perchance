"""
OpenAI-compatible API over Perchance's free text generator, with tool calling for coding agents
such as Cline, Roo Code and Continue, plus image generation through its text-to-image plugin.

Point the client at  http://127.0.0.1:8010/v1  with any API key and model "perchance".
When a request has tools, they are described to the model, which calls them with ```tool_call
blocks; those go back to the client as real OpenAI tool_calls (see agent.py).
Images: POST /v1/images/generations (model "perchance-image"); they are also saved under images/.
"""

import argparse
import base64
import json
import os
import sys
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from agent import (
    MAX_REPAIR_ROUNDS, NUDGE_PROMPT, build_full_prompt, content_to_text, cut_invented_turns,
    needs_nudge, normalize_tools, parse_model_reply, repair_prompt, split_large_writes,
)
from perchance import IMAGES_DIR, PerchanceError, approx_tokens, generate, generate_image, save_image


def _env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_ID = "perchance"
IMAGE_MODEL_ID = "perchance-image"
MAX_IMAGES_PER_REQUEST = 4   # Perchance makes them one after another
IMAGE_TYPES = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
STREAM_PROGRESS = _env_bool("STREAM_PROGRESS", True)   # live model text as reasoning_content
DEBUG_DUMPS = _env_bool("DEBUG_DUMPS", True)           # last_request.json / last_prompt.txt / last_reply.txt
# The model sometimes keeps going and invents the tool results itself; stop it there.
AGENT_STOP_SEQUENCES = ["=== TOOL RESULT", "=== USER ==="]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dump_debug(name, text):
    if not DEBUG_DUMPS:
        return
    try:
        with open(os.path.join(SCRIPT_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def log_continue(n, max_continues, chars):
    log(f"[~] Perchance cut the reply at {chars} chars; continuing ({n}/{max_continues})...")


def log_wait(status):
    log(f"[~] Perchance: {status} (it runs one request per key at a time); waiting...")


def perchance_resolution(size):
    """OpenAI sizes (1024x1024, 1024x1792, ...) -> the plugin's square, portrait or landscape resolution."""
    try:
        width, height = (int(x) for x in str(size or "512x512").lower().split("x"))
    except ValueError:
        return "512x512"
    if width == height:
        return "512x512"
    return "512x768" if height > width else "768x512"


def approx_usage(prompt, reply):
    prompt_tokens, completion_tokens = approx_tokens(prompt), approx_tokens(reply)
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}


class ClientGone(Exception):
    pass


# ==============================================================================
# Agent turns
# ==============================================================================

class PerchanceBackend:
    """Perchance is stateless, so every call sends the whole prompt; follow-ups are appended to it."""

    def __init__(self, prompt, emitter):
        self.prompt = prompt
        self.emitter = emitter
        self.last_text = ""
        self.truncated = None   # why the last reply couldn't be finished, if it couldn't

    def send_initial(self):
        log(f"[>] Perchance: {len(self.prompt)} chars (~{approx_tokens(self.prompt)} tokens)")
        return self._call()

    def send_followup(self, followup):
        self.prompt += f"\n\n=== ASSISTANT (you) ===\n{self.last_text}\n\n=== USER ===\n{followup}"
        return self._call()

    def _call(self):
        dump_debug("last_prompt.txt", self.prompt)
        self.emitter.progress_break()
        text, info = "", {}
        for chunk in generate(self.prompt, stop=AGENT_STOP_SEQUENCES, on_continue=log_continue, info=info,
                              on_wait=log_wait):
            text += chunk
            self.emitter.progress_snapshot(text)
        self.truncated = info.get("truncated")
        if self.truncated:
            log(f"[!] The reply was cut off at {len(text)} chars: {self.truncated}.")
        dump_debug("last_reply.txt", text)
        text = cut_invented_turns(text)
        if not text.strip():
            raise PerchanceError("Perchance returned an empty reply.")
        self.last_text = text
        return text


def run_agent_turn(backend, tools):
    """Gets one reply from the model, asking it to fix unparseable tool calls or missing actions.

    A fix is only requested when nothing in the reply can run: resending a long reply takes Perchance
    minutes, so usable calls run and the broken ones are reported back to the model instead."""
    parsed = parse_model_reply(backend.send_initial(), tools)
    repairs = nudges = 0
    while True:
        if parsed.errors and not parsed.calls and repairs < MAX_REPAIR_ROUNDS:
            repairs += 1
            log(f"[!] Tool calls could not be parsed ({'; '.join(parsed.errors)}). "
                f"Asking the model to fix them ({repairs}/{MAX_REPAIR_ROUNDS})...")
            followup = repair_prompt(parsed.errors)
        elif not parsed.calls and not parsed.errors and nudges < 1 and needs_nudge(parsed.prose):
            nudges += 1
            log("[!] Reply had no tool calls but looks unfinished. Nudging the model to act...")
            followup = NUDGE_PROMPT
        else:
            return parsed
        try:
            parsed = parse_model_reply(backend.send_followup(followup), tools)
        except PerchanceError as e:
            log(f"[!] Follow-up failed: {e}")
            return parsed


# ==============================================================================
# Writing the OpenAI response
# ==============================================================================

class Emitter:
    """Writes SSE chunks when streaming, otherwise one JSON body. The stream starts on the first
    output, so a failure before that (an expired key, say) is still a plain HTTP error."""

    def __init__(self, handler, model, stream, include_usage):
        self.h = handler
        self.model = model
        self.stream = stream
        self.include_usage = include_usage
        self.chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.started = False
        self.progress_sent = ""

    def _write(self, data: bytes):
        try:
            self.h.wfile.write(data)
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise ClientGone(str(e))

    def _event(self, obj):
        self._write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))

    def _chunk(self, delta, finish_reason=None):
        self._event({"id": self.chat_id, "object": "chat.completion.chunk", "created": self.created,
                     "model": self.model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})

    def begin(self):
        if not self.stream or self.started:
            return
        self.h.send_response(200)
        self.h.send_header("Content-Type", "text/event-stream")
        self.h.send_header("Cache-Control", "no-cache")
        self.h.send_header("Connection", "close")
        self.h.end_headers()
        self.started = True
        self._chunk({"role": "assistant"})

    def content(self, text):
        """Streams answer text as it arrives (plain chat without tools)."""
        if self.stream and text:
            self.begin()
            self._chunk({"content": text})

    def progress_break(self):
        if self.progress_sent and self.started and STREAM_PROGRESS:
            self._chunk({"reasoning_content": "\n\n---\n\n"})
        self.progress_sent = ""

    def progress_snapshot(self, text):
        """Streams the model's live text as reasoning, so the agent shows it working."""
        if not (self.stream and STREAM_PROGRESS) or not text.startswith(self.progress_sent):
            return
        delta = text[len(self.progress_sent):]
        if delta:
            self.begin()
            self._chunk({"reasoning_content": delta})
        self.progress_sent = text

    def finish(self, content, calls, usage):
        """Sends the final content and tool calls."""
        tool_calls = [{"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                       "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                      for name, args in calls]
        finish_reason = "tool_calls" if tool_calls else "stop"
        if not self.stream:
            message = {"role": "assistant", "content": content or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            self.h.send_json(200, {"id": self.chat_id, "object": "chat.completion", "created": self.created,
                                   "model": self.model, "usage": usage,
                                   "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]})
            return
        self.begin()
        if content:
            self._chunk({"content": content})
        for i, tc in enumerate(tool_calls):
            self._chunk({"tool_calls": [dict(tc, index=i)]})
        self._chunk({}, finish_reason)
        if self.include_usage:
            self._event({"id": self.chat_id, "object": "chat.completion.chunk", "created": self.created,
                         "model": self.model, "choices": [], "usage": usage})
        self._write(b"data: [DONE]\n\n")

    def error(self, message, status=502, code="perchance_error"):
        err = {"error": {"message": message, "type": code, "code": status}}
        try:
            if self.started:
                self._event(err)
                self._write(b"data: [DONE]\n\n")
            else:
                self.h.send_json(status, err)
        except ClientGone:
            pass


# ==============================================================================
# HTTP server
# ==============================================================================

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(fmt % args)

    def send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self.send_json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "perchance"},
                {"id": IMAGE_MODEL_ID, "object": "model", "created": 0, "owned_by": "perchance"},
            ]})
        elif path.startswith("/images/"):
            self._send_image(unquote(path[len("/images/"):]))
        elif path in ("", "/health"):
            self.send_json(200, {"ok": True})
        else:
            self.send_json(404, {"error": {"message": f"Unknown path {self.path}"}})

    def _send_image(self, name):
        path = os.path.join(IMAGES_DIR, name)
        if os.path.basename(name) != name or not os.path.isfile(path):
            return self.send_json(404, {"error": {"message": f"No image named {name!r}"}})
        with open(path, "rb") as f:
            data = f.read()
        try:
            self.send_response(200)
            self.send_header("Content-Type", IMAGE_TYPES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        images = path in ("/v1/images/generations", "/images/generations")
        if not images and path not in ("/v1/chat/completions", "/chat/completions"):
            return self.send_json(404, {"error": {"message": f"Unknown path {self.path}"}})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", errors="replace")
        if not images:
            dump_debug("last_request.json", raw)
        try:
            req = json.loads(raw) if raw else {}
        except ValueError:
            return self.send_json(400, {"error": {"message": "Request body is not valid JSON",
                                                  "type": "invalid_request_error"}})
        if images:
            return self._images_reply(req)

        messages = req.get("messages") or []
        stream = bool(req.get("stream"))
        tools = [] if req.get("tool_choice") == "none" else normalize_tools(req.get("tools"))
        include_usage = bool((req.get("stream_options") or {}).get("include_usage"))
        emitter = Emitter(self, req.get("model") or MODEL_ID, stream, include_usage)
        log(f"[>] Request: {len(messages)} messages, {len(tools)} tools, stream={stream}")
        try:
            if tools:
                self._agent_reply(messages, tools, emitter)
            else:
                self._chat_reply(messages, req.get("stop"), emitter)
        except ClientGone:
            log("[!] The client disconnected; dropping this turn.")
        except PerchanceError as e:
            log(f"[!] {e}")
            emitter.error(str(e))
        except Exception as e:
            traceback.print_exc()
            emitter.error(f"Proxy error: {e}", 500, "proxy_error")

    def _agent_reply(self, messages, tools, emitter):
        backend = PerchanceBackend(build_full_prompt(messages, tools), emitter)
        parsed = run_agent_turn(backend, tools)
        calls = split_large_writes(parsed.calls, tools)
        if len(calls) > len(parsed.calls):
            log(f"[~] Split large new files: {len(parsed.calls)} tool calls became {len(calls)} "
                f"(Cline's editor takes at most 6000 characters per call)")
        content = parsed.prose
        if parsed.errors:
            if calls:
                content += "\n\n⚠️ Some tool_call blocks couldn't be parsed and were skipped (the others ran):\n"
            else:
                content += "\n\n⚠️ The proxy couldn't parse the model's tool calls, so nothing was run:\n"
            content += "\n".join(f"- {e}" for e in parsed.errors)
            if backend.truncated:
                content += f"\n(The reply was cut off because {backend.truncated}: write less per reply.)"
            if calls:
                content += "\nSend the skipped work again in the next reply."
        if not content and not calls:
            content = "(The model returned an empty reply.)"
        emitter.finish(content, calls, approx_usage(backend.prompt, backend.last_text))
        if calls:
            log(f"[<] Sent {len(calls)} tool call(s): {', '.join(name for name, _ in calls)}")
        else:
            log("[<] Sent final answer (no tool calls): the agent loop ends here")

    def _chat_reply(self, messages, stop, emitter):
        if len(messages) == 1 and messages[0].get("role") == "user":
            prompt = content_to_text(messages[0].get("content"))
        else:
            prompt = build_full_prompt(messages, [])
        stop = [stop] if isinstance(stop, str) else list(stop or [])
        text = ""
        for chunk in generate(prompt, stop=stop, on_continue=log_continue, on_wait=log_wait):
            text += chunk
            emitter.content(chunk)
        emitter.finish("" if emitter.stream else text, [], approx_usage(prompt, text))
        log(f"[<] Sent {len(text)} chars")

    def _images_reply(self, req):
        """OpenAI /v1/images/generations: prompt, n, size, response_format ("url" or "b64_json"), plus
        Perchance's negative_prompt, seed and guidance_scale. Every image is also saved under images/."""
        prompt = str(req.get("prompt") or "").strip()
        try:
            n = max(1, min(MAX_IMAGES_PER_REQUEST, int(req.get("n") or 1)))
            seed, guidance = int(req.get("seed", -1)), float(req.get("guidance_scale", 7))
        except (TypeError, ValueError):
            prompt = ""
        if not prompt:
            return self.send_json(400, {"error": {"message": "A prompt is required (and n, seed, guidance_scale "
                                                             "must be numbers).", "type": "invalid_request_error"}})
        resolution = perchance_resolution(req.get("size"))
        log(f"[>] Image request: {n} x {resolution}, {prompt[:80]!r}")
        data = []
        try:
            for _ in range(n):
                image = generate_image(prompt, str(req.get("negative_prompt") or ""), resolution, seed, guidance,
                                       on_wait=log_wait)
                path = save_image(image, prompt)
                log(f"[<] Image saved: images/{os.path.basename(path)} (seed {image['seed']})")
                item = {"revised_prompt": prompt}
                if req.get("response_format") == "b64_json":
                    item["b64_json"] = base64.b64encode(image["data"]).decode("ascii")
                else:
                    item["url"] = f"http://{self.headers.get('Host') or '127.0.0.1:8010'}/images/{os.path.basename(path)}"
                data.append(item)
        except PerchanceError as e:
            log(f"[!] {e}")
            if not data:
                return self.send_json(502, {"error": {"message": str(e), "type": "perchance_error"}})
        self.send_json(200, {"created": int(time.time()), "data": data})


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="OpenAI-compatible agent proxy for Perchance's text generator")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8010")))
    args = parser.parse_args()
    try:
        server = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Can't listen on {args.host}:{args.port} ({e}). Is the proxy already running? "
                 f"Try another port with --port.")
    print(f"Perchance proxy on http://{args.host}:{args.port}/v1  (model: {MODEL_ID})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
