"""Tune: run each of your models through three small fixed jobs and say which one to use.

The jobs need real tool use (make a file, fix a failing test), so the score says whether a
model can work as an agent on this PC, not how well it chats.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from . import agent as agent_module, backends, config as config_module, util

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\nassert add(2, 3) == 5, add(2, 3)\nprint('test passed')\n"


def _reply(folder: Path, text: str) -> bool:
    return "READY" in text.upper()


def _file(folder: Path, text: str) -> bool:
    path = folder / "answer.txt"
    return path.is_file() and "391" in util.read_text(path)


def _fix(folder: Path, text: str) -> bool:
    code, _ = backends.run_argv([sys.executable, "test_calc.py"], str(folder), 60)
    return code == 0


JOBS = [
    ("answers", "Reply with exactly one word: READY", {}, _reply),
    ("makes a file", "Create the file answer.txt containing only the result of 17 * 23.", {}, _file),
    ("fixes a failing test", "python test_calc.py fails. Fix calc.py so it passes, then run it to confirm.",
     {"calc.py": BUGGY, "test_calc.py": TEST}, _fix),
]


def measure(config: dict, store, model: str, report=None) -> dict:
    passed, details, started = 0, [], time.time()
    for name, prompt, files, check in JOBS:
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for file_name, content in files.items():
                util.write_text(folder / file_name, content)
            job_config = dict(config, _cwd=str(folder), learn="off", budget={"steps": 12, "tokens": 0, "cost": 0})
            runner = agent_module.Agent(job_config, store, source="batch", auto_yes=False, model=model,
                                        deny_tools=("task", "cronjob", "send_message", "question"))
            try:
                result = runner.run(prompt)
                good = bool(check(folder, result.text))
            except Exception:
                good = False
            finally:
                runner.close()
                store.delete_session(runner.session)
        passed += good
        details.append({"job": name, "passed": good})
        if report:
            report(f"  {model}: {name}: {'pass' if good else 'fail'}")
    return {"model": model, "passed": passed, "of": len(JOBS), "seconds": round(time.time() - started, 1),
            "jobs": details}


def run(config: dict, store, models=None, report=None) -> dict:
    models = models or config_module.list_models(config)
    results = [measure(config, store, model, report) for model in models]
    results.sort(key=lambda item: (-item["passed"], item["seconds"]))
    best = results[0]["model"] if results and results[0]["passed"] else ""
    outcome = {"when": util.now_iso(), "results": results, "recommended": best}
    util.save_json_file(util.home() / "tune.json", outcome)
    return outcome


def apply(outcome: dict) -> str:
    if not outcome.get("recommended"):
        return "No model passed a job, so nothing was changed."
    path = util.home() / "talos.json"
    data = util.load_json_file(path, {}) or {}
    data["model"] = outcome["recommended"]
    util.save_json_file(path, data)
    return f"Set the default model to {outcome['recommended']} in {path}."
