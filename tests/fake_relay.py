"""The relay's rules (relay/functions/c/[[path]].js) in memory, for tests on this computer.

Same paths, limits and answers as the Pages Function, so `talos relay` and the phone page meet here
what they meet on Cloudflare. It also hands out the phone page (relay/site), so a real browser can be
pointed at it. Everything is kept in dictionaries and forgotten when the test ends.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "relay" / "site"
KINDS = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json; charset=utf-8"}
MAX_STATE, MAX_ASK, MAX_ASKS, MAX_ANSWER = 300_000, 1_200_000, 30, 1_600_000
CHANNEL = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ASK_ID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
TAKE_HOLD_S, ANSWER_HOLD_S, LOOK_S = 8, 6, 0.02


class FakeRelay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.owners, self.states, self.asks, self.answers = {}, {}, {}, {}
        self.seen = []                            # every sealed body that passed through, for "it is sealed" checks
        self.closing = False
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def address(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request, client_address) -> None:
        pass                                      # a caller that was stopped in the middle of a held request

    def close(self) -> None:
        self.closing = True
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *arguments) -> None:
        pass

    def reply(self, status: int, body="", kind: str = "text/plain") -> None:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Relay-Write")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if status != 204:
            self.wfile.write(data)

    def owner(self, channel: str, claim: bool) -> bool:
        token = self.headers.get("X-Relay-Write") or ""
        if len(token) < 20:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        known = self.server.owners.get(channel)
        if known is None:
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
        path = SITE / (name or "index.html")
        if path.suffix not in KINDS or not path.is_file() or path.parent != SITE:
            return self.reply(404, "Not found.")
        self.reply(200, path.read_bytes(), KINDS[path.suffix])

    def handle_one(self, method: str) -> None:
        relay = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
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
        with relay.lock:
            if body:
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
                waiting[ask_id] = body
                return self.reply(200, "ok")
            if what == "asks/take" and method == "POST":
                if not self.owner(channel, False):
                    return self.reply(404, "Nothing here.")
                waiting = relay.asks.pop(channel, {})
                asks = [{"id": key, "body": value} for key, value in waiting.items()]
                return self.reply(200, json.dumps({"asks": asks}), "application/json")
            if len(parts) == 4 and parts[2] == "answer" and ASK_ID.match(ask_id):
                if method == "GET":
                    answer = relay.answers.get(channel, {}).pop(ask_id, None)
                    return self.reply(204) if answer is None else self.reply(200, answer)
                if method == "PUT":
                    if not body or len(body) > MAX_ANSWER:
                        return self.reply(413, "Too large.")
                    if not self.owner(channel, False):
                        return self.reply(404, "Nothing here.")
                    relay.answers.setdefault(channel, {})[ask_id] = body
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
