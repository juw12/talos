import http.client
import json
import threading
import time

from talos import server
from talos.fake import call, say
from tests.base import Case


class ServerTest(Case):
    def serve(self, steps, password="", **extra):
        model = self.model(steps)
        overrides = model.config(proof="off", **extra)
        made, app = server.serve(self.config(model, proof="off", **extra), self.store, port=0,
                                 password=password, overrides=overrides)
        threading.Thread(target=made.serve_forever, daemon=True).start()
        self.closers.append(made.server_close)
        self.closers.append(made.shutdown)
        self.port = made.server_address[1]
        return app, model

    def call(self, method, path, body=None, headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = dict(headers or {})
        if host:
            headers["Host"] = host
        data = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8")
        connection.close()
        kind = response.getheader("Content-Type") or ""
        return response.status, (json.loads(text) if "json" in kind and text else text)

    def events(self, session, after=0):
        _, text = self.call("GET", f"/api/sessions/{session}/events?after={after}")
        return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]

    def wait_for(self, condition, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline and not condition():
            time.sleep(0.05)
        return condition()

    def test_page_and_state(self):
        self.serve([], projects={"gh": False})
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        for word in ("<title>Talos", "data-theme", "'ink'", "'paper'", "/api/ping", "Check-up"):
            self.assertTrue(word in page, word)
        self.assertFalse("http://" in page or "https://" in page, "the page must load nothing from the internet")
        self.assertIn("<svg", self.call("GET", "/icon.svg")[1])
        for word in ('rel="icon" href="/favicon.ico"', 'href="/icon.png"', 'rel="manifest"', 'data-page="knowledge"'):
            self.assertTrue(word in page, word)
        for path, start in (("/favicon.ico", b"\x00\x00\x01\x00"), ("/icon.png", b"\x89PNG")):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()[:4]), (200, start), path)
            connection.close()
        manifest = self.call("GET", "/manifest.webmanifest")[1]
        self.assertEqual((manifest["name"], manifest["icons"][0]["src"]), ("Talos", "/icon.png"))
        self.assertEqual(self.call("GET", "/api/knowledge")[1], {"vaults": [], "projects": []})
        self.assertEqual(self.call("POST", "/api/knowledge", {"q": "bolt"})[1], {"notes": [], "projects": []})
        self.assertEqual(self.call("GET", "/api/ping")[1]["app"], "talos")

        state = self.call("GET", "/api/state")[1]
        self.assertEqual(state["model"], "fake/script")
        self.assertTrue({"build", "plan"} <= {item["name"] for item in state["agents"]})
        parity = self.call("GET", "/api/parity")[1]
        self.assertEqual(set(parity["counts"]), {"opencode", "hermes", "talos"})
        self.assertIsInstance(self.call("GET", "/api/doctor")[1], list)
        self.assertEqual(self.call("GET", "/api/stats")[1]["sessions"], 0)
        self.assertEqual(self.call("GET", "/api/skills")[1], [])
        self.assertEqual(self.call("GET", "/api/search?q=anything")[1], [])

        saved = self.call("POST", "/api/memory", {"action": "add", "target": "user", "content": "Likes short answers."})[1]
        self.assertIn("Saved.", saved["result"])
        self.assertEqual(self.call("GET", "/api/memory")[1]["user"], ["Likes short answers."])
        made = self.call("POST", "/api/cron", {"schedule": "daily 07:00", "prompt": "Morning summary"})[1]
        self.assertIn("Scheduled job", made["result"])
        jobs = self.call("GET", "/api/cron")[1]
        self.assertEqual(jobs[0]["cwd"], str(self.work))
        self.assertIn("remove done", self.call("POST", "/api/cron", {"action": "remove", "id": jobs[0]["id"]})[1]["result"])

        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("GET", "/api/sessions/nope")[0], 404)
        self.assertEqual(self.call("POST", "/api/sessions/new/prompt", {"text": "  "})[0], 400)

    def test_prompt_events_undo(self):
        app, model = self.serve([call("write", filePath="made.txt", content="one\n"), say("The file is made."),
                                 call("write", filePath="asked.txt", content="two\n"), say("Second file made."),
                                 call("write", filePath="refused.txt", content="x\n"), say("Not written.")])
        status, started = self.call("POST", "/api/sessions/new/prompt", {"text": "make the file"})
        self.assertEqual(status, 200)
        session = started["session"]
        seen = self.events(session)
        kinds = [item["event"] for item in seen]
        for kind in ("tool", "tool_result", "text", "done", "closed"):
            self.assertIn(kind, kinds)
        self.assertEqual("".join(item["data"] for item in seen if item["event"] == "text"), "The file is made.")
        self.assertEqual(self.events(session, after=len(seen))[-1]["event"], "closed")
        self.assertTrue((self.work / "made.txt").exists())

        view = self.call("GET", f"/api/sessions/{session}")[1]
        self.assertEqual([message["role"] for message in view["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertFalse(view["running"])
        listed = self.call("GET", "/api/sessions")[1]
        self.assertEqual((listed[0]["id"], listed[0]["source"]), (session, "web"))

        self.assertTrue(self.call("POST", f"/api/sessions/{session}/undo")[1]["undone"])
        self.assertFalse((self.work / "made.txt").exists())
        self.assertTrue(self.call("POST", f"/api/sessions/{session}/redo")[1]["redone"])
        self.assertEqual((self.work / "made.txt").read_text(encoding="utf-8"), "one\n")
        self.assertIn("The file is made.", self.call("GET", f"/api/sessions/{session}/share")[1])
        self.assertEqual(self.call("GET", f"/api/sessions/{session}/export")[1]["talos"], 1)

        # a permission prompt goes to the page and waits for the answer
        app.overrides["permission"] = {"edit": "ask"}
        for answer, name in (("once", "asked.txt"), ("reject", "refused.txt")):
            self.call("POST", f"/api/sessions/{session}/prompt", {"text": "another file"})
            self.assertTrue(self.wait_for(lambda: app.pending))
            self.assertEqual(self.call("POST", f"/api/sessions/{session}/prompt", {"text": "meanwhile"})[0], 400)
            ident = list(app.pending)[0]
            self.assertTrue(self.call("POST", "/api/reply", {"id": ident, "answer": answer})[1]["ok"])
            seen = self.events(session)
            asked = [item["data"] for item in seen if item["event"] == "permission"][0]
            self.assertEqual((asked["tool"], asked["id"]), ("write", ident))
            self.assertEqual((self.work / name).exists(), answer == "once")
        self.assertFalse(self.call("POST", "/api/reply", {"id": "gone", "answer": "once"})[1]["ok"])

        self.assertEqual(self.call("DELETE", f"/api/sessions/{session}")[1], {"ok": True})
        self.assertEqual(self.call("GET", "/api/sessions")[1], [])

    def test_openai_endpoint(self):
        _, model = self.serve([say("Four."), say("Streamed answer here."), say("Planned.")])
        models = self.call("GET", "/v1/models")[1]
        self.assertIn("talos", [item["id"] for item in models["data"]])
        self.assertIn("talos/plan", [item["id"] for item in models["data"]])

        status, answer = self.call("POST", "/v1/chat/completions", {"model": "talos", "messages": [
            {"role": "system", "content": "be brief"}, {"role": "user", "content": "one plus one?"},
            {"role": "assistant", "content": "Two."}, {"role": "user", "content": [{"type": "text", "text": "and two plus two?"}]}]})
        self.assertEqual(status, 200)
        self.assertEqual(answer["choices"][0]["message"], {"role": "assistant", "content": "Four."})
        self.assertEqual(answer["usage"]["total_tokens"], 120)
        sent = str(model.requests[0]["messages"][-1]["content"])
        self.assertIn("assistant: Two.", sent)
        self.assertIn("Now: and two plus two?", sent)

        _, text = self.call("POST", "/v1/chat/completions", {"stream": True, "messages": [{"role": "user", "content": "go"}]})
        lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        self.assertEqual(lines[-1], "[DONE]")
        chunks = [json.loads(line) for line in lines[:-1]]
        self.assertEqual("".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks), "Streamed answer here.")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

        self.call("POST", "/v1/chat/completions", {"model": "talos/plan", "messages": [{"role": "user", "content": "plan it"}]})
        self.assertIn("plan mode", str(model.requests[-1]["messages"][0]["content"]))
        self.assertEqual(self.call("POST", "/v1/chat/completions", {"messages": []})[0], 400)

    def test_webhook(self):
        _, model = self.serve([say("Build 81 failed in the lint step.")], gateway={"webhook": {"secret": "s3cret"}})
        body = {"chat": "builds", "user": "ci", "text": "build 81 failed, why?"}
        self.assertEqual(self.call("POST", "/hook/ci", body)[0], 403)
        self.assertEqual(self.call("POST", "/hook/ci", dict(body, secret="wrong"))[0], 403)
        self.assertEqual(len(model.requests), 0)
        status, answer = self.call("POST", "/hook/ci", dict(body, secret="s3cret"))
        self.assertEqual((status, answer), (200, {"text": "Build 81 failed in the lint step."}))
        status, answer = self.call("POST", "/hook/ci", dict(body, text="/status"), headers={"X-Talos-Secret": "s3cret"})
        self.assertIn("idle; session", answer["text"])

    def test_refuses_other_origins(self):
        self.serve([])
        local = f"127.0.0.1:{self.port}"
        self.assertEqual(self.call("GET", "/api/state", headers={"Origin": f"http://{local}"})[0], 200)
        self.assertEqual(self.call("GET", "/api/state", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/sessions/new/prompt", {"text": "delete things"},
                                   headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/state", host="evil.example")[0], 403)       # DNS rebinding
        self.assertEqual(self.call("GET", "/", host=f"evil.example:{self.port}")[0], 403)
        with self.assertRaises(ValueError):
            server.serve(self.config(), self.store, host="0.0.0.0", port=0)

        self.serve([], password="open-sesame")
        self.assertEqual(self.call("GET", "/api/state")[0], 403)
        self.assertIn("Password", self.call("GET", "/")[1])
        self.assertEqual(self.call("POST", "/login", {"password": "guess"})[0], 401)
        self.assertEqual(self.call("POST", "/login", {"password": "open-sesame"})[0], 200)
        self.assertEqual(self.call("GET", "/api/state", headers={"Authorization": "Bearer open-sesame"})[0], 200)
        self.assertEqual(self.call("GET", "/api/state", headers={"Cookie": "a=1; talos=open-sesame"})[0], 200)
        self.assertEqual(self.call("GET", "/api/state", headers={"Authorization": "Bearer open-sesame",
                                                               "Origin": "https://evil.example"})[0], 403)
