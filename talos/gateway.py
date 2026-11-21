"""Talk to the agent from chat apps. `talos gateway` runs every platform in the config plus the
scheduler, in one process.

    "gateway": {
      "telegram": {"token": "...", "allowed_users": ["123456"]},
      "discord":  {"token": "...", "channels": ["111"], "allowed_users": ["222"]},
      "email":    {"imap_host": "...", "smtp_host": "...", "user": "...", "password": "...",
                   "allowed_users": ["me@example.com"]}
    }

Only people in allowed_users are answered; everyone else gets their id so the owner can add it.
Each adapter needs only the standard library: it polls the platform's HTTP API (or a socket).
"""
from __future__ import annotations

import email
import email.message
import imaplib
import queue
import smtplib
import socket
import ssl
import threading
import time
import urllib.parse

from . import util


class Incoming:
    def __init__(self, chat, user, text):
        self.chat, self.user, self.text = str(chat), str(user), text


def _chunks(text: str, size: int):
    text = text or "(empty answer)"
    return [text[start:start + size] for start in range(0, len(text), size)]


class Adapter:
    limit = 4000

    def __init__(self, settings: dict):
        self.settings = settings

    def fetch(self) -> list:
        return []

    def post(self, chat: str, text: str) -> None:
        raise NotImplementedError

    def send(self, chat: str, text: str) -> None:
        for piece in _chunks(util.redact(text), self.limit):
            self.post(chat, piece)


class Telegram(Adapter):
    def __init__(self, settings):
        super().__init__(settings)
        self.base = settings.get("base", "https://api.telegram.org").rstrip("/") + "/bot" + settings["token"]
        self.offset = 0

    def fetch(self):
        _, data = util.http_json(
            "GET", f"{self.base}/getUpdates?timeout={self.settings.get('wait', 25)}&offset={self.offset}",
            timeout=40)
        found = []
        for update in data.get("result") or []:
            self.offset = max(self.offset, update["update_id"] + 1)
            message = update.get("message") or {}
            if message.get("text"):
                found.append(Incoming(message["chat"]["id"], message.get("from", {}).get("id", ""), message["text"]))
        return found

    def post(self, chat, text):
        util.http_json("POST", f"{self.base}/sendMessage", {"chat_id": chat, "text": text})


class Discord(Adapter):
    limit = 1900

    def __init__(self, settings):
        super().__init__(settings)
        self.base = settings.get("base", "https://discord.com/api/v10").rstrip("/")
        self.headers = {"Authorization": "Bot " + settings["token"]}
        self.last = {}

    def fetch(self):
        found = []
        for channel in self.settings.get("channels") or []:
            after = self.last.get(channel)
            query = f"?limit=50&after={after}" if after else "?limit=1"
            _, data = util.http_json("GET", f"{self.base}/channels/{channel}/messages{query}", headers=self.headers)
            messages = sorted(data if isinstance(data, list) else [], key=lambda item: int(item["id"]))
            if messages:
                self.last[channel] = messages[-1]["id"]
            if not after:
                continue                    # first look only marks where "now" is
            for message in messages:
                author = message.get("author") or {}
                if not author.get("bot") and message.get("content"):
                    found.append(Incoming(channel, author.get("id", ""), message["content"]))
        return found

    def post(self, chat, text):
        util.http_json("POST", f"{self.base}/channels/{chat}/messages", {"content": text}, self.headers)


class Slack(Adapter):
    def __init__(self, settings):
        super().__init__(settings)
        self.base = settings.get("base", "https://slack.com/api").rstrip("/")
        self.headers = {"Authorization": "Bearer " + settings["token"]}
        self.last = {}

    def fetch(self):
        found = []
        for channel in self.settings.get("channels") or []:
            oldest = self.last.get(channel)
            if oldest is None:
                self.last[channel] = f"{time.time():.6f}"
                continue
            _, data = util.http_json(
                "GET", f"{self.base}/conversations.history?channel={channel}&oldest={oldest}", headers=self.headers)
            for message in sorted(data.get("messages") or [], key=lambda item: float(item["ts"])):
                self.last[channel] = message["ts"]
                if not message.get("bot_id") and message.get("text"):
                    found.append(Incoming(channel, message.get("user", ""), message["text"]))
        return found

    def post(self, chat, text):
        util.http_json("POST", f"{self.base}/chat.postMessage", {"channel": chat, "text": text}, self.headers)


