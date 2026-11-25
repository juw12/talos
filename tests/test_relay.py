"""Talos Relay: the cipher, the code for the phone, and a phone that reaches Talos through a relay."""
import argparse
import hashlib
import json
import secrets
import threading
import time
import urllib.request
from pathlib import Path

from talos import login, qr, relay, util
from talos.fake import call, say
from tests.base import Case
from tests.fake_relay import FakeRelay
from tests.test_cli import CliCase

ROOT = Path(__file__).resolve().parent.parent


class CipherTest(Case):
    def test_published_vectors(self):
        """AES-256-GCM test cases 13, 14 and 16 of the GCM specification (McGrew and Viega)."""
        zero = relay.Cipher(bytes(32))
        self.assertEqual(zero.seal(b"", b"", bytes(12))[12:].hex(), "530f8afbc74536b9a963b4f1c4cb738b")
        self.assertEqual(zero.seal(bytes(16), b"", bytes(12))[12:].hex(),
                         "cea7403d4d606b6e074ec5d3baf39d18" "d0d1c8a799996bf0265b98b5d48ab919")
        key = bytes.fromhex("feffe9928665731c6d6a8f9467308308" * 2)
        nonce = bytes.fromhex("cafebabefacedbaddecaf888")
        plain = bytes.fromhex("d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
                              "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39")
        extra = bytes.fromhex("feedfacedeadbeeffeedfacedeadbeefabaddad2")
        sealed = relay.Cipher(key).seal(plain, extra, nonce)
        self.assertEqual(sealed[12:].hex(),
                         "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
                         "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662"
                         "76fc6ece0f4e1768cddf8853bb2d551b")
        self.assertEqual(relay.Cipher(key).open(sealed, extra), plain)

    def test_changed_text_is_refused(self):
        cipher = relay.Cipher(secrets.token_bytes(32))
        sealed = cipher.seal("write the report \u2713".encode("utf-8"), b"about")
        self.assertEqual(cipher.open(sealed, b"about").decode("utf-8"), "write the report \u2713")
        self.assertNotEqual(sealed, cipher.seal("write the report \u2713".encode("utf-8"), b"about"))
        changed = bytearray(sealed)
        changed[14] ^= 1
        for data, extra, key in ((bytes(changed), b"about", cipher), (sealed, b"other", cipher),
                                 (sealed[:20], b"about", cipher), (sealed, b"about", relay.Cipher(bytes(32)))):
            with self.assertRaises(ValueError):
                key.open(data, extra)
        with self.assertRaises(ValueError):
            relay.Cipher(b"short")


