"""
Tool calling for a text-only model, ported from GPT-6-astra/proxy.py.

Perchance has no native tool calling, so the agent's tools are described in the prompt and the
model calls them by writing ```tool_call blocks (JSON) or ```apply_patch blocks. Those are parsed
back into real OpenAI tool_calls; malformed ones are sent back to the model to fix first.
"""

import json
import os
import re

# Perchance's context holds about 52,000 characters (prompt + reply), so the prompt stays well below.
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "34000"))
MAX_TOOL_RESULT_CHARS = int(os.environ.get("MAX_TOOL_RESULT_CHARS", "12000"))
MAX_REPAIR_ROUNDS = int(os.environ.get("MAX_REPAIR_ROUNDS", "2"))
# Cline's editor rejects new_text/old_text over 6000 characters; bigger new files are split.
WRITE_CHUNK_CHARS = int(os.environ.get("WRITE_CHUNK_CHARS", "5000"))
# How much of each file the model wrote earlier is repeated in the conversation history.
HISTORY_FILE_CHARS = int(os.environ.get("HISTORY_FILE_CHARS", "1500"))
# Cline's team_* tools and sub-agents only make the prompt bigger and multiply requests here.
TOOL_EXCLUDE = os.environ.get("TOOL_EXCLUDE", r"^(team_|spawn_agent$)")
TOOL_EXCLUDE_RE = re.compile(TOOL_EXCLUDE) if TOOL_EXCLUDE else None


# ==============================================================================
# Message helpers
# ==============================================================================

def content_to_text(content) -> str:
    """Flattens OpenAI message content (string or list of parts) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if p.get("type") in ("image_url", "input_image", "image"):
                    parts.append("[image attached]")
                elif "text" in p:
                    parts.append(str(p.get("text") or ""))
        return "\n".join(parts)
    return str(content)


def truncate_middle(text, limit):
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - limit} characters omitted by the proxy ...]\n\n{text[-tail:]}"


WORKSPACE_PATTERNS = [
    re.compile(r"Working Directory:\s*([^\n\r]+)", re.IGNORECASE),
    re.compile(r"Workspace Directory \(([^)\n]+)\)", re.IGNORECASE),
    re.compile(r"Current Workspace Directory:\s*([^\n\r]+)", re.IGNORECASE),
]


def extract_workspace_dir(system_text: str) -> str:
    """Finds the workspace path in the agent's system prompt, else falls back to cwd."""
    for pattern in WORKSPACE_PATTERNS:
        m = pattern.search(system_text or "")
        if m:
            return m.group(1).strip().rstrip("/") or "/"
    return os.environ.get("WORKSPACE_DIR") or os.getcwd()


def system_text_of(messages):
    return "\n\n".join(
        content_to_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")
    ).strip()


def collect_call_names(messages):
    """Maps tool_call ids to tool names so tool results can be labelled."""
    names = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            names[tc.get("id")] = (tc.get("function") or {}).get("name") or "tool"
    return names


# ==============================================================================
# Tools
# ==============================================================================

def normalize_tools(client_tools):
    """OpenAI tool definitions -> [{name, description, parameters}]."""
    tools = []
    for t in client_tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name or (TOOL_EXCLUDE_RE and TOOL_EXCLUDE_RE.search(name)):
            continue
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}}
        tools.append({"name": name, "description": (fn.get("description") or "").strip(), "parameters": params})
    return tools


def single_string_arg(tool):
    """Name of the tool's only required string argument (e.g. apply_patch -> "input"), if any."""
    params = tool["parameters"]
    props = params.get("properties") or {}
    required = params.get("required") or list(props)
    strings = [k for k in required if "string" in _schema_types(props.get(k) or {})]
    return strings[0] if len(strings) == 1 else None


def find_command_tool(tools):
    """Returns (tool_name, argument_name, takes_list) for the shell tool, or None."""
    for t in tools:
        if "commands" in (t["parameters"].get("properties") or {}):
            return t["name"], "commands", True
    for t in tools:
        props = t["parameters"].get("properties") or {}
        if "command" in props or t["name"] in ("execute_command", "run_command", "bash", "shell", "terminal", "shell_command"):
            return t["name"], "command", False
    return None


