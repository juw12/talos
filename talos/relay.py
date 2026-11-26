"""Talos Relay: the Talos chats of this computer on your phone, from anywhere.

    phone page  --sealed prompt-->  relay  --sealed prompt-->  talos relay (this file)  -->  Talos
    phone page  <--sealed chats---  relay  <--sealed chats---  talos relay

The relay is a small pass-on service (relay/ in the repository: one Cloudflare Pages Function and
the phone page). It holds only text sealed with AES-256-GCM. The key is made here and reaches the
phone inside the link, after the #, which a browser sends to no server.

Talos does not depend on that service. `talos relay mode local` makes `talos relay` run the same
pass-on service and hand out the same page itself (relay_local.py), for a phone on the same network;
`both` does the two at once. The link, the key and what is sealed are the same either way.

Standard library only, so the cipher is written out below. It is checked against the published
test vectors and against a browser's own AES-GCM (tests/test_relay.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import platform
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__, config as config_module, qr, util

SITE = "https://talos-relay.pages.dev"
PROTOCOL = 1
LOCK_PORT = 4748            # held by the one `talos relay` of this computer
LOCAL_PORT = 4749           # where the relay inside Talos listens (relay.mode local or both; relay.port changes it)
MODES = ("relay", "local", "both")
MODE_WORDS = {"relay": "through the relay on the internet, from anywhere",
              "local": "straight to this computer on the same network, with no service on the internet",
              "both": "through the relay from anywhere, and straight to this computer on the same network"}
TAKE_WAIT_S = 8             # the relay holds a request for prompts this long
STATE_EVERY_S = 600         # how often the relay hears that this computer is still there
STATE_GAP_S = 5             # ... and how soon again when a chat started, ended or asks something
FRESH_S = 600               # a prompt older than this is not carried out
ROOM = 60_000               # characters of one chat sent to the phone
LISTED = 40                 # chats the phone is shown
PHOTO_CHARS = 800_000       # a photo from the phone, as base64 (the page makes it 600,000 at most)
WIRE_REST_S = 300           # after the kept connection failed, the plain way is used this long
CODE_VALID_S = 5 * 60       # a 4-digit code links a phone this long
CODE_TRIES = 3              # ... and takes this many answers; then it is dead, right or wrong
CODE_GRACE_S = 15           # the phone gets this long to fetch the last answer before the room is wiped
ROOM_EVERY_S = 5 * 60       # the room where a code is used gets a new name this often
MODELS_FRESH_S = 30         # the list of models the phone picks from is read again this often
MODELS_LISTED = 60          # models the phone is shown
PHONE_SERVICES = ("openrouter", "groq", "cerebras")   # services the phone page can ask with no computer
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---------------------------------------------------------------- AES-256-GCM

def _tables():
    box, p, q = [0] * 256, 1, 1
    while True:
        p = (p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        mixed = q ^ (q << 1 | q >> 7) ^ (q << 2 | q >> 6) ^ (q << 3 | q >> 5) ^ (q << 4 | q >> 4)
        box[p] = (mixed ^ 0x63) & 0xFF
        if p == 1:
            break
    box[0] = 0x63
    tables = [[], [], [], []]
    for value in box:
        double = (value << 1) ^ (0x11B if value & 0x80 else 0)
        triple = double ^ value
        tables[0].append(double << 24 | value << 16 | value << 8 | triple)
        tables[1].append(triple << 24 | double << 16 | value << 8 | value)
        tables[2].append(value << 24 | triple << 16 | double << 8 | value)
        tables[3].append(value << 24 | value << 16 | triple << 8 | double)
    return box, tables


SBOX, (T0, T1, T2, T3) = _tables()


class Cipher:
    """AES-256-GCM, the cipher every browser has built in (WebCrypto)."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("The key must be 32 bytes.")
        words, constant = list(struct.unpack(">8I", key)), 1
        for index in range(8, 60):
            word = words[-1]
            if index % 8 == 0:
                word = ((word << 8) | (word >> 24)) & 0xFFFFFFFF
            if index % 4 == 0:
                word = (SBOX[word >> 24] << 24 | SBOX[word >> 16 & 255] << 16
                        | SBOX[word >> 8 & 255] << 8 | SBOX[word & 255])
            if index % 8 == 0:
                word ^= constant << 24
                constant = (constant << 1) ^ (0x11B if constant & 0x80 else 0)
            words.append(words[-8] ^ word)
        self.keys = words
        self.h = int.from_bytes(self._block(bytes(16)), "big")

    def _block(self, block: bytes) -> bytes:
        keys = self.keys
        a, b, c, d = struct.unpack(">4I", block)
        a, b, c, d = a ^ keys[0], b ^ keys[1], c ^ keys[2], d ^ keys[3]
        for at in range(4, 56, 4):
            a, b, c, d = (
                T0[a >> 24] ^ T1[b >> 16 & 255] ^ T2[c >> 8 & 255] ^ T3[d & 255] ^ keys[at],
                T0[b >> 24] ^ T1[c >> 16 & 255] ^ T2[d >> 8 & 255] ^ T3[a & 255] ^ keys[at + 1],
                T0[c >> 24] ^ T1[d >> 16 & 255] ^ T2[a >> 8 & 255] ^ T3[b & 255] ^ keys[at + 2],
                T0[d >> 24] ^ T1[a >> 16 & 255] ^ T2[b >> 8 & 255] ^ T3[c & 255] ^ keys[at + 3])
        box = SBOX
        return struct.pack(
            ">4I",
            (box[a >> 24] << 24 | box[b >> 16 & 255] << 16 | box[c >> 8 & 255] << 8 | box[d & 255]) ^ keys[56],
            (box[b >> 24] << 24 | box[c >> 16 & 255] << 16 | box[d >> 8 & 255] << 8 | box[a & 255]) ^ keys[57],
            (box[c >> 24] << 24 | box[d >> 16 & 255] << 16 | box[a >> 8 & 255] << 8 | box[b & 255]) ^ keys[58],
            (box[d >> 24] << 24 | box[a >> 16 & 255] << 16 | box[b >> 8 & 255] << 8 | box[c & 255]) ^ keys[59])

    def _mix(self, nonce: bytes, data: bytes) -> bytes:
        """Counter mode: the same call seals and opens."""
        stream = b"".join(self._block(nonce + struct.pack(">I", count))
                          for count in range(2, 2 + (len(data) + 15) // 16))
        if not data:
            return b""
        return (int.from_bytes(data, "big") ^ int.from_bytes(stream[:len(data)], "big")).to_bytes(len(data), "big")

    def _tag(self, nonce: bytes, extra: bytes, sealed: bytes) -> bytes:
        h, total = self.h, 0
        whole = (extra + bytes(-len(extra) % 16) + sealed + bytes(-len(sealed) % 16)
                 + struct.pack(">QQ", len(extra) * 8, len(sealed) * 8))
        for at in range(0, len(whole), 16):
            total ^= int.from_bytes(whole[at:at + 16], "big")
            product, power = 0, h
            for bit in range(127, -1, -1):                 # one multiplication in GF(2^128)
                if total >> bit & 1:
                    product ^= power
                power = (power >> 1) ^ (0xE1 << 120 if power & 1 else 0)
            total = product
        first = int.from_bytes(self._block(nonce + b"\x00\x00\x00\x01"), "big")
        return (total ^ first).to_bytes(16, "big")

    def seal(self, text: bytes, extra: bytes = b"", nonce: bytes = b"") -> bytes:
        """nonce (12 bytes) + sealed text + tag (16 bytes). `extra` is not hidden, but must match to open."""
        nonce = nonce or secrets.token_bytes(12)
        sealed = self._mix(nonce, text)
        return nonce + sealed + self._tag(nonce, extra, sealed)

    def open(self, data: bytes, extra: bytes = b"") -> bytes:
        if len(data) < 28:
            raise ValueError("This was not sealed with the key of this computer.")
        nonce, sealed, tag = data[:12], data[12:-16], data[-16:]
        if not hmac.compare_digest(tag, self._tag(nonce, extra, sealed)):
            raise ValueError("This was not sealed with the key of this computer.")
        return self._mix(nonce, sealed)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- the link between this computer and its phones

def link_path() -> Path:
    return util.home() / "relay.json"


def load_link():
    link = util.load_json_file(link_path())
    return link if isinstance(link, dict) and all(link.get(name) for name in ("url", "channel", "key", "token")) else None


def make_link(config: dict) -> dict:
    """A new channel and key. The old phones stop working: they hold the old key."""
    link = {"url": str((config.get("relay") or {}).get("url") or SITE).rstrip("/"),
            "channel": secrets.token_urlsafe(16), "key": b64(secrets.token_bytes(32)),
            "token": secrets.token_urlsafe(32), "computer": platform.node() or "computer"}
    util.save_json_file(link_path(), link)
    try:
        link_path().chmod(0o600)
    except OSError:
        pass
    return link


def phone_address(link: dict) -> str:
    return f"{link['url']}/#{link['channel']}.{link['key']}"


def mode_of(config: dict) -> str:
    """relay: the service on the internet. local: the relay inside Talos. both: the two at once."""
    mode = str((config.get("relay") or {}).get("mode") or "relay").strip().lower()
    return mode if mode in MODES else "relay"


def phone_services(config: dict) -> list:
    """The services whose key a linked phone may fetch, to chat with no computer (relay.phone_keys; none if unset)."""
    value = (config.get("relay") or {}).get("phone_keys") or []
    named = [str(name).strip().lower() for name in (value if isinstance(value, (list, tuple)) else str(value).split(","))]
    return [name for name in PHONE_SERVICES if name in named]


def local_port(config: dict) -> int:
    try:
        port = int((config.get("relay") or {}).get("port") or LOCAL_PORT)
    except (TypeError, ValueError):
        port = LOCAL_PORT
    return port if 0 < port < 65536 else LOCAL_PORT


def local_host(config: dict) -> str:
    """Where the relay inside Talos listens: every network of this computer, unless relay.host names one."""
    return str((config.get("relay") or {}).get("host") or "0.0.0.0")


def local_link(link: dict, config: dict) -> dict:
    """The same link, asked at the relay inside this computer."""
    return dict(link, url=f"http://127.0.0.1:{local_port(config)}")


def local_addresses(config: dict) -> list:
    """Where a phone on the same network finds the relay inside this computer, the most likely first."""
    from . import relay_local
    host = local_host(config)
    names = relay_local.lan_addresses() if host == "0.0.0.0" else [host]
    return [f"http://{name}:{local_port(config)}" for name in names]


class Wire:
    """One connection to the relay, kept open between requests.

    Behind an office proxy a new connection cost about 2.2 s per request (measured 2026-11-25:
    proxy, tunnel, TLS). A kept one pays that once."""

    def __init__(self, url: str):
        parts = urllib.parse.urlsplit(url)
        self.secure, self.host = parts.scheme == "https", parts.hostname or ""
        self.port = parts.port or (443 if self.secure else 80)
        self.root = parts.path.rstrip("/")
        self.conn, self.lock = None, threading.Lock()

    def _connect(self, timeout: float):
        local = self.host in ("localhost", "127.0.0.1", "::1")
        proxy = ""
        if not local and not urllib.request.proxy_bypass(self.host):
            proxy = urllib.request.getproxies().get("https" if self.secure else "http", "")
        if not self.secure:
            if proxy:
                raise OSError("a plain http relay behind a proxy is asked the plain way")
            return http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        if not proxy:
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=util.tls_context())
        via = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if via.scheme != "http" or not via.hostname:
            raise OSError("this kind of proxy is asked the plain way")
        conn = http.client.HTTPSConnection(via.hostname, via.port or 80, timeout=timeout, context=util.tls_context())
        headers = {}
        if via.username:
            name = urllib.parse.unquote(via.username) + ":" + urllib.parse.unquote(via.password or "")
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(name.encode("utf-8")).decode("ascii")
        conn.set_tunnel(self.host, self.port, headers)
        return conn

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn:
            try:
                conn.close()
            except OSError:
                pass

    def __del__(self):
        self.close()

    def call(self, method: str, path: str, body, headers: dict, timeout: float):
        """(status, text). A kept connection the other side closed meanwhile is opened again once."""
        data = body.encode("utf-8") if isinstance(body, str) else body
        with self.lock:
            while True:
                fresh = self.conn is None
                try:
                    if fresh:
                        self.conn = self._connect(timeout)
                    elif self.conn.sock:
                        self.conn.sock.settimeout(timeout)
                    self.conn.request(method, self.root + path, body=data, headers=headers)
                    response = self.conn.getresponse()
                    text = response.read().decode("utf-8", errors="replace")
                    if response.will_close:
                        self.close()
                    return response.status, text
                except (OSError, http.client.HTTPException):
                    self.close()
                    if fresh:
                        raise


class Line:
    """Sealed text to and from the relay, for one link."""

    def __init__(self, link: dict):
        self.link, self.cipher = link, Cipher(unb64(link["key"]))
        self.base = f"{link['url']}/c/{link['channel']}"
        self.mine = {"X-Relay-Write": link["token"], "Content-Type": "text/plain", "User-Agent": util.USER_AGENT}
        self.wire, self.rest = Wire(self.base), 0.0

    def call(self, method: str, path: str, body=None, timeout: float = 25):
        """(status, text), over the kept connection; the plain way when that one does not work here."""
        if time.time() >= self.rest:
            try:
                return self.wire.call(method, path, "" if body is None and method in ("POST", "PUT") else body,
                                      self.mine, timeout)
            except (OSError, http.client.HTTPException):
                pass
        result = util.http(method, self.base + path, body, self.mine, timeout)   # raises when the relay is not reached
        if time.time() >= self.rest:
            self.rest = time.time() + WIRE_REST_S       # the plain way works and the kept connection does not
        return result

    def _extra(self, kind: str, ident: str) -> bytes:
        return f"talos-relay/{PROTOCOL}/{self.link['channel']}/{kind}/{ident}".encode()

    def seal(self, kind: str, ident: str, data) -> str:
        return b64(self.cipher.seal(json.dumps(data, ensure_ascii=False).encode("utf-8"), self._extra(kind, ident)))

    def open(self, kind: str, ident: str, text: str):
        try:
            return json.loads(self.cipher.open(unb64(text), self._extra(kind, ident)).decode("utf-8"))
        except (ValueError, TypeError) as error:           # also: not base64, not JSON
            raise ValueError("This was not sealed with the key of this computer.") from error

    def put_state(self, data) -> int:
        """Tell the relay this computer is there. The first call makes the channel this computer's."""
        return self.call("PUT", "/state", self.seal("state", "", data))[0]

    def take(self, wait: float = TAKE_WAIT_S):
        """(status, [(id, sealed prompt), ...]): what the phones left since the last call."""
        status, text = self.call("POST", f"/asks/take?wait={wait:g}", "", wait + 20)
        try:
            data = json.loads(text) if status == 200 else None
        except ValueError:
            data = None
        asks = data.get("asks") if isinstance(data, dict) else None
        return status, [(str(item.get("id")), str(item.get("body"))) for item in asks or [] if isinstance(item, dict)]

    def answer(self, ident: str, data) -> int:
        return self.call("PUT", f"/answer/{ident}", self.seal("answer", ident, data))[0]

    def forget(self) -> int:
        return self.call("DELETE", "")[0]


# ---------------------------------------------------------------- what the phone is shown

def _target(arguments) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return arguments[:140]
    if not isinstance(arguments, dict):
        return ""
    for name in ("command", "filePath", "file_path", "path", "pattern", "url", "query", "name", "description", "prompt"):
        if arguments.get(name):
            return re.sub(r"\s+", " ", str(arguments[name]))[:140]
    return ""


def shape(messages: list, room: int = ROOM):
    """A chat as the phone shows it: what you said, what Talos said, and each step in one line.
    Returns (items, how many older ones were left out to fit)."""
    items = []
    for message in messages:
        role, content, when = message.get("role"), message.get("content"), int(message.get("_t") or 0)
        text, picture = content if isinstance(content, str) else "(attachment)", False
        if isinstance(content, list):                     # a prompt with a photo: its words and the picture
            words = [str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            picture = any(isinstance(part, dict) and part.get("type") == "image_url" for part in content)
            text = "\n".join(word for word in words if word) or ("(photo)" if picture else "(attachment)")
        if role == "user":
            item = {"role": "you", "text": util.clip(text, 4000), "t": when}
            if picture and message.get("_seq"):
                item["img"] = {"at": message["_seq"]}
            items.append(item)
        elif role == "assistant":
            if text.strip():
                items.append({"role": "talos", "text": util.clip(text, 8000), "t": when})
            for call in message.get("tool_calls") or []:
                items.append({"role": "step", "name": str(call.get("name") or ""),
                              "target": _target(call.get("arguments")), "result": None, "t": when})
        elif role == "tool":
            for item in reversed(items):
                if item["role"] == "step" and item["result"] is None:
                    item["result"] = util.clip(text, 300)
                    break
    kept, size = [], 0
    for item in reversed(items):
        size += len(json.dumps(item, ensure_ascii=False))
        if size > room and kept:
            break
        kept.append(item)
    return kept[::-1], len(items) - len(kept)


class Relay:
    """Answers the phone: lists the chats, shows one, starts a prompt, stops a run."""

    def __init__(self, config: dict, store, link: dict, overrides=None, auto_yes=False, show=print):
        from . import server
        self.app = server.App(config, store, overrides)
        self.config, self.store, self.link, self.line = config, store, link, Line(link)
        self.auto_yes, self.show = bool(auto_yes or (config.get("relay") or {}).get("yes")), show
        self.folder = str(Path((config.get("relay") or {}).get("folder") or self.app.cwd).expanduser().resolve())
        self.seen, self.said, self.told, self.listed = {}, {}, ("", 0.0), (0.0, [])
        self.busy = threading.Lock()                  # one request at a time, also with two relays (mode both)

    # ------------------------------------------------------------ views
    def fresh(self) -> dict:
        """The settings as they are now: a model or a key added while this runs is seen."""
        try:
            return config_module.load(self.folder, self.app.overrides)
        except (OSError, ValueError):
            return self.config

    def models(self) -> list:
        """What the phone picks from: the model in use, the spares, then every other model this computer knows."""
        when, names = self.listed
        if not names or time.time() - when > MODELS_FRESH_S:
            config, names = self.fresh(), []
            for name in ([config_module.pick_default_model(config)] + config_module.spares(config)
                         + config_module.list_models(config)):
                if name and name not in names:
                    names.append(name)
            names = names[:MODELS_LISTED]
            self.listed = (time.time(), names)
        return list(names)

    def phone_keys(self) -> dict:
        """The keys the owner lets a linked phone fetch. Only a key that goes to the service's own address."""
        config, found = self.fresh(), {}
        for name in phone_services(config):
            try:
                model = config_module.resolve_model(config, f"{name}/any")
            except ValueError:
                continue
            if model["api_key"] and model["base_url"] == config_module.PRESETS[name][0].rstrip("/"):
                found[name] = model["api_key"]
        return found

    def folders(self) -> list:
        found = [self.folder]
        for item in self.store.sessions(60):
            if item.get("cwd") and item["cwd"] not in found and Path(item["cwd"]).is_dir():
                found.append(item["cwd"])
        return found[:12]

    def live(self, session: str):
        """What the run of this chat is doing right now, or how it ended."""
        run = self.app.runs.get(session)
        if not run:
            return None
        with run.changed:
            events, finished = list(run.events), run.finished
        text, asks, notes, done, steps = "", {}, [], None, []
        for item in events:
            event, data = item["event"], item["data"]
            if event == "text":
                text += str(data)
            elif event in ("assistant", "tool"):
                text = ""
                if event == "tool" and isinstance(data, dict):
                    steps.append([int(item.get("t") or 0),
                                  (str(data.get("name") or "") + "  " + _target(data.get("arguments"))).strip()])
            elif event in ("permission", "question"):
                asks[data["id"]] = dict(data, kind=event)
            elif event == "answered":
                asks.pop(data["id"], None)
            elif event == "warning":
                notes.append(util.clip(str(data), 400))
            elif event == "done":
                done = {"stopped": data.get("stopped") or "", "proof": data.get("proof") or "",
                        "text": util.clip(str(data.get("text") or ""), 2000)
                        if data.get("stopped") in ("model error", "error") else ""}
        started = getattr(run, "started", None) or (events[0].get("t") if events else 0) or time.time()
        return {"running": not finished, "text": text[-3000:], "notes": notes[-3:], "done": done,
                "ask": None if finished or not asks else list(asks.values())[-1],
                "steps": steps[-8:], "since": int(started), "seconds": int(time.time() - started)}

    def hidden(self, add: str = "") -> list:
        """The chats taken off the phone's list (they stay on the computer)."""
        path = util.home() / "relay-hidden.json"
        kept = util.load_json_file(path)
        kept = [str(item) for item in kept] if isinstance(kept, list) else []
        if add and add not in kept:
            kept = (kept + [add])[-500:]
            util.save_json_file(path, kept)
        return kept

    def chats(self) -> list:
        """Every chat as one row of the phone's list: is it working, does it ask, its last step."""
        hidden, rows = set(self.hidden()), []
        for item in self.store.sessions(LISTED + len(hidden)):
            ident = item["id"]
            if ident in hidden or len(rows) >= LISTED:
                continue
            running = self.app.busy(ident)
            row = {"id": ident, "title": item.get("title") or "(no title)", "updated": item.get("updated"),
                   "started": int(item.get("created") or item.get("updated") or 0), "running": running,
                   "folder": Path(item.get("cwd") or "").name, "source": item.get("source") or "",
                   "waits": False, "step": "", "steps": []}
            if running:
                live = self.live(ident) or {}
                row["waits"], row["steps"] = bool(live.get("ask")), (live.get("steps") or [])[-6:]
                row["step"] = row["steps"][-1][1] if row["steps"] else ""
            else:
                kept = self.said.get(ident)
                if not kept or kept[0] != item.get("updated"):
                    words = " ".join(self.store.last_said(ident).split())
                    kept = self.said[ident] = (item.get("updated"), words[:200])
                    if len(self.said) > 400:
                        self.said.pop(next(iter(self.said)))
                row["step"] = kept[1]
            rows.append(row)
        return rows

    def look(self, session: str = "") -> dict:
        answer = {"ok": True, "protocol": PROTOCOL, "version": __version__, "computer": self.link.get("computer", ""),
                  "model": config_module.pick_default_model(self.config), "models": self.models(),
                  "asks_first": not self.auto_yes,
                  "folders": self.folders(), "chats": self.chats(), "chat": None, "pc": int(time.time())}
        info = self.store.session(session) if session else None
        if info:
            messages = [dict(message, _t=when, _seq=seq) for seq, when, message in self.store.timed_messages(session)
                        if message.get("role") != "system" and not message.get("internal")]
            items, earlier = shape(messages)
            answer["chat"] = {"id": info["id"], "title": info.get("title") or "(no title)", "folder": info.get("cwd") or "",
                              "model": info.get("model") or "", "running": self.app.busy(session), "items": items,
                              "earlier": earlier, "live": self.live(info["id"]),
                              "prompts": sum(1 for message in messages if message.get("role") == "user")}
        elif session:
            answer["gone"] = session
        return answer

    def picture(self, session: str, at) -> dict:
        """The photo that came with one prompt of a chat, for the phone that shows that chat."""
        for seq, _, message in self.store.timed_messages(session):
            content = message.get("content")
            if seq != at or message.get("role") != "user" or not isinstance(content, list):
                continue
            for part in content:
                address = str(((part or {}).get("image_url") or {}).get("url") or "") if isinstance(part, dict) else ""
                if address.startswith("data:image/") and ";base64," in address and len(address) < 1_400_000:
                    return {"ok": True, "data": address.split(";base64,", 1)[1],
                            "kind": address[5:address.index(";")]}
        return {"ok": False}

    def photo(self, data) -> dict:
        """A photo from the phone as the attachment the model reads. Raises ValueError with a plain reason."""
        if not isinstance(data, str) or not data or len(data) > PHOTO_CHARS:
            raise ValueError("The photo is too large for the computer. Send a smaller one.")
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError:
            raw = b""
        if raw[:3] != b"\xff\xd8\xff":
            raise ValueError("The computer could not read the photo.")
        return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}}

    # ------------------------------------------------------------ requests
    def handle(self, ask: dict) -> dict:
        op, session = ask.get("op"), str(ask.get("session") or "")
        if op == "look":
            return self.look(session)
        try:
            age = abs(time.time() - float(ask.get("t") or 0) / 1000)
        except (TypeError, ValueError):
            age = FRESH_S + 1
        if age > FRESH_S:
            return {"error": "The clock of the phone and the clock of the computer are more than 10 minutes apart. "
                             "Set the time right, then send again."}
        if op == "image":
            try:
                return self.picture(session, int(ask.get("at") or 0))
            except (TypeError, ValueError):
                return {"ok": False}
        if op == "send":
            text, folder = str(ask.get("text") or "").strip(), str(ask.get("folder") or "")
            model = str(ask.get("model") or "")
            if model and model not in self.models():
                return {"error": f"This computer does not have {model}. Pick another model."}
            try:
                attachments = [self.photo(ask.get("img"))] if ask.get("img") else None
                if not text and attachments:
                    text = "Look at this photo."
                if not text:
                    return {"error": "Type a message first."}
                started, run = self.app.start(session, text, model=model,
                                              cwd=folder if folder in self.folders() else self.folder,
                                              source="relay", auto_yes=self.auto_yes, attachments=attachments)
            except ValueError as error:
                return {"error": str(error)}
            run.started = time.time()
            return {"ok": True, "session": started}
        if op == "hide":
            if not self.store.session(session):
                return {"ok": False, "error": "That chat is no longer on the computer."}
            if self.app.busy(session):
                return {"ok": False, "error": "This chat is still working. Stop it first."}
            self.hidden(session)
            return {"ok": True, "chats": self.chats()}
        if op == "stop":
            run = self.app.runs.get(session)
            waiting = (self.live(session) or {}).get("ask")
            if run:
                run.abort.set()
            if waiting:                                   # a run that waits for a yes or no ends at once
                self.app.reply(waiting["id"], "reject")
            return {"ok": bool(run)}
        if op == "reply":
            return {"ok": self.app.reply(str(ask.get("id")), ask.get("answer"))}
        if op == "keys":                                  # for the phone alone; nothing unless the owner named it
            return {"ok": True, "keys": self.phone_keys()}
        return {"error": "The Talos on the computer is older than this page and does not know that request. "
                         "Update it: talos upgrade."}

    def copy(self, rows=None) -> dict:
        """What the relay keeps of this computer, sealed: the phone shows it while the computer does not answer."""
        return {"computer": self.link.get("computer", ""), "t": int(time.time() * 1000), "version": __version__,
                "pc": int(time.time()), "chats": self.chats() if rows is None else rows}

    def serve(self, stop=None, line=None) -> str:
        """Carry requests until `stop` is set. Returns the reason it ended by itself, or ''.
        `line` is the relay asked: the one of the link, unless another is given (the relay inside Talos)."""
        stop, line = stop or threading.Event(), line or self.line
        told, failures, renewed, listed = "", 0, 0.0, None
        while not stop.is_set():
            try:
                rows = self.chats()
                mark = [(row["id"], row["title"], row["running"], row["waits"]) for row in rows]
                if time.time() - renewed > STATE_EVERY_S or (mark != listed and time.time() - renewed > STATE_GAP_S):
                    status = line.put_state(self.copy(rows))
                    if status == 403:
                        return ("The relay holds this link for another computer. Make a new link: "
                                "talos relay pair --new")
                    if status != 200:
                        raise OSError(f"the relay answered HTTP {status}")
                    renewed, listed = time.time(), mark
                status, asks = line.take()
                if status == 404:                         # the relay forgot this computer: say again who it is
                    renewed = 0.0
                    continue
                if status != 200:
                    raise OSError(f"the relay answered HTTP {status}")
            except (OSError, http.client.HTTPException) as error:
                failures += 1
                if failures == 3 and told != "down":
                    told = "down"
                    self.show(f"Talos Relay cannot reach {line.link['url']} ({error}). It keeps trying.")
                stop.wait(min(60, 2 ** failures))
                continue
            if told == "down":
                self.show(f"Talos Relay reaches {line.link['url']} again.")
            told, failures = "", 0
            for ident, body in asks:
                if ident in self.seen:
                    continue
                self.seen[ident] = True
                if len(self.seen) > 2000:
                    self.seen.pop(next(iter(self.seen)))
                try:
                    ask = line.open("ask", ident, body)
                    with self.busy:
                        answer = self.handle(ask) if isinstance(ask, dict) else None
                except ValueError:
                    answer = None                         # not sealed with this key: no answer at all
                except Exception as error:                # one bad request must not end the service
                    answer = {"error": f"Talos could not do that: {type(error).__name__}: {error}"}
                if answer is not None:
                    try:
                        line.answer(ident, answer)
                    except (OSError, http.client.HTTPException):
                        pass                              # the phone asks again
        return ""


