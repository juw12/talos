"""Skills (SKILL.md folders) and memory (MEMORY.md, USER.md), both checked before use.

Skills are read in place from Talos's folder and from the folders OpenCode, Hermes, Claude
Code, Codex and Vector already use (plus "skill_paths" in ~/.talos/talos.json), so nothing has
to be copied; "skill_hide" there leaves out skills by name pattern. Every skill and every memory entry
is scanned for hidden instructions first; a skill that fails is listed but not loaded
until the user trusts it.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path

from . import util

# (pattern, severity, what it is)
GUARD = [
    (r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]", "high", "invisible characters"),
    (r"[\U000e0000-\U000e007f]", "high", "hidden tag characters"),
    (r"(?i)ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts)", "high", "tells the agent to ignore its rules"),
    (r"(?i)(do\s+not|don't|never)\s+(tell|inform|mention|reveal|show)\s+(this\s+)?(to\s+)?the\s+user", "high", "tells the agent to hide something from the user"),
    (r"(?i)(curl|wget)[^\n|]*\|\s*(ba|z)?sh\b", "high", "runs a script from the internet"),
    (r"(?i)(iwr|irm|invoke-webrequest)[^\n|]*\|\s*(iex|invoke-expression)", "high", "runs a script from the internet"),
    (r"(?i)base64\s+(-d|--decode)[^\n]*\|\s*(ba)?sh", "high", "runs hidden (encoded) commands"),
    (r"(?i)(curl|wget|invoke-webrequest|requests\.post|fetch\()[^\n]*(\.ssh|id_rsa|\.env\b|credentials|api[_-]?key|token|printenv|\$env:)", "high", "sends secrets to the network"),
    (r"(?i)(cat|type|get-content)\s+[^\n]*(\.ssh[/\\]|id_rsa|\.aws[/\\]credentials)", "medium", "reads private keys"),
    (r"(?i)rm\s+-rf\s+(~|/|\$HOME)(\s|$)", "high", "deletes the home folder"),
    (r"(?i)<!--[^>]*(instruction|system prompt|you must|ignore)[^>]*-->", "medium", "instructions inside a hidden comment"),
    (r"(?i)you\s+are\s+now\s+(in\s+)?(developer|dan|jailbreak|unrestricted)", "medium", "tries to change the agent's role"),
]


def scan(text: str) -> list:
    """Returns [(severity, what)] for anything suspicious in the text."""
    findings = []
    for pattern, severity, what in GUARD:
        if re.search(pattern, text):
            findings.append((severity, what))
    return findings


def blocked(text: str) -> str:
    """The reason this text must not be loaded, or ''."""
    high = [what for severity, what in scan(text) if severity == "high"]
    return "; ".join(high)


# ---------------------------------------------------------------- skills

def _own_settings() -> dict:
    """skill_paths and skill_hide from Talos's own config file (the user's, not a project's)."""
    data = util.load_json_file(util.home() / "talos.json", {})
    return data if isinstance(data, dict) else {}


def skill_dirs(cwd) -> list:
    cwd = Path(cwd)
    user = util.user_home()
    extra = [Path(str(item)).expanduser() for item in (_own_settings().get("skill_paths") or [])]
    return extra + [
        cwd / ".talos" / "skills", cwd / ".opencode" / "skills", cwd / ".opencode" / "skill",
        cwd / ".claude" / "skills", cwd / ".agents" / "skills",
        util.home() / "skills",
        user / ".config" / "opencode" / "skills", user / ".config" / "opencode" / "skill",
        user / ".hermes" / "skills", user / ".claude" / "skills", user / ".agents" / "skills",
        user / ".codex" / "skills", user / ".vector" / "skills",
    ]


def _trusted() -> dict:
    return util.load_json_file(util.home() / "trusted.json", {}) or {}


def discover(cwd) -> dict:
    """name -> {name, description, path, origin, blocked}. The first folder that has a name wins."""
    trusted = _trusted()
    hidden = [str(pattern) for pattern in (_own_settings().get("skill_hide") or [])]
    found = {}
    for folder in skill_dirs(cwd):
        if not folder.is_dir():
            continue
        files = sorted(
            path for pattern in ("*/SKILL.md", "*/*/SKILL.md") for path in folder.glob(pattern)
        )
        for path in files:
            text = util.read_text(path)
            meta, _ = util.front_matter(text)
            name = str(meta.get("name") or path.parent.name)
            if name in found or any(fnmatch.fnmatch(name, pattern) for pattern in hidden):
                continue
            reason = blocked(text)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if reason and trusted.get(name) == digest:
                reason = ""
            found[name] = {
                "name": name,
                "description": str(meta.get("description") or "").strip(),
                "path": str(path),
                "origin": folder.parts[-2].lstrip("."),
                "blocked": reason,
            }
    return found


def trust(cwd, name: str) -> bool:
    """Let a flagged skill load. Trust is tied to the exact file content."""
    skill = discover(cwd).get(name)
    if not skill:
        return False
    trusted = _trusted()
    trusted[name] = hashlib.sha256(util.read_text(skill["path"]).encode("utf-8")).hexdigest()
    util.save_json_file(util.home() / "trusted.json", trusted)
    return True


