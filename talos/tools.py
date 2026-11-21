"""The built-in tools. Names follow OpenCode (read, write, edit, patch, bash, grep, glob, list,
webfetch, websearch, todowrite, todoread, question, task, skill, lsp) and Hermes (memory,
session_search, cronjob, execute_code, send_message, skill_manage, image_generate,
text_to_speech, process), so prompts and agent files written for either keep working.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import backends, knowledge, permissions, skills, util

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next",
             "target", ".mypy_cache", ".pytest_cache"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
OUTPUT_LIMIT = 30000
TOOLS = {}


class Tool:
    def __init__(self, name, kind, description, params, handler):
        self.name, self.kind, self.description, self.handler = name, kind, description, handler
        properties, required = {}, []
        for key, spec in params.items():
            kind_name, _, text = spec.partition("|")
            optional = key.endswith("?")
            key = key.rstrip("?")
            properties[key] = {"type": kind_name, "description": text}
            if kind_name == "array":
                properties[key]["items"] = {"type": "object"}
            if not optional:
                required.append(key)
        self.schema = {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        }}


def tool(name, kind, description, /, **params):
    def register(handler):
        TOOLS[name] = Tool(name, kind, description, {
            key.replace("_opt", "?"): value for key, value in params.items()
        }, handler)
        return handler
    return register


class Context:
    """Everything a tool may touch during one agent run."""

    def __init__(self, config, store, session, gate, agent=None, abort=None, emit=None, ask=None):
        self.config, self.store, self.session, self.gate = config, store, session, gate
        self.agent, self.ask = agent, ask
        self.cwd = Path(config.get("_cwd", ".")).resolve()
        self.abort = abort or threading.Event()
        self.emit = emit or (lambda event, data: None)
        self.backend = backends.make(config.get("backend"))
        self.todos, self.attachments, self.processes = [], [], {}
        self.edits, self.checks, self.reread = [], [], False

    def path(self, name) -> Path:
        path = Path(os.path.expanduser(str(name)))
        return (path if path.is_absolute() else self.cwd / path).resolve()


# ---------------------------------------------------------------- files

def _after_edit(ctx, path: Path) -> str:
    """Format the file, then report problems the language tools see in it."""
    from . import lsp
    ctx.edits.append(str(path))
    ctx.checks.clear()
    ctx.reread = False
    notes = [note for note in (lsp.format_file(ctx.config, path), lsp.diagnostics(ctx.config, path)) if note]
    return ("\n" + "\n".join(notes)) if notes else ""


def _save(ctx, path: Path, text) -> None:
    existed = path.exists()
    ctx.store.record_change(ctx.session, path, path.read_bytes() if existed else None, existed)
    if text is None:
        path.unlink()
    else:
        util.write_text(path, text)


@tool("read", "read", "Read a text file with line numbers. Images are attached so you can see them.",
      filePath="string|Path of the file", offset_opt="integer|First line to read, starting at 0",
      limit_opt="integer|How many lines (default 2000)")
def read(ctx, filePath, offset=0, limit=2000):
    path = ctx.path(filePath)
    if path.is_dir():
        return f"{path} is a folder. Use the list tool."
    if not path.exists():
        near = [item.name for item in path.parent.glob("*")][:30] if path.parent.is_dir() else []
        close = [name for name in near if name.lower().startswith(path.name.lower()[:3])]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"File not found: {path}.{hint}"
    if path.suffix.lower() in IMAGE_TYPES:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        ctx.attachments.append({"type": "image_url", "image_url": {
            "url": f"data:{IMAGE_TYPES[path.suffix.lower()]};base64,{encoded}"}})
        return f"Image {path.name} is attached to the next message."
    data = path.read_bytes()
    if b"\x00" in data[:8000]:
        return f"{path} is a binary file ({len(data)} bytes); it cannot be shown as text."
    lines = data.decode("utf-8", errors="replace").splitlines()
    offset, limit = int(offset or 0), int(limit or 2000)
    chosen = lines[offset:offset + limit]
    if str(path) in ctx.edits:
        ctx.reread = True
    body = "\n".join(f"{number:>5}| {line[:2000]}" for number, line in enumerate(chosen, offset + 1))
    more = f"\n({len(lines) - offset - len(chosen)} more lines)" if offset + len(chosen) < len(lines) else ""
    return body + more if body else "(empty file)"


@tool("write", "edit", "Create a file or replace its whole content.",
      filePath="string|Path of the file", content="string|The complete new content")
def write(ctx, filePath, content):
    path = ctx.path(filePath)
    existed = path.exists()
    _save(ctx, path, content)
    return f"{'Replaced' if existed else 'Created'} {path} ({len(content.splitlines())} lines)." + _after_edit(ctx, path)


def _spans(text: str, old: str) -> list:
    """Where `old` occurs in `text`: exact first, then ignoring indentation and line ends."""
    exact = [(match.start(), match.end()) for match in re.finditer(re.escape(old), text)]
    if exact or not old.strip():
        return exact
    lines = text.splitlines(keepends=True)
    wanted = [line.strip() for line in old.strip("\n").splitlines()]
    starts, position = [], 0
    for line in lines:
        starts.append(position)
        position += len(line)
    starts.append(position)
    found = []
    for index in range(len(lines) - len(wanted) + 1):
        if all(lines[index + step].strip() == wanted[step] for step in range(len(wanted))):
            end = starts[index + len(wanted)]
            if text[end - 1:end] == "\n" and not old.endswith("\n"):
                end -= 1
            found.append((starts[index], end))
    return found


def _replace(text: str, old: str, new: str, every=False):
    spans = _spans(text, old)
    if not spans:
        return None, "oldString was not found. Read the file again and copy the exact lines."
    if len(spans) > 1 and not every:
        return None, f"oldString matches {len(spans)} places. Add more lines around it, or set replaceAll."
    for start, end in reversed(spans if every else spans[:1]):
        text = text[:start] + new + text[end:]
    return text, ""


@tool("edit", "edit", "Replace one exact piece of text in a file with another.",
      filePath="string|Path of the file", oldString="string|Text to replace, copied from the file",
      newString="string|Text to put there", replaceAll_opt="boolean|Replace every occurrence")
def edit(ctx, filePath, oldString, newString, replaceAll=False):
    path = ctx.path(filePath)
    if not path.exists():
        return f"File not found: {path}. Use write to create it."
    if oldString == newString:
        return "oldString and newString are the same; nothing to change."
    text, problem = _replace(util.read_text(path), oldString, newString, bool(replaceAll))
    if text is None:
        return problem
    _save(ctx, path, text)
    return f"Edited {path}." + _after_edit(ctx, path)


@tool("patch", "edit", "Apply a patch that adds, updates, moves or deletes several files at once. "
      "Format: *** Begin Patch / *** Add File: p / *** Update File: p / *** Delete File: p / *** End Patch; "
      "in a file, lines start with '+', '-' or ' ', hunks are separated by '@@'.",
      patchText="string|The whole patch")
def patch(ctx, patchText):
    sections, current = [], None
    for line in patchText.splitlines():
        header = re.match(r"\*\*\* (Add File|Update File|Delete File|Move to): (.+)", line)
        if header:
            if header.group(1) == "Move to" and current:
                current["move"] = header.group(2).strip()
            else:
                current = {"op": header.group(1), "path": header.group(2).strip(), "hunks": [[]]}
                sections.append(current)
        elif line.startswith("*** "):
            continue
        elif current is not None:
            if line.startswith("@@"):
                current["hunks"].append([])
            else:
                current["hunks"][-1].append(line)
    if not sections:
        return "No '*** Add File', '*** Update File' or '*** Delete File' section found in the patch."
    planned = []
    for section in sections:
        path = ctx.path(section["path"])
        if section["op"] == "Add File":
            lines = [line[1:] for hunk in section["hunks"] for line in hunk if line.startswith("+")]
            planned.append((path, "\n".join(lines) + "\n", None))
        elif section["op"] == "Delete File":
            if not path.exists():
                return f"Cannot delete {path}: it does not exist. Nothing was changed."
            planned.append((path, None, None))
        else:
            if not path.exists():
                return f"Cannot update {path}: it does not exist. Nothing was changed."
            text = util.read_text(path)
            for hunk in section["hunks"]:
                old = "\n".join(line[1:] for line in hunk if line[:1] in (" ", "-"))
                new = "\n".join(line[1:] for line in hunk if line[:1] in (" ", "+"))
                if not old and not new:
                    continue
                text, problem = _replace(text, old, new)
                if text is None:
                    return f"{path}: {problem} Nothing was changed."
            planned.append((path, text, section.get("move")))
    notes = []
    for path, text, move in planned:           # all hunks matched, so now write
        if move:
            _save(ctx, path, None)
            path = ctx.path(move)
        _save(ctx, path, text)
        notes.append(f"{'Deleted' if text is None else 'Wrote'} {path}." + (
            _after_edit(ctx, path) if text is not None else ""))
    return "\n".join(notes)


def _walk(root: Path, include=""):
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        for name in sorted(files):
            if not include or fnmatch.fnmatch(name, include):
                yield Path(folder) / name


@tool("grep", "read", "Search file contents with a regular expression.",
      pattern="string|Regular expression", path_opt="string|Folder or file to search (default: project)",
      include_opt="string|Only files matching this glob, e.g. *.py")
def grep(ctx, pattern, path="", include=""):
    try:
        regex = re.compile(pattern)
    except re.error as error:
        return f"Bad regular expression: {error}"
    root = ctx.path(path or ".")
    hits = []
    for file in ([root] if root.is_file() else _walk(root, include)):
        try:
            data = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4000] or len(data) > 2_000_000:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                hits.append(f"{file}:{number}: {line.strip()[:300]}")
                if len(hits) >= 100:
                    return "\n".join(hits) + "\n(first 100 matches; narrow the search)"
    return "\n".join(hits) or "No matches."


@tool("glob", "read", "Find files by name pattern, newest first.",
      pattern="string|Glob such as **/*.py", path_opt="string|Folder to search (default: project)")
def glob(ctx, pattern, path=""):
    root = ctx.path(path or ".")
    found = [item for item in root.glob(pattern)
             if item.is_file() and not (set(item.relative_to(root).parts) & SKIP_DIRS)]
    found.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    more = f"\n({len(found) - 100} more)" if len(found) > 100 else ""
    return "\n".join(str(item) for item in found[:100]) + more if found else "No files match."


@tool("list", "read", "List the files in a folder as a tree.",
      path_opt="string|Folder (default: project)")
def list_(ctx, path=""):
    root = ctx.path(path or ".")
    if not root.is_dir():
        return f"Not a folder: {root}"
    lines = [str(root)]
    for count, file in enumerate(_walk(root)):
        if count >= 200:
            lines.append("  ... (more files; list a subfolder)")
            break
        lines.append("  " + str(file.relative_to(root)).replace("\\", "/"))
    return "\n".join(lines)


# ---------------------------------------------------------------- shell

@tool("bash", "bash", "Run a command in the shell named in the system prompt. Use background for servers.",
      command="string|The command line", timeout_opt="integer|Seconds to wait (default 120)",
      workdir_opt="string|Folder to run in (default: project)",
      background_opt="boolean|Start it and return at once; check it with the process tool")
def bash(ctx, command, timeout=120, workdir="", background=False):
    cwd = str(ctx.path(workdir)) if workdir else str(ctx.cwd)
    if background:
        argv, where = ctx.backend.argv(command, cwd)
        log = tempfile.NamedTemporaryFile(prefix="talos-", suffix=".log", delete=False)
        process = subprocess.Popen(argv, cwd=where, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
        identifier = f"p{len(ctx.processes) + 1}"
        ctx.processes[identifier] = {"process": process, "log": log.name, "command": command}
        return f"Started in the background as {identifier}. Use the process tool to read its output."
    code, output = ctx.backend.run(command, cwd, int(timeout or 120), ctx.abort)
    ctx.checks.append({"command": command, "exit": code})
    return f"{util.clip(output.strip(), OUTPUT_LIMIT) or '(no output)'}\n[exit code {code}]"


@tool("process", "bash", "Look at, or stop, commands started in the background.",
      action="string|list, poll or kill", id_opt="string|Process id such as p1")
def process(ctx, action, id=""):
    if action == "list":
        return "\n".join(
            f"{key}: {'running' if item['process'].poll() is None else 'ended'} - {item['command']}"
            for key, item in ctx.processes.items()) or "No background processes."
    item = ctx.processes.get(id)
    if not item:
        return f"No background process '{id}'."
    if action == "kill":
        backends.kill_tree(item["process"])
        return f"Stopped {id}."
    state = item["process"].poll()
    output = util.clip(util.read_text(item["log"]), OUTPUT_LIMIT)
    return f"{output}\n[{'still running' if state is None else f'exit code {state}'}]"


# ---------------------------------------------------------------- web

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@tool("webfetch", "web", "Fetch a web page and return its text.",
      url="string|Address starting with http:// or https://",
      format_opt="string|text (default) or html")
def webfetch(ctx, url, format="text"):
    if not re.match(r"https?://", url):
        return "The address must start with http:// or https://"
    status, body = util.http("GET", url, timeout=30)
    if status >= 400:
        return f"The page answered HTTP {status}."
    text = body if format == "html" else html_to_text(body)
    return util.clip(text, 50000)


@tool("websearch", "web", "Search the web. Returns titles, links and short descriptions.",
      query="string|What to search for")
def websearch(ctx, query):
    base = (ctx.config.get("search") or {}).get("url", "")
    status, body = util.http("GET", base + "?q=" + urllib.parse.quote(query), timeout=30)
    if status >= 400:
        return f"The search service answered HTTP {status}."
    results = []
    links = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|div|td)>', body, re.S)
    for index, (link, title) in enumerate(links[:8]):
        target = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("uddg", [link])[0]
        snippet = html_to_text(snippets[index]) if index < len(snippets) else ""
        results.append(f"{html_to_text(title)}\n{target}\n{snippet}")
    return "\n\n".join(results) or "No results."


# ---------------------------------------------------------------- planning and asking

@tool("todowrite", "other", "Write the task list for this session (replaces the old list).",
      todos="array|Items like {content, status: pending|in_progress|completed}")
def todowrite(ctx, todos):
    ctx.todos = [item for item in todos if isinstance(item, dict)]
    ctx.emit("todos", ctx.todos)
    return todoread(ctx)


@tool("todoread", "other", "Read the task list for this session.")
def todoread(ctx):
    marks = {"completed": "[x]", "in_progress": "[~]"}
    return "\n".join(
        f"{marks.get(item.get('status'), '[ ]')} {item.get('content', '')}" for item in ctx.todos
    ) or "The task list is empty."


@tool("question", "other", "Ask the user a question and wait for the answer.",
      question="string|The question", options_opt="array|Suggested answers")
def question(ctx, question, options=None):
    if not ctx.ask:
        return "Nobody is here to answer. Decide yourself and state the assumption you made."
    return "The user answered: " + str(ctx.ask(question, options or []))


@tool("task", "task", "Hand a self-contained job to a helper agent and get its result. Give 'tasks' "
      "to run several helpers at the same time. 'engine' sends the job to another installed agent.",
      prompt_opt="string|Complete instructions; the helper sees nothing else",
      description_opt="string|Three to five words", subagent_type_opt="string|general, explore, or a custom agent",
      engine_opt="string|opencode, hermes, claude, codex or gemini",
      tasks_opt="array|Several jobs: [{prompt, description, subagent_type, engine}]")
def task(ctx, prompt="", description="", subagent_type="general", engine="", tasks=None):
    jobs = tasks or [{"prompt": prompt, "subagent_type": subagent_type, "engine": engine}]
    jobs = [job for job in jobs if isinstance(job, dict) and job.get("prompt")]
    if not jobs:
        return "Give a prompt, or tasks with prompts."
    if not ctx.agent:
        return "Helpers are not available here."
    results = [None] * len(jobs)

    def work(index, job):
        try:
            if job.get("engine"):
                from . import engines
                results[index] = engines.run(ctx.config, job["engine"], job["prompt"], str(ctx.cwd), ctx.abort)
            else:
                results[index] = ctx.agent.spawn(job["prompt"], job.get("subagent_type") or "general")
        except Exception as error:      # one failed helper must not lose the others
            results[index] = f"The helper failed: {error}"

    for start in range(0, len(jobs), 3):
        threads = [threading.Thread(target=work, args=(index, jobs[index]))
                   for index in range(start, min(start + 3, len(jobs)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    if len(jobs) == 1:
        return str(results[0])
    return "\n\n".join(f"## Result {index + 1}\n{result}" for index, result in enumerate(results))


# ---------------------------------------------------------------- skills, memory, recall

@tool("skill", "other", "Load a skill (instructions for one kind of task) by name.",
      name="string|Skill name from the list in the system prompt")
def skill(ctx, name):
    return skills.load(ctx.cwd, name)


@tool("skill_manage", "other", "Save what you learned as a reusable skill, or fix one of your own skills.",
      action="string|create, patch or delete", name="string|lowercase-with-dashes",
      description_opt="string|When to use it, one sentence", content_opt="string|The instructions (Markdown)",
      old_text_opt="string|For patch: the text to replace")
def skill_manage(ctx, action, name, description="", content="", old_text=""):
    return skills.manage(action, name, description, content, old_text)


@tool("memory", "other", "Keep durable facts across sessions. target 'memory' = things you learned "
      "(environment, conventions, fixes); 'user' = who the user is and what they prefer.",
      action="string|add, replace or remove", target="string|memory or user",
      content_opt="string|The entry", old_text_opt="string|Part of the entry to replace or remove")
def memory(ctx, action, target, content="", old_text=""):
    return skills.memory_edit(action, target, content, old_text)


@tool("session_search", "read", "Search everything said in past sessions.",
      query="string|Words to look for", limit_opt="integer|How many results (default 8)")
def session_search(ctx, query, limit=8):
    hits = ctx.store.search(query, int(limit or 8), skip_session=ctx.session)
    return "\n".join(
        f"{hit['when']} {hit['session']} \"{hit['title']}\" ({hit['role']}): {hit['hit']}" for hit in hits
    ) or "Nothing found in past sessions."


@tool("notes", "read", "Search the user's own notes (their Obsidian or Cairn folder). Returns the "
      "matching notes with their paths; open one with the read tool.",
      query="string|Words that must all be in the note", limit_opt="integer|How many results (default 8)")
def notes(ctx, query, limit=8):
    folders = knowledge.vaults(ctx.config)
    if not folders:
        return ("No notes folder is known. Name one in the config: \"notes\": [\"path to the folder\"].")
    hits = knowledge.search_notes(ctx.config, query, int(limit or 8))
    if not hits:
        return f"No note contains all of: {query}. Try fewer or other words."
    return "\n".join(f"{hit['path']} ({hit['when']}): {hit['hit']}" for hit in hits)


@tool("projects", "read", "List or search the user's own projects (their GitHub repositories and "
      "local project folders): name, what it is, where it is.",
      query_opt="string|Words to look for; leave empty to list all",
      refresh_opt="boolean|Ask GitHub again instead of using the saved list")
def projects(ctx, query="", refresh=False):
    rows = knowledge.find_projects(ctx.config, query or "", bool(refresh))
    if not rows:
        return ("No project matches." if query else
                "No projects found. Sign in with `gh auth login`, or name folders in the config: "
                "\"projects\": {\"folders\": [\"path\"]}.")
    lines = [knowledge.project_line(row) for row in rows[:80]]
    if len(rows) > 80:
        lines.append(f"... and {len(rows) - 80} more; give a query to narrow it down.")
    return "\n".join(lines)


@tool("cronjob", "other", "Schedule a prompt to run later or repeatedly. Schedules: 'in 20m', "
      "'every 2h', 'daily 09:00', 'weekdays 08:30', a 5-field cron line, or a date and time.",
      action="string|create, list, remove, pause, resume or run",
      schedule_opt="string|When to run", prompt_opt="string|What to do, self-contained",
      name_opt="string|Short name", deliver_opt="string|local, or platform:chat such as telegram:12345",
      id_opt="string|Job id for remove, pause, resume, run")
def cronjob(ctx, action, schedule="", prompt="", name="", deliver="local", id=""):
    from . import cron
    if action == "create":
        return cron.create(schedule, prompt, name, deliver, str(ctx.cwd))
    if action == "list":
        return cron.describe()
    if action == "run":
        return cron.run_job(ctx.config, id, ctx.store)
    return cron.change(action, id)


@tool("send_message", "other", "Send a message to a chat on a connected platform.",
      platform="string|telegram, discord, slack, email, ntfy, matrix, mattermost, signal, irc",
      chat="string|Chat, channel or address", text="string|The message")
def send_message(ctx, platform, chat, text):
    from . import gateway
    return gateway.send(ctx.config, platform, chat, text)


# ---------------------------------------------------------------- code, language tools, media

RPC_PRELUDE = '''
import json as _json, urllib.request as _request
def tool(name, **arguments):
    """Call a Talos tool from this script and get its result as text."""
    body = _json.dumps({"token": %r, "name": name, "arguments": arguments}).encode()
    request = _request.Request(%r, data=body, headers={"Content-Type": "application/json"})
    opener = _request.build_opener(_request.ProxyHandler({}))
    with opener.open(request, timeout=300) as response:
        return _json.loads(response.read())["result"]
'''
SCRIPT_TOOLS = {"read", "write", "edit", "grep", "glob", "list", "webfetch", "websearch", "bash"}


@tool("execute_code", "bash", "Run a Python script. Inside it, tool(name, **arguments) calls read, write, "
      "edit, grep, glob, list, webfetch, websearch or bash, so many steps cost one turn. Print the result.",
      code="string|Python source", timeout_opt="integer|Seconds (default 120)")
def execute_code(ctx, code, timeout=120):
    token = base64.b16encode(os.urandom(12)).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            if data.get("token") != token or data.get("name") not in SCRIPT_TOOLS:
                result = "That tool cannot be called from a script."
            else:
                result = run_tool(ctx, data["name"], data.get("arguments") or {})
            body = json.dumps({"result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as script:
        script.write(RPC_PRELUDE % (token, url) + "\n" + code)
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        exit_code, output = backends.run_argv(
            [sys.executable, script.name], str(ctx.cwd), int(timeout or 120), ctx.abort, env)
    finally:
        server.shutdown()
        server.server_close()
        os.unlink(script.name)
    return f"{util.clip(output.strip(), OUTPUT_LIMIT) or '(no output)'}\n[exit code {exit_code}]"


@tool("lsp", "read", "Ask the language server about code: problems in a file, or what is at a position.",
      operation="string|diagnostics, hover, definition, references or documentSymbol",
      filePath="string|Path of the file", line_opt="integer|Line, starting at 1",
      character_opt="integer|Column, starting at 1")
def lsp_tool(ctx, operation, filePath, line=1, character=1):
    from . import lsp
    return lsp.query(ctx.config, ctx.path(filePath), operation, int(line or 1), int(character or 1))


@tool("image_generate", "web", "Make an image from a description (needs an image model in the config).",
      prompt="string|What the image should show", filePath_opt="string|Where to save the PNG")
def image_generate(ctx, prompt, filePath=""):
    settings = ctx.config.get("image") or {}
    if not settings.get("baseURL"):
        return "No image model is set. Add \"image\": {\"baseURL\": ..., \"model\": ...} to the config."
    headers = {"Authorization": "Bearer " + settings["apiKey"]} if settings.get("apiKey") else {}
    status, data = util.http_json("POST", settings["baseURL"].rstrip("/") + "/images/generations", {
        "model": settings.get("model", ""), "prompt": prompt, "response_format": "b64_json",
    }, headers, timeout=180)
    items = data.get("data") or []
    if status >= 400 or not items or not items[0].get("b64_json"):
        return f"The image service answered HTTP {status} without an image."
    path = ctx.path(filePath or "image.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(items[0]["b64_json"]))
    return f"Saved the image to {path}."


def speech_argv(text: str, path: str = "") -> list:
    """The command that speaks `text` aloud (or into a file) with what the system already has."""
    if os.name == "nt":
        target = f"$s.SetOutputToWaveFile('{path}');" if path else ""
        quoted = text.replace("'", "''")
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object "
                  f"System.Speech.Synthesis.SpeechSynthesizer; {target} $s.Speak('{quoted}'); $s.Dispose()")
        return ["powershell", "-NoProfile", "-Command", script]
    if sys.platform == "darwin":
        return ["say"] + (["-o", path] if path else []) + [text]
    return ["espeak"] + (["-w", path] if path else []) + [text]


@tool("text_to_speech", "other", "Say a text aloud, or save it as an audio file.",
      text="string|What to say", filePath_opt="string|Save here instead of speaking")
def text_to_speech(ctx, text, filePath=""):
    path = str(ctx.path(filePath)) if filePath else ""
    code, output = backends.run_argv(speech_argv(text[:3000], path), None, 120, ctx.abort)
    if code != 0:
        return f"Speech is not available on this system: {output.strip()[:200]}"
    return f"Saved speech to {path}." if path else "Spoken."


# ---------------------------------------------------------------- running a tool

def target_of(name: str, arguments: dict) -> str:
    """The thing a permission rule is matched against."""
    for key in ("command", "filePath", "url", "query", "name", "prompt"):
        if arguments.get(key):
            return str(arguments[key])
    if name == "patch":
        return " ".join(re.findall(r"\*\*\* (?:Add|Update|Delete) File: (.+)", arguments.get("patchText", "")))
    return ""


def run_tool(ctx: Context, name: str, arguments: dict, extra=None) -> str:
    """Check permission, run, and always come back with text the model can act on."""
    from . import plugins
    registry = dict(TOOLS)
    registry.update(extra or {})
    found = registry.get(name)
    if not found:
        return f"There is no tool named '{name}'. Tools: {', '.join(sorted(registry))}"
    schema = found.schema["function"]["parameters"]
    missing = [key for key in schema["required"] if key not in arguments]
    if missing:
        return f"Missing argument(s) for {name}: {', '.join(missing)}"
    arguments = {key: value for key, value in arguments.items() if key in schema["properties"]}
    target = target_of(name, arguments)
    if name == "patch":
        for part in re.findall(r"\*\*\* (?:Add|Update|Delete) File: (.+)", arguments.get("patchText", "")):
            refusal = ctx.gate.check(name, str(ctx.path(part.strip())), found.kind)
            if refusal:
                return refusal
    else:
        if found.kind == "edit":
            target = str(ctx.path(target))
        refusal = ctx.gate.check(name, target, found.kind)
        if refusal:
            return refusal
    changed = plugins.fire("tool.before", ctx.cwd, name=name, arguments=arguments)
    if isinstance(changed, dict):
        arguments = changed
    try:
        result = found.handler(ctx, **arguments)
    except Exception as error:      # a tool error is information for the model, not a crash
        result = f"Error in {name}: {type(error).__name__}: {error}"
    result = util.clip(str(result), OUTPUT_LIMIT + 2000)
    after = plugins.fire("tool.after", ctx.cwd, name=name, arguments=arguments, result=result)
    return after if isinstance(after, str) else result
