import ast
import importlib
import subprocess
import sys
from pathlib import Path

from talos import parity
from tests.base import PYTHON, Case

ROOT = Path(__file__).resolve().parent.parent


class ParityTest(Case):
    def test_standard_library_only(self):
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("needs Python 3.10 or newer to list the standard library")
        allowed = set(sys.stdlib_module_names) | {"talos"}
        for path in sorted((ROOT / "talos").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, f"{path.name} imports {name}")

    def test_native_rows_name_real_tests(self):
        for row in parity.rows():
            if row["status"] != "native":
                self.assertEqual(row["test"], "", row["feature"])
                continue
            module_name, class_name, method = row["test"].split(".")
            module = importlib.import_module("tests." + module_name)
            self.assertTrue(callable(getattr(getattr(module, class_name), method, None)), row["test"])
        statuses = {row["status"] for row in parity.rows()}
        self.assertEqual(statuses, {"native", "bridged", "untested", "missing"})

    def test_counts_and_file(self):
        counts = parity.counts()
        self.assertEqual(sum(sum(group.values()) for group in counts.values()), len(parity.ROWS))
        self.assertEqual(set(counts), {"opencode", "hermes", "talos"})
        text = parity.markdown()
        for row in parity.rows():
            self.assertIn(row["feature"], text)
        saved = ROOT / "PARITY.md"
        if saved.exists():
            self.assertEqual(saved.read_text(encoding="utf-8").strip(), text.strip(), "run: talos parity --write")

    def test_every_tool_is_tested(self):
        from talos import tools
        used = "".join(path.read_text(encoding="utf-8") for path in (ROOT / "tests").glob("test_*.py"))
        missing = [name for name in tools.TOOLS if f'call("{name}"' not in used]
        self.assertEqual(missing, [], "no test calls these tools")