def index_text(cwd) -> str:
    """The short list that goes into the system prompt."""
    lines = []
    for skill in discover(cwd).values():
        if skill["blocked"]:
            continue
        lines.append(f"- {skill['name']}: {skill['description'][:200]}")
    return "\n".join(lines)


def load(cwd, name: str) -> str:
    skill = discover(cwd).get(name)
    if not skill:
        names = ", ".join(sorted(discover(cwd))) or "none"
        return f"No skill named '{name}'. Available: {names}"
    if skill["blocked"]:
        return (
            f"Skill '{name}' was not loaded: it {skill['blocked']}. "
            f"The user can allow it with: talos skills trust {name}"
        )
    folder = Path(skill["path"]).parent
    _, body = util.front_matter(util.read_text(skill["path"]))
    extra = sorted(
        str(path.relative_to(folder)) for path in folder.rglob("*")
        if path.is_file() and path.name != "SKILL.md"
    )[:40]
    listing = ("\n\nFiles in this skill (" + str(folder) + "):\n" + "\n".join(extra)) if extra else ""
    return f"# Skill: {name}\n\n{body}{listing}"


def manage(action: str, name: str, description: str = "", content: str = "", old_text: str = "") -> str:
    """The agent writes its own skills here (create, patch, delete) in ~/.talos/skills."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,60}", name or ""):
        return "Skill names use lowercase letters, digits and dashes (2 to 61 characters)."
    path = util.home() / "skills" / name / "SKILL.md"
    if action == "create":
        if not description or not content:
            return "create needs a description and the content."
        text = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n{content.strip()}\n"
    elif action == "patch":
        if not path.exists():
            return f"No own skill named '{name}' to patch."
        current = util.read_text(path)
        if old_text and old_text not in current:
            return "old_text was not found in the skill."
        text = current.replace(old_text, content, 1) if old_text else current.rstrip() + "\n\n" + content.strip() + "\n"
    elif action == "delete":
        if not path.exists():
            return f"No own skill named '{name}'."
        path.unlink()
        return f"Deleted skill '{name}'."
    else:
        return "action must be create, patch or delete."
    reason = blocked(text)
    if reason:
        return f"Not saved: the skill text {reason}."
    util.write_text(path, text)
    return f"Saved skill '{name}' at {path}."


# ---------------------------------------------------------------- memory

MEMORY_LIMITS = {"memory": 2200, "user": 1375}
MEMORY_FILES = {"memory": "MEMORY.md", "user": "USER.md"}
SEPARATOR = "\n\u00a7\n"


def _memory_path(target: str) -> Path:
    return util.home() / "memories" / MEMORY_FILES[target]


def memory_entries(target: str) -> list:
    path = _memory_path(target)
    if not path.exists():
        return []
    return [entry.strip() for entry in util.read_text(path).split(SEPARATOR) if entry.strip()]


def memory_prompt() -> str:
    """Both memory files, as they go into the system prompt at the start of a session."""
    blocks = []
    for target, title in (("memory", "What you have learned"), ("user", "About the user")):
        entries = memory_entries(target)
        if entries:
            used = len(SEPARATOR.join(entries))
            blocks.append(
                f"## {title} ({used}/{MEMORY_LIMITS[target]} characters)\n" + "\n".join(
                    f"- {entry}" for entry in entries
                )
            )
    return "\n\n".join(blocks)


def memory_edit(action: str, target: str, content: str = "", old_text: str = "") -> str:
    if target not in MEMORY_FILES:
        return "target must be 'memory' or 'user'."
    entries = memory_entries(target)
    content = (content or "").strip()
    if action in ("add", "replace"):
        if not content:
            return "content is empty."
        reason = blocked(content)
        if reason:
            return f"Not saved: the text {reason}."
        if util.redact(content) != content:
            return "Not saved: this looks like a key or password. Memory must not hold secrets."
    if action == "add":
        if content in entries:
            return "Already in memory."
        entries.append(content)
    elif action in ("replace", "remove"):
        matches = [index for index, entry in enumerate(entries) if old_text and old_text in entry]
        if len(matches) != 1:
            return f"old_text must match exactly one entry (it matched {len(matches)})."
        if action == "replace":
            entries[matches[0]] = content
        else:
            entries.pop(matches[0])
    else:
        return "action must be add, replace or remove."
    used = len(SEPARATOR.join(entries))
    limit = MEMORY_LIMITS[target]
    if used > limit:
        return (
            f"Memory is full ({used}/{limit} characters). Merge or remove entries first, "
            "then add again. Current entries:\n" + "\n".join(f"- {entry}" for entry in entries[:-1])
        )
    util.write_text(_memory_path(target), SEPARATOR.join(entries) + ("\n" if entries else ""))
    return f"Saved. {target}: {used}/{limit} characters, {len(entries)} entries."
