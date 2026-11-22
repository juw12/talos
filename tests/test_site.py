"""The site (docs/, shown at https://juw12.github.io/talos/) may only say what the program does."""
import html
import re
import unittest

from talos import __version__, parity
from tests.test_cli import CliCase
from tests.test_parity import ROOT

PAGE = ROOT / "docs" / "index.html"


def shortened(output: str) -> list:
    """The demo's output without what differs per computer: the temporary folder and the path of Python."""
    lines = []
    for line in output.strip().splitlines():
        line = re.sub(r"Working folder: .*$", "Working folder: ~/demo", line.rstrip())
        line = re.sub(r"(Created|Edited) .*[\\/]area\.py", r"\1 ~/demo/area.py", line)
        line = re.sub(r'"[^"]*" area\.py', "python area.py", line)
        if line.startswith("Proof:"):                 # the proof line keeps 70 characters of a command: a long path of
            line = re.sub(r'`"[^`]*`', "`python area.py`", line)             # Python cuts the file's name off there
        lines.append(line)
    return lines


class SiteTest(CliCase):
    def setUp(self):
        super().setUp()
        self.page = PAGE.read_text(encoding="utf-8")

    def found(self, pattern: str) -> str:
        match = re.search(pattern, self.page, re.S)
        self.assertIsNotNone(match, pattern)
        return html.unescape(match.group(1))

    def test_a_search_engine_can_read_what_it_is(self):
        self.assertIn("Talos", self.found(r"<title>(.*?)</title>"))
        self.assertGreaterEqual(len(self.found(r'<meta name="description" content="(.*?)">')), 50)
        self.assertNotIn("noindex", self.page.lower())
        for name in re.findall(r'(?:src|href)="([^":#]+\.(?:png|svg))"', self.page):
            self.assertTrue((PAGE.parent / name).is_file(), name)             # every picture the page shows is there

    def test_the_run_on_the_page_is_what_the_demo_prints(self):
        code, output, error = self.talos("demo")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(self.found(r'<pre id="demo">(.*?)</pre>').strip().splitlines(), shortened(output))

    def test_the_numbers_are_the_program_s(self):
        self.assertEqual(self.found(r'<span id="version">(.*?)</span>'), __version__)
        shown = {(source, status): int(number)
                 for source, status, number in re.findall(r'data-count="(\w+)-(\w+)">(\d+)<', self.page)}
        counts = parity.counts()
        for (source, status), number in shown.items():
            self.assertEqual(number, counts[source][status], f"{source} {status}")
        for source, group in counts.items():                                  # nothing left out that is not zero
            for status, number in group.items():
                if number and status != "native":
                    self.assertIn((source, status), shown)
            self.assertIn((source, "native"), shown)
        tests = unittest.TestLoader().discover(str(ROOT / "tests"), top_level_dir=str(ROOT)).countTestCases()
        self.assertEqual(int(self.found(r'<span id="tests">(\d+)</span>')), tests)
