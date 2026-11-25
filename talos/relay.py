"""Talos Relay: the Talos chats of this computer on your phone, from anywhere.

    phone page  --sealed prompt-->  relay  --sealed prompt-->  talos relay (this file)  -->  Talos
    phone page  <--sealed chats---  relay  <--sealed chats---  talos relay

The relay is a small pass-on service (relay/ in the repository: one Cloudflare Pages Function and
the phone page). It holds only text sealed with AES-256-GCM. The key is made here and reaches the
phone inside the link, after the #, which a browser sends to no server.

Standard library only, so the cipher is written out below. It is checked against the published
test vectors and against a browser's own AES-GCM (tests/test_relay.py).
"""
from __future__ import annotations

import base64
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
import webbrowser
from pathlib import Path

from . import __version__, config as config_module, qr, util

SITE = "https://talos-relay.pages.dev"
PROTOCOL = 1
LOCK_PORT = 4748            # held by the one `talos relay` of this computer
TAKE_WAIT_S = 8             # the relay holds a request for prompts this long
STATE_EVERY_S = 600         # how often the relay hears that this computer is still there
FRESH_S = 600               # a prompt older than this is not carried out
ROOM = 60_000               # characters of one chat sent to the phone
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


class Line:
    """Sealed text to and from the relay, for one link."""

    def __init__(self, link: dict):
        self.link, self.cipher = link, Cipher(unb64(link["key"]))
        self.base = f"{link['url']}/c/{link['channel']}"
        self.mine = {"X-Relay-Write": link["token"], "Content-Type": "text/plain"}

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
        return util.http("PUT", self.base + "/state", self.seal("state", "", data), self.mine, 25)[0]

    def take(self, wait: float = TAKE_WAIT_S):
        """(status, [(id, sealed prompt), ...]): what the phones left since the last call."""
        status, data = util.http_json("POST", f"{self.base}/asks/take?wait={wait:g}", "", self.mine, wait + 20)
        asks = data.get("asks") if status == 200 and isinstance(data, dict) else None
        return status, [(str(item.get("id")), str(item.get("body"))) for item in asks or [] if isinstance(item, dict)]

    def answer(self, ident: str, data) -> int:
        return util.http("PUT", f"{self.base}/answer/{ident}", self.seal("answer", ident, data), self.mine, 25)[0]

    def forget(self) -> int:
        return util.http("DELETE", self.base, None, self.mine, 25)[0]


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
        role, content = message.get("role"), message.get("content")
        text = content if isinstance(content, str) else "(attachment)"
        if role == "user":
            items.append({"role": "you", "text": util.clip(text, 4000)})
        elif role == "assistant":
            if text.strip():
                items.append({"role": "talos", "text": util.clip(text, 8000)})
            for call in message.get("tool_calls") or []:
                items.append({"role": "step", "name": str(call.get("name") or ""),
                              "target": _target(call.get("arguments")), "result": None})
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
        self.seen = {}

    # ------------------------------------------------------------ views
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
        text, asks, notes, done = "", {}, [], None
        for item in events:
            event, data = item["event"], item["data"]
            if event == "text":
                text += str(data)
            elif event in ("assistant", "tool"):
                text = ""
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
        return {"running": not finished, "text": text[-3000:], "notes": notes[-3:], "done": done,
                "ask": None if finished or not asks else list(asks.values())[-1],
                "seconds": int(time.time() - getattr(run, "started", time.time()))}

    def look(self, session: str = "") -> dict:
        app = self.app
        chats = [{"id": item["id"], "title": item.get("title") or "(no title)", "updated": item.get("updated"),
                  "running": app.busy(item["id"]), "folder": Path(item.get("cwd") or "").name,
                  "source": item.get("source") or ""} for item in self.store.sessions(40)]
        answer = {"ok": True, "protocol": PROTOCOL, "version": __version__, "computer": self.link.get("computer", ""),
                  "model": config_module.pick_default_model(self.config), "asks_first": not self.auto_yes,
                  "folders": self.folders(), "chats": chats, "chat": None}
        view = app.session_view(session) if session else {}
        if view:
            info = view["session"]
            items, earlier = shape(view["messages"])
            answer["chat"] = {"id": info["id"], "title": info.get("title") or "(no title)", "folder": info.get("cwd") or "",
                              "model": info.get("model") or "", "running": view["running"], "items": items,
                              "earlier": earlier, "live": self.live(info["id"])}
        elif session:
            answer["gone"] = session
        return answer

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
        if op == "send":
            text, folder = str(ask.get("text") or "").strip(), str(ask.get("folder") or "")
            if not text:
                return {"error": "Type a message first."}
            try:
                started, run = self.app.start(session, text, cwd=folder if folder in self.folders() else self.folder,
                                              source="relay", auto_yes=self.auto_yes)
            except ValueError as error:
                return {"error": str(error)}
            run.started = time.time()
            return {"ok": True, "session": started}
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
        return {"error": "The Talos on the computer is older than this page and does not know that request. "
                         "Update it: talos upgrade."}

    def serve(self, stop=None) -> str:
        """Carry requests until `stop` is set. Returns the reason it ended by itself, or ''."""
        stop, line = stop or threading.Event(), self.line
        told, failures, renewed = "", 0, 0.0
        while not stop.is_set():
            try:
                if time.time() - renewed > STATE_EVERY_S:
                    status = line.put_state({"computer": self.link.get("computer", ""), "t": int(time.time() * 1000),
                                             "version": __version__})
                    if status == 403:
                        return ("The relay holds this link for another computer. Make a new link: "
                                "talos relay pair --new")
                    if status != 200:
                        raise OSError(f"the relay answered HTTP {status}")
                    renewed = time.time()
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
                    self.show(f"Talos Relay cannot reach {self.link['url']} ({error}). It keeps trying.")
                stop.wait(min(60, 2 ** failures))
                continue
            if told == "down":
                self.show("Talos Relay reaches the relay again.")
            told, failures = "", 0
            for ident, body in asks:
                if ident in self.seen:
                    continue
                self.seen[ident] = True
                if len(self.seen) > 2000:
                    self.seen.pop(next(iter(self.seen)))
                try:
                    ask = line.open("ask", ident, body)
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


