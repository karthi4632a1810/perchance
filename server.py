"""
OpenAI-compatible API over Perchance's free AI Agent (minimal#edit via Chrome Bridge)
and free text generator, with tool calling for coding agents such as Cline, Roo Code,
and Continue, plus image generation through its text-to-image plugin.

Point the client at http://127.0.0.1:8010/v1 with any API key and model "perchance" or "perchance-agent".
When the Chrome Bridge extension is active, requests run directly through Perchance AI Agent
(Claude 3.5 Sonnet class model on perchance.org/minimal#edit) with live reasoning and native tool calls.
If the bridge is offline, requests fall back to the text generator (text-generation.perchance.org).
Images: POST /v1/images/generations (model "perchance-image"); they are saved under images/.
"""

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from agent import (
    MAX_REPAIR_ROUNDS, NUDGE_PROMPT, build_full_prompt, content_to_text, cut_invented_turns,
    needs_nudge, normalize_tools, parse_model_reply, repair_prompt, split_large_writes,
)
from perchance import IMAGES_DIR, PerchanceError, approx_tokens, generate, generate_image, save_image


def _env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_ID = "perchance"
AGENT_MODEL_ID = "perchance-agent"
IMAGE_MODEL_ID = "perchance-image"
MAX_IMAGES_PER_REQUEST = 4
IMAGE_TYPES = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
STREAM_PROGRESS = _env_bool("STREAM_PROGRESS", True)
DEBUG_DUMPS = _env_bool("DEBUG_DUMPS", True)
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
    try:
        width, height = (int(x) for x in str(size or "512x512").lower().split("x"))
    except ValueError:
        return "512x512"
    if width == height:
        return "512x512"
    return "512x768" if height > width else "768x512"


def approx_usage(prompt, reply):
    prompt_tokens, completion_tokens = approx_tokens(prompt), approx_tokens(reply)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


class ClientGone(Exception):
    pass


# ==============================================================================
# Chrome Bridge Manager
# ==============================================================================

