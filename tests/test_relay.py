"""Talos Relay: the cipher, the code for the phone, and a phone that reaches Talos through a relay."""
import argparse
import base64
import hashlib
import json
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from talos import config as config_module, glow, login, qr, relay, relay_local, util
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
        model = self.scripted = self.model(steps)
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

    def test_the_phone_picks_the_model(self):
        phone = self.start([say("From the spare.")], fallback_model="fake/spare")
        seen = phone.call("look")
        self.assertEqual(seen["models"][:2], ["fake/script", "fake/spare"], "the model in use first, then the spares")
        self.assertIn("glow/auto", seen["models"])
        self.assertEqual(len(seen["models"]), len(set(seen["models"])))
        refused = phone.call("send", session="", text="hello", folder="", model="nowhere/nothing")
        self.assertIn("does not have nowhere/nothing", refused["error"])
        self.assertEqual(self.store.sessions(), [], "a prompt for a model this computer lacks starts nothing")
        session = phone.call("send", session="", text="hello", folder="", model="fake/spare")["session"]
        done = phone.until(session, lambda chat: not chat["running"])
        self.assertEqual((done["model"], done["items"][-1]["text"]), ("fake/spare", "From the spare."))
        self.assertIn('"model": "spare"', json.dumps(self.scripted.requests[-1]))

    def test_keys_for_the_phone_alone(self):
        phone = self.start([say("not asked")])
        settings = self.service.app.overrides
        settings["provider"].update(openrouter={"options": {"apiKey": "sk-or-free-key"}},
                                    groq={"options": {"apiKey": "gsk-other-place", "baseURL": "http://127.0.0.1:9/v1"}},
                                    cerebras={"options": {"apiKey": "csk-third"}},
                                    openai={"options": {"apiKey": "sk-paid"}})
        self.assertEqual(phone.call("keys"), {"ok": True, "keys": {}}, "no key leaves the computer unless its owner said so")
        settings["relay"]["phone_keys"] = ["openrouter", "groq", "openai"]
        self.assertEqual(phone.call("keys"), {"ok": True, "keys": {"openrouter": "sk-or-free-key"}},
                         "not a key that goes to another address, and not a service the page cannot ask")
        settings["relay"]["phone_keys"] = "Cerebras, openrouter"
        self.assertEqual(phone.call("keys")["keys"], {"openrouter": "sk-or-free-key", "cerebras": "csk-third"})
        self.assertIn("clock", phone.call("keys", t=int((time.time() - 660) * 1000))["error"])
        self.assertNotIn("sk-or-free-key", "\n".join(self.relay.seen), "the key crossed the relay sealed")

    def test_with_yes_it_does_not_ask(self):
        phone = self.start([say("done")], relay={"yes": True})
        self.assertFalse(phone.call("look")["asks_first"])

    def test_a_photo_the_list_and_the_copy(self):
        before, relay.STATE_GAP_S = relay.STATE_GAP_S, 0
        self.closers.append(lambda: setattr(relay, "STATE_GAP_S", before))
        phone = self.start([say("A red square.")])
        photo = base64.b64encode(b"\xff\xd8\xff\xe0" + bytes(range(200))).decode("ascii")
        session = phone.call("send", session="", text="", folder="", img=photo)["session"]
        done = phone.until(session, lambda chat: not chat["running"])
        first = done["items"][0]
        self.assertEqual((first["role"], first["text"], done["prompts"]), ("you", "Look at this photo.", 1))
        self.assertGreater(first["t"], time.time() - 300)
        shown = phone.call("image", session=session, at=first["img"]["at"])
        self.assertEqual((shown["ok"], shown["kind"], shown["data"]), (True, "image/jpeg", photo))
        self.assertEqual(phone.call("image", session=session, at=first["img"]["at"] + 50), {"ok": False})
        self.assertIn("could not read", phone.call("send", session="", text="x", img="bm90IGEgcGhvdG8=")["error"])
        with self.assertRaises(ValueError):
            self.service.photo("A" * (relay.PHOTO_CHARS + 1))
        self.assertNotIn(photo[:60], "\n".join(self.relay.seen), "the photo crossed the relay sealed")

        # the list has one row with what the chat last said; the relay keeps the same rows, sealed
        row = phone.call("look")["chats"][0]
        self.assertEqual((row["id"], row["running"], row["waits"], row["step"]), (session, False, False, "A red square."))
        deadline, kept = time.time() + 30, {}
        while time.time() < deadline and not kept.get("chats"):
            phone.call("look")                                   # each request lets the computer go round once
            status, text = util.http("GET", f"{self.relay.address}/c/{self.link['channel']}/state")
            kept = relay.Line(self.link).open("state", "", text) if status == 200 else {}
        self.assertEqual([(chat["id"], chat["title"]) for chat in kept["chats"]], [(session, row["title"])])
        self.assertNotIn(row["title"], self.relay.states[self.link["channel"]])

        # taken off the phone's list, the chat stays on the computer
        self.assertEqual(phone.call("hide", session="nothing")["ok"], False)
        self.assertEqual(phone.call("hide", session=session), {"ok": True, "chats": []})
        self.assertEqual(phone.call("look")["chats"], [])
        self.assertTrue(self.store.session(session))
        self.assertEqual(phone.call("look", session=session)["chat"]["id"], session)

    def test_one_connection_is_kept(self):
        fake = FakeRelay()
        self.closers.append(fake.close)
        line = relay.Line(relay.make_link(self.config(relay={"url": fake.address})))
        self.closers.append(line.wire.close)
        self.assertEqual(line.put_state({"computer": "here"}), 200)
        kept = line.wire.conn
        self.assertIsNotNone(kept, "the connection stays open after a request")
        self.assertEqual(line.take(0), (200, []))
        self.assertIs(line.wire.conn, kept)
        kept.sock.close()                                        # as when the other side hung up meanwhile
        self.assertEqual(line.put_state({"computer": "here"}), 200)
        self.assertIsNot(line.wire.conn, kept)

        # where the kept connection cannot be made, the plain way carries the request and is used for a while
        line.wire._connect = lambda timeout: (_ for _ in ()).throw(OSError("no tunnel here"))
        line.wire.close()
        self.assertEqual(line.put_state({"computer": "here"}), 200)
        self.assertGreater(line.rest, time.time() + relay.WIRE_REST_S - 30)


