"""The local server: the web app, a REST API with live events, an OpenAI-compatible endpoint
and the webhook route for the gateway. Standard library only.

Safety: it listens on 127.0.0.1 unless told otherwise, refuses requests that come from another
web page (Origin and Host checks), and a non-local address needs a password.
"""
from __future__ import annotations

import hmac
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, agent as agent_module, config as config_module, cron, engines
from . import gateway as gateway_module, plugins, skills, util

WEB = Path(__file__).parent / "web"
LOCAL = ("127.0.0.1", "localhost", "[::1]")


class Run:
    """One prompt being answered: its events, and how to stop it."""

    def __init__(self):
        self.events, self.changed = [], threading.Condition()
        self.abort, self.finished = threading.Event(), False

    def emit(self, event, data):
        with self.changed:
            self.events.append({"event": event, "data": data, "t": time.time()})
            self.changed.notify_all()

    def finish(self):
        with self.changed:
            self.finished = True
            self.changed.notify_all()


class App:
    def __init__(self, config, store, overrides=None):
        self.config, self.store, self.overrides = config, store, overrides or {}
        self.cwd = config.get("_cwd", ".")
        self.runs, self.pending = {}, {}
        self.last_seen, self.started = time.time(), time.time()
        self.gateway = None

    # ------------------------------------------------------------ running prompts
    def busy(self, session) -> bool:
        run = self.runs.get(session)
        return bool(run and not run.finished)

    def _wait(self, run, kind, payload, timeout):
        ident = uuid.uuid4().hex[:10]
        answers = queue.Queue()
        self.pending[ident] = answers
        run.emit(kind, dict(payload, id=ident))
        try:
            return answers.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.pending.pop(ident, None)
            run.emit("answered", {"id": ident})

    def reply(self, ident, answer) -> bool:
        answers = self.pending.get(ident)
        if answers:
            answers.put(answer)
        return bool(answers)

    def start(self, session, text, model="", agent="", cwd="", source="web", auto_yes=False, attachments=None):
        """Begin answering `text`. Returns (session id, Run). Raises ValueError with a plain reason."""
        from . import repl
        info = self.store.session(session) if session else None
        if session and not info:
            raise ValueError("That session does not exist.")
        if session and self.busy(session):
            raise ValueError("This session is still working. Stop it first.")
        folder = Path((info or {}).get("cwd") or cwd or self.cwd).expanduser()
        if not folder.is_dir():
            raise ValueError(f"The folder {folder} does not exist.")
        config = config_module.load(folder, self.overrides)
        text, agent, model = repl.prepare(text, folder, config, agent, model)
        run = Run()
        wait = float(config.get("approval_wait", 600))

        def asker(tool, target, reason):
            answer = self._wait(run, "permission", {"tool": tool, "target": target[:2000], "reason": reason}, wait)
            return answer if answer in ("once", "always") else "reject"

        def ask(question, options):
            answer = self._wait(run, "question", {"question": question, "options": options}, wait)
            return answer if answer is not None else "(no answer; decide yourself)"

        runner = agent_module.Agent(
            config, self.store, session=session or "", agent=agent or (info or {}).get("agent") or "build",
            source=source, asker=asker, ask=ask, emit=run.emit, abort=run.abort, model=model,
            auto_yes=auto_yes)
        if model:
            self.store.update_session(runner.session, model=model)
        self.runs[runner.session] = run

        def work():
            try:
                run.result = runner.run(text, attachments)
            except Exception as error:
                run.result = None
                run.emit("done", {"text": f"Something went wrong: {error}", "stopped": "error", "proof": ""})
            finally:
                runner.close()
                run.finish()

        run.thread = threading.Thread(target=work, daemon=True)
        run.thread.start()
        return runner.session, run

    # ------------------------------------------------------------ what the pages show
    def state(self) -> dict:
        config = self.config
        return {
            "version": __version__, "cwd": str(self.cwd),
            "model": config_module.pick_default_model(config),
            "models": config_module.list_models(config),
            "agents": [{"name": name, "description": entry.get("description", ""), "mode": entry.get("mode", "all")}
                       for name, entry in plugins.agents(self.cwd, config).items()],
            "commands": [{"name": name, "description": entry.get("description", "")}
                         for name, entry in plugins.commands(self.cwd, config).items()],
            "engines": engines.available(config), "sources": config.get("_sources", []),
            "theme": config.get("theme", "paper"), "proof": config.get("proof"),
            "budget": config.get("budget"),
        }

    def session_view(self, session) -> dict:
        info = self.store.session(session)
        if not info:
            return {}
        messages = [message for message in self.store.messages(session)
                    if message["role"] != "system" and not message.get("internal")]
        for message in messages:
            if not isinstance(message.get("content"), str):
                message["content"] = "(attachment)"
        return {"session": info, "messages": messages, "running": self.busy(session),
                "events": len(self.runs[session].events) if self.busy(session) else 0}

    def doctor(self) -> list:
        from . import cli
        return cli.doctor_rows(self.config)