class Mattermost(Adapter):
    def __init__(self, settings):
        super().__init__(settings)
        self.base = settings["base"].rstrip("/") + "/api/v4"
        self.headers = {"Authorization": "Bearer " + settings["token"]}
        self.since = int(time.time() * 1000)

    def fetch(self):
        found = []
        for channel in self.settings.get("channels") or []:
            _, data = util.http_json(
                "GET", f"{self.base}/channels/{channel}/posts?since={self.since}", headers=self.headers)
            posts = data.get("posts") or {}
            for identifier in data.get("order") or []:
                post = posts.get(identifier) or {}
                if post.get("user_id") != self.settings.get("bot_user_id") and post.get("message"):
                    found.append(Incoming(channel, post.get("user_id", ""), post["message"]))
        self.since = int(time.time() * 1000)
        return found

    def post(self, chat, text):
        util.http_json("POST", f"{self.base}/posts", {"channel_id": chat, "message": text}, self.headers)


class Matrix(Adapter):
    def __init__(self, settings):
        super().__init__(settings)
        self.base = settings["base"].rstrip("/") + "/_matrix/client/v3"
        self.headers = {"Authorization": "Bearer " + settings["token"]}
        self.since = ""

    def fetch(self):
        query = "?timeout=20000" + (f"&since={self.since}" if self.since else "")
        _, data = util.http_json("GET", f"{self.base}/sync{query}", headers=self.headers, timeout=40)
        first = not self.since
        self.since = data.get("next_batch") or self.since
        found = []
        if first:
            return found
        for room, content in ((data.get("rooms") or {}).get("join") or {}).items():
            for event in (content.get("timeline") or {}).get("events") or []:
                body = (event.get("content") or {}).get("body")
                if (event.get("type") == "m.room.message" and body
                        and event.get("sender") != self.settings.get("user_id")):
                    found.append(Incoming(room, event.get("sender", ""), body))
        return found

    def post(self, chat, text):
        room = urllib.parse.quote(chat)
        util.http_json(
            "PUT", f"{self.base}/rooms/{room}/send/m.room.message/{int(time.time() * 1000)}",
            {"msgtype": "m.text", "body": text}, self.headers)


class Ntfy(Adapter):
    """Push notifications through ntfy: listens on one topic, answers on another."""

    def __init__(self, settings):
        super().__init__(settings)
        self.base = settings.get("base", "https://ntfy.sh").rstrip("/")
        self.since = str(int(time.time()))

    def fetch(self):
        import json
        topic = self.settings.get("topic", "")
        _, text = util.http("GET", f"{self.base}/{topic}/json?poll=1&since={self.since}")
        found = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "message" and event.get("message"):
                self.since = event.get("id") or self.since
                found.append(Incoming(topic, event.get("title") or "ntfy", event["message"]))
        return found

    def post(self, chat, text):
        util.http("POST", f"{self.base}/{self.settings.get('reply_topic') or chat}", text)


class Signal(Adapter):
    """Through a signal-cli-rest-api server."""

    def __init__(self, settings):
        super().__init__(settings)
        self.base = settings.get("base", "http://127.0.0.1:8080").rstrip("/")
        self.number = settings["number"]

    def fetch(self):
        _, data = util.http_json("GET", f"{self.base}/v1/receive/{urllib.parse.quote(self.number)}")
        found = []
        for item in data if isinstance(data, list) else []:
            envelope = item.get("envelope") or {}
            text = (envelope.get("dataMessage") or {}).get("message")
            if text:
                found.append(Incoming(envelope.get("source", ""), envelope.get("source", ""), text))
        return found

    def post(self, chat, text):
        util.http_json("POST", f"{self.base}/v2/send", {"message": text, "number": self.number, "recipients": [chat]})


