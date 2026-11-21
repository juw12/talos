from talos import lsp
from talos.fake import call, say
from tests.base import PYTHON, Case

SERVER = r"""
import json, sys

def send(message):
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()

while True:
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line in (b"\r\n", b"\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    message = json.loads(sys.stdin.buffer.read(length))
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "textDocument/didOpen":
        document = message["params"]["textDocument"]
        problems = []
        for number, text in enumerate(document["text"].splitlines()):
            if "BAD" in text:
                problems.append({"range": {"start": {"line": number, "character": 0}, "end": {"line": number, "character": 3}},
                                 "severity": 1, "message": "BAD is not allowed"})
            if "hmm" in text:
                problems.append({"range": {"start": {"line": number, "character": 0}, "end": {"line": number, "character": 3}},
                                 "severity": 4, "message": "only a hint"})
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": document["uri"], "diagnostics": problems}})
    elif method == "textDocument/hover":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"contents": "hover text for line %d" % message["params"]["position"]["line"]}})
    elif method == "exit":
        sys.exit(0)
"""

FORMATTER = "import sys\npath = sys.argv[1]\ntext = open(path, encoding='utf-8').read()\nopen(path, 'w', encoding='utf-8').write(text.upper())\n"


class LspTest(Case):
    def test_server_diagnostics(self):
        script = self.tmp / "fake_lsp.py"
        script.write_text(SERVER, encoding="utf-8")
        settings = {"lsp": {"foo": {"command": [PYTHON, str(script)], "extensions": [".foo"]}}}
        runner, _, events = self.agent([
            call("write", filePath="a.foo", content="fine\nBAD line\nhmm\n"),
            call("write", filePath="b.foo", content="all fine\n"),
            call("lsp", operation="hover", filePath="a.foo", line=2, character=1),
            call("lsp", operation="diagnostics", filePath="b.foo"),
            call("write", filePath="broken.py", content="def f(:\n    pass\n"),
            call("write", filePath="broken.json", content="{\"a\": }\n"),
            say("done"),
        ], proof="off", **settings)
        runner.run("write the files")
        first, second, python, data = self.results(events, "write")
        self.assertIn("Problems in a.foo", first)
        self.assertIn("error line 2: BAD is not allowed", first)
        self.assertNotIn("only a hint", first)
        self.assertNotIn("Problems", second)
        hover, clean = self.results(events, "lsp")
        self.assertIn("hover text for line 1", hover)
        self.assertIn("No problems found", clean)
        self.assertIn("Problems in broken.py", python)        # the built-in check, no server needed
        self.assertIn("error line 1", python)
        self.assertIn("Problems in broken.json", data)

    def test_formatter_runs(self):
        script = self.tmp / "upper.py"
        script.write_text(FORMATTER, encoding="utf-8")
        settings = {"formatter": {"upper": {"command": [PYTHON, str(script), "$FILE"], "extensions": [".shout"]}}}
        runner, _, _ = self.agent([
            call("write", filePath="a.shout", content="hello\n"),
            call("write", filePath="a.quiet", content="hello\n"),
            say("done"),
        ], proof="off", **settings)
        runner.run("write the files")
        self.assertEqual((self.work / "a.shout").read_text(encoding="utf-8"), "HELLO\n")
        self.assertEqual((self.work / "a.quiet").read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(lsp.format_file({"formatter": False}, self.work / "a.shout"), "")
