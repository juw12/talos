"""The agent loop: ask the model, run the tools it calls, repeat until it answers in plain text.

Three things here are Talos's own:
- proof: a run that changed files does not end quietly without a check. The result says
  whether the change was checked, only re-read, or is unverified, with the commands as evidence.
- budget: steps, tokens and money each have a hard stop.
- learning: after a long run the agent may save a skill or a memory entry (both scanned first).
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
from pathlib import Path

from . import config as config_module
from . import llm as llm_module
from . import mcp, permissions, plugins, skills, tools, util

IDENTITY = """You are Talos, an agent that works on the user's computer through tools.

How to work:
- Do the task end to end. Look before you change: read the files, search the project.
- Change files with edit, write or patch. Run commands with bash. Keep changes small.
- After changing something, run a check that shows it works and report what it printed.
- Never say something works unless you ran it. Say plainly what you could not verify.
- Use todowrite for jobs with several steps. Use task to hand a self-contained job to a helper.
- Load a skill with the skill tool when its description fits the task.
- Answer briefly, in plain words. No filler."""

PERSONALITIES = {
    "concise": "Answer in as few words as the task allows.",
    "teacher": "Explain each step so a beginner can follow, and say why you chose it.",
    "careful": "Before any change, state the risk and how it can be undone.",
    "cheerful": "Be warm and encouraging, without wasting words.",
}

NUDGE = ("Before you finish: you changed {files} but nothing has been run since that shows the "
         "change works. Run a check now (a test, a build, or the code itself) and report what it "
         "printed. If no check is possible here, say exactly what is unverified.")

ACT = ("You showed code but did not call a tool, so nothing was created or run. Do it now with a "
       "tool block, for example:\n"
       '<tool>{{"name": "write", "arguments": {{"filePath": "{name}", "content": "..."}}}}</tool>\n'
       "Then run it with the bash tool. If the user only wanted to see the code, repeat your answer "
       "in one line.")
ASKS_FOR_ACTION = re.compile(r"\b(create|make|write|save|add|fix|change|edit|update|run|install|delete|"
                             r"remove|rename|build|generate)\b", re.I)

REPAIR = ("Your tool call could not be read: it was not valid JSON. Inside a string write a quote "
          'as \\" and a line break as \\n, and close every brace. Send it again exactly in this form:\n'
          '<tool>{{"name": "{name}", "arguments": {{...}}}}</tool>')
BROKEN_CALL = re.compile(r"(?:<tool|```)[^{]{0,20}\{\s*\"name\"\s*:\s*\"([\w.-]+)\"", re.S)

LEARN = ("The task above is finished. If it taught you a procedure worth repeating (several steps, "
         "likely to come up again), save it with skill_manage. If you learned a lasting fact about "
         "this environment or this user, save it with memory. Save nothing obvious or one-off. "
         "If there is nothing to save, answer exactly: nothing to save.")

LEAN_BELOW = 12000        # tokens; under this the model gets the short prompt
CORE_TOOLS = ("read", "write", "edit", "grep", "glob", "list", "bash", "webfetch", "websearch",
              "skill", "memory", "question", "notes", "projects")

TRIVIAL = re.compile(r"^\s*(cd|ls|dir|pwd|echo|cat|type|get-childitem|get-location|get-content|set-location|push-location|"
                     r"pop-location|write-host|write-output|mkdir|new-item|git\s+(status|log|diff|add|commit))\b", re.I)
PARTS = re.compile(r"""("[^"]*"|'[^']*')|(;|&&|\|\||\n)""")   # a quoted piece, or what ends one command of a line


def trivial(command: str) -> bool:
    """True when a command line proves nothing about the work: every command on it only looks
    around (cd, ls, git status). `cd project; python test.py` is a real check, though it starts with cd."""
    parts, start = [], 0
    for found in PARTS.finditer(command):
        if found.group(2):
            parts.append(command[start:found.start()])
            start = found.end()
    parts.append(command[start:])
    return all(TRIVIAL.match(part) for part in parts if part.strip())


