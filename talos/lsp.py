"""Language help after every edit: a formatter, then problems found in the file.

Problems come from a real language server when one is installed (pyright, typescript-language-server,
gopls, rust-analyzer, clangd, or any server named in the config), and from built-in checks
otherwise (Python syntax, JSON, JavaScript via `node --check`). Either way the agent sees the
error right after the edit, in the same tool result.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import backends, util

SERVERS = {
    "pyright": {"command": ["pyright-langserver", "--stdio"], "extensions": [".py"]},
    "typescript": {"command": ["typescript-language-server", "--stdio"],
                   "extensions": [".ts", ".tsx", ".js", ".jsx"]},
    "gopls": {"command": ["gopls"], "extensions": [".go"]},
    "rust-analyzer": {"command": ["rust-analyzer"], "extensions": [".rs"]},
    "clangd": {"command": ["clangd"], "extensions": [".c", ".h", ".cpp", ".hpp", ".cc"]},
}
FORMATTERS = {
    "ruff": {"command": ["ruff", "format", "$FILE"], "extensions": [".py"]},
    "black": {"command": ["black", "-q", "$FILE"], "extensions": [".py"]},
    "prettier": {"command": ["prettier", "--write", "$FILE"],
                 "extensions": [".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json", ".md"]},
    "gofmt": {"command": ["gofmt", "-w", "$FILE"], "extensions": [".go"]},
    "rustfmt": {"command": ["rustfmt", "$FILE"], "extensions": [".rs"]},
}
METHODS = {"hover": "textDocument/hover", "definition": "textDocument/definition",
           "references": "textDocument/references", "documentSymbol": "textDocument/documentSymbol"}
SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _pick(configured, defaults, path: Path):
    """The first enabled entry for this file type whose program exists."""
    table = dict(defaults)
    for name, entry in (configured or {}).items():
        if isinstance(entry, dict):
            table[name] = {**table.get(name, {}), **entry}
    for name, entry in table.items():
        if entry.get("disabled") or path.suffix.lower() not in (entry.get("extensions") or []):
            continue
        command = entry.get("command") or []
        if command and shutil.which(command[0]):
            return entry
    return None


def format_file(config: dict, path: Path) -> str:
    if config.get("formatter") is False:
        return ""
    entry = _pick(config.get("formatter"), FORMATTERS, path)
    if not entry:
        return ""
    argv = [str(path) if part == "$FILE" else part for part in entry["command"]]
    code, output = backends.run_argv(argv, str(path.parent), 30)
    return "" if code == 0 else f"Formatter {argv[0]} failed: {output.strip()[:300]}"


class Server:
    """One language server process, spoken to with JSON-RPC over its stdin and stdout."""

    def __init__(self, command, root: Path):
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.next_id, self.replies, self.problems = 0, {}, {}
        self.signal = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {
            "processId": None, "rootUri": root.as_uri(), "capabilities": {
                "textDocument": {"publishDiagnostics": {}, "hover": {}, "definition": {}}},
        })
        self.notify("initialized", {})

    def _send(self, message):
        body = json.dumps(message).encode("utf-8")
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def _read(self):
        stream = self.process.stdout
        while True:
            length = 0
            while True:
                line = stream.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            message = json.loads(stream.read(length) or b"{}")
            with self.signal:
                if message.get("method") == "textDocument/publishDiagnostics":
                    params = message["params"]
                    self.problems[params["uri"]] = params.get("diagnostics") or []
                elif "id" in message and "method" not in message:
                    self.replies[message["id"]] = message
                self.signal.notify_all()

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=10):
        self.next_id += 1
        identifier = self.next_id
        self._send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        with self.signal:
            self.signal.wait_for(lambda: identifier in self.replies, timeout)
            return (self.replies.pop(identifier, None) or {}).get("result")

    def open(self, path: Path):
        self.problems.pop(path.as_uri(), None)
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": path.as_uri(), "languageId": path.suffix.lstrip("."), "version": int(time.time()),
            "text": util.read_text(path)}})

    def wait_problems(self, path: Path, timeout=6):
        with self.signal:
            self.signal.wait_for(lambda: path.as_uri() in self.problems, timeout)
            return self.problems.get(path.as_uri())

    def close(self):
        try:
            self.notify("exit", {})
        except OSError:
            pass
        backends.kill_tree(self.process)


def _builtin(path: Path) -> list:
    """Checks that need no language server."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            compile(util.read_text(path), str(path), "exec")
        elif suffix == ".json":
            json.loads(util.read_text(path))
        elif suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
            code, output = backends.run_argv(["node", "--check", str(path)], None, 20)
            if code != 0:
                return [f"error: {output.strip()[:400]}"]
    except SyntaxError as error:
        return [f"error line {error.lineno}: {error.msg}"]
    except json.JSONDecodeError as error:
        return [f"error line {error.lineno}: {error.msg}"]
    return []


def _with_server(config, path: Path, action):
    entry = None if config.get("lsp") is False else _pick(config.get("lsp"), SERVERS, path)
    if not entry:
        return None
    try:
        server = Server(entry["command"], Path(config.get("_cwd", ".")))
    except OSError:
        return None
    try:
        server.open(path)
        return action(server)
    finally:
        server.close()


def diagnostics(config: dict, path: Path) -> str:
    def collect(server):
        found = server.wait_problems(path)
        if found is None:
            return None
        return [
            f"{SEVERITY.get(item.get('severity', 1), 'error')} line "
            f"{item['range']['start']['line'] + 1}: {item.get('message', '')}"
            for item in found if item.get("severity", 1) <= 2
        ]

    problems = _with_server(config, path, collect)
    if problems is None:
        problems = _builtin(path)
    if not problems:
        return ""
    return f"Problems in {path.name}:\n" + "\n".join(problems[:20])


def query(config: dict, path: Path, operation: str, line: int, character: int) -> str:
    if not path.exists():
        return f"File not found: {path}"
    if operation == "diagnostics":
        return diagnostics(config, path) or "No problems found."
    if operation not in METHODS:
        return f"operation must be one of: diagnostics, {', '.join(METHODS)}"

    def ask(server):
        params = {"textDocument": {"uri": path.as_uri()},
                  "position": {"line": line - 1, "character": character - 1}}
        if operation == "references":
            params["context"] = {"includeDeclaration": True}
        return json.dumps(server.request(METHODS[operation], params), indent=1)[:8000]

    answer = _with_server(config, path, ask)
    if answer is None:
        return f"No language server is installed for {path.suffix} files, so only diagnostics work."
    return answer