def find_patch_tool(tools):
    """Returns (tool_name, argument_name) for an apply_patch style tool, or None."""
    for t in tools:
        if t["name"] == "apply_patch" or "*** Begin Patch" in t["description"]:
            return t["name"], single_string_arg(t) or "input"
    return None


def find_write_tool(tools):
    """Returns (tool_name, path_argument, text_argument) for a tool that creates a file from its
    full text, such as the Cline CLI's editor (path + new_text), or None."""
    for t in tools:
        props = t["parameters"].get("properties") or {}
        text_arg = next((k for k in ("new_text", "content", "file_text") if k in props), None)
        if "path" in props and text_arg:
            return t["name"], "path", text_arg
    return None


def _is_create(args, write_tool):
    """True for a write-tool call that creates a file from its full text (no edit, no insert)."""
    return (isinstance(args, dict) and isinstance(args.get(write_tool[2]), str) and not args.get("old_text")
            and args.get("insert_line") is None)


def _can_split(write_tool, tools):
    """Splitting appends with old_text -> old_text + more, so the tool must take old_text."""
    tool = next((t for t in tools if t["name"] == write_tool[0]), None)
    return bool(tool) and "old_text" in (tool["parameters"].get("properties") or {})


def find_completion_tool(tools):
    for t in tools:
        if t["name"] in ("attempt_completion", "task_complete", "complete_task"):
            return t["name"]
    return None


def _clean_schema(schema):
    if isinstance(schema, dict):
        return {
            k: _clean_schema(v) for k, v in schema.items()
            if k != "$schema" and not (k == "additionalProperties" and v is False)
        }
    if isinstance(schema, list):
        return [_clean_schema(x) for x in schema]
    return schema


def render_tool_catalog(tools):
    blocks = []
    for t in tools:
        schema = json.dumps(_clean_schema(t["parameters"]), ensure_ascii=False, separators=(",", ":"))
        blocks.append(f"### {t['name']}\n{t['description']}\nArguments (JSON Schema): {schema}")
    return "\n\n".join(blocks)


def format_tool_call_block(name, args):
    body = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
    fence = "````" if re.search(r"^[ \t]*```", body, re.M) else "```"
    return f"{fence}tool_call\n{body}\n{fence}"


def format_file_block(name, path, content):
    """A raw file block (```editor /abs/path), the format the model is asked to write new files in."""
    fence = "````" if re.search(r"^[ \t]*```", content, re.M) else "```"
    return f"{fence}{name} {path}\n{content.rstrip(chr(10))}\n{fence}"


def _line_chunks(text, limit):
    """Splits text after newlines into pieces of at most limit characters (None if a line is longer)."""
    lines = text.split("\n")
    pieces = [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    chunks, current = [], ""
    for piece in pieces:
        if len(piece) > limit:
            return None
        if current and len(current) + len(piece) > limit:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)
    return chunks


def _append_anchor(written, max_chars=800):
    """The shortest run of whole lines at the end of written that occurs in it only once."""
    lines = written.split("\n")
    for k in range(2, len(lines) + 1):
        anchor = "\n".join(lines[-k:])
        if len(anchor) > max_chars:
            return None
        if len(anchor.strip()) >= 20 and written.count(anchor) == 1:
            return anchor
    return None


def split_large_writes(calls, tools):
    """Splits a new file bigger than WRITE_CHUNK_CHARS into a create call plus appends.

    Each append replaces the unique last lines written so far (old_text) with those lines plus the
    next piece; Cline runs the calls of one reply in order. If the file already existed, the create
    fails and the appends find no match, so an existing file is never changed by mistake.
    """
    write = find_write_tool(tools)
    if not write or not _can_split(write, tools):
        return calls
    name, path_arg, text_arg = write
    out = []
    for call_name, args in calls:
        chunks = None
        if call_name == name and _is_create(args, write) and len(args[text_arg]) > WRITE_CHUNK_CHARS:
            chunks = _line_chunks(args[text_arg], WRITE_CHUNK_CHARS)
        appends, written = [], chunks[0] if chunks else ""
        for chunk in (chunks or [])[1:]:
            anchor = _append_anchor(written)
            if anchor is None:
                appends = None
                break
            appends.append((name, {path_arg: args[path_arg], "old_text": anchor, text_arg: anchor + chunk}))
            written += chunk
        if not chunks or appends is None:
            out.append((call_name, args))
            continue
        out.append((name, {**args, text_arg: chunks[0]}))
        out.extend(appends)
    return out


