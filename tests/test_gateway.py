import json
import time

from talos import gateway
from talos.fake import call, say
from tests.base import Case


class GatewayTest(Case):
    def wait_for(self, condition, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def test_telegram_round_trip(self):
        updates = [{"update_id": 5, "message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "what is 6 times 7?"}},
                   {"update_id": 6, "message": {"chat": {"id": 99}, "from": {"id": 99}, "text": "let me in"}},
                   {"update_id": 7, "message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/status"}}]
        sent = []

        def reply(method, path, body, headers):
            if "/getUpdates" in path:
                offset = int(path.split("offset=")[1])
                return 200, {"ok": True, "result": [item for item in updates if item["update_id"] >= offset]}
            sent.append(json.loads(body))
            return 200, {"ok": True}

        server = self.http(reply)
        model = self.model([say("It is 42."), say("Still 42.")])
        loaded = self.config(model, gateway={"telegram": {"token": "T0K", "base": server.url, "allowed_users": [7], "wait": 0}})
        bridge = gateway.Gateway(loaded, self.store)
        adapter = bridge.adapters["telegram"]
        incoming = adapter.fetch()
        self.assertEqual([(item.chat, item.text) for item in incoming], [("7", "what is 6 times 7?"), ("99", "let me in"), ("7", "/status")])
        self.assertEqual(adapter.fetch(), [])                       # the offset moved on
        self.assertTrue(all(path.startswith("/botT0K/") for _, path, _, _ in server.requests))

        bridge.handle("telegram", incoming[0], wait=True)
        bridge.handle("telegram", incoming[1], wait=True)
        bridge.handle("telegram", incoming[2], wait=True)
        self.assertEqual(sent[0], {"chat_id": "7", "text": "It is 42."})
        self.assertIn("Not authorised", sent[1]["text"])
        self.assertEqual(sent[1]["chat_id"], "99")
        self.assertIn("idle; session", sent[2]["text"])
        self.assertEqual(len(model.requests), 1)                    # the stranger never reached the model

        session = bridge.sessions["telegram:7"]
        bridge.handle("telegram", gateway.Incoming(7, 7, "and again?"), wait=True)
        self.assertEqual(bridge.sessions["telegram:7"], session)    # same chat, same conversation
        self.assertEqual(len(model.requests[-1]["messages"]), 4)
        bridge.handle("telegram", gateway.Incoming(7, 7, "/new"), wait=True)
        self.assertNotIn("telegram:7", bridge.sessions)
        self.assertEqual(self.store.session(session)["source"], "telegram")

    def test_other_platforms(self):
        sent = []
        state = {"discord": 0, "matrix": 0}

        def reply(method, path, body, headers):
            if method in ("POST", "PUT"):
                sent.append((path, body, headers.get("Authorization", "")))
                return 200, {"ok": True}
            if path.startswith("/channels/c1/messages"):                # Discord
                state["discord"] += 1
                if "after=" not in path:
                    return 200, [{"id": "100", "content": "old", "author": {"id": "u1"}}]
                return 200, [{"id": "102", "content": "from a bot", "author": {"id": "b", "bot": True}},
                             {"id": "101", "content": "hello discord", "author": {"id": "u1"}}]
            if path.startswith("/conversations.history"):               # Slack
                return 200, {"ok": True, "messages": [{"ts": "9999999999.000200", "text": "hello slack", "user": "U1"},
                                                      {"ts": "9999999999.000100", "text": "bot text", "bot_id": "B1"}]}
            if path.startswith("/api/v4/channels/m1/posts"):            # Mattermost
                return 200, {"order": ["p1", "p2"], "posts": {"p1": {"user_id": "u9", "message": "hello mattermost"},
                                                               "p2": {"user_id": "bot", "message": "my own post"}}}
            if path.startswith("/_matrix/client/v3/sync"):              # Matrix
                state["matrix"] += 1
                if "since=" not in path:
                    return 200, {"next_batch": "s1"}
                return 200, {"next_batch": "s2", "rooms": {"join": {"!room:x": {"timeline": {"events": [
                    {"type": "m.room.message", "sender": "@amy:x", "content": {"body": "hello matrix"}},
                    {"type": "m.room.message", "sender": "@me:x", "content": {"body": "my own"}}]}}}}}
            if path.startswith("/ask/json"):                            # ntfy
                lines = [json.dumps({"id": "n1", "event": "open"}),
                         json.dumps({"id": "n2", "event": "message", "message": "hello ntfy", "title": "phone"})]
                return 200, "\n".join(lines), "application/x-ndjson"
            if path.startswith("/v1/receive/"):                         # Signal
                return 200, [{"envelope": {"source": "+62811", "dataMessage": {"message": "hello signal"}}},
                             {"envelope": {"source": "+62811", "receiptMessage": {}}}]
            return 404, {}

        server = self.http(reply)
        made = {
            "discord": gateway.Discord({"token": "D", "base": server.url, "channels": ["c1"]}),
            "slack": gateway.Slack({"token": "S", "base": server.url, "channels": ["s1"]}),
            "mattermost": gateway.Mattermost({"token": "M", "base": server.url, "channels": ["m1"], "bot_user_id": "bot"}),
            "matrix": gateway.Matrix({"token": "X", "base": server.url, "user_id": "@me:x"}),
            "ntfy": gateway.Ntfy({"base": server.url, "topic": "ask", "reply_topic": "answers"}),
            "signal": gateway.Signal({"base": server.url, "number": "+62800"}),
        }
        for name in ("discord", "slack", "matrix"):
            self.assertEqual(made[name].fetch(), [], name)             # the first look only marks "now"
        heard = {name: [(item.chat, item.user, item.text) for item in adapter.fetch()] for name, adapter in made.items()}
        self.assertEqual(heard["discord"], [("c1", "u1", "hello discord")])
        self.assertEqual(heard["slack"], [("s1", "U1", "hello slack")])
        self.assertEqual(heard["mattermost"], [("m1", "u9", "hello mattermost")])
        self.assertEqual(heard["matrix"], [("!room:x", "@amy:x", "hello matrix")])
        self.assertEqual(heard["ntfy"], [("ask", "phone", "hello ntfy")])
        self.assertEqual(heard["signal"], [("+62811", "+62811", "hello signal")])

        for name, adapter in made.items():
            adapter.send(heard[name][0][0], f"answer for {name}")
        paths = [path for path, _, _ in sent]
        self.assertEqual(paths[0], "/channels/c1/messages")
        self.assertEqual(paths[1], "/chat.postMessage")
        self.assertEqual(paths[2], "/api/v4/posts")
        self.assertTrue(paths[3].startswith("/_matrix/client/v3/rooms/%21room%3Ax/send/m.room.message/"))
        self.assertEqual(paths[4], "/answers")
        self.assertEqual(paths[5], "/v2/send")
        self.assertEqual([auth for _, _, auth in sent[:4]], ["Bot D", "Bearer S", "Bearer M", "Bearer X"])
        self.assertEqual(json.loads(sent[0][1]), {"content": "answer for discord"})
        self.assertEqual(json.loads(sent[5][1])["recipients"], ["+62811"])
        self.assertEqual(sent[4][1], "answer for ntfy")

        long_text = "x" * 4500
        made["discord"].send("c1", long_text)
        self.assertEqual([len(json.loads(body)["content"]) for _, body, _ in sent[6:]], [1900, 1900, 700])

        model = self.model([say("Hello from Talos.")])
        loaded = self.config(model, gateway={"discord": {"token": "D", "base": server.url, "channels": ["c1"], "allowed_users": "*"}})
        bridge = gateway.Gateway(loaded, self.store, adapters={"discord": made["discord"]})
        del sent[:]
        bridge.handle("discord", gateway.Incoming("c1", "u1", "hello discord"), wait=True)
        self.assertEqual(json.loads(sent[0][1]), {"content": "Hello from Talos."})

    def test_approval_in_chat(self):
        sent = []

        class Recorder(gateway.Adapter):
            def post(self, chat, text):
                sent.append(text)

        (self.work / "keep").mkdir()
        (self.work / "keep" / "data.txt").write_text("important\n", encoding="utf-8")
        steps = [call("bash", command="Remove-Item keep -Recurse -Force"), say("I did not delete it.")]
        model = self.model(steps + steps + [call("write", filePath="note.txt", content="ok\n"), say("note written"), say("note written")])
        loaded = self.config(model, gateway={"chat": {"allowed_users": ["boss"]}}, approval_wait=20,
                             permission={"edit": "ask"})
        bridge = gateway.Gateway(loaded, self.store, adapters={"chat": Recorder({})})

        thread = bridge.handle("chat", gateway.Incoming("room", "boss", "clean up the folder"))
        self.assertTrue(self.wait_for(lambda: "chat:room" in bridge.waiting))
        self.assertIn("Allow bash: Remove-Item keep -Recurse -Force", sent[0])
        self.assertIn("deletes folders", sent[0])
        bridge.handle("chat", gateway.Incoming("room", "boss", "no"))
        thread.join(30)
        self.assertTrue((self.work / "keep" / "data.txt").exists())
        self.assertIn("I did not delete it.", sent[-1])

        del sent[:]
        loaded["approval_wait"] = 0.5                                   # nobody answers: that is a no
        thread = bridge.handle("chat", gateway.Incoming("room", "boss", "try again"))
        thread.join(30)
        self.assertTrue((self.work / "keep" / "data.txt").exists())

        del sent[:]
        loaded["approval_wait"] = 20
        thread = bridge.handle("chat", gateway.Incoming("room", "boss", "write a note"))
        self.assertTrue(self.wait_for(lambda: "chat:room" in bridge.waiting))
        self.assertIn("Allow write", sent[0])
        bridge.handle("chat", gateway.Incoming("room", "boss", "yes"))
        thread.join(30)
        self.assertEqual((self.work / "note.txt").read_text(encoding="utf-8"), "ok\n")

        bridge.handle("chat", gateway.Incoming("room", "stranger", "delete everything"), wait=True)
        self.assertIn("Not authorised", sent[-1])
