"""Who may do what. Same shape as OpenCode's `permission` config, with safer defaults.

A rule is "allow", "ask" or "deny", either for a whole tool or per pattern:
    {"edit": "ask", "bash": {"*": "allow", "git push *": "ask", "rm *": "deny"}}
The last matching pattern wins. With no rule at all, everything is allowed except commands
that can destroy work and writes outside the project folder, which ask first.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

DANGEROUS = [
    (r"\brm\s+(-\w+\s+)*-\w*[rR]", "deletes folders"),
    (r"\b(del|erase)\s+.*/[sq]\b", "deletes files in bulk"),
    (r"\b(rmdir|rd)\s+/s", "deletes folders"),
    (r"\bRemove-Item\b.*-Recurse", "deletes folders"),
    (r"\bformat(\.com)?\s+[a-zA-Z]:", "formats a drive"),
    (r"\bmkfs\b|\bdd\s+if=|>\s*/dev/sd", "overwrites a disk"),
    (r"\bgit\s+push\b.*(--force|\s-f\b)", "force-pushes"),
    (r"\bgit\s+reset\s+--hard|\bgit\s+clean\s+-\w*f", "throws away local changes"),
    (r"\bshutdown\b|\breboot\b|\bStop-Computer\b|\bRestart-Computer\b", "turns the PC off"),
    (r"(curl|wget)[^|;&]*\|\s*(ba|z)?sh\b", "runs a script from the internet"),
    (r"\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^|]*\|\s*(iex|Invoke-Expression)", "runs a script from the internet"),
    (r"\breg(\.exe)?\s+delete\b", "deletes registry keys"),
    (r"\bchmod\s+-R\s+777\b|\bchown\s+-R\b", "changes permissions in bulk"),
    (r"\bsudo\b|\brunas\b", "runs as administrator"),
    (r"\bDROP\s+(TABLE|DATABASE)\b|\bTRUNCATE\s+TABLE\b", "deletes database data"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"\bkill\s+-9\s+-1\b|\btaskkill\b.*\s/im\s+\*", "kills every process"),
]

# tool name -> permission key (OpenCode groups all file writes under "edit")
KEYS = {"write": "edit", "edit": "edit", "patch": "edit", "list": "read", "glob": "read",
        "grep": "read"}


def danger(command: str) -> str:
    """Why a shell command is risky, or '' when it is not."""
    for pattern, reason in DANGEROUS:
        if re.search(pattern, command, re.I | re.S):
            return reason
    return ""


def _rule(rules, key: str, target: str):
    if not isinstance(rules, dict):
        return None
    rule = rules.get(key)
    if rule is None:
        rule = rules.get("*")
    if isinstance(rule, str):
        return rule
    if isinstance(rule, dict):
        found = None
        for pattern, action in rule.items():
            if fnmatch.fnmatch(target, pattern) or fnmatch.fnmatch(target.split(" ")[0], pattern):
                found = action
        return found
    return None


def decide(tool: str, target: str, config_rules=None, agent_rules=None, cwd=".", kind="other"):
    """Returns (action, reason). action is allow | ask | deny."""
    key = KEYS.get(tool, tool)
    for rules in (agent_rules, config_rules):       # the agent's own rules come first
        action = _rule(rules, key, target)
        if action in ("allow", "ask", "deny"):
            return action, f"rule for {key}"
    if tool == "bash":
        reason = danger(target)
        if reason:
            return "ask", reason
    if kind == "edit" and target:
        try:
            Path(target).resolve().relative_to(Path(cwd).resolve())
        except ValueError:
            outside = _rule(config_rules, "external_directory", target)
            return outside or "ask", "outside the project folder"
    return "allow", ""


class Gate:
    """Remembers "always" answers for one session and asks through `asker`."""

    def __init__(self, config: dict, agent_rules=None, asker=None, auto_yes=False):
        self.config = config
        self.agent_rules = agent_rules or {}
        self.asker = asker
        self.auto_yes = auto_yes
        self.always = set()

    def check(self, tool: str, target: str, kind="other") -> str:
        """'' when allowed, else the sentence to hand back to the model."""
        action, reason = decide(
            tool, target, self.config.get("permission"), self.agent_rules,
            self.config.get("_cwd", "."), kind,
        )
        if action == "allow" or (tool, target) in self.always or (tool, "*") in self.always:
            return ""
        if action == "deny":
            return f"Denied by the permission rules ({reason}). Do not retry; choose another way."
        if self.auto_yes:
            return ""
        if not self.asker:
            return (
                f"Not allowed without the user's approval ({reason}), and nobody is here to ask. "
                "Choose a safer way or say what needs approval."
            )
        answer = self.asker(tool, target, reason)
        if answer == "always":
            self.always.add((tool, target if tool == "bash" else "*"))
            return ""
        if answer == "once":
            return ""
        return "The user said no to this. Do not retry; ask what they want instead."