def _clean(session: dict) -> dict:
    return {key: session.get(key) for key in (
        "id", "title", "agent", "model", "cwd", "source", "updated", "tokens_in", "tokens_out", "cost")}


def make_handler(app: App, password: str = ""):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        # -------------------------------------------------------- plumbing
        def send(self, status, body, kind="application/json", extra=None):
            if not isinstance(body, (bytes, bytearray)):
                body = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if "charset" not in kind else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20_000_000:
                raise ValueError("request too large")
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("the body must be a JSON object")
            return data

        def allowed(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            origin = self.headers.get("Origin") or ""
            if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
                return False                         # another web page is calling
            if password:
                given = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)
                cookie = dict(part.strip().split("=", 1) for part in (
                    self.headers.get("Cookie") or "").split(";") if "=" in part).get("talos", "")
                return hmac.compare_digest(given, password) or hmac.compare_digest(cookie, password)
            return host in LOCAL                     # no password: this computer only

        def stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def event(self, data, name=""):
            text = (f"event: {name}\n" if name else "") + f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

        # -------------------------------------------------------- routes
        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method):
            url = urlparse(self.path)
            parts = [part for part in url.path.split("/") if part]
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            try:
                if method == "POST" and parts[:1] == ["login"]:
                    given = str(self.body().get("password", ""))
                    if password and hmac.compare_digest(given, password):
                        return self.send(200, {"ok": True}, extra={
                            "Set-Cookie": f"talos={password}; HttpOnly; SameSite=Strict; Path=/"})
                    return self.send(401, {"error": "Wrong password."})
                if not self.allowed():
                    if password and method == "GET" and not parts:
                        return self.send(200, LOGIN_PAGE, "text/html")
                    return self.send(403, {"error": "Not allowed from here."})
                app.last_seen = time.time()
                if not parts or parts == ["index.html"]:
                    return self.send(200, (WEB / "index.html").read_bytes(), "text/html")
                if parts[0] == "icon.svg":
                    return self.send(200, (WEB / "icon.svg").read_bytes(), "image/svg+xml")
                if parts[0] in ("favicon.ico", "icon.png"):       # the window and taskbar logo come from these
                    name, kind = (("talos.ico", "image/x-icon") if parts[0] == "favicon.ico"
                                  else ("talos.png", "image/png"))
                    return self.send(200, (WEB / name).read_bytes(), kind)
                if parts[0] == "manifest.webmanifest":
                    return self.send(200, {
                        "name": "Talos", "short_name": "Talos", "start_url": "/", "display": "standalone",
                        "background_color": "#1d1b16", "theme_color": "#1d1b16",
                        "icons": [{"src": "/icon.png", "sizes": "256x256", "type": "image/png"}]})
                if parts[0] == "api":
                    return self.api(method, parts[1:], query)
                if parts[0] == "v1":
                    return self.openai(method, parts[1:])
                if parts[0] == "hook" and method == "POST" and len(parts) == 2:
                    return self.hook(parts[1])
                self.send(404, {"error": "No such page."})
            except (BrokenPipeError, ConnectionError):
                pass
            except ValueError as error:
                self.send(400, {"error": str(error)})
            except Exception as error:
                self.send(500, {"error": f"{type(error).__name__}: {error}"})

        def api(self, method, parts, query):
            store = app.store
            head = parts[0] if parts else ""
            if head == "ping":
                return self.send(200, {"ok": True, "version": __version__, "app": "talos"})
            if head == "state":
                return self.send(200, app.state())
            if head == "doctor":
                return self.send(200, app.doctor())
            if head == "stats":
                return self.send(200, store.stats())
            if head == "parity":
                from . import parity
                return self.send(200, {"rows": parity.rows(), "counts": parity.counts()})
            if head == "search":
                return self.send(200, store.search(query.get("q", ""), 20))
            if head == "reply" and method == "POST":
                data = self.body()
                return self.send(200, {"ok": app.reply(str(data.get("id")), data.get("answer"))})
            if head == "skills":
                if method == "POST":
                    return self.send(200, {"ok": skills.trust(app.cwd, str(self.body().get("trust", "")))})
                return self.send(200, list(skills.discover(app.cwd).values()))
            if head == "knowledge":
                from . import knowledge
                if method == "POST":
                    data = self.body()
                    return self.send(200, {
                        "notes": knowledge.search_notes(app.config, str(data.get("q", "")), 12),
                        "projects": knowledge.find_projects(app.config, str(data.get("q", "")))[:40]})
                return self.send(200, knowledge.overview(app.config))
            if head == "memory":
                if method == "POST":
                    data = self.body()
                    return self.send(200, {"result": skills.memory_edit(
                        str(data.get("action", "add")), str(data.get("target", "memory")),
                        str(data.get("content", "")), str(data.get("old_text", "")))})
                return self.send(200, {target: skills.memory_entries(target) for target in skills.MEMORY_FILES})
            if head == "cron":
                if method == "POST":
                    data = self.body()
                    action = str(data.get("action", "create"))
                    if action == "create":
                        result = cron.create(str(data.get("schedule", "")), str(data.get("prompt", "")),
                                             str(data.get("name", "")), str(data.get("deliver", "local")),
                                             str(data.get("cwd") or app.cwd))
                    else:
                        result = cron.change(action, str(data.get("id", "")))
                    return self.send(200, {"result": result})
                return self.send(200, cron.jobs())
            if head == "sessions":
                return self.sessions(method, parts[1:], query)
            self.send(404, {"error": "No such API."})

        def sessions(self, method, parts, query):
            store = app.store
            if not parts:
                return self.send(200, [dict(_clean(item), running=app.busy(item["id"]))
                                       for item in store.sessions(100)])
            session = parts[0]
            action = parts[1] if len(parts) > 1 else ""
            if action == "prompt" and method == "POST":
                data = self.body()
                if not str(data.get("text", "")).strip():
                    raise ValueError("Type a message first.")
                started, _ = app.start("" if session == "new" else session, str(data["text"]),
                                       str(data.get("model") or ""), str(data.get("agent") or ""),
                                       str(data.get("cwd") or ""))
                return self.send(200, {"session": started})
            if not store.session(session):
                return self.send(404, {"error": "That session does not exist."})
            if method == "DELETE":
                store.delete_session(session)
                return self.send(200, {"ok": True})
            if not action:
                return self.send(200, app.session_view(session))
            if action == "events":
                return self.events(session, int(query.get("after", 0)))
            if action == "export":
                return self.send(200, store.export(session))
            if action == "share":
                return self.send(200, store.share_html(session), "text/html")
            if method != "POST":
                return self.send(405, {"error": "Use POST."})
            if action == "abort":
                run = app.runs.get(session)
                if run:
                    run.abort.set()
                return self.send(200, {"ok": bool(run)})
            if app.busy(session):
                raise ValueError("This session is still working. Stop it first.")
            if action == "undo":
                return self.send(200, {"undone": store.undo(session)})
            if action == "redo":
                return self.send(200, {"redone": store.redo(session)})
            if action == "compact":
                info = store.session(session)
                runner = agent_module.Agent(config_module.load(info["cwd"], app.overrides), store, session=session)
                try:
                    before = store.messages(session)
                    after = runner.compact(before, force=True)
                finally:
                    runner.close()
                return self.send(200, {"before": len(before), "after": len(after)})
            self.send(404, {"error": "No such action."})

        def events(self, session, after):
            run = app.runs.get(session)
            self.stream()
            if not run:
                return self.event({"event": "idle", "data": {}})
            index = after
            while True:
                with run.changed:
                    while index >= len(run.events) and not run.finished:
                        run.changed.wait(15)
                        if index >= len(run.events) and not run.finished:
                            break
                    batch = run.events[index:]
                    finished = run.finished
                if not batch and not finished:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                for item in batch:
                    index += 1
                    self.event(dict(item, index=index))
                if finished and index >= len(run.events):
                    return self.event({"event": "closed", "data": {}, "index": index})

        # -------------------------------------------------------- OpenAI-compatible endpoint
        def openai(self, method, parts):
            if parts == ["models"]:
                names = ["talos"] + [f"talos/{item['name']}" for item in app.state()["agents"]]
                return self.send(200, {"object": "list", "data": [
                    {"id": name, "object": "model", "owned_by": "talos"} for name in names]})
            if parts != ["chat", "completions"] or method != "POST":
                return self.send(404, {"error": {"message": "Only /v1/models and /v1/chat/completions."}})
            data = self.body()
            messages = [message for message in data.get("messages") or [] if isinstance(message, dict)]
            users = [message for message in messages if message.get("role") == "user"]
            if not users:
                raise ValueError("messages needs at least one user message")

            def text_of(message):
                content = message.get("content")
                if isinstance(content, list):
                    return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
                return str(content or "")

            earlier = "\n".join(f"{message.get('role')}: {text_of(message)}" for message in messages[:-1]
                                if message.get("role") in ("user", "assistant"))
            prompt = text_of(users[-1])
            if earlier:
                prompt = f"Earlier in this conversation:\n{util.clip(earlier, 12000)}\n\nNow: {prompt}"
            name = str(data.get("model") or "talos")
            agent_name = name.split("/", 1)[1] if name.startswith("talos/") else ""
            session, run = app.start("", prompt, agent=agent_name, source="api")
            ident, created = "chatcmpl-" + session, int(time.time())
            if data.get("stream"):
                self.stream()
                index = 0
                while True:
                    with run.changed:
                        while index >= len(run.events) and not run.finished:
                            run.changed.wait(15)
                        batch, finished = run.events[index:], run.finished
                    for item in batch:
                        index += 1
                        if item["event"] == "text":
                            self.event({"id": ident, "object": "chat.completion.chunk", "created": created,
                                        "model": name, "choices": [{"index": 0, "delta": {"content": item["data"]},
                                                                    "finish_reason": None}]})
                    if finished and index >= len(run.events):
                        break
                self.event({"id": ident, "object": "chat.completion.chunk", "created": created, "model": name,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                self.wfile.write(b"data: [DONE]\n\n")
                return
            run.thread.join()
            result = getattr(run, "result", None)
            text = result.text if result else run.events[-1]["data"].get("text", "")
            usage = result.usage if result else {"in": 0, "out": 0}
            self.send(200, {
                "id": ident, "object": "chat.completion", "created": created, "model": name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": usage["in"], "completion_tokens": usage["out"],
                          "total_tokens": usage["in"] + usage["out"]},
                "talos": {"session": session, "proof": result.proof if result else None},
            })

        # -------------------------------------------------------- webhook into the gateway
        def hook(self, name):
            settings = (app.config.get("gateway") or {}).get("webhook") or {}
            data = self.body()
            secret = str(settings.get("secret") or "")
            if not secret or not hmac.compare_digest(str(data.get("secret") or self.headers.get("X-Talos-Secret") or ""), secret):
                return self.send(403, {"error": "The webhook needs gateway.webhook.secret in the config, sent as 'secret'."})
            if app.gateway is None:
                app.gateway = gateway_module.Gateway(app.config, app.store, adapters={})
            sent = []

            class Reply(gateway_module.Webhook):
                def post(self, chat, text):
                    sent.append(text)
                    super().post(chat, text)

            app.gateway.adapters["webhook"] = Reply(dict(settings, allowed_users=settings.get("allowed_users", "*")))
            app.gateway.config = dict(app.config, gateway=dict(app.config.get("gateway") or {}, webhook=dict(
                settings, allowed_users=settings.get("allowed_users", "*"))))
            incoming = gateway_module.Incoming(f"{name}:{data.get('chat', 'default')}", str(data.get("user", name)),
                                               str(data.get("text", "")))
            app.gateway.handle("webhook", incoming, wait=True)
            self.send(200, {"text": "\n".join(sent)})

    return Handler


LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>Talos</title>
<style>body{font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f6f1e7;color:#1d1b16}
form{display:grid;gap:.6rem;width:16rem}input,button{font:inherit;padding:.5rem .7rem;border:1px solid #b9ae98;border-radius:6px}
button{background:#1d1b16;color:#f6f1e7;cursor:pointer}</style>
<form onsubmit="event.preventDefault();fetch('/login',{method:'POST',body:JSON.stringify({password:p.value})})
.then(r=>r.ok?location.reload():(m.textContent='Wrong password.'))">
<label for="p">Password</label><input id="p" type="password" autofocus><button>Open Talos</button><span id="m"></span></form>"""


def serve(config, store, host="127.0.0.1", port=4747, password="", overrides=None, idle_exit=0):
    """Returns (server, app). The caller runs server.serve_forever()."""
    if host not in ("127.0.0.1", "localhost", "::1") and not password:
        raise ValueError("A password is required to listen on anything but this computer (--password).")
    app = App(config, store, overrides)
    server = ThreadingHTTPServer((host, port), make_handler(app, password))
    server.daemon_threads = True
    if idle_exit:
        def watch():
            while True:
                time.sleep(5)
                quiet = time.time() - app.last_seen
                if quiet > idle_exit and not any(not run.finished for run in app.runs.values()):
                    server.shutdown()
                    return
        threading.Thread(target=watch, daemon=True).start()
    return server, app