def merge_split_writes(calls, write_tool):
    """Undoes split_large_writes for the history, so the model sees (and copies) one block per file."""
    merged = []
    for name, args in calls:
        prev = merged[-1] if merged else None
        if (write_tool and prev and name == prev[0] == write_tool[0] and _is_create(prev[1], write_tool)
                and isinstance(args, dict) and args.get(write_tool[1]) == prev[1].get(write_tool[1])):
            old, new, written = args.get("old_text"), args.get(write_tool[2]), prev[1][write_tool[2]]
            if isinstance(old, str) and old and isinstance(new, str) and written.endswith(old) and new.startswith(old):
                prev[1][write_tool[2]] = written + new[len(old):]
                continue
        merged.append((name, dict(args) if isinstance(args, dict) else args))
    return merged


def _example_calls(tools, workspace):
    names = {t["name"] for t in tools}
    ws = workspace.rstrip("/")
    examples = []
    if "read_files" in names:
        examples.append(("read_files", {"files": [{"path": f"{ws}/package.json"}, {"path": f"{ws}/src/index.js", "start_line": 1, "end_line": 150}]}))
    cmd = find_command_tool(tools)
    if cmd:
        name, key, takes_list = cmd
        commands = [f'cd "{ws}" && git status --short', f'cd "{ws}" && ls -la']
        examples.append((name, {key: commands} if takes_list else {key: " && ".join(commands)}))
    if "search_codebase" in names:
        examples.append(("search_codebase", {"queries": ["function handleLogin", "TODO"]}))
    if not examples:
        t = tools[0]
        props = t["parameters"].get("properties") or {}
        examples.append((t["name"], {k: "..." for k in (t["parameters"].get("required") or list(props)[:1])}))
    return "\n\n".join(format_tool_call_block(n, a) for n, a in examples[:3])


# ==============================================================================
# Prompts
# ==============================================================================

def turn_reminder(tools):
    done = find_completion_tool(tools)
    if done:
        return (f"[proxy] Reply with ```tool_call blocks to act (several at once when they are independent). "
                f"When the task is complete and verified, call {done}.")
    return ("[proxy] Reply with ```tool_call blocks to act (several at once when they are independent), "
            "or, only when the task is complete and verified, a final answer with no tool calls.")