class PairCodeTest(Case):
    def offer(self, **limits):
        fake = FakeRelay()
        self.closers.append(fake.close)
        link = relay.make_link(self.config(relay={"url": fake.address}))
        said, codes, ended = [], [], []
        thread = threading.Thread(daemon=True, target=lambda: ended.append(
            relay.offer_code(link, said.append, shown=codes.append, **limits)))
        thread.start()
        deadline = time.time() + 20
        while not codes and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(codes, said)
        return fake, link, codes[0], thread, ended, said

    def phone(self, address, code):
        """What the page does with 4 digits (relay/site/app.js, linkByCode). Returns the link, how many
        tries are left after a wrong code, or 'no-code'."""
        for moment in (time.time(), time.time() - relay.ROOM_EVERY_S):
            room = f"{address}/c/{relay.code_room(moment)}"
            status, text = util.http("GET", room + "/state")
            if status == 200:
                break
        else:
            return "no-code"
        theirs = int.from_bytes(relay.unb64(json.loads(text)["a"]), "big")
        secret = 2 + secrets.randbits(256)
        mine = relay.code_public(code, secret)
        proof, seal = relay.code_keys(secret, theirs, theirs, mine)
        ident = secrets.token_urlsafe(12)
        said = json.dumps({"b": relay.b64(mine.to_bytes(256, "big")), "m": relay.b64(proof)})
        self.assertEqual(util.http("POST", f"{room}/ask/{ident}", said, {"Content-Type": "text/plain"})[0], 200)
        deadline = time.time() + 30
        while time.time() < deadline:
            status, text = util.http("GET", f"{room}/answer/{ident}?wait=1")
            if status == 200:
                answer = json.loads(text)
                if not answer["ok"]:
                    return answer["left"]
                return json.loads(relay.Cipher(seal).open(relay.unb64(answer["link"])).decode("utf-8"))["link"]
        raise AssertionError("the computer did not answer the code")

    def test_four_digits_link_a_phone(self):
        fake, link, code, thread, ended, said = self.offer(valid=60, grace=0.3)
        self.assertRegex(code, r"^[0-9]{4}$")
        self.assertIn(f"Code for the phone: {code}", said)
        self.assertEqual(self.phone(fake.address, f"{(int(code) + 1) % 10000:04d}"), relay.CODE_TRIES - 1)
        self.assertEqual(self.phone(fake.address, code), relay.phone_address(link))
        thread.join(20)
        self.assertEqual(ended, ["linked"])
        self.assertIn(link["channel"], fake.owners, "the phone finds the computer at the relay at once")
        self.assertEqual([name for name in fake.owners if name.startswith("pair")], [], "the room is wiped")
        self.assertNotIn(link["key"], json.dumps(fake.seen), "the key crossed the relay sealed")

    def test_three_wrong_codes_end_it(self):
        fake, link, code, thread, ended, said = self.offer(valid=60, grace=0.3)
        wrong = f"{(int(code) + 7) % 10000:04d}"
        self.assertEqual([self.phone(fake.address, wrong) for _ in range(relay.CODE_TRIES)], [2, 1, 0])
        thread.join(20)
        self.assertEqual(ended, ["wrong"])
        self.assertEqual(self.phone(fake.address, code), "no-code")
        self.assertNotIn(link["channel"], fake.owners)

    def test_a_code_runs_out_and_one_computer_holds_the_room(self):
        fake, link, code, thread, ended, said = self.offer(valid=1.5, grace=0.1)
        other = dict(link, token=secrets.token_urlsafe(32))
        lines = []
        self.assertEqual(relay.offer_code(other, lines.append, valid=1, grace=-10), "taken")
        self.assertIn("another computer", lines[0])
        thread.join(20)
        self.assertEqual(ended, ["expired"])
        self.assertIn("ran out", said[-1])

    def test_the_two_sides_agree(self):
        one, two = 2 + secrets.randbits(256), 2 + secrets.randbits(256)
        first, second = relay.code_public("0042", one), relay.code_public("0042", two)
        self.assertEqual(relay.code_keys(one, second, first, second), relay.code_keys(two, first, first, second))
        other = relay.code_public("0043", two)
        self.assertNotEqual(relay.code_keys(one, other, first, other), relay.code_keys(two, first, first, other))
        for bad in (0, 1, relay.CODE_PRIME - 1, relay.CODE_PRIME):
            self.assertIsNone(relay.code_keys(one, bad, first, bad))
        self.assertRegex(relay.code_room(time.time()), r"^pair[A-Za-z0-9_-]{28}$")
        self.assertNotEqual(relay.code_room(0), relay.code_room(relay.ROOM_EVERY_S))


