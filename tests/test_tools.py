import time
from pathlib import Path

from talos import tools
from talos.fake import call, say
from tests.base import PYTHON, Case

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


class FileToolsTest(Case):
    def test_edit_and_patch(self):
        (self.work / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        patch_text = "\n".join([
            "*** Begin Patch",
            "*** Add File: new.txt",
            "+first line",
            "+second line",
            "*** Update File: notes.txt",
            "@@",
            " alpha",
            "-beta",
            "+BETA",
            " gamma",
            "*** End Patch",
        ])
        runner, _, events = self.agent([
            call("read", filePath="notes.txt"),
            call("edit", filePath="notes.txt", oldString="alpha", newString="ALPHA"),
            call("edit", filePath="notes.txt", oldString="missing words", newString="x"),
            call("patch", patchText=patch_text.replace(" alpha", " ALPHA")),
            call("write", filePath="sub/made.txt", content="made\n"),
            call("read", filePath="nope.txt"),
            say("done"),
        ])
        runner.run("change the files")
        self.assertIn("1| alpha", self.results(events, "read")[0])
        self.assertEqual((self.work / "notes.txt").read_text(encoding="utf-8"), "ALPHA\nBETA\ngamma\n")
        self.assertEqual((self.work / "new.txt").read_text(encoding="utf-8"), "first line\nsecond line\n")
        self.assertEqual((self.work / "sub" / "made.txt").read_text(encoding="utf-8"), "made\n")
        self.assertNotIn("Edited", self.results(events, "edit")[1])
        self.assertIn("File not found", self.results(events, "read")[1])

    def test_search_tools(self):
        (self.work / "src").mkdir()
        (self.work / "src" / "one.py").write_text("def find_me():\n    return 1\n", encoding="utf-8")
        (self.work / "src" / "two.txt").write_text("nothing here\n", encoding="utf-8")
        runner, _, events = self.agent([
            call("grep", pattern="find_me"),
            call("grep", pattern="find_me", include="*.txt"),
            call("glob", pattern="**/*.py"),
            call("list", path="src"),
            say("done"),
        ])
        runner.run("look")
        found, not_found = self.results(events, "grep")
        self.assertIn("one.py", found)
        self.assertNotIn("one.py", not_found)
        self.assertIn("one.py", self.results(events, "glob")[0])
        listing = self.results(events, "list")[0]
        self.assertIn("one.py", listing)
        self.assertIn("two.txt", listing)

    def test_read_image(self):
        (self.work / "dot.png").write_bytes(PNG)
        runner, model, events = self.agent([call("read", filePath="dot.png"), say("a small dot")])
        runner.run("what is in dot.png")
        self.assertIn("attached", self.results(events, "read")[0])
        sent = str(model.requests[-1]["messages"])
        self.assertIn("data:image/png;base64,", sent)


class ShellTest(Case):
    def test_bash_and_background(self):
        (self.work / "slow.py").write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
        runner, _, events = self.agent([
            call("bash", command=f'"{PYTHON}" -c "print(6 * 7)"'),
            call("bash", command=f'"{PYTHON}" -c "raise SystemExit(4)"'),
            call("bash", command=f'"{PYTHON}" slow.py', background=True),
            call("process", action="list"),
            lambda request: (time.sleep(1.5), call("process", action="output", id="p1"))[1],
            call("process", action="kill", id="p1"),
            say("done"),
        ])
        runner.run("run things")
        good, bad, started = self.results(events, "bash")
        self.assertIn("42", good)
        self.assertIn("[exit code 0]", good)
        self.assertIn("[exit code 4]", bad)
        self.assertIn("p1", started)
        listing, output, killed = self.results(events, "process")
        self.assertIn("running", listing)
        self.assertIn("started", output)
        self.assertIn("still running", output)
        self.assertIn("Stopped p1", killed)

    def test_execute_code_calls_tools(self):
        (self.work / "data.txt").write_text("seven\n", encoding="utf-8")
        script = "\n".join([
            "text = tool('read', filePath='data.txt')",
            "tool('write', filePath='copy.txt', content='copied ' + text.split('| ')[1])",
            "print('refused:', tool('memory', action='add', target='memory', content='x'))",
            "print('ok')",
        ])
        runner, _, events = self.agent([call("execute_code", code=script), say("done")])
        runner.run("copy it")
        answer = self.results(events, "execute_code")[0]
        self.assertIn("ok", answer)
        self.assertIn("cannot be called from a script", answer)
        self.assertEqual((self.work / "copy.txt").read_text(encoding="utf-8").strip(), "copied seven")


class MiscToolsTest(Case):
    def test_todos_and_question(self):
        asked = []

        def ask(question, options):
            asked.append((question, options))
            return "blue"

        runner, _, events = self.agent([
            call("todowrite", todos=[{"content": "paint the wall", "status": "in_progress"},
                                     {"content": "clean up", "status": "pending"}]),
            call("todoread"),
            call("question", question="Which colour?", options=["red", "blue"]),
            say("done"),
        ], ask=ask)
        runner.run("plan")
        self.assertIn("paint the wall", self.results(events, "todoread")[0])
        self.assertIn("clean up", self.results(events, "todowrite")[0])
        self.assertEqual(asked, [("Which colour?", ["red", "blue"])])
        self.assertIn("blue", self.results(events, "question")[0])
        self.assertTrue([data for event, data in events if event == "todos"])

        runner, _, events = self.agent([call("question", question="Which colour?"), say("done")])
        runner.run("plan")
        self.assertIn("Nobody is here", self.results(events, "question")[0])


class WebTest(Case):
    def test_fetch_and_search(self):
        page = "<html><head><style>p{}</style></head><body><h1>Title</h1><p>Body words</p><script>bad()</script></body></html>"
        results = ('<div><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">'
                   'First <b>hit</b></a><a class="result__snippet" href="x">About the first hit</a></div>')

        def reply(method, path, body, headers):
            if path.startswith("/search"):
                return 200, results, "text/html"
            if path == "/gone":
                return 404, "no", "text/html"
            return 200, page, "text/html"

        server = self.http(reply)
        runner, _, events = self.agent([
            call("webfetch", url=server.url + "/page"),
            call("webfetch", url=server.url + "/gone"),
            call("webfetch", url="file:///etc/passwd"),
            call("websearch", query="first hit"),
            say("done"),
        ], search={"url": server.url + "/search"}, permission={"webfetch": "allow", "websearch": "allow"})
        runner.run("look it up")
        fetched, gone, refused = self.results(events, "webfetch")
        self.assertIn("Body words", fetched)
        self.assertNotIn("bad()", fetched)
        self.assertIn("404", gone)
        self.assertIn("must start with http", refused)
        found = self.results(events, "websearch")[0]
        self.assertIn("First hit", found)
        self.assertIn("https://example.com/a", found)
        self.assertIn("About the first hit", found)
        self.assertIn("q=first%20hit", server.requests[-1][1])


class MediaTest(Case):
    def test_image_generate(self):
        model = self.model([call("image_generate", prompt="a red dot", filePath="art/dot.png"), say("done")])
        events = []
        from talos import agent as agent_module
        config = self.config(model, image={"baseURL": model.url, "model": "painter"})
        runner = agent_module.Agent(config, self.store, emit=lambda event, data: events.append((event, data)))
        self.closers.append(runner.close)
        runner.run("draw")
        self.assertIn("Saved the image", self.results(events, "image_generate")[0])
        self.assertEqual((self.work / "art" / "dot.png").read_bytes(), b"hello")

        runner, _, events = self.agent([call("image_generate", prompt="a dot"), say("done")])
        runner.run("draw")
        self.assertIn("No image model is set", self.results(events, "image_generate")[0])

    def test_speech_command(self):
        spoken = tools.speech_argv("it's done")
        self.assertTrue(any("done" in part for part in spoken))
        saved = tools.speech_argv("hello", str(Path("out.wav")))
        self.assertTrue(any("out.wav" in part for part in saved))
        self.assertNotEqual(spoken, saved)

        runner, _, events = self.agent([call("text_to_speech", text="Talos is ready", filePath="ready.wav"), say("done")])
        runner.run("say it")
        answer = self.results(events, "text_to_speech")[0]
        if "Saved speech" in answer:            # a PC with a voice (Windows, macOS, Linux with espeak)
            self.assertGreater((self.work / "ready.wav").stat().st_size, 1000)
        else:                                   # no voice here: it must say so, not pretend
            self.assertIn("Speech is not available", answer)