def build_agent_directive(workspace, tools):
    names = {t["name"] for t in tools}
    cmd = find_command_tool(tools)
    patch = find_patch_tool(tools)
    write = find_write_tool(tools)
    done = find_completion_tool(tools)
    ws = workspace.rstrip("/")

    raw_rules = []
    if patch:
        raw_rules.append(
            f"For {patch[0]} you can skip the JSON: put the raw patch in a block with the info string "
            f"`apply_patch` (no escaping needed):\n\n"
            f"```apply_patch\n*** Begin Patch\n*** Update File: {ws}/src/server.js\n@@\n"
            f"-const port = 3000;\n+const port = process.env.PORT || 3000;\n*** End Patch\n```\n"
        )
    if write:
        whole = (f"Put each new file in ONE such block, however long it is: the proxy splits big files into "
                 f"several {write[0]} calls for you, so never split a file into parts yourself. "
                 if _can_split(write, tools) else "")
        raw_rules.append(
            f"To create a file with {write[0]}, skip the JSON: put the file's raw contents in a block whose info "
            f"string is `{write[0]}` followed by the file's absolute path (no escaping needed):\n\n"
            f"```{write[0]} {ws}/index.html\n<!DOCTYPE html>\n<html lang=\"en\">\n"
            f"<body><h1 class=\"title\">Hello</h1></body>\n</html>\n```\n\n"
            f"   {whole}Change existing files with normal tool_call blocks.\n"
        )
    extra_rules = "".join(f"{i}. {rule}" for i, rule in enumerate(raw_rules, 6))

    if done:
        loop = (
            "Every reply is a working step: one or two sentences on what you're doing next, then the tool_call blocks. "
            f"When the task is fully done and verified, call {done} with your summary. A reply without any tool call "
            "stops the agent and hands control back to the user, so only do that to ask the user something."
        )
    else:
        ask = " (use ask_question for that)" if "ask_question" in names else ""
        loop = (
            "Every reply is one of two kinds:\n"
            "- **Working step**: one or two sentences on what you're doing next, then the tool_call blocks.\n"
            "- **Final answer**: no tool_call blocks at all. A reply without tool calls ENDS the task and hands "
            "control back to the user. Send it only when the task is fully done and verified, or when you are "
            f"blocked on a decision only the user can make{ask}."
        )

    if patch:
        edit_rule = (f"Edit files with {patch[0]}: small, targeted hunks with enough context lines; "
                     "`*** Add File:` for new files. Re-read a file before patching it if it may have changed.")
    elif write:
        edit_rule = (f"Create new files with raw ```{write[0]} blocks. Change existing files with small, targeted "
                     "edits instead of rewriting them, and re-read a file before editing it if it may have changed.")
    else:
        edit_rule = "Edit files with the editing tools listed below, in small, targeted changes."

    shell = ""
    if cmd:
        quote = ("Quote every path: the workspace path contains spaces."
                 if " " in ws else "Quote paths that contain spaces or special characters.")
        shell = (
            f"\n## Shell rules ({cmd[0]})\n"
            "- Linux bash only. Never PowerShell or cmd.exe syntax.\n"
            "- Commands must finish on their own and never wait for input: use -y/--yes, CI=1, "
            "git --no-pager, and never open editors or pagers.\n"
            f"- {quote}\n"
            "- Start dev servers and watchers in the background, e.g. `nohup npm run dev > /tmp/dev.log 2>&1 &`, "
            "then read the log.\n"
            "- Ask before destructive actions the user didn't request: deleting user files, rm -rf outside "
            "build output, git push --force, git reset --hard, dropping databases, sudo or system-wide changes.\n"
        )

    final = "" if done else (
        "\n## Final answer\nSummarize what you changed (which files), how you verified it (commands and "
        "results), and anything the user still needs to do. Keep it short.\n"
    )

    return f"""# You are connected to the user's computer

You are the model behind a coding agent running in the user's VS Code on Linux. It works the same way as Claude Code, Codex CLI and Antigravity. This is not an ordinary chat: a proxy on the user's machine sends you this conversation, runs the tool calls in your reply on the user's real computer, and sends the results back in the next message. The tools listed below give you real access to the file system and a real bash terminal. Use them, and never say you can't access the user's files or run commands.

Workspace: {ws}
Shell: Linux, bash

## How to call a tool
Write a fenced code block with the info string `tool_call` containing one JSON object with "name" and "arguments":

{_example_calls(tools, ws)}

1. One JSON object per block. Use only the tool names and argument names from "Available tools".
2. The JSON must be valid: double-quoted keys and strings, newlines inside strings written as \\n, inner double quotes escaped as \\", no comments, no trailing commas.
3. Batch independent work: put several tool_call blocks in one reply, or use the list arguments of tools that accept lists. They run in order and all the results come back together.
4. If an argument contains a line that starts with three backticks, open the block with four backticks (````tool_call) and close it with four.
5. After your last tool_call block, stop writing. Never predict or invent tool results; they arrive in the next message.
{extra_rules}
## The agent loop
{loop}

Work like a senior engineer:
1. Explore before you change anything: list, search and read the relevant files. Never guess what a file contains; read it.
2. Act, don't narrate: never say you'll do something without the tool call that does it in the same reply.
3. {edit_rule}
4. Verify your work: run the build, tests, linter or the program itself, read the output, fix what fails and run it again.
5. Keep going until the whole task is done. Don't stop after one step to ask whether to continue.
6. When a tool call fails, read the error and adjust. If the same approach fails twice, try a different one.
7. Be truthful: say something was created, ran or passed only if a tool result in this conversation shows it.
8. Never ask the user to run commands, create files, download anything or paste output. Do it yourself with the tools: files the user asks for must be written on their machine with tool calls.
9. If the agent app says you are in plan mode, only explore and present a plan: no edits and no state-changing commands until the user switches to act mode.
{shell}{final}
## Available tools
{render_tool_catalog(tools)}"""