class SignInStartTest(Case):
    def test_what_starts_it_at_sign_in(self):
        command = ["/usr/bin/python3", "/home/me/.talos/talos relay.pyw", "a&b"]
        path, text = relay.login_entry("Darwin", command)
        self.assertEqual(path.parts[-3:], ("Library", "LaunchAgents", "dev.talos.relay.plist"))
        self.assertIn("<string>/home/me/.talos/talos relay.pyw</string>", text)
        self.assertIn("<string>a&amp;b</string>", text)
        self.assertIn("<key>RunAtLoad</key>\n  <true/>", text)
        path, text = relay.login_entry("systemd", command)
        self.assertEqual(path.parts[-4:], (".config", "systemd", "user", "talos-relay.service"))
        self.assertIn('ExecStart="/usr/bin/python3" "/home/me/.talos/talos relay.pyw" "a&b"', text)
        self.assertIn("WantedBy=default.target", text)
        path, text = relay.login_entry("xdg", command)
        self.assertEqual(path.parts[-3:], (".config", "autostart", "talos-relay.desktop"))
        self.assertIn('Exec="/usr/bin/python3" "/home/me/.talos/talos relay.pyw" "a&b"', text)
        self.assertTrue(str(path).startswith(str(self.user)), "the entry is written in this account only")