# ---------------------------------------------------------------- a 4-digit code instead of the QR code
# How 4 digits can stand in for a 256-bit key.
#
# Both sides turn the code into a number, raise it to a secret of their own and swap the results
# (SPEKE, in the 2048-bit group of RFC 3526). Only two sides that started from the same code end
# with the same key. What crosses the relay says nothing about the code: nobody can try the 10,000
# codes at home, and each guess has to be sent to this computer, which takes CODE_TRIES of them.
# The link with the real key then travels sealed with the key the two just made.
#
# The swap happens in a "room": a channel of the relay whose name comes from the time alone, so the
# name gives the code away to nobody. One computer at a time can hold it.
CODE_PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)


def code_room(moment: float) -> str:
    """The channel where a code is used in the ROOM_EVERY_S that hold this moment."""
    named = hashlib.sha256(f"talos-relay pair room v1|{int(moment // ROOM_EVERY_S)}".encode("ascii")).digest()
    return "pair" + b64(named)[:28]


def code_base(code: str) -> int:
    """The code as a member of the group: 320 hashed bytes, squared so that it lands in the large half."""
    wide = b"".join(hashlib.sha256(b"talos-relay pair v1|" + code.encode("utf-8") + bytes([count])).digest()
                    for count in range(10))
    return pow(int.from_bytes(wide, "big") % CODE_PRIME, 2, CODE_PRIME)


