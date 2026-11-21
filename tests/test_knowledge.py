import json
import time

from talos import knowledge, skills, util
from talos.fake import call, say
from tests.base import PYTHON, Case
from tests.test_skills import make_skill

REPOS = [
    {"name": "bracket-maker", "description": "Makes sheet metal brackets", "url": "https://example.test/u/bracket-maker",
     "updatedAt": "2026-03-04T10:00:00Z", "isPrivate": False, "primaryLanguage": {"name": "Python"}},
    {"name": "leak-notes", "description": "Air leak test log", "url": "https://example.test/u/leak-notes",
     "updatedAt": "2026-05-06T10:00:00Z", "isPrivate": True, "primaryLanguage": None},
]


class KnowledgeTest(Case):
    def setUp(self):
        super().setUp()
        self.vault = self.user / "Vault"
        (self.vault / "decisions").mkdir(parents=True)
        (self.vault / ".obsidian").mkdir()
        (self.vault / "decisions" / "0001 bolt size.md").write_text(
            "# Bolt size\nWe chose M4 bolts for the cover because M3 stripped twice.\n", encoding="utf-8")
        (self.vault / "shopping.md").write_text("Buy bolts and tape.\n", encoding="utf-8")
        (self.vault / ".obsidian" / "cache.md").write_text("cover bolts M4 hidden settings\n", encoding="utf-8")
        settings = self.user / "AppData" / "Roaming" / "obsidian"
        settings.mkdir(parents=True)
        (settings / "obsidian.json").write_text(json.dumps({"vaults": {"a1": {"path": str(self.vault)}}}), encoding="utf-8")
        script = self.tmp / "fake_gh.py"
        script.write_text(
            f"import json, sys\nrows = {REPOS!r}\n"
            "if sys.argv[1:] == ['second-owner']:\n    rows = [dict(rows[0], name='from-second-owner')]\n"
            "print(json.dumps(rows))\n", encoding="utf-8")
        local = self.tmp / "code"
        (local / "bracket-maker" / ".git").mkdir(parents=True)
        (local / "only-here" / ".git").mkdir(parents=True)
        (local / "not-a-project").mkdir()
        self.extra = {"projects": {"gh": [PYTHON, str(script)], "folders": [str(local)]}}

    def test_notes_are_found_where_they_are(self):
        config = self.config(**self.extra)
        self.assertEqual(knowledge.vaults(config), [self.vault])
        hits = knowledge.search_notes(config, "bolts cover")
        self.assertEqual([hit["title"] for hit in hits], ["0001 bolt size"])
        self.assertIn("M4 bolts", hits[0]["hit"])
        self.assertEqual(len(knowledge.search_notes(config, "bolts")), 2)        # .obsidian is skipped
        self.assertEqual(knowledge.search_notes(config, "no-such-word"), [])
        self.assertEqual(knowledge.vaults(self.config(notes=False)), [])
        named = self.config(notes=[str(self.vault / "decisions")])
        self.assertEqual(knowledge.vaults(named), [self.vault / "decisions"])

    def test_cairn_folder_is_found_too(self):
        (self.user / "AppData" / "Roaming" / "obsidian" / "obsidian.json").unlink()
        cairn = self.user / "AppData" / "Roaming" / "Cairn"
        cairn.mkdir(parents=True)
        (cairn / "config.json").write_text(json.dumps({"folder": str(self.vault)}), encoding="utf-8")
        self.assertEqual(knowledge.vaults(self.config()), [self.vault])

    def test_projects_from_github_and_local_folders(self):
        config = self.config(**self.extra)
        rows = knowledge.projects(config)
        self.assertEqual([row["name"] for row in rows], ["leak-notes", "bracket-maker", "only-here"])
        bracket = knowledge.find_projects(config, "sheet metal")[0]
        self.assertTrue(bracket["path"].endswith("bracket-maker"))
        self.assertEqual(bracket["language"], "Python")
        self.assertTrue((self.home / "projects.json").exists())
        old = util.load_json_file(self.home / "projects.json")
        util.save_json_file(self.home / "projects.json", {"at": 1, "projects": old["projects"][:1]})
        self.assertEqual(len(knowledge.projects(self.config(projects={"gh": self.extra["projects"]["gh"]}))), 1)
        deadline = time.time() + 30                     # the old list came back at once; the new one follows
        while time.time() < deadline and len(util.load_json_file(self.home / "projects.json")["projects"]) < 2:
            time.sleep(0.1)
        self.assertEqual(len(util.load_json_file(self.home / "projects.json")["projects"]), 2)
        both = self.config(projects={"gh": [PYTHON, str(self.tmp / "fake_gh.py")], "owners": ["first-owner", "second-owner"]})
        self.assertIn("from-second-owner", [row["name"] for row in knowledge.projects(both, refresh=True)])
        offline = self.config(projects={"gh": [PYTHON, "-c", "raise SystemExit(1)"]})
        self.assertEqual(len(knowledge.projects(offline, refresh=True)), 3)      # falls back to the saved list
        self.assertEqual(knowledge.projects(self.config(projects={"gh": False})), [])

    def test_agent_looks_things_up(self):
        runner, model, events = self.agent([
            call("notes", query="bolts cover"),
            call("projects", query="leak"),
            call("projects"),
            call("notes", query="zzz-nothing"),
            say("We chose M4."),
        ], **self.extra)
        result = runner.run("which bolts did we choose for the cover?")
        self.assertEqual(result.text, "We chose M4.")
        found, nothing = self.results(events, "notes")
        self.assertIn("0001 bolt size.md", found)
        self.assertIn("No note contains", nothing)
        one, everything = self.results(events, "projects")
        self.assertIn("leak-notes (private)", one)
        self.assertNotIn("bracket-maker", one)
        self.assertIn("only-here", everything)
        system = model.requests[0]["messages"][0]["content"]
        self.assertIn("# What the user already has", system)
        self.assertIn(str(self.vault), system)

    def test_no_notes_no_hint(self):
        (self.user / "AppData" / "Roaming" / "obsidian" / "obsidian.json").unlink()
        runner, model, events = self.agent([call("notes", query="bolts"), say("ok")], projects={"gh": False})
        runner.run("look it up")
        self.assertIn("No notes folder is known", self.results(events, "notes")[0])
        self.assertNotIn("What the user already has", model.requests[0]["messages"][0]["content"])

    def test_more_skill_folders_and_hidden_names(self):
        make_skill(self.user / ".vector" / "skills", "from-vector", "made in Vector")
        make_skill(self.user / ".codex" / "skills", "from-codex", "made in Codex")
        make_skill(self.tmp / "shared", "from-shared", "kept in a shared folder")
        make_skill(self.user / ".claude" / "skills", "work-secret", "should stay out")
        self.assertNotIn("from-shared", skills.discover(self.work))
        util.save_json_file(self.home / "talos.json", {"skill_paths": [str(self.tmp / "shared")], "skill_hide": ["work-*"]})
        found = skills.discover(self.work)
        self.assertEqual(sorted(found), ["from-codex", "from-shared", "from-vector"])
        self.assertEqual(found["from-vector"]["origin"], "vector")
