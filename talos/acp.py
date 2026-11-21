"""Agent Client Protocol over stdin/stdout, so editors that speak ACP can use Talos.

Covers the core: initialize, session/new, session/prompt (with message and tool updates,
permission requests) and session/cancel.
"""
from __future__ import annotations

import json
import sys
import threading

from . import __version__, agent as agent_module, config as config_module


class Acp:
    def __init__(self, store, reader=None, writer=None, overrides=None):
        self.store, self.overrides = store, overrides or {}
        self.reader, self.writer = reader or sys.stdin, writer or sys.stdout
        self.sessions, self.aborts, self.waiting = {}, {}, {}
        self.lock, self.next_id = threading.Lock(), 1000

    def send(self, message):
        with self.lock:
            self.writer.write(json.dumps(dict(message, jsonrpc="2.0")) + "\n")
            self.writer.flush()

    def update(self, session, payload):
        self.send({"method": "session/update", "params": {"sessionId": session, "update": payload}})

    def request(self, method, params, timeout=300):
        with self.lock:
            self.next_id += 1
            ident = self.next_id
        done = threading.Event()
        self.waiting[ident] = [done, None]
        self.send({"id": ident, "method": method, "params": params})
        done.wait(timeout)
        return self.waiting.pop(ident)[1]

    def prompt(self, ident, params):
        session = params.get("sessionId", "")
        entry = self.sessions.get(session)
        if not entry:
            return self.send({"id": ident, "error": {"code": -32602, "message": "unknown session"}})
        text = "\n".join(block.get("text", "") for block in params.get("prompt") or []
                         if isinstance(block, dict) and block.get("type") == "text")
        abort = self.aborts[session] = threading.Event()

        def emit(event, data):
            if event == "text":
                self.update(session, {"sessionUpdate": "agent_message_chunk",
                                      "content": {"type": "text", "text": data}})
            elif event == "tool":
                self.update(session, {"sessionUpdate": "tool_call", "toolCallId": f"{data['name']}-{id(data)}",
                                      "title": data["name"], "status": "in_progress",
                                      "rawInput": data["arguments"]})
            elif event == "tool_result":
                self.update(session, {"sessionUpdate": "tool_call_update", "title": data["name"],
                                      "status": "completed",
                                      "content": [{"type": "content", "content": {
                                          "type": "text", "text": data["result"][:4000]}}]})

        def asker(tool, target, reason):
            answer = self.request("session/request_permission", {
                "sessionId": session, "toolCall": {"title": f"{tool}: {target[:200]} ({reason})"},
                "options": [{"optionId": "once", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                            {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]})
            chosen = ((answer or {}).get("outcome") or {}).get("optionId", "reject")
            return chosen if chosen in ("once", "always") else "reject"

        def work():
            runner = agent_module.Agent(entry["config"], self.store, session=entry["session"], source="acp",
                                        asker=asker, emit=emit, abort=abort)
            entry["session"] = runner.session
            try:
                result = runner.run(text)
                if result.proof_line():
                    emit("text", "\n\n" + result.proof_line())
                stop = "cancelled" if abort.is_set() else "end_turn"
                self.send({"id": ident, "result": {"stopReason": stop}})
            except Exception as error:
                self.send({"id": ident, "error": {"code": -32000, "message": str(error)}})
            finally:
                runner.close()

        thread = threading.Thread(target=work, daemon=True)
        entry["thread"] = thread
        thread.start()

    def handle(self, message):
        method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
        if method is None and ident in self.waiting:
            self.waiting[ident][1] = message.get("result")
            self.waiting[ident][0].set()
        elif method == "initialize":
            self.send({"id": ident, "result": {
                "protocolVersion": params.get("protocolVersion", 1),
                "agentCapabilities": {"loadSession": False, "promptCapabilities": {"image": False}},
                "agentInfo": {"name": "talos", "version": __version__}, "authMethods": []}})
        elif method == "session/new":
            config = config_module.load(params.get("cwd") or ".", self.overrides)
            name = f"acp_{len(self.sessions) + 1}"
            self.sessions[name] = {"config": config, "session": ""}
            self.send({"id": ident, "result": {"sessionId": name}})
        elif method == "session/prompt":
            self.prompt(ident, params)
        elif method == "session/cancel":
            abort = self.aborts.get(params.get("sessionId", ""))
            if abort:
                abort.set()
        elif ident is not None:
            self.send({"id": ident, "error": {"code": -32601, "message": f"method not found: {method}"}})

    def serve(self):
        for line in self.reader:
            line = line.strip()
            if not line:
                continue
            try:
                self.handle(json.loads(line))
            except json.JSONDecodeError:
                self.send({"id": None, "error": {"code": -32700, "message": "parse error"}})
        for entry in self.sessions.values():
            if entry.get("thread"):
                entry["thread"].join(60)