def code_public(code: str, secret: int) -> int:
    """What one side sends the other."""
    return pow(code_base(code), secret, CODE_PRIME)


def code_keys(secret: int, theirs: int, computer: int, phone: int):
    """(the proof the phone sends, the key that seals the link), or None for a number no honest side sends."""
    if not 1 < theirs < CODE_PRIME - 1:
        return None
    shared = pow(theirs * theirs % CODE_PRIME, secret, CODE_PRIME)
    key = hashlib.sha256(b"talos-relay pair v1 key|" + b"".join(
        number.to_bytes(256, "big") for number in (computer, phone, shared))).digest()
    return hmac.new(key, b"proof", "sha256").digest(), hmac.new(key, b"seal", "sha256").digest()


def offer_code(link: dict, out, valid: float = CODE_VALID_S, grace: float = CODE_GRACE_S, shown=None) -> str:
    """Show 4 digits and wait for the phone that types them. Returns how it ended:
    'linked', 'wrong' (answered wrong CODE_TRIES times), 'expired', 'taken' or 'unreachable'."""
    code, secret = f"{secrets.randbelow(10_000):04d}", 2 + secrets.randbits(256)
    mine, started, token = code_public(code, secret), time.time(), secrets.token_urlsafe(24)
    head = {"X-Relay-Write": token, "Content-Type": "text/plain"}
    while True:
        now = time.time()
        room = f"{link['url']}/c/{code_room(now)}"
        try:
            status = util.http("PUT", room + "/state", json.dumps(
                {"v": 1, "a": b64(mine.to_bytes(256, "big")), "t": int(now * 1000)}), head, 25)[0]
        except (OSError, http.client.HTTPException) as error:
            out(f"No code: the relay could not be reached ({error}). The QR code still works: talos relay pair")
            return "unreachable"
        # the room is kept a moment after a code was used: a second computer waits that long for it
        if status != 403 or now - started > grace + 5:
            break
        time.sleep(2)
    if status != 200:
        out("No code: another computer is showing a code on this relay right now. Try again in a few minutes."
            if status == 403 else f"No code: the relay answered HTTP {status}. The QR code still works: talos relay pair")
        return "taken" if status == 403 else "unreachable"
    out(f"Code for the phone: {code}")
    out(f"On the phone: Device, + Add device, a name, these 4 digits. It links one phone, within "
        f"{max(1, round(valid / 60))} minutes. Leave this running until then.")
    if shown:
        shown(code)
    tries, ended, until = CODE_TRIES, "expired", now + valid
    try:
        while ended == "expired" and time.time() < until:
            hold = int(max(1, min(TAKE_WAIT_S, until - time.time())))
            try:
                status, data = util.http_json("POST", f"{room}/asks/take?wait={hold}", "", head, hold + 20)
            except (OSError, http.client.HTTPException):
                time.sleep(3)
                continue
            if status == 404:                              # the room is another computer's by now
                break
            asks = data.get("asks") if status == 200 and isinstance(data, dict) else None
            for ask in asks or []:
                try:
                    said = json.loads(str(ask.get("body") or "")[:2000])
                    theirs, proof = int.from_bytes(unb64(said["b"]), "big"), unb64(said["m"])
                    keys = code_keys(secret, theirs, mine, theirs)
                except (ValueError, KeyError, TypeError, AttributeError):
                    keys, proof = None, b""
                reply = {"ok": False, "left": tries - 1}
                if keys is not None and hmac.compare_digest(keys[0], proof):
                    problem = claim(link)                  # the phone finds this computer at the relay at once
                    if problem:
                        out(problem)
                    sealed = Cipher(keys[1]).seal(json.dumps({"link": phone_address(link)}).encode("utf-8"))
                    reply, ended = {"ok": True, "link": b64(sealed)}, "linked"
                else:
                    tries -= 1
                    ended = "wrong" if tries <= 0 else ended
                try:
                    util.http("PUT", f"{room}/answer/{ask.get('id')}", json.dumps(reply), head, 25)
                except (OSError, http.client.HTTPException):
                    pass
                if ended != "expired":
                    break
            if not asks:
                time.sleep(0.2)                            # a relay that does not hold the request
        if ended in ("linked", "wrong"):
            time.sleep(grace)                              # the phone fetches the answer, then the room is wiped
    finally:
        try:
            util.http("DELETE", room, None, head, 25)
        except (OSError, http.client.HTTPException):
            pass
    out({"linked": "A phone was linked with the code.",
         "wrong": f"The code was answered wrong {CODE_TRIES} times, so it is dead. Make a new one: talos relay code",
         "expired": "The code ran out before a phone used it. Make a new one: talos relay code"}[ended])
    return ended