class Email(Adapter):
    limit = 100000

    def fetch(self):
        settings = self.settings
        box = imaplib.IMAP4_SSL(settings["imap_host"], int(settings.get("imap_port", 993)))
        found = []
        try:
            box.login(settings["user"], settings["password"])
            box.select("INBOX")
            _, data = box.search(None, "UNSEEN")
            for number in (data[0] or b"").split():
                _, parts = box.fetch(number, "(RFC822)")
                message = email.message_from_bytes(parts[0][1])
                sender = email.utils.parseaddr(message.get("From", ""))[1]
                body = message.get_payload(decode=True) if not message.is_multipart() else next(
                    (part.get_payload(decode=True) for part in message.walk()
                     if part.get_content_type() == "text/plain"), b"")
                text = f"{message.get('Subject', '')}\n\n{(body or b'').decode('utf-8', errors='replace')}"
                found.append(Incoming(sender, sender, text.strip()))
        finally:
            box.logout()
        return found

    def post(self, chat, text):
        settings = self.settings
        message = email.message.EmailMessage()
        message["From"], message["To"], message["Subject"] = settings["user"], chat, "Talos"
        message.set_content(text)
        with smtplib.SMTP_SSL(settings["smtp_host"], int(settings.get("smtp_port", 465))) as server:
            server.login(settings["user"], settings["password"])
            server.send_message(message)


class Irc(Adapter):
    limit = 400

    def __init__(self, settings):
        super().__init__(settings)
        self.nick = settings.get("nick", "talos")
        raw = socket.create_connection((settings["host"], int(settings.get("port", 6667))), timeout=30)
        self.socket = ssl.create_default_context().wrap_socket(raw, server_hostname=settings["host"]) \
            if settings.get("tls") else raw
        self.socket.settimeout(float(settings.get("wait", 5)))
        self.buffer = b""
        self._raw(f"NICK {self.nick}")
        self._raw(f"USER {self.nick} 0 * :Talos")
        for channel in settings.get("channels") or []:
            self._raw(f"JOIN {channel}")

    def _raw(self, line):
        self.socket.sendall((line + "\r\n").encode("utf-8"))

    def fetch(self):
        try:
            self.buffer += self.socket.recv(4096)
        except (socket.timeout, TimeoutError):
            return []
        *lines, self.buffer = self.buffer.split(b"\r\n")
        found = []
        for raw in lines:
            line = raw.decode("utf-8", errors="replace")
            if line.startswith("PING"):
                self._raw("PONG" + line[4:])
            elif " PRIVMSG " in line and line.startswith(":"):
                sender = line[1:].split("!")[0]
                target, _, text = line.split(" PRIVMSG ", 1)[1].partition(" :")
                found.append(Incoming(sender if target == self.nick else target, sender, text))
        return found

    def post(self, chat, text):
        for line in text.splitlines():
            if line.strip():
                self._raw(f"PRIVMSG {chat} :{line}")


class Webhook(Adapter):
    """Any system that can POST JSON {chat, user, text} to /hook/<name> on `talos serve`.
    The answer goes back in the HTTP response, and to reply_url when one is set."""

    def post(self, chat, text):
        if self.settings.get("reply_url"):
            util.http_json("POST", self.settings["reply_url"], {"chat": chat, "text": text})


ADAPTERS = {"telegram": Telegram, "discord": Discord, "slack": Slack, "mattermost": Mattermost,
            "matrix": Matrix, "ntfy": Ntfy, "signal": Signal, "email": Email, "irc": Irc,
            "webhook": Webhook}


def send(config: dict, platform: str, chat: str, text: str) -> str:
    settings = (config.get("gateway") or {}).get(platform)
    if platform not in ADAPTERS or not isinstance(settings, dict):
        return f"The platform '{platform}' is not set up in the gateway config."
    try:
        ADAPTERS[platform](settings).send(chat, text)
    except Exception as error:
        return f"Could not send to {platform}: {error}"
    return f"Sent to {platform}:{chat}."


HELP = ("Commands: /new start over, /model [name] show or set the model, /undo take back the last "
        "turn, /stop cancel the running job, /status, /help. Anything else goes to the agent.")


