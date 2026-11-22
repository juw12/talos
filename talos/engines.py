"""Other agents as engines. If OpenCode, Hermes, Claude Code, Codex or Gemini CLI is installed,
Talos can hand a job to it and take the answer back into the same session, memory and history.
This is how features that Talos does not have natively stay reachable from one place.
"""
from __future__ import annotations

import re

from . import backends, util

# colours and cursor moves a terminal program prints; they are noise in a session and to a model
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# name -> (program, argv after the program; {prompt} and {model} are filled in)
ENGINES = {
    "opencode": ("opencode", ["run", "{prompt}"], ["--model", "{model}"]),
    "hermes": ("hermes", ["chat", "-q", "{prompt}"], ["--model", "{model}"]),
    "claude": ("claude", ["-p", "{prompt}"], ["--model", "{model}"]),
    "codex": ("codex", ["exec", "{prompt}"], ["--model", "{model}"]),
    "gemini": ("gemini", ["-p", "{prompt}"], ["--model", "{model}"]),
}


def _entry(config: dict, name: str):
    custom = (config.get("engines") or {}).get(name) or {}
    if isinstance(custom, (list, str)):          # short form: ["program", "arg", "{prompt}"]
        parts = [custom] if isinstance(custom, str) else [str(part) for part in custom]
        rest = parts[1:] if any("{prompt}" in part for part in parts[1:]) else parts[1:] + ["{prompt}"]
        custom = {"command": parts[0], "args": rest}
    program, arguments, model_arguments = ENGINES.get(name, (name, ["{prompt}"], []))
    return (
        custom.get("command") or program,
        custom.get("args") or arguments,
        custom.get("model_args") or model_arguments,
        custom.get("model") or "",
        int(custom.get("timeout") or 900),
    )


def find(program: str):
    """Full path of an engine's program, or None. See util.find_program."""
    return util.find_program(program)


def available(config: dict) -> dict:
    """name -> full path of the program, for every engine that is installed."""
    names = set(ENGINES) | set(config.get("engines") or {})
    found = {}
    for name in sorted(names):
        path = find(_entry(config, name)[0])
        if path:
            found[name] = path
    return found


def argv(config: dict, name: str, prompt: str) -> list:
    program, arguments, model_arguments, model, _ = _entry(config, name)
    path = find(program) or str(program)
    full = [path] + [part.replace("{prompt}", prompt) for part in arguments]
    if model:
        full += [part.replace("{model}", model) for part in model_arguments]
    return full


def run(config: dict, name: str, prompt: str, cwd: str, abort=None) -> str:
    if name not in available(config):
        installed = ", ".join(available(config)) or "none"
        return f"Engine '{name}' is not installed on this PC. Installed engines: {installed}."
    timeout = _entry(config, name)[4]
    code, output = backends.run_argv(argv(config, name, prompt), cwd, timeout, abort)
    text = util.clip(ANSI.sub("", output).strip(), 20000)
    if code != 0:
        return f"Engine '{name}' ended with exit code {code}.\n{text}"
    return text or f"Engine '{name}' gave no output."