# ---------------------------------------------------------------- the commands

PAIR_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Talos Relay: link your phone</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<body style="font:16px/1.5 Inter,'Segoe UI',system-ui,sans-serif;background:#1a1a1a;color:#f5f5f5;margin:0">
<main style="max-width:34rem;margin:0 auto;padding:2rem 1.2rem">
<h1 style="font-size:1.4rem;font-weight:600;margin:0 0 .3rem">Talos Relay</h1>
<p style="margin:.2rem 0 1.2rem;color:#bbb">Link your phone to Talos on <b>{computer}</b>.</p>
{code}
<ol style="padding-left:1.2rem">
<li>Scan the code with the phone's camera and open the page.</li>
<li>iPhone: Share, then <b>Add to Home Screen</b>. Android: the browser menu, then <b>Add to Home screen</b>.</li>
<li>Keep <code>talos relay</code> running on this computer. {reach}</li>
</ol>
<p style="color:#bbb">A second computer on the same phone: run <code>talos relay code</code> there and type its 4
digits on the phone (Device, + Add device). That goes through the relay on the internet.</p>
<p style="color:#ff7a52"><b>Whoever has this code or link can give Talos on this computer work to do.</b>
Do not share it, and close this page when the phone is linked. Lost the phone:
<code>talos relay pair --new</code> makes a new link and the old one stops working.</p>
<p style="color:#6b6b6b;font-size:.85rem;word-break:break-all">No camera at hand? Send yourself this link in a
way only you can read:<br>{address}</p>
</main></body></html>
"""
PAIR_CODE = ('<p style="margin:1rem 0 .4rem;color:#bbb">{title}</p>'
             '<div style="background:#fff;border:1px solid #383838;border-radius:14px;padding:1rem;text-align:center">'
             '{code}</div>')
PAIR_TITLES = {"relay": "From anywhere, through the relay on the internet",
               "local": "On the same Wi-Fi as this computer, with nothing on the internet"}
PAIR_REACH = {"relay": "The phone reaches it from anywhere.",
              "local": "The phone reaches it on the same Wi-Fi as this computer.",
              "both": "The first code works from anywhere. The second works on the same Wi-Fi as this computer, "
                      "also when the internet is down."}
FIREWALL = ("The first time, Windows asks whether Python may be reached on private networks: allow it, "
            "or the phone gets no answer on the same network.")


def pair_entries(link: dict, config: dict) -> list:
    """[(which, address for the phone), ...] for the mode in use: 'relay', 'local' or the two."""
    mode, entries = mode_of(config), []
    if mode != "local":
        entries.append(("relay", phone_address(link)))
    if mode != "relay":
        for address in local_addresses(config)[:1]:
            entries.append(("local", phone_address(dict(link, url=address))))
    return entries


def listening() -> bool:
    """Is a `talos relay` of this computer running? Asked by trying to take its port: connecting to it
    instead fills the short queue of a port nobody reads, and Windows then refuses, so it looked stopped."""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", LOCK_PORT))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def launcher() -> list:
    """The command that starts Talos Relay without a window, in the folder new chats work in."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "relay"]
    workspace = Path(os.environ.get("TALOS_WORKSPACE") or util.user_home() / "Talos")
    workspace.mkdir(parents=True, exist_ok=True)
    script = util.home() / "talos-relay.pyw"
    util.write_text(script, "import os, sys\n"
                    f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
                    f"os.chdir({str(workspace)!r})\n"
                    "from talos.cli import main\nmain(['relay'])\n")
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return [str(windowless if windowless.is_file() else sys.executable), str(script)]


