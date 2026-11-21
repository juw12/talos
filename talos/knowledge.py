"""What the user already knows: their notes and their projects.

Talos reads the user's own note folders (an Obsidian vault, a Cairn folder, or any folder of
Markdown files) and the list of their projects (GitHub through the `gh` program, plus local
folders), so the agent can look things up before it answers. Everything is read in place;
nothing is copied or sent anywhere except to the model the user chose.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import util

SKIP = {".obsidian", ".trash", ".git", "node_modules", "__pycache__", ".stversions"}
NOTE_TYPES = {".md", ".markdown", ".txt"}
MAX_NOTE_BYTES = 1_000_000
CACHE_HOURS = 6
GH_FIELDS = "name,description,url,updatedAt,isPrivate,primaryLanguage"
_refreshing = threading.Lock()


# ---------------------------------------------------------------- notes

def _settings_files(user: Path) -> list:
    return [
        (user / "AppData" / "Roaming" / "obsidian" / "obsidian.json", "obsidian"),
        (user / ".config" / "obsidian" / "obsidian.json", "obsidian"),
        (user / "Library" / "Application Support" / "obsidian" / "obsidian.json", "obsidian"),
        (user / "AppData" / "Roaming" / "Cairn" / "config.json", "cairn"),
        (user / ".config" / "Cairn" / "config.json", "cairn"),
    ]


def vaults(config) -> list:
    """Folders of notes: the ones named in the config ("notes"), else the ones found on this computer."""
    named = config.get("notes")
    if named is False:
        return []
    if isinstance(named, str):
        named = [named]
    found = []
    if named:
        found = [Path(os.path.expanduser(str(item))) for item in named]
    else:
        for path, kind in _settings_files(util.user_home()):
            data = util.load_json_file(path, None)
            if not isinstance(data, dict):
                continue
            if kind == "obsidian":
                entries = data.get("vaults")
                entries = entries.values() if isinstance(entries, dict) else []
                found += [Path(str(entry.get("path"))) for entry in entries if isinstance(entry, dict) and entry.get("path")]
            elif data.get("folder"):
                found.append(Path(str(data["folder"])))
    unique, seen = [], set()
    for folder in found:
        key = os.path.normcase(str(folder))
        if key not in seen and folder.is_dir():
            seen.add(key)
            unique.append(folder)
    return unique


def _note_files(folder: Path):
    for root, folders, files in os.walk(folder):
        folders[:] = [name for name in folders if name not in SKIP]
        for name in files:
            if os.path.splitext(name)[1].lower() in NOTE_TYPES:
                yield Path(root) / name


def _words(query: str) -> list:
    return [word for word in re.findall(r"[^\s,;]+", str(query).lower()) if word]


def search_notes(config, query: str, limit: int = 8) -> list:
    """Notes that contain every word of the query, best first: [{path, title, hit, when}]."""
    words = _words(query)
    if not words:
        return []
    hits = []
    for folder in vaults(config):
        for path in _note_files(folder):
            try:
                stat = path.stat()
                if stat.st_size > MAX_NOTE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            title = path.stem
            low, low_title = text.lower(), title.lower()
            if not all(word in low or word in low_title for word in words):
                continue
            score = sum(min(low.count(word), 5) + (10 if word in low_title else 0) for word in words)
            first = min((low.find(word) for word in words if word in low), default=0)
            start = max(0, first - 80)
            snippet = " ".join(text[start:start + 260].split())
            hits.append({"path": str(path), "title": title, "hit": util.redact(snippet),
                         "when": time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)),
                         "score": score, "changed": stat.st_mtime})
    hits.sort(key=lambda hit: (-hit["score"], -hit["changed"]))
    for hit in hits:
        del hit["score"], hit["changed"]
    return hits[:max(1, int(limit or 8))]


# ---------------------------------------------------------------- projects

def _settings(config) -> dict:
    settings = config.get("projects")
    return settings if isinstance(settings, dict) else {}


def _from_github(config) -> list:
    settings = _settings(config)
    command = settings.get("gh")
    if command is False:
        return []
    if not command:
        command = ["gh", "repo", "list", "--json", GH_FIELDS, "--limit", "300"]
    rows = []
    for owner in settings.get("owners") or [""]:        # "" = the account gh is signed in to
        try:
            done = subprocess.run(list(command) + ([str(owner)] if owner else []), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=40,
                                  stdin=subprocess.DEVNULL)
            found = json.loads(done.stdout) if done.returncode == 0 else []
        except (OSError, ValueError, subprocess.SubprocessError):
            found = []
        rows += found if isinstance(found, list) else []
    projects = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        language = row.get("primaryLanguage")
        projects.append({
            "name": str(row["name"]), "description": str(row.get("description") or ""),
            "url": str(row.get("url") or ""), "updated": str(row.get("updatedAt") or "")[:10],
            "private": bool(row.get("isPrivate")),
            "language": str(language.get("name") or "") if isinstance(language, dict) else str(language or ""),
            "path": "",
        })
    return projects


def _local(config) -> list:
    projects = []
    for folder in _settings(config).get("folders") or []:
        folder = Path(os.path.expanduser(str(folder)))
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                projects.append({"name": child.name, "path": str(child)})
    return projects


def projects(config, refresh: bool = False) -> list:
    """The user's projects: GitHub (kept for a few hours) joined with local folders by name."""
    cache_file = util.home() / "projects.json"
    cache = util.load_json_file(cache_file, None)
    fresh = isinstance(cache, dict) and time.time() - float(cache.get("at") or 0) < CACHE_HOURS * 3600
    def ask_github() -> list:
        found = _from_github(config)
        if found:
            util.save_json_file(cache_file, {"at": time.time(), "projects": found})
        return found

    if _settings(config).get("gh") is False:
        remote = []
    elif fresh and not refresh:
        remote = cache.get("projects") or []
    elif isinstance(cache, dict) and not refresh:
        remote = cache.get("projects") or []            # an old list now, a new one for next time
        if _refreshing.acquire(blocking=False):
            def renew():
                try:
                    ask_github()
                finally:
                    _refreshing.release()
            threading.Thread(target=renew, daemon=True).start()
    else:
        remote = ask_github()
        if not remote and isinstance(cache, dict):
            remote = cache.get("projects") or []        # offline: the last list is better than none
    by_name = {project["name"].lower(): dict(project) for project in remote}
    for local in _local(config):
        entry = by_name.setdefault(local["name"].lower(), {
            "name": local["name"], "description": "", "url": "", "updated": "", "private": False,
            "language": "", "path": ""})
        entry["path"] = local["path"]
    return sorted(by_name.values(), key=lambda project: project.get("updated") or "", reverse=True)


