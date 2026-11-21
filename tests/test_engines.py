from talos import engines
from talos.fake import call, say
from tests.base import PYTHON, Case

ECHO = "import sys; print('engine heard: ' + sys.argv[1]); print('model: ' + ' '.join(sys.argv[2:]))"


class EnginesTest(Case):
    def test_runs_an_engine(self):
        settings = {"engines": {"echo": {"command": PYTHON, "args": ["-c", ECHO, "{prompt}"],
                                         "model_args": ["--model", "{model}"], "model": "tiny"},
                                "broken": {"command": PYTHON, "args": ["-c", "raise SystemExit(2)", "{prompt}"]}}}
        loaded = self.config(**settings)
        self.assertIn("echo", engines.available(loaded))
        self.assertEqual(engines.argv(loaded, "echo", "hi")[-3:], ["hi", "--model", "tiny"])
        self.assertEqual(engines.argv(loaded, "opencode", "fix it")[1:], ["run", "fix it"])
        self.assertEqual(engines.argv(loaded, "claude", "fix it")[1:], ["-p", "fix it"])
        answer = engines.run(loaded, "echo", "count the files", str(self.work))
        self.assertIn("engine heard: count the files", answer)
        self.assertIn("model: --model tiny", answer)
        self.assertIn("exit code 2", engines.run(loaded, "broken", "x", str(self.work)))
        self.assertIn("not installed", engines.run(loaded, "no-such-engine", "x", str(self.work)))

        runner, _, events = self.agent([call("task", prompt="count the files", engine="echo"), say("the engine answered")],
                                       **settings)
        result = runner.run("ask the other agent")
        self.assertIn("engine heard: count the files", self.results(events, "task")[0])
        self.assertEqual(result.text, "the engine answered")
