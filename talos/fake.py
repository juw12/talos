"""A scripted stand-in for a model server (OpenAI-compatible, streaming).

Used by the tests and by `talos demo`, so the whole agent can be shown and checked with no
model, no key and no network. Each queued step is one model answer:
    {"content": "text"}  or  {"tool_calls": [{"name": "read", "arguments": {...}}]}
A step may also be a function(request_payload) -> step, or {"status": 400} to refuse.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModel:
    def __init__(self, steps=None, native_tools=True, stream=True):
        self.steps = list(steps or [])
        self.native_tools = native_tools
        self.stream = stream
        self.requests = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/images/generations"):
                    self._json(200, {"data": [{"b64_json": "aGVsbG8="}]})
                    return
                with fake._lock:
                    fake.requests.append(payload)
                    if payload.get("tools") and not fake.native_tools:
                        self._json(400, {"error": {"message": "this model does not support tools"}})
                        return
                    step = fake.steps.pop(0) if fake.steps else {"content": "(script finished)"}
                if callable(step):
                    step = step(payload)
                if step.get("status"):
                    self._json(step["status"], {"error": {"message": step.get("error", "refused")}})
                    return
                if fake.stream and payload.get("stream"):
                    self._stream(step)
                else:
                    self._json(200, fake._whole(step))

            def _json(self, status, data):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, step):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                text = step.get("content") or ""
                for start in range(0, len(text), 12):
                    self._chunk({"content": text[start:start + 12]})
                for index, call in enumerate(step.get("tool_calls") or []):
                    self._chunk({"tool_calls": [{
                        "index": index,
                        "id": f"call_{len(fake.requests)}_{index}",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}),
                        },
                    }]})
                usage = {"prompt_tokens": 100, "completion_tokens": 20}
                self.wfile.write(
                    f"data: {json.dumps({'choices': [], 'usage': usage})}\n\ndata: [DONE]\n\n".encode()
                )

            def _chunk(self, delta):
                data = {"choices": [{"index": 0, "delta": delta}]}
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _whole(self, step):
        calls = [
            {
                "id": f"call_{len(self.requests)}_{index}",
                "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments") or {})},
            }
            for index, call in enumerate(step.get("tool_calls") or [])
        ]
        message = {"role": "assistant", "content": step.get("content") or ""}
        if calls:
            message["tool_calls"] = calls
        return {
            "choices": [{"index": 0, "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    def config(self, **extra) -> dict:
        """Config overrides that point Talos at this stand-in."""
        base = {
            "model": "fake/script",
            "provider": {"fake": {"options": {"baseURL": self.url}}},
            "retry_wait": 0,
        }
        base.update(extra)
        return base

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def call(name, /, **arguments):
    """Shorthand for one scripted tool call step."""
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def say(text):
    return {"content": text}