def render_assistant(msg, write_tool=None):
    text = re.sub(r"<thinking>.*?</thinking>\s*", "", content_to_text(msg.get("content")), flags=re.S).strip()
    parts = [text] if text else []
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        calls.append((fn.get("name"), args if args is not None else {}))
    for name, args in merge_split_writes(calls, write_tool):
        if write_tool and name == write_tool[0] and _is_create(args, write_tool):
            # The file is on disk; repeating all of it would crowd Perchance's small context.
            content = args[write_tool[2]]
            if len(content) > HISTORY_FILE_CHARS:
                head = HISTORY_FILE_CHARS * 2 // 3
                content = (f"{content[:head]}\n[... {len(content) - HISTORY_FILE_CHARS} more characters of this "
                           f"file, not repeated here ...]\n{content[-(HISTORY_FILE_CHARS - head):]}")
            parts.append(format_file_block(name, args.get(write_tool[1]), content))
        else:
            parts.append(format_tool_call_block(name, args))
    return "\n\n".join(parts) or "(empty reply)"


def render_message(msg, call_names, result_limit, write_tool=None):
    role = msg.get("role")
    text = content_to_text(msg.get("content"))
    if role == "assistant":
        return f"=== ASSISTANT (you) ===\n{render_assistant(msg, write_tool)}"
    if role in ("tool", "function"):
        call_id = msg.get("tool_call_id") or ""
        name = call_names.get(call_id) or msg.get("name") or "tool"
        body = truncate_middle(text, result_limit)
        return f"=== TOOL RESULT: {name} (id {call_id}) ===\n{body}\n=== END TOOL RESULT ==="
    if role in ("system", "developer"):
        return f"=== NOTE FROM THE AGENT APP ===\n{text}"
    return f"=== USER ===\n{text}"


def render_conversation(convo, call_names, budget, write_tool=None):
    """Renders the conversation, shrinking old tool results (then old messages) to fit the budget."""
    recent = 6
    parts = []
    for old_limit in (MAX_TOOL_RESULT_CHARS, 8000, 2000, 400):
        parts = [
            render_message(m, call_names, MAX_TOOL_RESULT_CHARS if i >= len(convo) - recent else old_limit, write_tool)
            for i, m in enumerate(convo)
        ]
        if sum(len(p) + 2 for p in parts) <= budget:
            return "\n\n".join(parts)
    first, rest, dropped = parts[:1], parts[1:], 0
    while len(rest) > 1 and sum(len(p) + 2 for p in first + rest) > budget:
        rest.pop(0)
        dropped += 1
    note = [f"[... {dropped} earlier messages omitted to fit the prompt size limit ...]"] if dropped else []
    return "\n\n".join(first + note + rest)


def build_full_prompt(messages, tools):
    """The whole conversation as one instruction, since Perchance is stateless."""
    system_text = system_text_of(messages)
    workspace = extract_workspace_dir(system_text)
    convo = [m for m in messages if m.get("role") not in ("system", "developer")]
    if tools:
        head = build_agent_directive(workspace, tools)
        if system_text:
            head += ("\n\n# Instructions from the agent app\nFollow these as well. Wherever they talk about "
                     "calling tools, use the tool_call block format described above.\n\n" + system_text)
        tail = turn_reminder(tools)
    else:
        head = f"# Instructions\n{system_text}" if system_text else ""
        tail = "Reply to the last USER message above."
    budget = MAX_PROMPT_CHARS - len(head) - len(tail) - 100
    body = render_conversation(convo, collect_call_names(messages), budget, find_write_tool(tools) if tools else None)
    return "\n\n".join(p for p in (head, "# Conversation so far", body, tail) if p)


# ==============================================================================
# Parsing the model's reply
# ==============================================================================

class ToolCallError(ValueError):
    pass


class ParsedReply:
    def __init__(self, prose, calls, errors):
        self.prose = prose
        self.calls = calls        # [(name, arguments_dict)]
        self.errors = errors      # [str]


FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
TOOL_BLOCK_LANGS = {"tool_call", "tool_calls", "toolcall", "tool", "tool_use", "function_call"}
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_UNFENCED_CALL_RE = re.compile(r'\{\s*"(?:name|tool)"\s*:\s*"([\w.-]+)"')
_ARG_KEYS = ("arguments", "args", "parameters", "params")
_META_KEYS = {"name", "tool", "tool_name", "type", "id", "function", "recipient_name"}
_LEADING_HEADER_RE = re.compile(r"^\s*=== ASSISTANT[^\n]*===[ \t]*\n")
_INVENTED_TURN_RE = re.compile(r"^=== (?:TOOL RESULT|USER\b|NOTE FROM)", re.M)


def cut_invented_turns(text):
    """Drops a leading "=== ASSISTANT ===" header and anything after the model starts writing
    a tool result or user turn itself (it should stop after its tool calls)."""
    text = _LEADING_HEADER_RE.sub("", text or "", count=1)
    m = _INVENTED_TURN_RE.search(text)
    return text[:m.start()].rstrip() if m else text


def split_fenced(text):
    """Splits markdown into ("text", str) and ("code", info, body, raw, closed) segments (CommonMark
    fences). closed is False when the text ends inside the block."""
    lines = text.split("\n")
    segments, prose, i = [], [], 0
    while i < len(lines):
        m = FENCE_OPEN_RE.match(lines[i])
        if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
            prose.append(lines[i])
            i += 1
            continue
        fence, info = m.group(1), m.group(2)
        close = re.compile(r"^[ \t]{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        if prose:
            segments.append(("text", "\n".join(prose)))
            prose = []
        segments.append(("code", info, "\n".join(lines[i + 1:j]), "\n".join(lines[i:j + 1]), j < len(lines)))
        i = j + 1
    if prose:
        segments.append(("text", "\n".join(prose)))
    return segments


def _repair_json(text):
    """Fixes two slips the model makes at the end of long calls: an escape like \\n written between
    tokens (`..."}\\n}}`) and missing closing brackets. Returns None if nothing could be fixed."""
    out, closers, in_string, escaped, i = [], [], False, False, 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == "\\" and text[i + 1:i + 2] in ("n", "r", "t"):
            i += 2
            continue
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            closers.append("}" if ch == "{" else "]")
        elif ch in "}]" and closers:
            closers.pop()
        out.append(ch)
        i += 1
    if in_string:
        return None
    repaired = "".join(out) + "".join(reversed(closers))
    return repaired if repaired != text else None


def _decode_json_values(body):
    body = body.strip().lstrip("﻿")
    error = None
    candidates = [body, _TRAILING_COMMA_RE.sub(r"\1", body)]
    repaired = _repair_json(candidates[1])
    if repaired:
        candidates.append(repaired)
    for candidate in candidates:
        try:
            decoder = json.JSONDecoder(strict=False)
            values, i = [], 0
            while True:
                while i < len(candidate) and candidate[i] in " \t\r\n,":
                    i += 1
                if i >= len(candidate):
                    break
                value, i = decoder.raw_decode(candidate, i)
                values.append(value)
            if not values:
                raise ValueError("the block is empty")
            return values
        except ValueError as e:
            error = error or e
    raise ToolCallError(f"invalid JSON ({error})")


def _flatten_call_items(values):
    items = []
    for v in values:
        if isinstance(v, list):
            items.extend(v)
        elif isinstance(v, dict) and isinstance(v.get("tool_calls"), list) and "name" not in v:
            items.extend(v["tool_calls"])
        else:
            items.append(v)
    return items


def _json_type(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def _schema_types(spec):
    t = spec.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return set(t)
    types = set()
    for sub in spec.get("anyOf") or spec.get("oneOf") or []:
        if isinstance(sub, dict):
            types |= _schema_types(sub)
    return types


def _coerce_args(args, schema, name):
    """Fixes common slips (string for a list, singular/plural key) and checks required arguments."""
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    args = dict(args)
    for key in required:
        if key not in args:
            for alt in (key + "s", key[:-1] if key.endswith("s") else None):
                if alt and alt in args and alt not in props:
                    args[key] = args.pop(alt)
                    break
    for key, spec in props.items():
        if key not in args or not isinstance(spec, dict):
            continue
        value = args[key]
        types = _schema_types(spec)
        if "array" in types and not isinstance(value, list) and value is not None and _json_type(value) not in types:
            value = [value]
        if isinstance(value, list):
            item_spec = spec.get("items") or {}
            if "object" in _schema_types(item_spec) and "path" in (item_spec.get("properties") or {}):
                value = [{"path": v} if isinstance(v, str) else v for v in value]
        if types == {"string"} and isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        args[key] = value
    missing = [k for k in required if k not in args]
    if missing:
        raise ToolCallError(f'{name}: missing required argument(s): {", ".join(missing)}')
    return args


def _item_to_call(item, explicit_name, tools_by_name):
    if not isinstance(item, dict):
        raise ToolCallError('expected a JSON object like {"name": "...", "arguments": {...}}')
    fn = item.get("function") if isinstance(item.get("function"), dict) else {}
    name = (item.get("name") or item.get("tool") or item.get("tool_name") or fn.get("name")
            or item.get("recipient_name") or explicit_name)
    if isinstance(name, str):
        name = name.split(".")[-1] if name.startswith("functions.") else name
    if not name:
        raise ToolCallError('missing "name"')
    if name not in tools_by_name:
        raise ToolCallError(f'unknown tool "{name}" (available: {", ".join(tools_by_name)})')
    tool = tools_by_name[name]

    if explicit_name and not any(k in item for k in ("name", "tool", "tool_name", "function", "recipient_name")):
        args = item
    elif fn and "arguments" in fn:
        args = fn["arguments"]
    else:
        key = next((k for k in _ARG_KEYS if k in item), None)
        if key:
            args = item[key]
        elif isinstance(item.get("input"), dict):
            args = item["input"]
        else:
            args = {k: v for k, v in item.items() if k not in _META_KEYS}
    if isinstance(args, str):
        try:
            args = json.loads(args, strict=False)
        except ValueError:
            key = single_string_arg(tool)
            if not key:
                raise ToolCallError(f'{name}: "arguments" must be a JSON object')
            args = {key: args}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolCallError(f'{name}: "arguments" must be a JSON object')
    return name, _coerce_args(args, tool["parameters"], name)


def _patch_call(body, patch_tool):
    name, key = patch_tool
    patch = body.strip()
    if not patch.startswith("*** Begin Patch"):
        raise ToolCallError(f"{name}: the patch must start with '*** Begin Patch'")
    if "*** End Patch" not in patch:
        raise ToolCallError(f"{name}: no '*** End Patch' line. If the patch contains ``` lines, fence the block with four backticks")
    return name, {key: patch + "\n"}


def _file_path(text):
    """The path from a raw file block's info string (```editor /abs/path), or None."""
    path = (text or "").strip().strip("\"'`")
    return path if path and not path.startswith("{") and ("/" in path or "." in path) else None


def _tidy(text):
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _find_unfenced_calls(text, tools_by_name):
    calls, spans, pos = [], [], 0
    decoder = json.JSONDecoder(strict=False)
    for m in _UNFENCED_CALL_RE.finditer(text):
        if m.start() < pos or m.group(1).split(".")[-1] not in tools_by_name:
            continue
        try:
            value, end = decoder.raw_decode(text, m.start())
            calls.append(_item_to_call(value, None, tools_by_name))
        except (ValueError, ToolCallError):
            continue
        spans.append((m.start(), end))
        pos = end
    return calls, spans


def parse_model_reply(text, tools):
    """Splits a model reply into prose and tool calls, collecting errors for anything malformed."""
    text = (text or "").replace("\r\n", "\n")
    if not tools:
        return ParsedReply(_tidy(text), [], [])
    by_name = {t["name"]: t for t in tools}
    patch_tool = find_patch_tool(tools)
    write_tool = find_write_tool(tools)

    calls, errors, pieces = [], [], []
    block_no = 0
    for seg in split_fenced(text):
        if seg[0] == "text":
            pieces.append(("text", seg[1]))
            continue
        _, info, body, raw, closed = seg
        words = re.split(r"[\s:]+", info.strip(), maxsplit=1) if info.strip() else [""]
        lang = words[0].lower()
        explicit = words[1].strip() if len(words) > 1 and words[1].strip() else None
        starts_patch = body.lstrip().startswith("*** Begin Patch")

        if patch_tool and starts_patch and (lang in TOOL_BLOCK_LANGS or lang.startswith("tool_call")
                                            or lang in (patch_tool[0], "apply_patch", "patch", "diff", "")):
            kind = "patch"
        elif write_tool and lang in (write_tool[0], "write_file") and _file_path(explicit):
            kind = "file"
        elif lang in TOOL_BLOCK_LANGS or lang.startswith("tool_call"):
            kind = "json"
        elif lang in by_name:
            kind, explicit = "json", lang
        elif lang in ("json", "jsonc", ""):
            kind = "maybe_json"
        else:
            kind = "prose"

        if kind == "maybe_json":
            try:
                items = _flatten_call_items(_decode_json_values(body))
                maybe = [i for i in items if isinstance(i, dict)
                         and str(i.get("name") or i.get("tool") or "").split(".")[-1] in by_name]
                kind = "json" if items and len(maybe) == len(items) else "prose"
            except ToolCallError:
                kind = "prose"
        if kind == "prose":
            pieces.append(("text", raw))
            continue

        block_no += 1
        pieces.append(("tool", None))
        try:
            if kind == "patch":
                calls.append(_patch_call(body, patch_tool))
            elif kind == "file":
                name, path_arg, text_arg = write_tool
                if not closed:
                    # Raw content has no syntax to prove it is complete; only the closing fence does.
                    raise ToolCallError(f"{name} {_file_path(explicit)}: the block was cut off before its closing "
                                        "fence, so the file was not written")
                calls.append((name, {path_arg: _file_path(explicit), text_arg: body + "\n"}))
            else:
                items = _flatten_call_items(_decode_json_values(body))
                for item in items:
                    calls.append(_item_to_call(item, explicit, by_name))
        except ToolCallError as e:
            errors.append(f"tool_call block {block_no}: {e}")

    if block_no == 0:
        unfenced, spans = _find_unfenced_calls(text, by_name)
        if unfenced:
            return ParsedReply(_tidy(text[:spans[0][0]]), unfenced, [])
        return ParsedReply(_tidy(text), [], [])

    last_tool = max(i for i, p in enumerate(pieces) if p[0] == "tool")
    prose = "\n".join(p[1] for p in pieces[:last_tool] if p[0] == "text")
    return ParsedReply(_tidy(prose), calls, errors)


# ==============================================================================
# Follow-ups when a reply can't be used as is
# ==============================================================================

def repair_prompt(errors):
    return (
        "[proxy] Nothing from your last reply was run, because some tool calls could not be parsed:\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n\nSend the whole step again with corrected tool_call blocks: valid JSON, and exact tool names and "
          "argument names from the tool list. Don't apologize or explain, just resend it."
    )


NUDGE_PROMPT = (
    "[proxy] Your reply had no tool_call blocks, so nothing was run and the task would end here. "
    "If there is still work to do (reading files, running commands, editing), send those tool_call blocks now. "
    "The tools really run on the user's machine, so never ask the user to run things. "
    "If the task is complete and verified, reply with just the final summary."
)

_PROMISE_RE = re.compile(r"\b(?:I'll|I will|I am going to|I'm going to|let me(?! know)|let's|next,? I|now I|I'm now)\b", re.I)
_NO_ACCESS_RE = re.compile(
    r"\bI (?:cannot|can't|don't|do not|am unable to|'m unable to|have no)\b[^.\n]{0,40}"
    r"\b(?:access|read|open|see|run|execute|modify|edit|write|create)\b[^.\n]{0,60}"
    r"\b(?:your|local|files?|file ?system|terminal|machine|computer|workspace|commands?|project|repo)", re.I)
_USER_RUN_RE = re.compile(
    r"\b(?:run (?:this|these|the following|it)\b|you can run\b|in your terminal\b|"
    r"(?:paste|copy) (?:this|it|the following)\b|save (?:this|it|the following) (?:as|to|in|into)\b)", re.I)


def needs_nudge(prose):
    """True when a reply without tool calls looks like the model meant to act, or refused to."""
    text = prose.replace("’", "'").strip()
    if not text:
        return True
    if _NO_ACCESS_RE.search(text) or _USER_RUN_RE.search(text):
        return True
    last = text.rsplit("\n\n", 1)[-1].strip()
    if last.endswith("?") or last.lower().startswith(("if ", "when ", "whenever ")) or len(last) > 400:
        return False
    return bool(_PROMISE_RE.search(last))