class Result:
    def __init__(self, text, session, stopped="", proof=None, usage=None, steps=0):
        self.text, self.session, self.stopped = text, session, stopped
        self.proof = proof or {"level": "nothing changed", "files": [], "evidence": []}
        self.usage = usage or {"in": 0, "out": 0, "cost": 0.0}
        self.steps = steps

    def proof_line(self) -> str:
        proof = self.proof
        if proof["level"] == "nothing changed":
            return ""
        evidence = "; ".join(f"`{item['command'][:70]}` exit {item['exit']}" for item in proof["evidence"][-3:])
        return f"Proof: {proof['level']} ({len(proof['files'])} file(s) changed" + (
            f"; {evidence})" if evidence else ")")


def rule_files(cwd: Path, config: dict) -> list:
    """Instruction files, most general first: global, project (walking up), then config globs."""
    user = util.user_home()
    found = [path for path in (
        util.home() / "AGENTS.md", user / ".config" / "opencode" / "AGENTS.md") if path.is_file()]
    folder = cwd
    for _ in range(8):
        local = [folder / name for name in ("AGENTS.md", "CLAUDE.md") if (folder / name).is_file()]
        if local:
            found.append(local[0])
            break
        if (folder / ".git").exists() or folder.parent == folder:
            break
        folder = folder.parent
    found += [path for path in (cwd / ".cursorrules",) if path.is_file()]
    for pattern in config.get("instructions") or []:
        expanded = os.path.expanduser(str(pattern))
        base = Path(expanded)
        found += sorted(base.parent.glob(base.name)) if base.is_absolute() else sorted(cwd.glob(expanded))
    return found