class CodeTest(Case):
    ADDRESS = "https://talos-relay.pages.dev/#" + "A" * 22 + "." + "B" * 43

    def test_code_for_the_phone(self):
        """The same modules as the reference encoder (segno) made for this text on 2026-11-25."""
        rows = qr.matrix(self.ADDRESS, mask=2)
        cells = "".join("".join("1" if cell else "0" for cell in row) for row in rows)
        self.assertEqual((len(rows), len(rows[0])), (37, 37))
        self.assertEqual(hashlib.sha256(cells.encode()).hexdigest(),
                         "edfa0f7fc6970c7ce85d5fcd1927b6f474fb188be68bd112bc2a020a235d2fc0")
        for corner in (rows[0][:7], rows[6][:7], rows[0][-7:], rows[-1][:7]):
            self.assertEqual(corner, [True] * 7)
        picture = qr.svg(qr.matrix(self.ADDRESS))
        self.assertTrue(picture.startswith("<svg") and "<script" not in picture)
        self.assertEqual(len(qr.blocks(rows).splitlines()), (37 + 2 * 2 + 1) // 2)
        self.assertEqual(len(qr.matrix("a")), 21)
        with self.assertRaises(ValueError):
            qr.matrix("x" * 300)


class Phone:
    """What the phone page does, in Python: seal a request, leave it, wait for the sealed answer."""

    def __init__(self, link):
        self.line = relay.Line(link)

    def leave(self, ident, body):
        return util.http("POST", f"{self.line.base}/ask/{ident}", body, {"Content-Type": "text/plain"})[0]

    def answer(self, ident, wait):
        deadline = time.time() + wait
        while time.time() < deadline:
            status, text = util.http("GET", f"{self.line.base}/answer/{ident}?wait=1")
            if status == 200:
                return self.line.open("answer", ident, text)
        return None

    def call(self, op, wait=30, **fields):
        ident = secrets.token_urlsafe(12)
        fields.setdefault("t", int(time.time() * 1000))
        body = self.line.seal("ask", ident, dict(fields, op=op))
        assert self.leave(ident, body) == 200
        self.last = (ident, body)
        return self.answer(ident, wait)

    def until(self, session, ready, wait=45):
        deadline = time.time() + wait
        while time.time() < deadline:
            chat = self.call("look", session=session)["chat"]
            if ready(chat):
                return chat
            time.sleep(0.05)
        raise AssertionError(f"Not reached in {wait} s: {chat}")


class RelayTest(Case):
    def start(self, steps, **settings):
        self.relay = FakeRelay()
        model = self.model(steps)
        settings = dict(settings, proof="off", relay=dict(settings.get("relay") or {}, url=self.relay.address))
        self.settings = self.config(model, **settings)
        self.link = relay.make_link(self.settings)
        self.assertEqual(relay.claim(self.link), "")           # as `talos relay pair` does
        self.notes = []
        self.service = relay.Relay(self.settings, self.store, self.link, model.config(**settings),
                                   show=self.notes.append)
        stop = threading.Event()
        thread = threading.Thread(target=self.service.serve, args=(stop,), daemon=True)
        thread.start()
        for close in (lambda: thread.join(15), self.relay.close, stop.set):     # run in the opposite order
            self.closers.append(close)
        return Phone(self.link)

    def test_phone_starts_a_chat_and_allows_a_step(self):
        note = self.tmp / "note.txt"                            # outside the folder Talos works in: it must ask
        phone = self.start([call("write", filePath=str(note), content="from the phone\n"),
                            say("The note is written.")])
        first = phone.call("look")
        self.assertEqual((first["ok"], first["chats"], first["chat"], first["asks_first"]), (True, [], None, True))
        self.assertEqual((first["folders"], first["model"]), ([str(self.work)], "fake/script"))

        session = phone.call("send", session="", text="write the note", folder="")["session"]
        waiting = phone.until(session, lambda chat: chat["live"] and chat["live"]["ask"])
        ask = waiting["live"]["ask"]
        self.assertEqual((ask["kind"], ask["tool"], waiting["running"]), ("permission", "write", True))
        self.assertEqual((ask["target"], ask["reason"]), (str(note), "outside the project folder"))
        self.assertFalse(note.exists(), "nothing is written before the phone says yes")
        self.assertIn("still working", phone.call("send", session=session, text="and another")["error"])
        self.assertEqual(phone.call("reply", id=ask["id"], answer="once"), {"ok": True})

        done = phone.until(session, lambda chat: not chat["running"])
        self.assertEqual(note.read_text(encoding="utf-8"), "from the phone\n")
        self.assertEqual([item["role"] for item in done["items"]], ["you", "step", "talos"])
        self.assertEqual((done["items"][0]["text"], done["items"][2]["text"]), ("write the note", "The note is written."))
        self.assertEqual((done["items"][1]["name"], done["items"][1]["target"]), ("write", str(note)[:140]))
        self.assertTrue(done["items"][1]["result"])
        self.assertEqual((done["live"]["running"], done["live"]["done"]["stopped"], done["live"]["ask"]), (False, "", None))
        listed = phone.call("look")["chats"]
        self.assertEqual([(chat["id"], chat["running"], chat["source"]) for chat in listed], [(session, False, "relay")])

        # the relay in between saw sealed text only
        passed = "\n".join(self.relay.seen)
        self.assertGreater(len(self.relay.seen), 6)
        for word in ("write the note", "note.txt", "The note is written", "fake/script", self.work.name):
            self.assertNotIn(word, passed)

    def test_stop_and_what_is_refused(self):
        (self.work / "build").mkdir()
        phone = self.start([call("bash", command="rm -rf build"), say("never said")])
        session = phone.call("send", session="", text="run it", folder=str(self.work))["session"]
        phone.until(session, lambda chat: chat["live"] and chat["live"]["ask"])
        self.assertEqual(phone.call("stop", session=session), {"ok": True})
        ended = phone.until(session, lambda chat: not chat["running"])
        self.assertEqual(ended["live"]["done"]["stopped"], "stopped by the user")
        self.assertTrue((self.work / "build").is_dir(), "the command it asked about was not run")
        self.assertEqual(phone.call("stop", session="nothing")["ok"], False)

        # the same sealed request a second time (someone who copied it from the relay) is not carried out
        chats = len(self.store.sessions())
        phone.call("send", session="", text="a second chat", folder="")
        ident, body = phone.last
        self.assertEqual(len(self.store.sessions()), chats + 1)
        self.assertEqual(phone.leave(ident, body), 200)
        self.assertIsNone(phone.answer(ident, 1.5))
        other = secrets.token_urlsafe(12)
        self.assertEqual(phone.leave(other, body), 200)          # under another name it does not open at all
        self.assertIsNone(phone.answer(other, 1.5))
        self.assertEqual(len(self.store.sessions()), chats + 1)

        # a phone with another key gets no answer; an old request and an unknown one are refused in words
        stranger = Phone(dict(self.link, key=relay.b64(secrets.token_bytes(32))))
        self.assertIsNone(stranger.call("look", wait=1.5))
        old = phone.call("send", session="", text="late", folder="", t=int((time.time() - 660) * 1000))
        self.assertIn("clock", old["error"])
        self.assertIn("talos upgrade", phone.call("dance")["error"])
        self.assertIn("Type a message", phone.call("send", session="", text="  ")["error"])
        self.assertEqual(phone.call("look", session="gone-chat")["gone"], "gone-chat")
        self.assertEqual(len(self.store.sessions()), chats + 1)

        # another computer cannot take the channel, and the relay says who was there
        thief = dict(self.link, token=secrets.token_urlsafe(32))
        self.assertEqual(relay.Line(thief).put_state({"computer": "thief"}), 403)
        self.assertEqual(relay.Line(thief).take(0)[0], 404)
        state = util.http("GET", f"{self.relay.address}/c/{self.link['channel']}/state")
        self.assertEqual(state[0], 200)
        self.assertEqual(relay.Line(self.link).open("state", "", state[1])["computer"], self.link["computer"])

    def test_with_yes_it_does_not_ask(self):
        phone = self.start([say("done")], relay={"yes": True})
        self.assertFalse(phone.call("look")["asks_first"])


class RelayCommandTest(CliCase):
    def run_relay(self, action, settings, **flags):
        lines = []
        args = argparse.Namespace(action=action, yes=False, new=flags.get("new", False), text=flags.get("text", True))
        return relay.command(args, settings, self.store, lines.append), "\n".join(lines)

    def test_pair_status_forget(self):
        fake = FakeRelay()
        self.closers.append(fake.close)
        settings = self.config(relay={"url": fake.address})
        self.assertEqual(self.run_relay("status", settings), (0, "No phone is linked. Link one: talos relay pair"))
        self.assertEqual(self.run_relay("run", settings)[0], 1)

        code, shown = self.run_relay("pair", settings)
        link = relay.load_link()
        self.assertEqual((code, link["url"]), (0, fake.address))
        self.assertIn("\u2588", shown)
        self.assertIn("do not share it", shown)
        self.assertEqual(relay.phone_address(link), f"{fake.address}/#{link['channel']}.{link['key']}")
        self.assertRegex(relay.phone_address(link).split("#")[1], r"^[A-Za-z0-9_-]{16,64}\.[A-Za-z0-9_-]{43}$")
        self.assertIn(link["channel"], fake.owners)
        self.assertNotIn(link["key"], json.dumps([fake.states, fake.seen]))

        self.run_relay("pair", settings)
        self.assertEqual(relay.load_link(), link, "pair again shows the same link")
        self.run_relay("pair", settings, new=True)
        fresh = relay.load_link()
        self.assertNotEqual((fresh["channel"], fresh["key"]), (link["channel"], link["key"]))
        self.assertEqual(list(fake.owners), [fresh["channel"]], "the old link is gone from the relay")

        self.assertIn("Linked through " + fake.address, self.run_relay("status", settings)[1])
        self.assertEqual(self.run_relay("forget", settings)[0], 0)
        self.assertEqual((relay.load_link(), fake.owners), (None, {}))
        self.assertEqual(self.run_relay("dance", settings)[0], 1)

    def test_the_command_is_wired(self):
        code, out, err = self.talos("relay", "status")
        self.assertEqual((code, out.strip()), (0, "No phone is linked. Link one: talos relay pair"), err)

    def test_pair_page(self):
        page = relay.PAIR_PAGE.format(computer="PC", code="<svg></svg>", address="https://x/#a.b")
        self.assertIn("talos relay pair --new", page)
        self.assertNotIn("<script", page)


class PageTest(Case):
    """The phone page and the computer must mean the same by every word they exchange."""

    def test_page_matches_the_computer(self):
        site = ROOT / "relay" / "site"
        script = (site / "app.js").read_text(encoding="utf-8")
        page = (site / "index.html").read_text(encoding="utf-8")
        computer = (ROOT / "talos" / "relay.py").read_text(encoding="utf-8")
        self.assertIn('"talos-relay/" + PROTOCOL + "/"', script)
        self.assertIn(f"var PROTOCOL = {relay.PROTOCOL};", script)
        for op in ("look", "send", "stop", "reply"):
            self.assertIn(f'call("{op}"', script)
            self.assertIn(f'op == "{op}"', computer)
        for answer in ("once", "always", "reject"):
            self.assertIn(f'"{answer}"', script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("manifest", page, "a manifest would make the Home Screen icon forget the link")
        for name in ("app.js", "styles.css", "icon.svg", "icon-180.png"):
            self.assertIn(name, page)
            self.assertTrue((site / name).is_file(), name)
        self.assertFalse("://" in page + script.replace("https://<relay>/", ""),
                         "the page must load nothing from another site")
        self.assertIn("script-src 'self'", (site / "_headers").read_text(encoding="utf-8"))
        worker = (ROOT / "relay" / "functions" / "c" / "[[path]].js").read_text(encoding="utf-8")
        for path in ("/state", "asks/take", '"ask"', '"answer"', "X-Relay-Write"):
            self.assertIn(path, worker)

    def test_page_is_handed_out(self):
        fake = FakeRelay()
        self.closers.append(fake.close)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for name, start in (("", b"<!doctype html>"), ("app.js", b"/* Talos Relay"), ("styles.css", b":root"),
                            ("icon-180.png", b"\x89PNG")):
            with opener.open(f"{fake.address}/{name}", timeout=20) as response:
                self.assertEqual(response.read()[:len(start)], start, name)


class SignInTest(Case):
    def test_openrouter_sign_in(self):
        asked = {}

        def reply(method, path, body, headers):
            asked["path"], asked["body"] = path, json.loads(body)
            return 200, {"key": "sk-or-v1-test"}

        site = self.http(reply)

        def browser(address):
            asked["page"] = address
            query = dict(part.split("=", 1) for part in address.split("?", 1)[1].split("&"))
            back = urllib.request.unquote(query["callback_url"]).replace("localhost", "127.0.0.1")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            threading.Thread(target=lambda: opener.open(back + "?code=one-time", timeout=20).read(), daemon=True).start()
            asked["challenge"] = query["code_challenge"]

        shown = []
        self.assertEqual(login.openrouter(shown.append, base=site.url, wait=30, open_browser=browser), "sk-or-v1-test")
        self.assertTrue(asked["page"].startswith(site.url + "/auth?callback_url=http%3A%2F%2Flocalhost%3A"))
        self.assertEqual((asked["path"], asked["body"]["code"]), ("/api/v1/auth/keys", "one-time"))
        made = hashlib.sha256(asked["body"]["code_verifier"].encode()).digest()
        self.assertEqual(relay.b64(made), asked["challenge"], "the secret matches what the page was shown")
        self.assertIn("Authorize", shown[0])

        with self.assertRaises(ValueError) as nobody:
            login.openrouter(shown.append, base=site.url, wait=0.2, open_browser=lambda address: None)
        self.assertIn("Nothing was saved", str(nobody.exception))
        refused = self.http(lambda *request: (403, {"error": {"message": "code already used"}}))
        with self.assertRaises(ValueError) as error:
            login.openrouter(shown.append, base=refused.url, wait=30, open_browser=browser)
        self.assertIn("code already used", str(error.exception))