# ---------------------------------------------------------------- the commands

PAIR_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Talos Relay: link your phone</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<body style="font:17px/1.5 'Segoe UI',system-ui,sans-serif;background:#f6f1e7;color:#1d1b16;margin:0">
<main style="max-width:34rem;margin:0 auto;padding:2rem 1.2rem">
<h1 style="font:600 1.7rem Georgia,serif;margin:0 0 .3rem">Talos Relay</h1>
<p style="margin:.2rem 0 1.2rem;color:#6b6253">Link your phone to Talos on <b>{computer}</b>.</p>
<div style="background:#fff;border:1px solid #d9cfba;border-radius:12px;padding:1rem;text-align:center">{code}</div>
<ol style="padding-left:1.2rem">
<li>Scan the code with the phone's camera and open the page.</li>
<li>iPhone: Share, then <b>Add to Home Screen</b>. Android: the browser menu, then <b>Add to Home screen</b>.</li>
<li>Keep <code>talos relay</code> running on this computer. The phone reaches it from anywhere.</li>
</ol>
<p style="color:#a8321f"><b>Whoever has this code or link can give Talos on this computer work to do.</b>
Do not share it, and close this page when the phone is linked. Lost the phone:
<code>talos relay pair --new</code> makes a new link and the old one stops working.</p>
<p style="color:#6b6253;font-size:.85rem;word-break:break-all">No camera at hand? Send yourself this link in a
way only you can read:<br>{address}</p>
</main></body></html>
"""


def listening() -> bool:
    """Is a `talos relay` of this computer running?"""
    try:
        socket.create_connection(("127.0.0.1", LOCK_PORT), 0.3).close()
        return True
    except OSError:
        return False


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


def at_login(change=None):
    """Windows: read (None), set (a command) or clear ('') the entry that starts Talos Relay at sign-in."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
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