class Gateway:
    def __init__(self, config: dict, store, adapters=None):
        self.config, self.store = config, store
        self.adapters = adapters if adapters is not None else {
            name: ADAPTERS[name](settings) for name, settings in (config.get("gateway") or {}).items()
            if name in ADAPTERS and isinstance(settings, dict) and settings.get("enabled", True)}
        self.state_path = util.home() / "gateway" / "sessions.json"
        self.sessions = util.load_json_file(self.state_path, {}) or {}
        self.running, self.waiting, self.models = {}, {}, {}
        self.stop = threading.Event()

    def allowed(self, platform: str, user: str) -> bool:
        people = (self.config.get("gateway") or {}).get(platform, {}).get("allowed_users")
        if people == "*":
            return True
        return str(user) in [str(person) for person in people or []]

    def handle(self, platform: str, incoming: Incoming, wait=False):
        """One message in. Returns the thread that runs the agent, when one was started."""
        from . import plugins
        adapter = self.adapters[platform]
        key = f"{platform}:{incoming.chat}"
        text = incoming.text.strip()
        if not self.allowed(platform, incoming.user):
            adapter.send(incoming.chat, f"Not authorised. Ask the owner to add your id: {incoming.user}")
            return None
        plugins.fire("gateway.message", self.config.get("_cwd", "."), platform=platform,
                     chat=incoming.chat, user=incoming.user, text=text)
        if key in self.waiting:
            self.waiting[key].put(text)
            return None
        command, _, argument = text.partition(" ")
        if command == "/stop" and key in self.running:
            self.running[key].set()
            adapter.send(incoming.chat, "Stopping.")
        elif command in ("/new", "/reset"):
            self.sessions.pop(key, None)
            util.save_json_file(self.state_path, self.sessions)
            adapter.send(incoming.chat, "Started a new conversation.")
        elif command == "/model":
            if argument:
                self.models[key] = argument.strip()
            adapter.send(incoming.chat, f"Model: {self.models.get(key) or self.config.get('model') or 'default'}")
        elif command == "/undo":
            undone = self.store.undo(self.sessions.get(key, "")) if self.sessions.get(key) else None
            adapter.send(incoming.chat, f"Took back: {undone['text'][:80]}" if undone else "Nothing to undo.")
        elif command == "/status":
            state = "working" if key in self.running else "idle"
            adapter.send(incoming.chat, f"{state}; session {self.sessions.get(key, 'none')}")
        elif command in ("/help", "/start"):
            adapter.send(incoming.chat, HELP)
        elif key in self.running:
            adapter.send(incoming.chat, "Still working on the last message. Send /stop to cancel it.")
        else:
            thread = threading.Thread(target=self._answer, args=(platform, incoming, key), daemon=True)
            self.running[key] = threading.Event()
            thread.start()
            if wait:
                thread.join()
            return thread
        return None

    def _answer(self, platform, incoming, key):
        from . import agent as agent_module
        adapter = self.adapters[platform]

        def asker(tool, target, reason):
            adapter.send(incoming.chat, f"Allow {tool}: {target[:300]}\n({reason}) Reply yes, always or no.")
            self.waiting[key] = queue.Queue()
            try:
                answer = self.waiting[key].get(timeout=float(self.config.get("approval_wait", 300))).lower()
            except queue.Empty:
                answer = "no"
            finally:
                self.waiting.pop(key, None)
            return {"yes": "once", "y": "once", "always": "always"}.get(answer.strip(), "reject")

        runner = None
        try:
            runner = agent_module.Agent(
                self.config, self.store, session=self.sessions.get(key, ""), source=platform,
                asker=asker, abort=self.running[key], model=self.models.get(key, ""))
            self.sessions[key] = runner.session
            util.save_json_file(self.state_path, self.sessions)
            result = runner.run(incoming.text)
            proof = result.proof_line()
            adapter.send(incoming.chat, result.text + (f"\n\n{proof}" if proof else ""))
        except Exception as error:
            adapter.send(incoming.chat, f"Something went wrong: {error}")
        finally:
            if runner:
                runner.close()
            self.running.pop(key, None)

    def serve(self):
        """Poll every platform and run the scheduler until stop is set."""
        from . import cron

        def poll(name, adapter):
            while not self.stop.is_set():
                try:
                    for incoming in adapter.fetch():
                        self.handle(name, incoming)
                except Exception as error:
                    print(f"[gateway] {name}: {error}")
                    self.stop.wait(10)
                self.stop.wait(float(self.config.get("poll_wait", 2)))

        for name, adapter in self.adapters.items():
            if name != "webhook":
                threading.Thread(target=poll, args=(name, adapter), daemon=True).start()
        while not self.stop.is_set():
            try:
                cron.tick(self.config, store=self.store)
            except Exception as error:
                print(f"[gateway] scheduler: {error}")
            self.stop.wait(30)