def login_entry(system: str, command: list):
    """(file, its text) that starts `command` at sign-in on macOS ('Darwin': a launchd agent) and on
    Linux (a systemd user service when `systemd` is given, else an XDG autostart entry)."""
    home = util.user_home()
    if system == "Darwin":
        items = "".join(f"    <string>{_xml(part)}</string>\n" for part in command)
        return home / "Library" / "LaunchAgents" / "dev.talos.relay.plist", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n  <key>Label</key>\n  <string>dev.talos.relay</string>\n'
            f"  <key>ProgramArguments</key>\n  <array>\n{items}  </array>\n"
            "  <key>RunAtLoad</key>\n  <true/>\n  <key>ProcessType</key>\n  <string>Background</string>\n"
            "</dict>\n</plist>\n")
    quoted = " ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"' for part in command)
    if system == "systemd":
        return home / ".config" / "systemd" / "user" / "talos-relay.service", (
            "[Unit]\nDescription=Talos Relay: the Talos chats of this computer on your phone\n"
            "After=network-online.target\n\n"
            f"[Service]\nExecStart={quoted}\nRestart=on-failure\nRestartSec=30\n\n"
            "[Install]\nWantedBy=default.target\n")
    return home / ".config" / "autostart" / "talos-relay.desktop", (
        "[Desktop Entry]\nType=Application\nName=Talos Relay\n"
        f"Exec={quoted}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _login_kind() -> str:
    if platform.system() == "Darwin":
        return "Darwin"
    return "systemd" if util.find_program("systemctl") and os.environ.get("XDG_RUNTIME_DIR") else "xdg"


def at_login(change=None):
    """Read (None), set (a command) or clear ('') what starts Talos Relay at sign-in. Windows: the Run
    key of this account. macOS: a launchd agent. Linux: a systemd user service, else XDG autostart."""
    if os.name != "nt":
        kind = _login_kind()
        if change is None:
            for each in ("Darwin",) if kind == "Darwin" else ("systemd", "xdg"):
                path = login_entry(each, [])[0]
                if path.is_file():
                    return str(path)
            return ""
        for each in ("Darwin",) if kind == "Darwin" else ("systemd", "xdg"):
            path = login_entry(each, [])[0]
            if path.is_file():                             # the old entry first: it may be of the other kind
                if each == "Darwin":
                    _quiet(["launchctl", "unload", str(path)])
                elif each == "systemd":
                    _quiet(["systemctl", "--user", "disable", "--now", "talos-relay.service"])
                path.unlink()
        if not change:
            return ""
        path, text = login_entry(kind, change)
        util.write_text(path, text)
        if kind == "systemd":
            _quiet(["systemctl", "--user", "daemon-reload"])
            _quiet(["systemctl", "--user", "enable", "talos-relay.service"])
        return str(path)
    import winreg
    if change is None:                                    # only asking: a new Windows account has no such key yet
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, "TalosRelay")[0]
        except FileNotFoundError:
            return ""
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        if change:
            winreg.SetValueEx(key, "TalosRelay", 0, winreg.REG_SZ, subprocess.list2cmdline(change))
        elif change == "":
            try:
                winreg.DeleteValue(key, "TalosRelay")
            except FileNotFoundError:
                pass
        try:
            return winreg.QueryValueEx(key, "TalosRelay")[0]
        except FileNotFoundError:
            return ""


