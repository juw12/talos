import json

from talos.fake import call, say
from tests.base import Case


class StoreTest(Case):
    def test_sessions(self):
        first = self.store.new_session(self.work, model="a/one", title="first job")
        second = self.store.new_session(self.work, model="a/two", title="second job")
        child = self.store.new_session(self.work, parent=second, title="helper")
        self.store.add_message(first, {"role": "user", "content": "hello"})
        self.store.add_message(first, {"role": "assistant", "content": "hi there"})
        self.store.add_usage(first, 120, 30, 0.002)
        self.store.add_usage(first, 80, 20, 0.001)

        listed = [item["id"] for item in self.store.sessions()]
        self.assertEqual(set(listed), {first, second})
        self.assertIn(child, [item["id"] for item in self.store.sessions(include_children=True)])
        self.assertEqual(self.store.find_session(first[:6]), first)
        self.assertEqual(self.store.find_session("last"), first)
        self.assertEqual([message["content"] for message in self.store.messages(first)], ["hello", "hi there"])

        info = self.store.session(first)
        self.assertEqual((info["tokens_in"], info["tokens_out"]), (200, 50))
        stats = self.store.stats()
        self.assertEqual((stats["sessions"], stats["tokens_in"], stats["messages"]), (3, 200, 2))
        self.assertAlmostEqual(stats["cost"], 0.003)

        self.store.delete_session(first)
        self.assertIsNone(self.store.session(first))
        self.assertEqual(self.store.messages(first), [])
        self.assertEqual(self.store.find_session("last"), second)

    def test_undo_redo(self):
        (self.work / "old.txt").write_text("before\n", encoding="utf-8")
        runner, _, _ = self.agent([
            call("write", filePath="new.txt", content="made\n"),
            call("edit", filePath="old.txt", oldString="before", newString="after"),
            say("first turn done"), say("really done"),
            say("second turn done"),
        ], proof="off")
        runner.run("change two files")
        runner.run("say something")
        session = runner.session
        self.assertEqual(len(self.store.turns(session)), 2)

        self.assertIsNotNone(self.store.undo(session))           # the talk-only turn
        self.assertTrue((self.work / "new.txt").exists())
        self.assertIsNotNone(self.store.undo(session))           # the turn that changed files
        self.assertFalse((self.work / "new.txt").exists())
        self.assertEqual((self.work / "old.txt").read_text(encoding="utf-8"), "before\n")
        self.assertEqual([message["role"] for message in self.store.messages(session)], ["system"])
        self.assertIsNone(self.store.undo(session))

        self.assertIsNotNone(self.store.redo(session))
        self.assertEqual((self.work / "new.txt").read_text(encoding="utf-8"), "made\n")
        self.assertEqual((self.work / "old.txt").read_text(encoding="utf-8"), "after\n")
        self.assertEqual(len(self.store.turns(session)), 1)
        self.assertIsNotNone(self.store.redo(session))
        self.assertEqual(len(self.store.turns(session)), 2)
        self.assertIsNone(self.store.redo(session))

    def test_share_and_export(self):
        session = self.store.new_session(self.work, title="keys <b>")
        self.store.add_message(session, {"role": "user", "content": "my key is sk-abcdefghijklmnopqrstuvwxyz123456 ok?"})
        self.store.add_message(session, {"role": "assistant", "content": "Use <script>alert(1)</script> never.",
                                         "tool_calls": [{"id": "c1", "name": "bash",
                                                         "arguments": {"command": "set GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789"}}]})
        page = self.store.share_html(session)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", page)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", page)
        self.assertNotIn("<script>alert", page)
        self.assertIn("my key is", page)
        self.assertIn("keys &lt;b&gt;", page)
        page.encode("ascii")

        exported = json.loads(json.dumps(self.store.export(session)))
        copy = self.store.import_(exported)
        self.assertNotEqual(copy, session)
        self.assertEqual(self.store.messages(copy), self.store.messages(session))
        self.assertEqual(self.store.session(copy)["title"], "keys <b>")

    def test_search(self):
        pump = self.store.new_session(self.work, title="pump job")
        self.store.add_message(pump, {"role": "user", "content": "The coolant pump bracket cracked at the weld."})
        other = self.store.new_session(self.work, title="other job")
        self.store.add_message(other, {"role": "user", "content": "Order more filament."})
        hits = self.store.search("bracket weld")
        self.assertEqual([hit["session"] for hit in hits], [pump])
        self.assertIn("bracket", str(hits[0]).lower())
        self.assertEqual(self.store.search("bracket", skip_session=pump), [])
        self.assertEqual(self.store.search("!!!"), [])

        runner, _, events = self.agent([call("session_search", query="coolant pump"), say("found")])
        runner.run("what broke before?")
        self.assertIn("pump", self.results(events, "session_search")[0])
