import json

from talos import tune
from talos.fake import call, say
from tests.base import PYTHON, Case


def good_model(request):
    """Does each of the three jobs the way a capable model would."""
    messages = request["messages"]
    task = next(str(message["content"]) for message in messages if message["role"] == "user")
    turns = sum(1 for message in messages if message["role"] == "assistant")
    if request["model"] == "weak":
        return say("I am not able to use tools.")
    if "READY" in task:
        return say("READY")
    if "answer.txt" in task:
        return call("write", filePath="answer.txt", content="391\n") if turns == 0 else say("Done, it is 391.")
    steps = [call("write", filePath="calc.py", content="def add(a, b):\n    return a + b\n"),
             call("bash", command=f'"{PYTHON}" test_calc.py')]
    return steps[turns] if turns < len(steps) else say("Fixed and confirmed.")


class TuneTest(Case):
    def test_scores_models(self):
        model = self.model([good_model] * 40)
        loaded = self.config(model, proof="off")
        lines = []
        outcome = tune.run(loaded, self.store, ["fake/weak", "fake/strong"], lines.append)
        strong, weak = outcome["results"]
        self.assertEqual((strong["model"], strong["passed"], strong["of"]), ("fake/strong", 3, 3))
        self.assertEqual((weak["model"], weak["passed"]), ("fake/weak", 0))
        self.assertEqual(outcome["recommended"], "fake/strong")
        self.assertEqual(len(lines), 6)
        self.assertEqual(self.store.sessions(), [])                           # test runs leave no sessions behind
        self.assertEqual(json.loads((self.home / "tune.json").read_text(encoding="utf-8"))["recommended"], "fake/strong")

        self.assertIn("fake/strong", tune.apply(outcome))
        self.assertEqual(json.loads((self.home / "talos.json").read_text(encoding="utf-8"))["model"], "fake/strong")
        self.assertIn("nothing was changed", tune.apply({"recommended": ""}))