class BridgeManager:
    """Coordinates jobs between Cline requests and the Chrome extension tab running Perchance AI Agent."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.job_queue = queue.Queue()
        self.last_poll_time = 0
        self.status = "waiting for extension"

    @property
    def is_online(self):
        return (time.time() - self.last_poll_time) < 45

    def register_poll(self, state=None):
        with self.lock:
            self.last_poll_time = time.time()
            if state and isinstance(state, dict) and state.get("mode"):
                self.status = f"connected ({state.get('mode')})"
            else:
                self.status = "connected"
        try:
            job = self.job_queue.get(timeout=20)
            return {"job": job}
        except queue.Empty:
            return {"job": None}

    def dispatch_job(self, prompt, tools=None, new_chat=False):
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job = {
            "id": job_id,
            "prompt": prompt,
            "tools": tools or [],
            "new_chat": new_chat,
            "stream_queue": queue.Queue(),
            "done_event": threading.Event(),
            "result": None,
            "error": None,
        }
        with self.lock:
            self.jobs[job_id] = job
        self.job_queue.put({
            "id": job_id,
            "prompt": prompt,
            "tools": tools or [],
            "new_chat": new_chat,
        })
        return job

    def on_chunk(self, job_id, delta):
        with self.lock:
            job = self.jobs.get(job_id)
        if job and delta:
            job["stream_queue"].put(delta)

    def on_done(self, job_id, result):
        with self.lock:
            job = self.jobs.get(job_id)
        if job:
            job["result"] = result or {}
            job["done_event"].set()

    def on_error(self, job_id, error_msg):
        with self.lock:
            job = self.jobs.get(job_id)
        if job:
            job["error"] = error_msg or "Unknown error"
            job["done_event"].set()

    def update_status(self, text):
        with self.lock:
            self.status = str(text)

    def cleanup(self, job_id):
        with self.lock:
            self.jobs.pop(job_id, None)


bridge_manager = BridgeManager()


# ==============================================================================
# Agent backends
# ==============================================================================

class BridgeBackend:
    """Uses the Chrome Extension bridge connected to Perchance AI Agent (minimal#edit)."""

    def __init__(self, prompt, emitter, tools=None):
        self.prompt = prompt
        self.emitter = emitter
        self.tools = tools or []
        self.last_text = ""
        self.truncated = None

    def send_initial(self):
        log(f"[>] Perchance AI Agent (Bridge): {len(self.prompt)} chars")
        return self._call()

    def send_followup(self, followup):
        self.prompt += f"\n\n=== ASSISTANT (you) ===\n{self.last_text}\n\n=== USER ===\n{followup}"
        return self._call()

    def _call(self):
        dump_debug("last_prompt.txt", self.prompt)
        job = bridge_manager.dispatch_job(self.prompt, self.tools)
        text = ""
        while True:
            try:
                item = job["stream_queue"].get(timeout=0.1)
                if not item:
                    break
                if isinstance(item, dict):
                    if "reasoning" in item and item["reasoning"]:
                        self.emitter.progress_snapshot(item["reasoning"])
                    if "content" in item and item["content"]:
                        delta = item["content"]
                        if not text:
                            delta = re.sub(r"^👋\s*I can edit the code and test it live\.[^\n]*\n*", "", delta, flags=re.IGNORECASE)
                            if not delta:
                                continue
                        text += delta
                        self.emitter.content(delta)
            except queue.Empty:
                if job["done_event"].is_set():
                    while not job["stream_queue"].empty():
                        item = job["stream_queue"].get_nowait()
                        if isinstance(item, dict) and "content" in item and item["content"]:
                            text += item["content"]
                    break

        if job["error"]:
            bridge_manager.cleanup(job["id"])
            raise PerchanceError(f"Perchance Bridge error: {job['error']}")

        res = job["result"] or {}
        if not text and res.get("text"):
            text = res.get("text")
        bridge_manager.cleanup(job["id"])
        text = re.sub(r"^👋\s*I can edit the code and test it live\.[^\n]*\n*", "", text, flags=re.IGNORECASE).strip()
        dump_debug("last_reply.txt", text)
        text = cut_invented_turns(text)
        if not text.strip():
            raise PerchanceError("Perchance Agent returned an empty reply.")
        self.last_text = text
        return text


class PerchanceBackend:
    """Fallback: Perchance text-generator backend."""

    def __init__(self, prompt, emitter):
        self.prompt = prompt
        self.emitter = emitter
        self.last_text = ""
        self.truncated = None

    def send_initial(self):
        log(f"[>] Perchance generator: {len(self.prompt)} chars (~{approx_tokens(self.prompt)} tokens)")
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
        self._event({
            "id": self.chat_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        })

    def begin(self):
        if not self.stream or self.started:
            return
        self.h.send_response(200)
        self.h.send_header("Content-Type", "text/event-stream")
        self.h.send_header("Cache-Control", "no-cache")
        self.h.send_header("Connection", "close")
        self.h.send_header("Access-Control-Allow-Origin", "*")
        self.h.end_headers()
        self.started = True
        self._chunk({"role": "assistant"})

    def content(self, text):
        if self.stream and text:
            self.begin()
            self._chunk({"content": text})

    def progress_break(self):
        if self.progress_sent and self.started and STREAM_PROGRESS:
            self._chunk({"reasoning_content": "\n\n---\n\n"})
        self.progress_sent = ""

    def progress_snapshot(self, text):
        if not (self.stream and STREAM_PROGRESS) or not text.startswith(self.progress_sent):
            return
        delta = text[len(self.progress_sent):]
        if delta:
            self.begin()
            self._chunk({"reasoning_content": delta})
        self.progress_sent = text

    def finish(self, content, calls, usage):
        tool_calls = [
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for name, args in calls
        ]
        finish_reason = "tool_calls" if tool_calls else "stop"
        if not self.stream:
            message = {"role": "assistant", "content": content or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            self.h.send_json(
                200,
                {
                    "id": self.chat_id,
                    "object": "chat.completion",
                    "created": self.created,
                    "model": self.model,
                    "usage": usage,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                },
            )
            return
        self.begin()
        if content:
            self._chunk({"content": content})
        for i, tc in enumerate(tool_calls):
            self._chunk({"tool_calls": [dict(tc, index=i)]})
        self._chunk({}, finish_reason)
        if self.include_usage:
            self._event({
                "id": self.chat_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [],
                "usage": usage,
            })
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
# HTTP Handler
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
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "perchance"},
                        {"id": AGENT_MODEL_ID, "object": "model", "created": 0, "owned_by": "perchance"},
                        {"id": IMAGE_MODEL_ID, "object": "model", "created": 0, "owned_by": "perchance"},
                    ],
                },
            )
        elif path.startswith("/images/"):
            self._send_image(unquote(path[len("/images/"):]))
        elif path in ("/bridge/status", "/status"):
            self.send_json(200, {
                "ok": True,
                "online": bridge_manager.is_online,
                "status": bridge_manager.status,
                "last_poll": bridge_manager.last_poll_time,
                "pending_jobs": bridge_manager.job_queue.qsize(),
            })
        elif path in ("", "/health"):
            self.send_json(200, {
                "ok": True,
                "bridge_online": bridge_manager.is_online,
                "status": bridge_manager.status,
            })
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
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/bridge":
            return self._handle_bridge()

        images = path in ("/v1/images/generations", "/images/generations")
        if not images and path not in ("/v1/chat/completions", "/chat/completions"):
            return self.send_json(404, {"error": {"message": f"Unknown path {self.path}"}})

        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", errors="replace")
        if not images:
            dump_debug("last_request.json", raw)
        try:
            req = json.loads(raw) if raw else {}
        except ValueError:
            return self.send_json(400, {"error": {"message": "Request body is not valid JSON", "type": "invalid_request_error"}})

        if images:
            return self._images_reply(req)

        messages = req.get("messages") or []
        stream = bool(req.get("stream"))
        tools = [] if req.get("tool_choice") == "none" else normalize_tools(req.get("tools"))
        include_usage = bool((req.get("stream_options") or {}).get("include_usage"))
        model_name = req.get("model") or (AGENT_MODEL_ID if bridge_manager.is_online else MODEL_ID)
        emitter = Emitter(self, model_name, stream, include_usage)

        backend_desc = "AI Agent (Bridge)" if bridge_manager.is_online else "text-generator"
        log(f"[>] Request: {len(messages)} messages, {len(tools)} tools, stream={stream} (using {backend_desc})")

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

    def _handle_bridge(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", errors="replace")
        try:
            req = json.loads(raw) if raw else {}
        except ValueError:
            return self.send_json(400, {"error": "Invalid JSON"})

        action = req.get("action")
        if not action and "?" in self.path:
            qs = parse_qs(urlparse(self.path).query)
            action = qs.get("action", [None])[0]

        if action == "poll":
            res = bridge_manager.register_poll(req.get("state"))
            return self.send_json(200, res)
        elif action == "start":
            return self.send_json(200, {"ok": True})
        elif action == "chunk":
            bridge_manager.on_chunk(req.get("id"), req.get("delta") or {})
            return self.send_json(200, {"ok": True})
        elif action in ("result", "done"):
            payload = req.get("result") or req
            bridge_manager.on_done(req.get("id") or payload.get("id"), payload)
            return self.send_json(200, {"ok": True})
        elif action == "status":
            bridge_manager.update_status(req.get("text") or "ok")
            return self.send_json(200, {"ok": True})
        elif action == "error":
            bridge_manager.on_error(req.get("id"), req.get("error") or "Unknown error")
            return self.send_json(200, {"ok": True})
        return self.send_json(400, {"error": f"Unknown bridge action {action}"})

    def _agent_reply(self, messages, tools, emitter):
        if bridge_manager.is_online:
            log("[*] Routing to Perchance AI Agent via Chrome Bridge")
            backend = BridgeBackend(build_full_prompt(messages, tools), emitter, tools)
        else:
            log("[~] Chrome Bridge offline; falling back to Perchance text-generator")
            backend = PerchanceBackend(build_full_prompt(messages, tools), emitter)

        parsed = run_agent_turn(backend, tools)
        calls = split_large_writes(parsed.calls, tools)
        if len(calls) > len(parsed.calls):
            log(f"[~] Split large new files: {len(parsed.calls)} tool calls became {len(calls)}")
        content = parsed.prose
        if parsed.errors:
            if calls:
                content += "\n\n⚠️ Some tool_call blocks couldn't be parsed and were skipped:\n"
            else:
                content += "\n\n⚠️ The proxy couldn't parse the model's tool calls:\n"
            content += "\n".join(f"- {e}" for e in parsed.errors)
        if not content and not calls:
            content = "(The model returned an empty reply.)"

        emitter.finish(content, calls, approx_usage(backend.prompt, backend.last_text))
        if calls:
            log(f"[<] Sent {len(calls)} tool call(s): {', '.join(name for name, _ in calls)}")
        else:
            log("[<] Sent final answer (no tool calls)")

    def _chat_reply(self, messages, stop, emitter):
        if len(messages) == 1 and messages[0].get("role") == "user":
            prompt = content_to_text(messages[0].get("content"))
        else:
            prompt = build_full_prompt(messages, [])

        if bridge_manager.is_online:
            log("[*] Routing to Perchance AI Agent via Chrome Bridge")
            backend = BridgeBackend(prompt, emitter)
            text = backend.send_initial()
            emitter.finish("" if emitter.stream else text, [], approx_usage(prompt, text))
            log(f"[<] Sent {len(text)} chars")
            return

        stop = [stop] if isinstance(stop, str) else list(stop or [])
        text = ""
        for chunk in generate(prompt, stop=stop, on_continue=log_continue, on_wait=log_wait):
            text += chunk
            emitter.content(chunk)
        emitter.finish("" if emitter.stream else text, [], approx_usage(prompt, text))
        log(f"[<] Sent {len(text)} chars")

    def _images_reply(self, req):
        prompt = str(req.get("prompt") or "").strip()
        try:
            n = max(1, min(MAX_IMAGES_PER_REQUEST, int(req.get("n") or 1)))
            seed, guidance = int(req.get("seed", -1)), float(req.get("guidance_scale", 7))
        except (TypeError, ValueError):
            prompt = ""
        if not prompt:
            return self.send_json(400, {"error": {"message": "A prompt is required (and n, seed, guidance_scale must be numbers).", "type": "invalid_request_error"}})
        resolution = perchance_resolution(req.get("size"))
        log(f"[>] Image request: {n} x {resolution}, {prompt[:80]!r}")
        data = []
        try:
            for _ in range(n):
                image = generate_image(prompt, str(req.get("negative_prompt") or ""), resolution, seed, guidance, on_wait=log_wait)
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
    parser = argparse.ArgumentParser(description="OpenAI-compatible agent proxy for Perchance AI Agent & text generator")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8010")))
    args = parser.parse_args()
    try:
        server = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Can't listen on {args.host}:{args.port} ({e}). Is the proxy already running? Try another port with --port.")
    print(f"Perchance proxy on http://{args.host}:{args.port}/v1  (models: {MODEL_ID}, {AGENT_MODEL_ID})", flush=True)
    print(f"Bridge endpoint ready at http://{args.host}:{args.port}/bridge", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