class Agent:
    def __init__(self, config, store, session="", agent="build", source="cli", asker=None,
                 ask=None, emit=None, auto_yes=False, deny_tools=(), depth=0, abort=None,
                 parent="", hub=None, model=""):
        self.config, self.store, self.depth, self.source = config, store, depth, source
        self.cwd = Path(config.get("_cwd", ".")).resolve()
        self.emit = emit or (lambda event, data: None)
        self.abort = abort or threading.Event()
        self.llm = llm_module.LLM(config)
        self.llm.notice = lambda text: self.emit("warning", text)
        self.agents = plugins.agents(self.cwd, config)
        self.spec = self.agents.get(agent) or self.agents["build"]
        self.agent_name = self.spec.get("name", agent)
        self.model_name = (model or self.spec.get("model") or (
            store.session(session) or {}).get("model") or config_module.pick_default_model(config))
        started = not session
        self.session = session or store.new_session(
            self.cwd, self.agent_name, self.model_name, parent=parent, source=source)
        if started:
            plugins.fire("session.start", self.cwd, session=self.session, agent=self.agent_name)
        self.gate = permissions.Gate(config, self.spec.get("permission"), asker, auto_yes)
        self.ctx = tools.Context(config, store, self.session, self.gate, self, self.abort, self.emit, ask)
        self.hub = hub or mcp.Hub(config)
        self.own_hub = hub is None
        self.extra = dict(plugins.custom_tools(self.cwd))
        self.own_tools = set(self.extra)              # the user's own tools stay even in the short prompt
        if config.get("mcp"):
            self.extra.update(self.hub.tools())
            for name, problem in self.hub.problems.items():
                self.emit("warning", f"MCP server '{name}' is not available: {problem}")
            for skipped in self.hub.skipped:
                self.emit("warning", f"MCP tool left out: {skipped}")
        off = {name for name, enabled in (self.spec.get("tools") or {}).items() if enabled is False}
        off |= set(deny_tools)
        if depth > 0:
            off |= {"question", "cronjob", "send_message"}
        if depth > 1:
            off.add("task")
        self.tool_names = [name for name in list(tools.TOOLS) + list(self.extra) if name not in off]
        self.context, self.lean = 128000, False       # set from the model at the start of a run

    def close(self):
        if self.own_hub:
            self.hub.close()
        for item in self.ctx.processes.values():
            from . import backends
            backends.kill_tree(item["process"])

    # ------------------------------------------------------------ prompt
    def system_prompt(self) -> str:
        parts = [IDENTITY]
        user = util.user_home()
        room = [int(self.context * 1.2)]      # lean: characters left for the optional parts (~30% of the window)

        def fit(text: str, most: int) -> str:
            if not self.lean:
                return text
            text = util.clip(text, max(0, min(most, room[0])))
            room[0] -= len(text)
            return text

        for soul in (util.home() / "SOUL.md", user / ".hermes" / "SOUL.md"):
            if soul.is_file():
                parts.append("# Who you are\n" + fit(util.read_text(soul)[:6000], 1200))
                break
        personality = self.config.get("personality") or ""
        if personality:
            parts.append("# Style\n" + PERSONALITIES.get(personality, personality))
        if self.spec.get("prompt"):
            parts.append(f"# Your role: {self.agent_name}\n{self.spec['prompt']}")
        from . import backends
        shell = backends.shell_name((self.config.get("backend") or {}).get("shell", ""))
        parts.append(
            "# Environment\n"
            f"Working folder: {self.cwd}\n"
            f"System: {platform.system()} {platform.release()}; commands for the bash tool run in {shell}"
            f" ({self.ctx.backend.name})\n"
            f"Date: {time.strftime('%Y-%m-%d')}\n"
            f"Git repository: {'yes' if (self.cwd / '.git').exists() else 'no'}")
        for path in rule_files(self.cwd, self.config):
            text = util.read_text(path)[:20000]
            reason = skills.blocked(text)
            if reason:
                self.emit("warning", f"{path} was not loaded: it {reason}")
                continue
            text = fit(text, 2400)
            if text:
                parts.append(f"# Instructions from {path}\n{text}")
        memory = fit(skills.memory_prompt(), 1500)
        if memory:
            parts.append("# Memory (saved in earlier sessions)\n" + memory)
        if "notes" in self.tool_names or "projects" in self.tool_names:
            from . import knowledge
            known = fit(knowledge.summary(self.config), 500)
            if known:
                parts.append("# What the user already has\n" + known)
        if "skill" in self.tool_names:
            if self.lean:       # names only; the skill tool loads the full text when one is needed
                names = [name for name, skill in skills.discover(self.cwd).items() if not skill["blocked"]]
                index = fit(", ".join(names), 900)
            else:
                index = skills.index_text(self.cwd)
            if index:
                parts.append("# Skills (load one with the skill tool before a matching task)\n" + index)
        return "\n\n".join(parts)

    def schemas(self) -> list:
        registry = {**tools.TOOLS, **self.extra}
        names = [name for name in self.tool_names if not self.lean or name in CORE_TOOLS or name in self.own_tools]
        return [registry[name].schema for name in names if name in registry]

    def model(self) -> dict:
        return config_module.resolve_model(self.config, self.model_name)

    # ------------------------------------------------------------ helpers
    def spawn(self, prompt: str, kind: str = "general") -> str:
        if self.depth >= 2:
            return "A helper of a helper cannot start more helpers. Do this job yourself."
        name = kind if self.agents.get(kind, {}).get("mode") != "primary" and kind in self.agents else "general"
        child = Agent(
            self.config, self.store, agent=name, source="task", asker=self.gate.asker,
            auto_yes=self.gate.auto_yes, depth=self.depth + 1, abort=self.abort, parent=self.session,
            hub=self.hub, model=self.agents[name].get("model") or self.model_name,
            emit=lambda event, data: self.emit("helper." + event, data))
        try:
            result = child.run(prompt)
        finally:
            child.close()
        return result.text + ("\n" + result.proof_line() if result.proof_line() else "")

    def compact(self, messages, force=False):
        """When the conversation is near the model's limit, replace the old part by a summary."""
        model = self.model()
        limit = config_module.context_of(self.config, model)
        if len(messages) < 8 or (not force and llm_module.estimate_tokens(messages) < limit * 0.8):
            return messages
        cut = len(messages) - 6
        while cut > 1 and messages[cut]["role"] == "tool":
            cut -= 1
        head = messages[1:cut]
        if not head:
            return messages
        lines = []
        for message in head:
            content = message.get("content")
            text = content if isinstance(content, str) else "(attachment)"
            calls = " ".join(f"[{call['name']} {json.dumps(call['arguments'])[:200]}]"
                             for call in message.get("tool_calls") or [])
            lines.append(f"{message['role']}: {util.clip(text, 1500)} {calls}".strip())
        small = self.config.get("small_model") or self.model_name
        reply = self.llm.chat(config_module.resolve_model(self.config, small), [
            {"role": "system", "content": "You write handover notes for an agent that continues this work."},
            {"role": "user", "content": "Summarise this conversation so the work can continue: the goal, "
                "decisions made, files touched, what is done, what is left, and anything the user asked "
                "to remember.\n\n" + util.clip("\n".join(lines), limit * 2)},
        ])
        shorter = [messages[0], {"role": "user", "internal": True,
                                 "content": "[Summary of the conversation so far]\n" + reply["content"]}]
        shorter += messages[cut:]
        self.store.replace_messages(self.session, shorter)
        self.emit("warning", f"The conversation was summarised to fit the model ({len(messages)} -> {len(shorter)} messages).")
        return shorter

    def proof(self) -> dict:
        files = sorted(set(self.ctx.edits))
        if not files:
            return {"level": "nothing changed", "files": [], "evidence": []}
        real = [check for check in self.ctx.checks if not trivial(check["command"])]
        if real and real[-1]["exit"] == 0:
            level = "checked"
        elif real:
            level = "check failed"
        elif self.ctx.reread:
            level = "re-read only"
        else:
            level = "unverified"
        return {"level": level, "files": files, "evidence": real}

    def _learn(self, messages, used_tools: int) -> None:
        if (self.config.get("learn") != "auto" or self.depth or used_tools < 8
                or self.source in ("cron", "task", "batch")):
            return
        registry = {name: tools.TOOLS[name] for name in ("memory", "skill_manage")}
        try:
            reply = self.llm.chat(self.model(), messages + [{"role": "user", "content": LEARN}],
                                  [tool.schema for tool in registry.values()], abort=self.abort)
        except llm_module.ModelError:
            return
        for call in reply["tool_calls"][:3]:
            if call["name"] in registry:
                outcome = tools.run_tool(self.ctx, call["name"], call["arguments"])
                self.emit("learned", f"{call['name']}: {outcome}")

    # ------------------------------------------------------------ the loop
    def run(self, text: str, attachments=None) -> Result:
        store, ctx = self.store, self.ctx
        ctx.edits, ctx.checks, ctx.reread = [], [], False
        model = self.model()
        self.context = config_module.context_of(self.config, model)
        self.lean = self.context < LEAN_BELOW
        own_model, spare = self.model_name, str(self.config.get("fallback_model") or "")
        if spare == own_model:
            spare = ""
        if not store.messages(self.session):
            store.add_message(self.session, {"role": "system", "content": self.system_prompt()})
            if self.lean:
                self.emit("warning", f"This model's window is small ({self.context} tokens), so Talos uses its "
                          f"short prompt: {len(self.schemas())} core tools, rules and skills shortened. "
                          "Every tool still works if the model calls it.")
        info = store.session(self.session) or {}
        if not info.get("title"):
            store.update_session(self.session, title=" ".join(text.split())[:60], model=self.model_name)
        content = [{"type": "text", "text": text}] + list(attachments) if attachments else text
        store.add_message(self.session, {"role": "user", "content": content})
        plugins.fire("message", self.cwd, session=self.session, text=text)
        budget = self.config.get("budget") or {}
        usage = {"in": 0, "out": 0, "cost": 0.0}
        steps = nudges = used_tools = pushes = repairs = 0
        recent, final, stopped = [], "", ""
        while True:
            if self.abort.is_set():
                stopped = "stopped by the user"
                break
            steps += 1
            if budget.get("steps") and steps > int(budget["steps"]):
                stopped = f"budget: {budget['steps']} steps used"
                break
            if budget.get("tokens") and usage["in"] + usage["out"] > int(budget["tokens"]):
                stopped = f"budget: {budget['tokens']} tokens used"
                break
            if budget.get("cost") and usage["cost"] > float(budget["cost"]):
                stopped = f"budget: {budget['cost']} spent"
                break
            messages = self.compact(store.messages(self.session))
            try:
                reply = self.llm.chat(model, messages, self.schemas(), abort=self.abort,
                                      on_text=lambda piece: self.emit("text", piece), spare=bool(spare))
            except llm_module.ModelError as error:
                other = None
                if spare and not self.abort.is_set():
                    try:
                        other = config_module.resolve_model(self.config, spare)
                    except ValueError:
                        other = None
                if other is None:
                    stopped, final = "model error", str(error)
                    break
                # the model does not answer: the spare one (fallback_model) does the rest of this run
                self.emit("warning", f"{error} Talos carries on with {spare}.")
                self.model_name, model, spare = spare, other, ""
                self.context = config_module.context_of(self.config, model)
                self.lean = self.context < LEAN_BELOW
                steps -= 1
                continue
            spent = llm_module.cost_of(model, reply["usage"])
            usage["in"] += reply["usage"]["in"]
            usage["out"] += reply["usage"]["out"]
            usage["cost"] += spent
            store.add_usage(self.session, reply["usage"]["in"], reply["usage"]["out"], spent)
            message = {"role": "assistant", "content": reply["content"]}
            if reply["tool_calls"]:
                message["tool_calls"] = reply["tool_calls"]
            store.add_message(self.session, message)
            self.emit("assistant", reply["content"])
            if not reply["tool_calls"]:
                level = self.proof()["level"]
                mode = self.config.get("proof", "nudge")
                wants = (mode == "nudge" and level == "unverified" and nudges < 1) or (
                    mode == "require" and level in ("unverified", "re-read only", "check failed")
                    and nudges < 2)
                if wants and not self.abort.is_set():
                    nudges += 1
                    names = ", ".join(Path(path).name for path in self.proof()["files"][:5])
                    store.add_message(self.session, {
                        "role": "user", "internal": True, "content": NUDGE.format(files=names)})
                    self.emit("warning", "Asked the agent to check its change before finishing.")
                    continue
                # a tool call that was written but cannot be read: say why, up to twice
                broken = BROKEN_CALL.search(reply["content"] or "")
                if broken and broken.group(1) in self.tool_names and repairs < 2 and not self.abort.is_set():
                    repairs += 1
                    store.add_message(self.session, {"role": "user", "internal": True,
                                                     "content": REPAIR.format(name=broken.group(1))})
                    self.emit("warning", "The model's tool call was not valid; asked it to send it again.")
                    continue
                # small models often show the code instead of doing the work: ask once for the tool call
                if (self.lean and not used_tools and not pushes and "```" in (reply["content"] or "")
                        and "write" in self.tool_names and ASKS_FOR_ACTION.search(text)
                        and not self.abort.is_set()):
                    pushes += 1
                    named = re.search(r"[\w./\\-]+\.\w{1,5}\b", text)
                    store.add_message(self.session, {"role": "user", "internal": True, "content": ACT.format(
                        name=named.group(0) if named else "file.txt")})
                    self.emit("warning", "The model showed code without doing the work; asked it to use its tools.")
                    continue
                final = reply["content"]
                break
            for call in reply["tool_calls"]:
                self.emit("tool", {"name": call["name"], "arguments": call["arguments"]})
                key = call["name"] + json.dumps(call["arguments"], sort_keys=True)
                if call["name"] not in self.tool_names:
                    outcome = f"The tool '{call['name']}' is not available to the {self.agent_name} agent."
                elif recent[-2:] == [key, key]:
                    outcome = ("You made this exact call twice already and got the same result. "
                               "Stop repeating it and try a different approach.")
                else:
                    outcome = tools.run_tool(ctx, call["name"], call["arguments"], self.extra)
                if self.lean:       # one long result must not push the task out of a small window
                    outcome = util.clip(outcome, max(800, int(self.context * 0.8)))
                recent.append(key)
                used_tools += 1
                store.add_message(self.session, {
                    "role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": outcome})
                self.emit("tool_result", {"name": call["name"], "result": outcome})
                if self.abort.is_set():
                    break
            if ctx.attachments:
                store.add_message(self.session, {"role": "user", "internal": True, "content": [
                    {"type": "text", "text": "The image(s) you asked to read:"}] + ctx.attachments})
                ctx.attachments = []
        proof = self.proof()
        if not stopped:
            self._learn(store.messages(self.session), used_tools)
        if stopped and not final:
            final = f"Stopped: {stopped}."
        self.model_name = own_model           # the next run tries the chosen model first again
        result = Result(final, self.session, stopped, proof, usage, steps)
        self.emit("done", {"text": final, "stopped": stopped, "proof": result.proof_line()})
        plugins.fire("session.end", self.cwd, session=self.session, text=final, proof=proof)
        return result