def claim(link: dict) -> str:
    """Make the channel this computer's on the relay. Returns '' or a plain reason."""
    try:
        status = Line(link).put_state({"computer": link.get("computer", ""), "t": int(time.time() * 1000),
                                       "version": __version__})
    except (OSError, http.client.HTTPException) as error:
        return f"The relay at {link['url']} could not be reached ({error}). The link works once it can."
    return "" if status == 200 else f"The relay at {link['url']} answered HTTP {status}. The link works once it accepts this computer."


def command(args, config, store, out, overrides=None) -> int:
    action, link = args.action, load_link()
    if action == "status":
        if not link:
            out("No phone is linked. Link one: talos relay pair")
            return 0
        out(f"Linked through {link['url']} as {link.get('computer', '')}.")
        out("Talos Relay is running: the phone reaches Talos." if listening()
            else "Talos Relay is not running: the phone gets no answer. Start it: talos relay")
        if os.name == "nt":
            out("It starts at sign-in." if at_login() else "It does not start at sign-in (talos relay install).")
        return 0
    if action == "forget":
        if link:
            try:
                Line(link).forget()
            except (OSError, http.client.HTTPException):
                out("The relay could not be reached; it drops the channel by itself after 30 days.")
            for path in (link_path(), util.home() / "relay-pair.html"):
                if path.exists():
                    path.unlink()
        out("No phone is linked now. A running talos relay keeps the old link until it is stopped.")
        return 0
    if action in ("install", "remove"):
        if os.name != "nt":
            out("Starting at sign-in is set up by Talos on Windows only. On this system, add this command to "
                "your login items yourself: talos relay")
            return 1
        if action == "remove":
            at_login("")
            out("Talos Relay no longer starts at sign-in. A running one keeps running until it is stopped.")
            return 0
        start = launcher()
        at_login(start)
        if not listening():
            subprocess.Popen(start, close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        out("Talos Relay starts at every sign-in, and is running now.")
        if not link:
            out("No phone is linked yet: talos relay pair")
        return 0
    if action == "pair":
        if args.new or not link:
            if link:
                try:
                    Line(link).forget()
                except (OSError, http.client.HTTPException):
                    pass
            link = make_link(config)
        problem = claim(link)
        address = phone_address(link)
        rows = qr.matrix(address)
        if args.text:
            out(qr.blocks(rows))
        else:
            page = util.home() / "relay-pair.html"
            util.write_text(page, PAIR_PAGE.format(computer=link.get("computer", ""), code=qr.svg(rows),
                                                   address=address))
            webbrowser.open(page.as_uri())
            out(f"A page with the code for the phone opens: {page}")
        out("Scan the code with the phone. Whoever has it can give Talos on this computer work: do not share it.")
        if problem:
            out(problem)
        if not listening():
            out("Then start it and leave it running: talos relay   (or: talos relay install)")
        return 0
    if action != "run":
        out("talos relay            answer the phone (leave it running)\n"
            "talos relay pair       show the code that links a phone (--new: a new link, --text: in the terminal)\n"
            "talos relay status     is a phone linked, is it running\n"
            "talos relay install    start at every sign-in (Windows); remove: stop doing so\n"
            "talos relay forget     unlink every phone")
        return 1
    if not link:
        out("No phone is linked yet. Link one first: talos relay pair")
        return 1
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
    except OSError:
        out("Talos Relay is already running on this computer.")
        return 0
    relay = Relay(config, store, link, overrides, getattr(args, "yes", False), out)
    out(f"Talos Relay answers the phone for {link.get('computer', 'this computer')}. New chats work in "
        f"{relay.folder}. " + ("It asks nobody before any step (--yes)." if relay.auto_yes
                               else "Before a step that can destroy work or leaves that folder, it asks on the phone.")
        + "  (Ctrl+C to stop)")
    try:
        reason = relay.serve()
    except KeyboardInterrupt:
        reason = ""
    finally:
        lock.close()
    if reason:
        out(reason)
    return 1 if reason else 0