class RelayCommandTest(CliCase):
    def run_relay(self, action, settings, **flags):
        lines = []
        args = argparse.Namespace(action=action, yes=False, new=flags.get("new", False), text=flags.get("text", True),
                                  value=flags.get("value", ""))
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

    def test_a_running_relay_is_seen_every_time(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        before, relay.LOCK_PORT = relay.LOCK_PORT, port
        self.closers.append(lambda: setattr(relay, "LOCK_PORT", before))
        self.assertFalse(relay.listening())
        lock = socket.socket()                                   # as `talos relay` holds it
        lock.bind(("127.0.0.1", port))
        lock.listen(1)
        self.closers.append(lock.close)
        self.assertEqual([relay.listening() for _ in range(8)], [True] * 8)
        lock.close()
        self.assertFalse(relay.listening())

    def test_the_command_is_wired(self):
        code, out, err = self.talos("relay", "status")
        self.assertEqual((code, out.strip()), (0, "No phone is linked. Link one: talos relay pair"), err)

    def test_pair_page(self):
        code = relay.PAIR_CODE.format(title=relay.PAIR_TITLES["relay"], code="<svg></svg>")
        page = relay.PAIR_PAGE.format(computer="PC", code=code, reach=relay.PAIR_REACH["relay"], address="https://x/#a.b")
        self.assertIn("talos relay pair --new", page)
        self.assertIn("<svg></svg>", page)
        self.assertNotIn("<script", page)

    def test_the_mode_is_the_users_choice(self):
        settings = self.config()
        self.assertEqual(relay.mode_of(settings), "relay", "without a choice, nothing listens on the network")
        self.assertEqual(relay.mode_of(self.config(relay={"mode": "sideways"})), "relay")
        code, shown = self.run_relay("mode", settings)
        self.assertEqual(code, 0)
        self.assertIn("Mode: relay (through the relay on the internet", shown)
        self.assertEqual(self.run_relay("mode", settings, value="sideways")[0], 1)
        saved = util.home() / "talos.json"
        util.save_json_file(saved, {"model": "kept", "relay": {"folder": "kept too"}})
        code, shown = self.run_relay("mode", settings, value="Both")
        self.assertEqual(code, 0)
        self.assertIn("Mode: both", shown)
        self.assertEqual(util.load_json_file(saved), {"model": "kept", "relay": {"folder": "kept too", "mode": "both"}})

        # local: the link holds the address of this computer, and nothing is asked of the relay on the internet
        nowhere = {"url": "http://127.0.0.1:9", "mode": "local", "host": "127.0.0.1", "port": 4999}
        local = self.config(relay=nowhere)
        code, shown = self.run_relay("pair", local)
        link = relay.load_link()
        self.assertEqual(code, 0, shown)
        self.assertIn(relay.PAIR_TITLES["local"], shown)
        self.assertNotIn(relay.PAIR_TITLES["relay"], shown)
        self.assertNotIn("could not be reached", shown)
        self.assertEqual(relay.pair_entries(link, local),
                         [("local", f"http://127.0.0.1:4999/#{link['channel']}.{link['key']}")])
        status = self.run_relay("status", local)[1]
        self.assertIn("On the same network the phone opens http://127.0.0.1:4999", status)
        self.assertIn("Mode: local", status)
        self.assertNotIn("Linked through", status)
        self.assertNotIn(link["key"], status)
        code, shown = self.run_relay("code", local)
        self.assertEqual(code, 1)
        self.assertIn("talos relay pair", shown)

        # both: one code for anywhere and one for the same network, with the same key
        fake = FakeRelay()
        self.closers.append(fake.close)
        both = self.config(relay=dict(nowhere, url=fake.address, mode="both"))
        relay.make_link(both)
        code, shown = self.run_relay("pair", both)
        link = relay.load_link()
        kinds = relay.pair_entries(link, both)
        self.assertEqual([which for which, _ in kinds], ["relay", "local"])
        self.assertEqual(len({address.split("#")[1] for _, address in kinds}), 1)
        self.assertIn(relay.PAIR_TITLES["relay"], shown)
        self.assertIn(relay.PAIR_TITLES["local"], shown)
        self.assertIn(link["channel"], fake.owners)

    def test_which_keys_a_phone_may_fetch(self):
        settings = self.config()
        code, shown = self.run_relay("keys", settings)
        self.assertEqual(code, 0)
        self.assertIn("handed no key", shown)
        self.assertEqual(relay.phone_services(settings), [])
        code, shown = self.run_relay("keys", settings, value="openrouter,openai")
        self.assertEqual(code, 1)
        self.assertIn("Not: openai", shown)
        saved = util.home() / "talos.json"
        util.save_json_file(saved, {"relay": {"mode": "both"}})
        code, shown = self.run_relay("keys", settings, value="Groq,openrouter")
        self.assertEqual(code, 0)
        self.assertIn("openrouter, groq", shown)
        self.assertEqual(util.load_json_file(saved), {"relay": {"mode": "both", "phone_keys": ["openrouter", "groq"]}})
        self.assertEqual(relay.phone_services(util.load_json_file(saved)), ["openrouter", "groq"])
        self.assertIn("openrouter, groq", self.run_relay("keys", util.load_json_file(saved))[1])
        self.assertEqual(self.run_relay("keys", settings, value="none")[0], 0)
        self.assertEqual(util.load_json_file(saved), {"relay": {"mode": "both", "phone_keys": []}})


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
        for op in ("image", "hide", "keys"):
            self.assertIn(f'call("{op}"', script)
            self.assertIn(f'op == "{op}"', computer)
        for answer in ("once", "always", "reject"):
            self.assertIn(f'"{answer}"', script)
        for label in ("talos-relay pair v1|", "talos-relay pair room v1|", "talos-relay pair v1 key|"):
            self.assertIn(label, script)
            self.assertIn(label, computer)
        self.assertIn(f"{relay.CODE_PRIME:X}", script.replace('" +\n    "', ""), "both sides use the same group")
        version = re.search(r'var APP_VERSION = "([0-9.]+)";', script).group(1)
        listed = json.loads((site / "versions.json").read_text(encoding="utf-8"))["releases"]
        self.assertEqual(listed[0]["version"], version, "the newest entry of the version history is this page")
        self.assertEqual(page.count(f"?v={version}"), 4, "a new version reaches a phone that kept the old files")
        self.assertLess(page.index("lite-crypto.js"), page.index("app.js"), "the page's own cipher is there first")
        self.assertIn("crypto.subtle) || window.TalosLiteCrypto", script, "the browser's own cipher comes first")
        self.assertIn("img-src 'self' data: blob:", (site / "_headers").read_text(encoding="utf-8"))
        photo = int(re.search(r"PHOTO_CHARS = (\d+)", script).group(1))
        self.assertLessEqual(photo, relay.PHOTO_CHARS, "the page sends no photo the computer refuses")
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("manifest", page, "a manifest would make the Home Screen icon forget the link")
        for name in ("app.js", "phone.js", "lite-crypto.js", "styles.css", "icon.svg", "icon-180.png"):
            self.assertIn(name, page)
            self.assertTrue((site / name).is_file(), name)
        lite = (site / "lite-crypto.js").read_text(encoding="utf-8")
        self.assertFalse("://" in page + lite + script.replace("https://<relay>/", ""),
                         "the page must load nothing from another site")
        headers = dict(line.strip().split(": ", 1) for line in (site / "_headers").read_text(encoding="utf-8").splitlines()
                       if ": " in line)
        self.assertEqual(headers, relay_local.PAGE_HEADERS, "the relay inside Talos hands the page out the same way")
        self.assertIn("script-src 'self'", (site / "_headers").read_text(encoding="utf-8"))

        # the part for a phone with no computer: it reaches the model services it names and nothing else
        alone = (site / "phone.js").read_text(encoding="utf-8")
        self.assertLess(page.index("phone.js"), page.index("app.js"))
        self.assertNotIn("innerHTML", alone)
        allowed = re.search(r"connect-src ([^;]+)", headers["Content-Security-Policy"]).group(1).split()
        self.assertEqual(sorted(set(allowed) - {"'self'"}), sorted(set(re.findall(r"https://[a-z0-9.-]+", alone))),
                         "the page may reach exactly the services phone.js asks")
        places = dict(re.findall(r'var (\w+) = "(https://[^"]+)";', alone))
        routes = [(family, places[place], name) for family, place, name in re.findall(r'\["(\w+)", (\w+), "([^"]+)"\]', alone)]
        self.assertEqual(routes, [tuple(route[:3]) for route in glow.ROUTES], "the same free models as glow/... on a computer")
        self.assertIn("var GLOW_NAMES = " + json.dumps(list(glow.NAMES)) + ";", alone)
        for name in relay.PHONE_SERVICES:
            self.assertIn(f'{name}: {{ name: ', alone)
            self.assertIn(f'base: "{config_module.PRESETS[name][0]}"', alone)
        worker = (ROOT / "relay" / "functions" / "c" / "[[path]].js").read_text(encoding="utf-8")
        for path in ("/state", "asks/take", '"ask"', '"answer"', "X-Relay-Write"):
            self.assertIn(path, worker)

    def test_page_is_handed_out(self):
        fake = FakeRelay()
        self.closers.append(fake.close)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for name, start in (("", b"<!doctype html>"), ("app.js", b"/* Talos Relay"), ("styles.css", b":root"),
                            ("lite-crypto.js", b"/* Talos Relay"), ("phone.js", b"/* Talos Relay"),
                            ("icon-180.png", b"\x89PNG"), ("versions.json", b"{")):
            with opener.open(f"{fake.address}/{name}", timeout=20) as response:
                self.assertEqual(response.read()[:len(start)], start, name)
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(relay_local.site_folder(), ROOT / "relay" / "site")

    def test_page_brings_its_own_cipher(self):
        """lite-crypto.js against the cipher of Node (the one a browser has) and against the computer side."""
        node = shutil.which("node")
        if not node:
            self.skipTest("needs Node")
        cipher, extra = relay.Cipher(bytes(range(32))), b"talos-relay/1/channel/ask/one"
        sealed = cipher.seal("from the computer \u2713".encode("utf-8"), extra)
        check = self.home / "lite-check.js"
        check.write_text(LITE_CHECK, encoding="utf-8")
        ran = subprocess.run([node, str(check), str(ROOT / "relay" / "site" / "lite-crypto.js"), sealed.hex(), extra.hex()],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        said, back = ran.stdout.split()
        self.assertEqual(said, "ok")
        self.assertEqual(cipher.open(bytes.fromhex(back), extra).decode("utf-8"), "from the computer \u2713, read on the phone")

    def test_phone_alone_answers_by_itself(self):
        """phone.js in Node with stand-in services: it chats, falls to the next free model, stops, keeps keys."""
        node = shutil.which("node")
        if not node:
            self.skipTest("needs Node")
        check = self.home / "phone-check.js"
        check.write_text(PHONE_CHECK, encoding="utf-8")
        ran = subprocess.run([node, str(check), str(ROOT / "relay" / "site" / "phone.js")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual((ran.returncode, ran.stdout.strip()), (0, "ok"), ran.stderr + ran.stdout)


PHONE_CHECK = r"""
const kept = {}, asked = [];
global.localStorage = { getItem: (key) => (key in kept ? kept[key] : null), setItem: (key, value) => { kept[key] = String(value); } };
global.window = {};
if (typeof crypto === "undefined") global.crypto = require("crypto").webcrypto;
let mode = "second";
const refuse = (status, words) => ({ ok: false, status: status, text: async () => JSON.stringify({ error: { message: words } }) });
const whole = (data) => ({ ok: true, status: 200, text: async () => JSON.stringify(data), json: async () => data });
function pieces(parts) {
  const lines = parts.map((part) => "data: " + JSON.stringify({ choices: [{ delta: { content: part } }] }) + "\n\n").concat(["data: [DONE]\n\n"]);
  let at = 0;
  return { ok: true, status: 200, body: { getReader: () => ({
    read: async () => (at < lines.length ? { done: false, value: new TextEncoder().encode(lines[at++]) } : { done: true }) }) } };
}
global.fetch = async (url, options) => {
  options = options || {};
  const body = options.body ? JSON.parse(options.body) : null;
  asked.push({ url: url, model: body && body.model, key: (options.headers || {}).Authorization || "", messages: body && body.messages });
  if (url.indexOf("openrouter.ai") >= 0) {
    if (mode === "wrong key") return refuse(401, "No auth credentials found");
    if (url.slice(-4) === "/key") return whole({ data: { label: "test" } });
    if (url.slice(-7) === "/models") return whole({ data: [{ id: "maker/small:free" }, { id: "maker/paid" }, { id: "openrouter/free" }, { id: "maker/whisper:free" }] });
    return whole({ choices: [{ message: { content: "From OpenRouter." } }] });
  }
  if (mode === "down") return refuse(500, "closed today");
  if (mode === "hang") {
    return new Promise((resolve, reject) => options.signal.addEventListener("abort", () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      reject(error);
    }));
  }
  if (body.model === "DeepSeek-V4.1-Flash-1017") return refuse(429, "slow down");
  return pieces(["Hel", "lo."]);
};
const check = (fine, what) => { if (!fine) throw new Error(what); };
const rest = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
require(process.argv[2]);
const phone = window.TalosPhone;
phone.version = "test";
async function until(session, ready) {
  for (let round = 0; round < 500; round++) {
    const seen = await phone.call("look", { session: session });
    if (ready(seen.chat)) return seen;
    await rest(10);
  }
  throw new Error("not reached");
}
(async () => {
  let seen = await phone.call("look", {});
  check(seen.ok && seen.alone && seen.chats.length === 0 && seen.chat === null, "the first look");
  check(seen.models.join() === "glow/auto,glow/deepseek,glow/qwen" && seen.model === "glow/auto", "without a key: the free models");
  check((await phone.call("send", { text: "  " })).error.indexOf("Type a message") === 0, "an empty prompt");
  check((await phone.call("send", { text: "x", img: "abc" })).error, "a photo is refused");
  check((await phone.call("send", { text: "x", model: "openrouter/openrouter/free" })).error.indexOf("does not have") > 0, "a model with no key");
  check(asked.length === 0 && (await phone.call("look", {})).chats.length === 0, "what was refused started nothing");

  // the first free model says no: the next one answers, piece by piece
  const first = (await phone.call("send", { text: "Say hello", model: "glow/auto" })).session;
  seen = await until(first, (chat) => !chat.running);
  check(JSON.stringify(seen.chat.items.map((item) => [item.role, item.text])) === '[["you","Say hello"],["talos","Hello."]]', "the answer");
  check(seen.chat.live === null && seen.chat.model === "glow/auto" && seen.chat.prompts === 1, "the chat after the answer");
  check(asked.map((one) => one.model).join() === "DeepSeek-V4.1-Flash-1017,Qwen3.8-27B", "the order of the free models");
  check(asked[1].url === "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions" && asked[1].key === "", "where it asked");
  check(asked[1].messages[0].role === "system" && asked[1].messages[1].content === "Say hello", "what it sent");
  check(seen.chats[0].id === first && seen.chats[0].step === "Hello." && seen.chats[0].title === "Say hello", "the row in the list");
  check(JSON.parse(kept["talos-phone-chats"])[first].messages.length === 2, "the chat is kept in the browser");

  // no service answers: it says which and why, and the chat keeps the prompt
  mode = "down";
  check((await phone.call("send", { session: first, text: "Again" })).session === first, "a second prompt in the same chat");
  seen = await until(first, (chat) => !chat.running);
  check(seen.chat.live.done.stopped === "model error" && seen.chat.live.done.text.indexOf("closed today") > 0, "why no answer came");
  check(seen.chat.items.length === 3 && asked.length === 2 + 8, "every free model was tried once");

  // stop ends the wait
  mode = "hang";
  const second = (await phone.call("send", { text: "Wait for me", model: "glow/qwen" })).session;
  seen = await until(second, (chat) => chat.running);
  check((await phone.call("send", { session: second, text: "more" })).error.indexOf("still working") > 0, "one answer at a time");
  check((await phone.call("hide", { session: second })).ok === false, "a working chat is not removed");
  check((await phone.call("stop", { session: second })).ok === true, "stop");
  seen = await until(second, (chat) => !chat.running);
  check(seen.chat.live.done.stopped === "you stopped it", "it says who stopped it");
  check((await phone.call("stop", { session: "nothing" })).ok === false, "nothing to stop");

  // a key: only the models of that service that cost nothing, and the key goes to that service alone
  mode = "second";
  check(phone.setKey("openrouter", " sk-or-test ") === true && phone.setKey("elsewhere", "x") === false, "a key is kept");
  check(phone.names().join() === "glow/auto,glow/deepseek,glow/qwen,openrouter/openrouter/free", "before the list is read");
  check((await phone.refresh("openrouter")) === 2, "the models that cost nothing");
  check(phone.names().slice(3).join() === "openrouter/maker/small:free,openrouter/openrouter/free", "the list of the service");
  const third = (await phone.call("send", { text: "With a key", model: "openrouter/maker/small:free" })).session;
  seen = await until(third, (chat) => !chat.running);
  const last = asked[asked.length - 1];
  check(seen.chat.items[1].text === "From OpenRouter." && last.model === "maker/small:free", "an answer that came in one piece");
  check(last.url === "https://openrouter.ai/api/v1/chat/completions" && last.key === "Bearer sk-or-test", "the key went along");
  check(asked.filter((one) => one.key && one.url.indexOf("https://openrouter.ai/") !== 0).length === 0, "no other service saw the key");
  mode = "wrong key";
  check(await phone.refresh("openrouter").then(() => false, (error) => error.message.indexOf("the key was refused") === 0), "a wrong key");
  check(phone.setKey("openrouter", "") === true && phone.names().length === 3 && !("openrouter" in phone.keys()), "the key is taken out");

  // taken off the phone, a chat is gone
  const left = await phone.call("hide", { session: first });
  check(left.ok && left.chats.map((row) => row.id).sort().join() === [third, second].sort().join(), "the list after removing one");
  check((await phone.call("look", { session: first })).gone === first, "it is gone");
  check((await phone.call("hide", { session: first })).ok === false && (await phone.call("reply", {})).ok === false, "what only a computer has");
  console.log("ok");
  process.exit(0);
})().catch((error) => { console.error(error.message); process.exit(1); });
"""


LITE_CHECK = r"""
const lite = require(process.argv[2]), real = require("crypto").webcrypto;
const hex = (data) => Buffer.from(new Uint8Array(data)).toString("hex");
const fail = (what) => { throw new Error(what); };
(async () => {
  for (const size of [0, 1, 15, 16, 17, 33, 55, 56, 63, 64, 65, 119, 120, 1000, 70001]) {
    const data = new Uint8Array(size);
    data.set(real.getRandomValues(new Uint8Array(Math.min(size, 65536))));
    if (hex(await lite.digest("SHA-256", data)) !== hex(await real.subtle.digest("SHA-256", data))) fail("digest " + size);
    for (const extraSize of [0, 5, 16, 40]) {
      const raw = real.getRandomValues(new Uint8Array(32)), iv = real.getRandomValues(new Uint8Array(12));
      const extra = real.getRandomValues(new Uint8Array(extraSize));
      const how = extraSize ? { name: "AES-GCM", iv: iv, additionalData: extra } : { name: "AES-GCM", iv: iv };
      const mine = await lite.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
      const theirs = await real.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
      const sealed = new Uint8Array(await lite.encrypt(how, mine, data));
      if (hex(sealed) !== hex(await real.subtle.encrypt(how, theirs, data))) fail("encrypt " + size + "/" + extraSize);
      if (hex(await lite.decrypt(how, mine, sealed.subarray(0))) !== hex(data)) fail("decrypt " + size);
      sealed[sealed.length - 1 - (size % 16)] ^= 4;
      if (await lite.decrypt(how, mine, sealed).then(() => true, () => false)) fail("a changed text was opened " + size);
    }
    for (const keySize of [1, 32, 64, 65, 200]) {
      const raw = real.getRandomValues(new Uint8Array(keySize)), kind = { name: "HMAC", hash: "SHA-256" };
      const mine = await lite.importKey("raw", raw, kind, false, ["sign"]);
      const theirs = await real.subtle.importKey("raw", raw, kind, false, ["sign"]);
      if (hex(await lite.sign("HMAC", mine, data)) !== hex(await real.subtle.sign("HMAC", theirs, data))) fail("sign " + size);
    }
  }
  // what the computer sealed is read, and answered so that the computer can read it
  const whole = Buffer.from(process.argv[3], "hex"), extra = Buffer.from(process.argv[4], "hex");
  const key = await lite.importKey("raw", Uint8Array.from({ length: 32 }, (_, at) => at), "AES-GCM", false, ["encrypt", "decrypt"]);
  const how = { name: "AES-GCM", iv: whole.subarray(0, 12), additionalData: extra };
  const text = new TextDecoder().decode(await lite.decrypt(how, key, whole.subarray(12)));
  const nonce = real.getRandomValues(new Uint8Array(12));
  const answer = await lite.encrypt({ name: "AES-GCM", iv: nonce, additionalData: extra }, key, new TextEncoder().encode(text + ", read on the phone"));
  console.log("ok", hex(nonce) + hex(answer));
})().catch((error) => { console.error(error.message); process.exit(1); });
"""


class LocalRelayTest(Case):
    """Talos without the relay on the internet: `talos relay` passes the sealed text on itself."""

    def free_port(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        return port

    def start(self, mode, url, steps):
        before, relay.LOCK_PORT = relay.LOCK_PORT, self.free_port()          # this computer may run a real one
        self.closers.append(lambda: setattr(relay, "LOCK_PORT", before))
        model = self.model(steps)
        settings = dict(proof="off", relay={"url": url, "mode": mode, "host": "127.0.0.1", "port": self.free_port()})
        self.settings = self.config(model, **settings)
        self.link = relay.make_link(self.settings)
        self.notes, stop, ended = [], threading.Event(), []
        thread = threading.Thread(daemon=True, target=lambda: ended.append(relay.run(
            self.settings, self.store, self.link, self.notes.append, model.config(**settings), stop=stop)))
        thread.start()
        for close in (lambda: thread.join(15), stop.set):
            self.closers.append(close)
        inside = relay.local_link(self.link, self.settings)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if util.http("GET", f"{inside['url']}/c/{inside['channel']}/state", None, None, 5)[0] == 200:
                    return Phone(inside), ended
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError(f"the relay inside Talos did not come up: {self.notes} {ended}")

    def test_phone_reaches_talos_through_the_relay_inside(self):
        # the relay on the internet cannot be reached at all, and in mode local nobody asks it
        phone, ended = self.start("local", "http://127.0.0.1:9", [say("Done without the internet.")])
        self.assertEqual(phone.call("look")["chats"], [])
        session = phone.call("send", session="", text="Say that it works.", folder="")["session"]
        chat = phone.until(session, lambda chat: not chat["running"])
        self.assertIn("Done without the internet.", json.dumps(chat))
        self.assertIn("The relay inside Talos is on", "\n".join(self.notes))
        self.assertNotIn("cannot reach", "\n".join(self.notes))
        address = relay.local_addresses(self.settings)[0]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(address + "/", timeout=20) as response:
            self.assertIn(b"lite-crypto.js", response.read())
        self.assertEqual(ended, [], "it runs until it is stopped")

    def test_both_ways_reach_the_same_talos(self):
        fake = FakeRelay()
        self.closers.append(fake.close)
        inside, ended = self.start("both", fake.address, [say("Seen from both sides.")])
        deadline = time.time() + 30
        while self.link["channel"] not in fake.owners and time.time() < deadline:
            time.sleep(0.05)
        outside = Phone(self.link)
        session = inside.call("send", session="", text="Start here.", folder="")["session"]
        chat = outside.until(session, lambda chat: not chat["running"])
        self.assertIn("Seen from both sides.", json.dumps(chat))
        self.assertEqual([row["id"] for row in inside.call("look")["chats"]], [session])
        self.assertNotIn(self.link["key"], json.dumps(fake.seen))

    def test_only_this_computer_makes_a_channel(self):
        fake = FakeRelay()
        self.closers.append(fake.close)
        link = relay.make_link(self.config(relay={"url": fake.address}))
        line, near = relay.Line(link), relay_local.Handler.near
        self.closers.append(line.wire.close)
        self.closers.append(lambda: setattr(relay_local.Handler, "near", near))
        relay_local.Handler.near = lambda handler: False                     # as a request from another device
        self.assertEqual(line.put_state({"computer": "theirs"}), 403)
        self.assertEqual(fake.owners, {})
        relay_local.Handler.near = near
        self.assertEqual(line.put_state({"computer": "mine"}), 200)
        relay_local.Handler.near = lambda handler: False
        phone = Phone(link)                                                  # a phone on the network still asks and reads
        self.assertEqual(phone.leave("a" * 12, "sealed"), 200)
        self.assertEqual(util.http("GET", f"{line.base}/state")[0], 200)
        other = relay.Line(dict(link, token=secrets.token_urlsafe(32)))
        self.closers.append(other.wire.close)
        self.assertEqual((other.put_state({"computer": "theirs"}), other.take(0)[0], other.forget()), (403, 404, 404))

        # what nobody fetched is dropped
        self.assertEqual(line.answer("b" * 12, {"late": True}), 200)
        self.assertEqual(len(fake.asks[link["channel"]]) + len(fake.answers[link["channel"]]), 2)
        kept, relay_local.KEEP_S = relay_local.KEEP_S, 0
        self.closers.append(lambda: setattr(relay_local, "KEEP_S", kept))
        fake.swept = 0
        time.sleep(0.05)
        self.assertEqual(util.http("GET", f"{line.base}/state")[0], 200)
        self.assertEqual((fake.asks, fake.answers), ({}, {}))
        too_large = util.http("PUT", f"{line.base}/state", "x" * (relay_local.MAX_ANSWER + 2000), line.mine)[0]
        self.assertEqual(too_large, 413)


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
