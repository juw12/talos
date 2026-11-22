"""Shared test setup: a private home folder, a work folder, a scripted model, a fake web server."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from talos import agent as agent_module, config as config_module, fake, plugins, store as store_module

PYTHON = sys.executable


class FakeHttp:
    """A local web server. `reply(method, path, body, headers)` returns (status, body, content type)."""

    def __init__(self, reply):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                outer.requests.append((self.command, self.path, body, dict(self.headers)))
                result = reply(self.command, self.path, body, dict(self.headers))
                status, payload = result[0], result[1]
                kind = result[2] if len(result) > 2 else "application/json"
                data = (payload if isinstance(payload, str) else json.dumps(payload)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass                                    # the caller had read enough and hung up

            do_GET = do_POST = do_PUT = answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="talos-test-")).resolve()
        self.home, self.user, self.work = self.tmp / "home", self.tmp / "user", self.tmp / "work"
        for folder in (self.home, self.user, self.work):
            folder.mkdir()
        self.saved = {name: os.environ.get(name) for name in ("TALOS_HOME", "TALOS_USER_HOME", "TALOS_CONFIG_CONTENT")}
        os.environ["TALOS_HOME"], os.environ["TALOS_USER_HOME"] = str(self.home), str(self.user)
        os.environ.pop("TALOS_CONFIG_CONTENT", None)
        plugins._loaded.clear()
        self.closers = []
        self.store = store_module.Store()
        self.closers.append(self.store.close)

    def tearDown(self):
        for close in reversed(self.closers):
            try:
                close()
            except Exception:
                pass
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def model(self, steps, **options):
        model = fake.FakeModel(steps, **options)
        self.closers.append(model.close)
        return model

    def config(self, model=None, **extra):
        overrides = model.config() if model else {}
        overrides.update(extra)
        return config_module.load(self.work, overrides)

    def agent(self, steps, model_options=None, agent="build", **extra):
        """An agent wired to a scripted model. Returns (agent, model, events)."""
        model = self.model(steps, **(model_options or {}))
        events = []
        runner_options = {key: extra.pop(key) for key in ("asker", "ask", "auto_yes", "source") if key in extra}
        runner = agent_module.Agent(self.config(model, **extra), self.store, agent=agent,
                                    emit=lambda event, data: events.append((event, data)), **runner_options)
        self.closers.append(runner.close)
        return runner, model, events

    def http(self, reply):
        server = FakeHttp(reply)
        self.closers.append(server.close)
        return server

    @staticmethod
    def results(events, name=None):
        return [data["result"] for event, data in events if event == "tool_result" and (not name or data["name"] == name)]