def find_projects(config, query: str = "", refresh: bool = False) -> list:
    words = _words(query)
    rows = projects(config, refresh)
    if not words:
        return rows
    return [row for row in rows if all(
        word in f"{row['name']} {row['description']} {row['language']}".lower() for word in words)]


def project_line(project: dict) -> str:
    parts = [project["name"]]
    if project.get("private"):
        parts.append("(private)")
    if project.get("language"):
        parts.append(f"[{project['language']}]")
    if project.get("description"):
        parts.append("- " + project["description"][:160])
    where = [item for item in (project.get("path"), project.get("url")) if item]
    if where:
        parts.append("| " + " | ".join(where))
    if project.get("updated"):
        parts.append("| updated " + project["updated"])
    return " ".join(parts)


def summary(config) -> str:
    """One or two lines for the system prompt: what is there to look up. Never starts a program."""
    lines = []
    folders = vaults(config)
    if folders:
        lines.append("The user's notes are in " + ", ".join(str(folder) for folder in folders[:3])
                     + ". Search them with the notes tool before you answer questions about their work, "
                       "decisions or past projects.")
    cache = util.load_json_file(util.home() / "projects.json", None)
    count = len(cache.get("projects") or []) if isinstance(cache, dict) else 0
    if count or _settings(config).get("folders"):
        lines.append(f"The user has {count or 'several'} projects. List or search them with the projects tool.")
    return "\n".join(lines)


def overview(config) -> dict:
    """For the app's Knowledge page."""
    folders = []
    for folder in vaults(config):
        folders.append({"path": str(folder), "notes": sum(1 for _ in _note_files(folder))})
    return {"vaults": folders, "projects": projects(config)}
