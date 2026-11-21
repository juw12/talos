"""Plugins, hooks, custom tools, custom commands and custom agents: plain files in a folder.

Plugins are Python files in .talos/plugin/ (project) or ~/.talos/plugin/. A plugin may define
    def on_event(event, **data): ...      # events: tool.before, tool.after, session.start,
                                          # session.end, message, gateway.message
    TOOLS = [{"name", "description", "parameters": {"x": "string|what it is"}, "run": callable}]
Commands and agents are Markdown files with front matter, in the same places OpenCode reads
them (.opencode/command, .opencode/agent, ~/.config/opencode/...) plus .talos/ equivalents.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from . import backends, util

_loaded = {}


def _folders(cwd, kinds) -> list:
    cwd, user = Path(cwd), util.user_home()
    roots = [user / ".config" / "opencode", util.home(), cwd / ".opencode", cwd / ".talos"]
    return [root / kind for root in roots for kind in kinds]


def load(cwd) -> list:
    """Import every plugin once per project folder."""
    key = str(Path(cwd).resolve())
    if key in _loaded:
        return _loaded[key]
    modules = []
    for folder in (util.home() / "plugin", Path(cwd) / ".talos" / "plugin"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.py")):
            spec = importlib.util.spec_from_file_location(f"talos_plugin_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                modules.append(module)
            except Exception as error:       # a broken plugin must not stop the agent
                print(f"[talos] plugin {path.name} was skipped: {error}")
    _loaded[key] = modules
    return modules


def fire(event: str, cwd, **data):
    """Tell every plugin. The last non-None answer is returned (tool.before may change arguments)."""
    answer = None
    for module in load(cwd):
        handler = getattr(module, "on_event", None)
        if not handler:
            continue
        try:
            result = handler(event, **data)
        except Exception as error:
            print(f"[talos] plugin {module.__name__} failed on {event}: {error}")
            continue
        if result is not None:
            answer = result
    return answer


def custom_tools(cwd) -> dict:
    from .tools import Tool
    found = {}
    for module in load(cwd):
        for spec in getattr(module, "TOOLS", []) or []:
            run = spec["run"]
            found[spec["name"]] = Tool(
                spec["name"], spec.get("kind", "other"), spec.get("description", ""),
                spec.get("parameters") or {}, lambda ctx, _run=run, **arguments: _run(**arguments))
    return found


def _markdown_items(cwd, kinds) -> dict:
    items = {}
    for folder in _folders(cwd, kinds):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.md")):
            meta, body = util.front_matter(util.read_text(path))
            items[path.stem] = {**meta, "name": path.stem, "body": body.strip(), "path": str(path)}
    return items


def commands(cwd, config: dict) -> dict:
    """Slash commands: Markdown files, plus the `command` section of the config."""
    items = _markdown_items(cwd, ("command", "commands"))
    for name, entry in (config.get("command") or {}).items():
        if isinstance(entry, dict):
            items[name] = {**entry, "name": name, "body": entry.get("template", "")}
    return items


def expand_command(command: dict, arguments: str, cwd) -> str:
    """Fill $ARGUMENTS, $1..$9, !`shell` and @file into a command's text."""
    text = command["body"]
    parts = arguments.split()
    text = text.replace("$ARGUMENTS", arguments)
    for index in range(9, 0, -1):
        text = text.replace(f"${index}", parts[index - 1] if index <= len(parts) else "")

    def shell(match):
        _, output = backends.run_argv(backends.shell_argv(match.group(1)), str(cwd), 30)
        return output.strip()

    def include(match):
        path = Path(cwd) / match.group(1)
        return util.read_text(path) if path.is_file() else match.group(0)

    text = re.sub(r"!`([^`]+)`", shell, text)
    return re.sub(r"(?<![\w@])@([\w./\\-]+\.\w+)", include, text)


BUILT_IN_AGENTS = {
    "build": {"mode": "primary", "description": "Does the work: reads, edits, runs.",
              "prompt": ""},
    "plan": {"mode": "primary", "description": "Reads and plans; changes nothing.",
             "tools": {"write": False, "edit": False, "patch": False},
             "permission": {"bash": "ask"},
             "prompt": "You are in plan mode. Read and think, then give a clear plan. Do not change "
                       "files and do not run commands that change anything."},
    "general": {"mode": "subagent", "description": "Helper for a self-contained job.",
                "tools": {"task": False}, "prompt": ""},
    "explore": {"mode": "subagent", "description": "Fast read-only search of a codebase.",
                "tools": {"write": False, "edit": False, "patch": False, "task": False,
                          "execute_code": False},
                "permission": {"bash": "deny"},
                "prompt": "You explore code. Use glob, grep, read and list. Report what you found "
                          "with file paths and line numbers. Never change anything."},
}


def agents(cwd, config: dict) -> dict:
    """Built-in agents, then Markdown agent files, then the `agent` section of the config."""
    found = {name: dict(entry, name=name) for name, entry in BUILT_IN_AGENTS.items()}
    for name, entry in _markdown_items(cwd, ("agent", "agents")).items():
        found[name] = {**found.get(name, {}), **entry, "prompt": entry["body"]}
    for name, entry in (config.get("agent") or {}).items():
        if isinstance(entry, dict):
            found[name] = {**found.get(name, {"name": name}), **entry}
    return {name: entry for name, entry in found.items() if not entry.get("disable")}
