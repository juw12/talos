"""The relay inside Talos: the pass-on service of Talos Relay, run by this computer itself.

    phone page  <--the same network-->  talos relay (this computer: the page, the pass-on service, Talos)

`talos relay mode local` (or `both`) makes `talos relay` start it. A phone on the same Wi-Fi opens
http://<this computer>:4749 and no service on the internet is needed. What crosses the network is
sealed with the key of the link exactly as it is through the relay on the internet, so plain http
gives away no more than that relay sees.

The rules are those of the Pages Function (relay/functions/c/[[path]].js): change both together.
Two are stricter here, because every device on the network can reach this port:
  - only this computer (a request from 127.0.0.1) can make a channel its own;
  - a prompt or an answer nobody fetched is dropped after KEEP_S.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KINDS = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".json": "application/json; charset=utf-8"}
# the same as relay/site/_headers (tests/test_relay.py keeps the two equal)
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self' https://openrouter.ai https://api.groq.com https://api.cerebras.ai "
                               "https://oai.endpoints.kepler.ai.cloud.ovh.net https://api.llm7.io "
                               "https://text.pollinations.ai; "
                               "img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
}
MAX_STATE, MAX_ASK, MAX_ASKS, MAX_ANSWER = 300_000, 1_200_000, 30, 1_600_000
MAX_CHANNELS = 200          # far more than one computer and its code rooms ever hold
MAX_READ = 8_000_000        # a body larger than this is not even read: the connection is closed
KEEP_S = 300                # a prompt or an answer nobody fetched is dropped after this
IDLE_S = 120                # a connection that sends nothing is closed after this
CHANNEL = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ASK_ID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
TAKE_HOLD_S, ANSWER_HOLD_S, LOOK_S = 8, 6, 0.02


def site_folder():
    """The folder with the phone page: inside an installed Talos, or relay/site of a checkout. None without one."""
    here = Path(__file__).resolve().parent
    for folder in (here / "relay_site", here.parent / "relay" / "site"):
        if (folder / "index.html").is_file():
            return folder
    return None


def lan_addresses() -> list:
    """The IPv4 addresses of this computer a phone on the same network can use, the most likely first."""
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))           # sends nothing: asks which address leaves by the default route
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        found += [entry[4][0] for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    kept = []
    for address in found:
        if address not in kept and not address.startswith(("127.", "169.254.", "0.")):
            kept.append(address)
    return kept


class LocalRelay(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"         # on Windows this option would let two programs share the port

    def __init__(self, host: str = "127.0.0.1", port: int = 0, site=None):
        super().__init__((host, port), Handler)
        self.site = Path(site) if site else site_folder()
        self.lock = threading.Lock()
        self.owners, self.states, self.asks, self.answers = {}, {}, {}, {}
        self.seen = None                          # tests put a list here: every sealed body that passed through
        self.closing, self.swept = False, time.time()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def address(self) -> str:
        """Where this computer itself asks it."""
        return f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request, client_address) -> None:
        pass                                      # a caller that went away in the middle of a held request

    def sweep(self, now: float) -> None:
        """Drop what nobody fetched in KEEP_S. Called with the lock held."""
        if now - self.swept < 30:
            return
        self.swept = now
        for kept in (self.asks, self.answers):
            for channel in list(kept):
                for ident in [ident for ident, (moment, _) in kept[channel].items() if now - moment > KEEP_S]:
                    del kept[channel][ident]
                if not kept[channel]:
                    del kept[channel]

    def close(self) -> None:
        self.closing = True
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IDLE_S

    def log_message(self, *arguments) -> None:
        pass

    def reply(self, status: int, body="", kind: str = "text/plain", headers=None) -> None:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        for name, value in ({"Cache-Control": "no-store"} if headers is None else headers).items():
            self.send_header(name, value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Relay-Write")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if status != 204:
            self.wfile.write(data)

    def near(self) -> bool:
        """Is the caller this computer itself?"""
        return self.client_address[0].startswith("127.")

    def owner(self, channel: str, claim: bool) -> bool:
        token = self.headers.get("X-Relay-Write") or ""
        if len(token) < 20:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        known = self.server.owners.get(channel)
        if known is None:
            # a channel is made by this computer only: no other device on the network can take one
            claim = claim and self.near() and len(self.server.owners) < MAX_CHANNELS
            if claim:
                self.server.owners[channel] = digest
            return claim
        return known == digest

    def wanted(self, most: float) -> float:
        asked = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("wait", ["0"])[0]
        try:
            return min(most, max(0.0, float(asked)))
        except ValueError:
            return 0.0

    def page(self, name: str) -> None:
        site = self.server.site
        path = site / (name or "index.html") if site else None
        if path is None or path.suffix not in KINDS or not path.is_file() or path.parent != site:
            return self.reply(404, "Not found.")
        self.reply(200, path.read_bytes(), KINDS[path.suffix], PAGE_HEADERS)

    def handle_one(self, method: str) -> None:
        relay = self.server
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_ANSWER + 1000:
            left = length if 0 < length <= MAX_READ else 0
            while left > 0:                       # read and dropped, so that the caller gets the answer
                dropped = self.rfile.read(min(left, 65536))
                if not dropped:
                    break
                left -= len(dropped)
            self.close_connection = True
            return self.reply(413, "Too large.")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        parts = [part for part in self.path.split("?")[0].split("/") if part]
        if method == "OPTIONS":
            return self.reply(204)
        if method == "GET" and len(parts) < 2:
            return self.page(parts[0] if parts else "")
        if len(parts) < 2 or parts[0] != "c" or not CHANNEL.match(parts[1]):
            return self.reply(404, "Not found.")
        channel, what = parts[1], "/".join(parts[2:])
        if what == "asks/take" and method == "POST":
            until = time.time() + self.wanted(TAKE_HOLD_S)
            while time.time() < until and not relay.asks.get(channel) and channel in relay.owners and not relay.closing:
                time.sleep(LOOK_S)
        if len(parts) == 4 and parts[2] == "answer" and method == "GET":
            until = time.time() + self.wanted(ANSWER_HOLD_S)
            while time.time() < until and parts[3] not in relay.answers.get(channel, {}) and not relay.closing:
                time.sleep(LOOK_S)
        now = time.time()
        with relay.lock:
            relay.sweep(now)
            if body and relay.seen is not None:
                relay.seen.append(body)
            if what == "state" and method == "PUT":
                if not body or len(body) > MAX_STATE:
                    return self.reply(413, "Too large.")
                if not self.owner(channel, True):
                    return self.reply(403, "Not yours.")
                relay.states[channel] = body
                return self.reply(200, "ok")
            if what == "state" and method == "GET":
                state = relay.states.get(channel)
                return self.reply(404, "Nothing here.") if state is None else self.reply(200, state)
            if what == "" and method == "DELETE":
                if not self.owner(channel, False):
                    return self.reply(404, "Nothing here.")
                for kept in (relay.states, relay.owners, relay.asks, relay.answers):
                    kept.pop(channel, None)
                return self.reply(200, "ok")
            ask_id = parts[3] if len(parts) == 4 else ""
            if len(parts) == 4 and parts[2] == "ask" and method == "POST":
                if not ASK_ID.match(ask_id):
                    return self.reply(404, "Not found.")
                if not body or len(body) > MAX_ASK:
                    return self.reply(413, "Too large.")
                if channel not in relay.owners:
                    return self.reply(404, "Nothing here.")
                waiting = relay.asks.setdefault(channel, {})
                if len(waiting) >= MAX_ASKS:
                    return self.reply(429, "The computer has not picked up the last requests yet.")
                waiting[ask_id] = (now, body)
                return self.reply(200, "ok")
            if what == "asks/take" and method == "POST":
                if not self.owner(channel, False):
                    return self.reply(404, "Nothing here.")
                waiting = relay.asks.pop(channel, {})
                asks = [{"id": key, "body": value[1]} for key, value in waiting.items()]
                return self.reply(200, json.dumps({"asks": asks}), "application/json")
            if len(parts) == 4 and parts[2] == "answer" and ASK_ID.match(ask_id):
                if method == "GET":
                    answer = relay.answers.get(channel, {}).pop(ask_id, None)
                    return self.reply(204) if answer is None else self.reply(200, answer[1])
                if method == "PUT":
                    if not body or len(body) > MAX_ANSWER:
                        return self.reply(413, "Too large.")
                    if not self.owner(channel, False):
                        return self.reply(404, "Nothing here.")
                    relay.answers.setdefault(channel, {})[ask_id] = (now, body)
                    return self.reply(200, "ok")
        return self.reply(404, "Not found.")

    def do_GET(self) -> None:
        self.handle_one("GET")

    def do_POST(self) -> None:
        self.handle_one("POST")

    def do_PUT(self) -> None:
        self.handle_one("PUT")

    def do_DELETE(self) -> None:
        self.handle_one("DELETE")

    def do_OPTIONS(self) -> None:
        self.handle_one("OPTIONS")