def _quiet(command: list) -> None:
    try:
        subprocess.run(command, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def claim(link: dict) -> str:
    """Make the channel this computer's on the relay. Returns '' or a plain reason."""
    try:
        status = Line(link).put_state({"computer": link.get("computer", ""), "t": int(time.time() * 1000),
                                       "version": __version__})
    except (OSError, http.client.HTTPException) as error:
        return f"The relay at {link['url']} could not be reached ({error}). The link works once it can."
    return "" if status == 200 else f"The relay at {link['url']} answered HTTP {status}. The link works once it accepts this computer."


def command(args, config, store, out, overrides=None) -> int:
    action, link, mode = args.action, load_link(), mode_of(config)
    if action == "mode":
        wanted = str(getattr(args, "value", "") or "").strip().lower()
        if not wanted:
            out(f"Mode: {mode} ({MODE_WORDS[mode]}).")
            out("Change it: talos relay mode relay | local | both")
            return 0
        if wanted not in MODES:
            out("The mode is one of: relay (through the internet), local (the same network, nothing on the "
                "internet), both.")
            return 1
        path = util.home() / "talos.json"
        data = util.load_json_file(path, {}) or {}
        kept = data.get("relay") if isinstance(data.get("relay"), dict) else {}
        data["relay"] = dict(kept, mode=wanted)
        util.save_json_file(path, data)
        out(f"Mode: {wanted} ({MODE_WORDS[wanted]}). Saved in {path}")
        if listening():
            out("The talos relay that is running keeps the old mode: stop it and start it again.")
        if wanted != "relay":
            out("The phone needs the address of this computer: talos relay pair"
                + ("\n" + FIREWALL if os.name == "nt" else ""))
        return 0
    if action == "keys":
        wanted = str(getattr(args, "value", "") or "").strip().lower()
        if not wanted:
            names = phone_services(config)
            out(f"A linked phone can fetch the key of: {', '.join(names)}." if names
                else "A linked phone is handed no key.")
            out(f"Change it: talos relay keys {','.join(PHONE_SERVICES)}   (any of them; none: hand out no key)")
            return 0
        names = [] if wanted == "none" else [name.strip() for name in wanted.split(",") if name.strip()]
        unknown = [name for name in names if name not in PHONE_SERVICES]
        if unknown:
            out(f"The phone page can ask these services by itself: {', '.join(PHONE_SERVICES)}. "
                f"Not: {', '.join(unknown)}.")
            return 1
        path = util.home() / "talos.json"
        data = util.load_json_file(path, {}) or {}
        kept = data.get("relay") if isinstance(data.get("relay"), dict) else {}
        data["relay"] = dict(kept, phone_keys=[name for name in PHONE_SERVICES if name in names])
        util.save_json_file(path, data)
        out((f"A linked phone can fetch the key of: {', '.join(data['relay']['phone_keys'])}. " if names
             else "A linked phone is handed no key. ") + f"Saved in {path}")
        if names:
            out("On the phone: Device, This phone (no computer), Models and keys, Get the keys. A key that was "
                "fetched stays in that phone until it is taken out there.")
        return 0
    if action == "status":
        if not link:
            out("No phone is linked. Link one: talos relay pair")
            return 0
        if mode != "local":
            out(f"Linked through {link['url']} as {link.get('computer', '')}.")
        if mode != "relay":
            addresses = local_addresses(config)
            out(f"On the same network the phone opens {' or '.join(addresses)} (the relay inside Talos)." if addresses
                else "This computer has no network address right now: no phone reaches the relay inside Talos.")
        out(f"Mode: {mode}. Change it: talos relay mode relay | local | both")
        out("Talos Relay is running: the phone reaches Talos." if listening()
            else "Talos Relay is not running: the phone gets no answer. Start it: talos relay")
        out("It starts at sign-in." if at_login() else "It does not start at sign-in (talos relay install).")
        return 0
    if action == "forget":
        if link:
            if mode != "local":
                try:
                    Line(link).forget()
                except (OSError, http.client.HTTPException):
                    out("The relay could not be reached; it drops the channel by itself after 30 days.")
            if mode != "relay" and listening():
                try:
                    Line(local_link(link, config)).forget()
                except (OSError, http.client.HTTPException):
                    pass                                  # the relay inside Talos forgets all when it stops
            for path in (link_path(), util.home() / "relay-pair.html"):
                if path.exists():
                    path.unlink()
        out("No phone is linked now. A running talos relay keeps the old link until it is stopped.")
        return 0
    if action in ("install", "remove"):
        if action == "remove":
            at_login("")
            out("Talos Relay no longer starts at sign-in. A running one keeps running until it is stopped.")
            return 0
        start = launcher()
        at_login(start)
        if not listening():
            subprocess.Popen(start, close_fds=True, start_new_session=os.name != "nt",
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        out("Talos Relay starts at every sign-in, and is running now.")
        if not link:
            out("No phone is linked yet: talos relay pair")
        return 0
    if action == "pair":
        if args.new or not link:
            if link and mode != "local":
                try:
                    Line(link).forget()
                except (OSError, http.client.HTTPException):
                    pass
            link = make_link(config)
        problem = claim(link) if mode != "local" else ""
        entries = pair_entries(link, config)
        if mode != "relay" and not [which for which, _ in entries if which == "local"]:
            out("This computer has no network address right now, so there is no code for the same network. "
                "Connect it to the Wi-Fi and run this again.")
            if not entries:
                return 1
        if args.text:
            for which, address in entries:
                out(PAIR_TITLES[which] + ":")
                out(qr.blocks(qr.matrix(address)))
        else:
            page = util.home() / "relay-pair.html"
            codes = "\n".join(PAIR_CODE.format(title=PAIR_TITLES[which], code=qr.svg(qr.matrix(address)))
                              for which, address in entries)
            util.write_text(page, PAIR_PAGE.format(computer=link.get("computer", ""), code=codes, reach=PAIR_REACH[mode],
                                                   address="<br>".join(address for _, address in entries)))
            webbrowser.open(page.as_uri())
            out(f"A page with the code for the phone opens: {page}")
        out("Scan the code with the phone. Whoever has it can give Talos on this computer work: do not share it.")
        if problem:
            out(problem)
        if mode != "relay" and os.name == "nt":
            out(FIREWALL)
        if not listening():
            out("Then start it and leave it running: talos relay   (or: talos relay install)")
        return 0
    if action == "code":
        if mode == "local":
            out("A 4-digit code goes through the relay on the internet, and the mode is local. "
                "Use the QR code: talos relay pair   (or change the mode: talos relay mode both)")
            return 1
        if not link:
            link = make_link(config)
        if not listening():
            out("Talos Relay is not running: after linking, start it and leave it running: talos relay   "
                "(or: talos relay install)")
        return 0 if offer_code(link, out) == "linked" else 1
    if action != "run":
        out("talos relay            answer the phone (leave it running)\n"
            "talos relay pair       show the QR code that links a phone (--new: a new link, --text: in the terminal)\n"
            "talos relay code       show 4 digits that link a phone instead (good for 5 minutes)\n"
            "talos relay mode       relay: through the internet, from anywhere. local: the same network, with\n"
            "                       nothing on the internet. both. (talos relay mode local)\n"
            "talos relay keys       which free keys a linked phone may fetch, to chat with no computer\n"
            "                       (talos relay keys openrouter; none)\n"
            "talos relay status     is a phone linked, is it running, which mode\n"
            "talos relay install    start at every sign-in; remove: stop doing so\n"
            "talos relay forget     unlink every phone")
        return 1
    if not link:
        out("No phone is linked yet. Link one first: talos relay pair")
        return 1
    return run(config, store, link, out, overrides, getattr(args, "yes", False))


def run(config, store, link, out, overrides=None, auto_yes=False, stop=None) -> int:
    """Answer the phone until `stop` is set (or Ctrl+C): through the relay, with the relay inside Talos, or both."""
    mode, stop = mode_of(config), stop or threading.Event()
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
    except OSError:
        out("Talos Relay is already running on this computer.")
        return 0
    inside = None
    if mode != "relay":
        from . import relay_local
        try:
            inside = relay_local.LocalRelay(local_host(config), local_port(config))
        except OSError as error:
            out(f"The relay inside Talos could not start on port {local_port(config)} ({error}). "
                "Another program may use that port: set another one as relay.port in talos.json.")
        if inside and not inside.site:
            out("This Talos came without the phone page, so the relay inside Talos has nothing to show a phone.")
        if mode == "local" and not (inside and inside.site):
            if inside:
                inside.close()
            lock.close()
            return 1
    relay = Relay(config, store, link, overrides, auto_yes, out)
    out(f"Talos Relay answers the phone for {link.get('computer', 'this computer')}. New chats work in "
        f"{relay.folder}. " + ("It asks nobody before any step (--yes)." if relay.auto_yes
                               else "Before a step that can destroy work or leaves that folder, it asks on the phone.")
        + "  (Ctrl+C to stop)")
    if inside:
        addresses = local_addresses(config)
        out(f"The relay inside Talos is on: a phone on the same network opens {' or '.join(addresses)} "
            "(the code with the key: talos relay pair)." if addresses
            else "The relay inside Talos is on, but this computer has no network address right now.")
    reason = ""
    try:
        if inside and mode == "both":
            threading.Thread(target=relay.serve, args=(stop, Line(local_link(link, config))), daemon=True).start()
        reason = relay.serve(stop, Line(local_link(link, config)) if mode == "local" else None)
    except KeyboardInterrupt:
        reason = ""
    finally:
        stop.set()
        if inside:
            inside.close()
        lock.close()
    if reason:
        out(reason)
    return 1 if reason else 0
