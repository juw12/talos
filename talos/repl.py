"""The terminal chat: type a task, watch the tools run, answer permission questions.

Slash commands cover what OpenCode's and Hermes' terminals offer; `/help` lists them.
Lines starting with ! run a shell command; @path adds a file to the message.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import agent as agent_module, backends, config as config_module, cron, engines, plugins, skills, util

HELP = """Commands
  /new                 start a new session          /sessions        list sessions
  /resume <id|last>    continue a session           /model [name]    show or set the model
  /agent [name]        show or switch agent         /models          list models
  /undo  /redo         take back or restore a turn  /rollback        same as /undo
  /compact             summarise a long session     /stats           tokens and cost
  /share [file]        save the session as a page   /export [file]   save the session as JSON
  /skills              list skills                  /memory          show saved memory
  /cron                list scheduled jobs          /tools           list tools
  /personality [name]  concise, teacher, careful, cheerful, or your own sentence
  /engine <name> <task> hand a task to OpenCode, Hermes, Claude, Codex or Gemini
  /init                write AGENTS.md for this project
  /search <words>      search past sessions         /exit            leave
  !command             run a shell command          @file            add a file to the message
  /<name> [args]       your own commands (Markdown files in .talos/command or .opencode/command)"""

INIT = ("Look through this project (structure, build and test commands, conventions) and write an "
        "AGENTS.md at its root that tells a coding agent how to work here. Keep it under 60 lines.")


def mention_files(text: str, cwd) -> str:
    """Append the content of every @file mentioned in the message."""
    added = []
    for name in re.findall(r"(?<![\w@])@([\w./\\:~-]+)", text):
        path = Path(name).expanduser()
        path = path if path.is_absolute() else Path(cwd) / path
        if path.is_file() and path.stat().st_size < 200_000:
            added.append(f"\n\n--- {name} ---\n{util.read_text(path)}")
    return text + "".join(added)


def prepare(text: str, cwd, config: dict, agent: str = "", model: str = ""):
    """Expand a custom /command and @files. Returns (text, agent, model)."""
    if text.startswith("/"):
        name, _, arguments = text[1:].partition(" ")
        command = plugins.commands(cwd, config).get(name)
        if command:
            text = plugins.expand_command(command, arguments.strip(), cwd)
            agent = str(command.get("agent") or agent)
            model = str(command.get("model") or model)
        elif name == "init":
            text = INIT
    return mention_files(text, cwd), agent, model


class Printer:
    """Turns agent events into terminal lines."""

    def __init__(self, out=None, quiet=False):
        self.out, self.quiet, self.open_line = out or sys.stdout, quiet, False

    def write(self, text):
        self.out.write(text)
        self.out.flush()

    def line(self, text):
        if self.open_line:
            self.write("\n")
            self.open_line = False
        self.write(text + "\n")

    def __call__(self, event, data):
        if event == "text":
            self.write(data)
            self.open_line = not data.endswith("\n")
        elif self.quiet:
            return
        elif event == "tool":
            arguments = data["arguments"]
            target = next((str(arguments[key]) for key in ("command", "filePath", "pattern", "url", "query",
                                                            "name", "prompt", "description") if arguments.get(key)), "")
            self.line(f"  > {data['name']} {util.clip(' '.join(target.split()), 110)}")
        elif event == "tool_result":
            first = (data["result"].strip().splitlines() or [""])[0]
            self.line(f"    {util.clip(first, 110)}")
        elif event == "warning":
            self.line(f"  ! {data}")
        elif event == "learned":
            self.line(f"  + saved: {util.clip(str(data), 110)}")
        elif event == "helper.tool":
            self.line(f"      helper > {data['name']}")
        elif event == "done":
            if self.open_line:
                self.write("\n")
                self.open_line = False


def console_asker(tool, target, reason):
    try:
        answer = input(f"\n  Allow {tool}: {util.clip(target, 300)}\n  ({reason})  [y]es / [a]lways / [n]o: ").strip().lower()
    except EOFError:
        return "reject"
    return {"y": "once", "yes": "once", "a": "always", "always": "always"}.get(answer, "reject")


def console_ask(question, options):
    listed = "".join(f"\n    {index + 1}. {option}" for index, option in enumerate(options))
    try:
        answer = input(f"\n  {question}{listed}\n  > ").strip()
    except EOFError:
        return "(no answer)"
    if answer.isdigit() and 0 < int(answer) <= len(options):
        return str(options[int(answer) - 1])
    return answer


class Repl:
    def __init__(self, config, store, session="", agent="build", model="", auto_yes=False, overrides=None):
        self.config, self.store, self.overrides = config, store, overrides or {}
        self.cwd = Path(config["_cwd"])
        self.session, self.agent, self.model, self.auto_yes = session, agent, model, auto_yes
        self.printer = Printer()

    def say(self, text):
        self.printer.line(text)

    def ask(self, text: str):
        text, agent, model = prepare(text, self.cwd, self.config, self.agent, self.model)
        runner = agent_module.Agent(
            self.config, self.store, session=self.session, agent=agent, model=model,
            asker=console_asker, ask=console_ask, emit=self.printer, auto_yes=self.auto_yes)
        self.session = runner.session
        try:
            result = runner.run(text)
        except KeyboardInterrupt:
            runner.abort.set()
            self.say("  (stopped)")
            return None
        finally:
            runner.close()
        if result.stopped:
            self.say(result.text if result.stopped == "model error" else f"  Stopped: {result.stopped}")
        if result.proof_line():
            self.say("  " + result.proof_line())
        return result

    def command(self, line: str) -> bool:
        """Run one slash command. Returns False to leave."""
        name, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        store = self.store
        if name in ("exit", "quit", "q"):
            return False
        if name == "help":
            self.say(HELP)
        elif name in ("new", "clear", "reset"):
            self.session = ""
            self.say("New session.")
        elif name == "sessions":
            for item in store.sessions(20):
                self.say(f"  {item['id']}  {item['title'] or '(no title)'}  [{item['model']}]")
        elif name in ("resume", "continue"):
            found = store.find_session(rest or "last")
            self.session = found or self.session
            self.say(f"Session {found}." if found else "No such session.")
        elif name == "model":
            if rest:
                self.model = rest
                self.config["model"] = rest
            self.say(f"Model: {self.model or config_module.pick_default_model(self.config) or 'none found'}")
        elif name == "models":
            self.say("\n".join("  " + item for item in config_module.list_models(self.config)) or "  none found")
        elif name in ("agent", "agents"):
            available = plugins.agents(self.cwd, self.config)
            if rest in available:
                self.agent = rest
            self.say(f"Agent: {self.agent}. Available: {', '.join(available)}")
        elif name in ("undo", "rollback"):
            undone = store.undo(self.session) if self.session else None
            self.say(f"Took back \"{util.clip(undone['text'], 60)}\" and restored {len(undone['files'])} file(s)."
                     if undone else "Nothing to undo.")
        elif name == "redo":
            redone = store.redo(self.session) if self.session else None
            self.say(f"Restored the turn ({len(redone['files'])} file(s))." if redone else "Nothing to redo.")
        elif name in ("compact", "compress"):
            if self.session:
                runner = agent_module.Agent(self.config, store, session=self.session, emit=self.printer)
                runner.compact(store.messages(self.session), force=True)
                runner.close()
        elif name in ("stats", "usage", "cost"):
            info = store.session(self.session) or {}
            self.say(f"This session: {info.get('tokens_in', 0)} in, {info.get('tokens_out', 0)} out, "
                     f"cost {info.get('cost', 0):.4f}")
        elif name in ("share", "export"):
            if not self.session:
                self.say("Nothing to save yet.")
            else:
                suffix = "html" if name == "share" else "json"
                path = Path(rest or f"{self.session}.{suffix}")
                path = path if path.is_absolute() else self.cwd / path
                import json
                util.write_text(path, store.share_html(self.session) if name == "share"
                                else json.dumps(store.export(self.session), indent=2, ensure_ascii=False))
                self.say(f"Saved {path}")
        elif name == "skills":
            for skill in skills.discover(self.cwd).values():
                flag = f"  [blocked: {skill['blocked']}]" if skill["blocked"] else ""
                self.say(f"  {skill['name']} ({skill['origin']}){flag} - {util.clip(skill['description'], 80)}")
        elif name == "memory":
            self.say(skills.memory_prompt() or "Nothing saved yet.")
        elif name == "cron":
            self.say(cron.describe())
        elif name == "tools":
            from . import tools
            self.say(", ".join(sorted(tools.TOOLS)))
        elif name == "personality":
            self.config["personality"] = rest
            self.session = ""
            self.say(f"Personality: {rest or 'default'} (new session).")
        elif name == "engine":
            engine, _, prompt = rest.partition(" ")
            self.say(engines.run(self.config, engine, prompt, str(self.cwd)) if prompt
                     else f"Installed engines: {', '.join(engines.available(self.config)) or 'none'}")
        elif name == "search":
            for hit in store.search(rest, 8):
                self.say(f"  {hit['when']} {hit['session']} {hit['title']}: {hit['hit']}")
        elif name == "init" or name in plugins.commands(self.cwd, self.config):
            self.ask(line)
        else:
            self.say(f"Unknown command /{name}. /help lists the commands.")
        return True

    def loop(self):
        model = self.model or config_module.pick_default_model(self.config)
        self.say(f"Talos - {self.cwd}\nModel: {model or 'none found (run: talos doctor)'}   /help for commands, /exit to leave")
        while True:
            try:
                line = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.say("")
                return
            if not line:
                continue
            if line.startswith("!"):
                _, output = backends.run_argv(backends.shell_argv(line[1:]), str(self.cwd), 300)
                self.say(output.rstrip())
            elif line.startswith("/"):
                if not self.command(line):
                    return
            else:
                self.ask(line)
