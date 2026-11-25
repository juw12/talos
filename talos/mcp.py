"""MCP client: local servers over stdin/stdout and remote servers over HTTP.

Reads the `mcp` section of the config in OpenCode's shape:
    {"name": {"type": "local", "command": ["npx", "-y", "pkg"], "environment": {...}}}
    {"name": {"type": "remote", "url": "https://...", "headers": {...}}}
Each server tool becomes a Talos tool called <server>_<tool>. Tool descriptions are scanned
for hidden instructions before the model ever sees them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

from . import skills, util
from .tools import Tool

PROTOCOL = "2025-03-26"
CLIENT = {"name": "talos", "version": "1"}


class McpError(Exception):
    pass


class Local:
    def __init__(self, command, environment=None, cwd=None):
        program = shutil.which(command[0]) or command[0]
        self.process = subprocess.Popen(
            [program] + list(command[1:]), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={**os.environ, **(environment or {})})
        self.lock = threading.Lock()
        self.next_id = 0

    def send(self, message, expect_reply=True, timeout=60):
        with self.lock:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
            if not expect_reply:
                return None
            result = {}

            def read():
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        return
                    try:
                        reply = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if reply.get("id") == message["id"]:
                        result.update(reply)
                        return

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(timeout)
            if not result:
                raise McpError("the MCP server did not answer in time")
            return result

    def close(self):
        """Ends the server and waits until it is gone: on Windows a program that still lives keeps
        the folder it was started in, and that folder then cannot be removed."""
        try:
            self.process.kill()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                pipe.close()
            except OSError:
                pass


class Remote:
    def __init__(self, url, headers=None):
        self.url, self.headers, self.session_id = url, dict(headers or {}), ""

    def send(self, message, expect_reply=True, timeout=60):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST")
        try:
            with util._opener(self.url).open(request, timeout=timeout) as response:
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                body = response.read().decode("utf-8", errors="replace")
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            raise McpError(f"HTTP {error.code} from the MCP server")
        if not expect_reply:
            return None
        if "event-stream" in kind:
            for line in body.splitlines():
                if line.startswith("data:"):
                    reply = json.loads(line[5:].strip())
                    if reply.get("id") == message.get("id"):
                        return reply
            raise McpError("the MCP server sent no answer")
        return json.loads(body)

    def close(self):
        return


class Client:
    def __init__(self, name: str, entry: dict, cwd=None):
        self.name = name
        if entry.get("type") == "remote" or entry.get("url"):
            self.transport = Remote(entry["url"], entry.get("headers"))
        else:
            self.transport = Local(entry["command"], entry.get("environment"), cwd)
        self.next_id = 0
        self.call("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT})
        self.transport.send(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)

    def call(self, method, params=None, timeout=60):
        self.next_id += 1
        reply = self.transport.send(
            {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}},
            timeout=timeout)
        if reply.get("error"):
            raise McpError(str(reply["error"].get("message") or reply["error"]))
        return reply.get("result") or {}

    def tools(self) -> list:
        return self.call("tools/list").get("tools") or []

    def run(self, tool: str, arguments: dict) -> str:
        result = self.call("tools/call", {"name": tool, "arguments": arguments}, timeout=300)
        parts = [item.get("text", "") for item in result.get("content") or []
                 if item.get("type") == "text"]
        text = "\n".join(parts) or json.dumps(result)[:4000]
        return ("Error from the MCP tool: " if result.get("isError") else "") + text

    def close(self):
        self.transport.close()


class Hub:
    """All configured servers. Connects on first use and keeps the connections for the run."""

    def __init__(self, config: dict):
        self.config = config
        self.clients, self.problems, self.skipped = {}, {}, []

    def tools(self) -> dict:
        found = {}
        for name, entry in (self.config.get("mcp") or {}).items():
            if not isinstance(entry, dict) or entry.get("enabled") is False:
                continue
            try:
                client = self.clients.get(name) or Client(name, entry, self.config.get("_cwd"))
                self.clients[name] = client
                listed = client.tools()
            except (McpError, OSError, ValueError, KeyError) as error:
                self.problems[name] = str(error)
                continue
            for spec in listed:
                reason = skills.blocked(spec.get("description") or "")
                if reason:      # a poisoned description never reaches the model
                    self.skipped.append(f"{name}_{spec['name']}: {reason}")
                    continue
                found[f"{name}_{spec['name']}"] = self._tool(client, name, spec)
        return found

    @staticmethod
    def _tool(client, server, spec):
        made = Tool(f"{server}_{spec['name']}", "mcp", spec.get("description") or "", {},
                    lambda ctx, **arguments: client.run(spec["name"], arguments))
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("properties", {})
        schema.setdefault("required", [])
        made.schema["function"]["parameters"] = schema
        return made

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients = {}
