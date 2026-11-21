import json

from talos import plugins
from talos.fake import call, say
from tests.base import PYTHON, Case

PLUGIN = r"""
import json, pathlib

LOG = pathlib.Path(__file__).with_name("events.jsonl")

def on_event(event, **data):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "keys": sorted(data), "name": data.get("name")}) + "\n")

def double(number):
    return "twice is " + str(int(number) * 2)

TOOLS = [{"name": "double", "description": "Doubles a number.",
          "parameters": {"number": "integer|the number to double"}, "run": double}]
"""


class PluginsTest(Case):
    def test_custom_agent_and_command(self):
        agent_folder = self.work / ".opencode" / "agent"
        agent_folder.mkdir(parents=True)
        (agent_folder / "reviewer.md").write_text(
            "---\ndescription: Reviews code, changes nothing\nmode: primary\ntools:\n  write: false\n  edit: false\n---\n\n"
            "You are a strict reviewer. Say REVIEWED at the end.\n", encoding="utf-8")
        command_folder = self.work / ".opencode" / "command"
        command_folder.mkdir(parents=True)
        (command_folder / "check.md").write_text(
            "---\ndescription: Check one file\nagent: reviewer\n---\n\n"
            "Check $1 for $ARGUMENTS. Second word: $2. Notes:\n@notes.txt\nVersion: !`\"" + PYTHON.replace("\\", "\\\\") + "\" -c \"print(40 + 2)\"`\n",
            encoding="utf-8")
        (self.work / "notes.txt").write_text("the notes file\n", encoding="utf-8")

        loaded = self.config(agent={"quick": {"description": "from the config", "prompt": "Be quick."}},
                             command={"hello": {"template": "Say hello to $ARGUMENTS"}})
        agents = plugins.agents(self.work, loaded)
        self.assertEqual(agents["reviewer"]["description"], "Reviews code, changes nothing")
        self.assertIn("strict reviewer", agents["reviewer"]["prompt"])
        self.assertEqual(agents["quick"]["prompt"], "Be quick.")
        self.assertTrue({"build", "plan", "general", "explore"} <= set(agents))

        commands = plugins.commands(self.work, loaded)
        self.assertEqual(commands["check"]["agent"], "reviewer")
        text = plugins.expand_command(commands["check"], "main.py speed", self.work)
        self.assertIn("Check main.py for main.py speed. Second word: speed.", text)
        self.assertIn("the notes file", text)
        self.assertIn("Version: 42", text)
        self.assertEqual(plugins.expand_command(commands["hello"], "Budi", self.work), "Say hello to Budi")

        runner, model, events = self.agent([call("write", filePath="x.txt", content="x"), say("REVIEWED")], agent="reviewer")
        result = runner.run("review it")
        self.assertIn("strict reviewer", str(model.requests[0]["messages"][0]["content"]))
        self.assertFalse((self.work / "x.txt").exists())
        self.assertIn("not available", self.results(events)[0])
        self.assertEqual(result.text, "REVIEWED")

    def test_hooks_and_custom_tool(self):
        folder = self.work / ".talos" / "plugin"
        folder.mkdir(parents=True)
        (folder / "mine.py").write_text(PLUGIN, encoding="utf-8")
        (folder / "broken.py").write_text("raise RuntimeError('bad plugin')\n", encoding="utf-8")

        runner, model, events = self.agent([call("double", number=21), say("it is 42")])
        result = runner.run("double 21")
        self.assertEqual(self.results(events, "double"), ["twice is 42"])
        self.assertEqual(result.text, "it is 42")
        offered = [item["function"]["name"] for item in model.requests[0]["tools"]]
        self.assertIn("double", offered)

        seen = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        names = [item["event"] for item in seen]
        for event in ("session.start", "tool.before", "tool.after", "session.end"):
            self.assertIn(event, names)
        self.assertIn("double", [item["name"] for item in seen if item["event"] == "tool.before"])
