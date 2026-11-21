import json
import queue
import threading

from talos import acp
from talos.fake import call, say
from tests.base import Case


class Pipe:
    """Stands in for the editor: collects what Talos writes, feeds lines when asked."""

    def __init__(self):
        self.lines, self.sent, self.changed = queue.Queue(), [], threading.Condition()

    def write(self, text):
        with self.changed:
            self.sent += [json.loads(line) for line in text.splitlines() if line.strip()]
            self.changed.notify_all()

    def flush(self):
        return

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line

    def say(self, message):
        self.lines.put(json.dumps(dict(message, jsonrpc="2.0")) + "\n")

    def wait(self, test, seconds=30):
        with self.changed:
            self.changed.wait_for(lambda: any(test(item) for item in self.sent), seconds)
            return next((item for item in self.sent if test(item)), None)


class AcpTest(Case):
    def test_prompt_round_trip(self):
        model = self.model([call("write", filePath="plan.txt", content="step one\n"), say("Wrote the plan.")])
        pipe = Pipe()
        overrides = model.config(proof="off", permission={"edit": "ask"})
        service = acp.Acp(self.store, reader=pipe, writer=pipe, overrides=overrides)
        thread = threading.Thread(target=service.serve, daemon=True)
        thread.start()

        pipe.say({"id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        hello = pipe.wait(lambda item: item.get("id") == 1)
        self.assertEqual(hello["result"]["agentInfo"]["name"], "talos")
        self.assertEqual(hello["result"]["protocolVersion"], 1)
        pipe.say({"id": 2, "method": "session/new", "params": {"cwd": str(self.work), "mcpServers": []}})
        session = pipe.wait(lambda item: item.get("id") == 2)["result"]["sessionId"]
        pipe.say({"id": 3, "method": "session/prompt",
                  "params": {"sessionId": session, "prompt": [{"type": "text", "text": "write the plan"}]}})

        asked = pipe.wait(lambda item: item.get("method") == "session/request_permission")
        self.assertIn("write", asked["params"]["toolCall"]["title"])
        self.assertFalse((self.work / "plan.txt").exists())
        pipe.say({"id": asked["id"], "result": {"outcome": {"outcome": "selected", "optionId": "once"}}})

        done = pipe.wait(lambda item: item.get("id") == 3)
        self.assertEqual(done["result"], {"stopReason": "end_turn"})
        self.assertEqual((self.work / "plan.txt").read_text(encoding="utf-8"), "step one\n")
        updates = [item["params"]["update"] for item in pipe.sent if item.get("method") == "session/update"]
        kinds = [update["sessionUpdate"] for update in updates]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_call_update", kinds)
        text = "".join(update["content"]["text"] for update in updates if update["sessionUpdate"] == "agent_message_chunk")
        self.assertTrue(text.startswith("Wrote the plan."), text)
        self.assertIn("Proof: unverified", text)                      # the editor sees the honest label too

        pipe.say({"id": 4, "method": "session/prompt", "params": {"sessionId": "nope", "prompt": []}})
        self.assertEqual(pipe.wait(lambda item: item.get("id") == 4)["error"]["code"], -32602)
        pipe.say({"id": 5, "method": "files/unknown", "params": {}})
        self.assertEqual(pipe.wait(lambda item: item.get("id") == 5)["error"]["code"], -32601)
        pipe.lines.put("this is not json\n")
        self.assertIsNotNone(pipe.wait(lambda item: (item.get("error") or {}).get("code") == -32700))
        pipe.lines.put(None)
        thread.join(30)
        self.assertFalse(thread.is_alive())
