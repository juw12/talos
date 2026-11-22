"""The Mac side of the app window: where the browser is, and Talos.app."""
import os
import plistlib
import subprocess
import sys
import unittest

from talos import __version__, cli
from tests.base import Case


class MacTest(Case):
    def test_finds_a_browser_inside_its_app_folder(self):
        apps = self.user / "Applications"
        self.assertEqual(cli.mac_browser([apps]), "")
        program = apps / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        program.parent.mkdir(parents=True)
        program.write_text("", encoding="utf-8")
        self.assertEqual(cli.mac_browser([self.tmp / "none", apps]), str(program))
        chrome = apps / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
        chrome.parent.mkdir(parents=True)
        chrome.write_text("", encoding="utf-8")
        self.assertEqual(cli.mac_browser([apps]), str(chrome))               # Chrome first when both are there

    def test_writes_an_app_that_starts_talos(self):
        workspace = self.user / "My Talos's work"                            # a space and a quote in the path
        workspace.mkdir()
        package_parent = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        picture = os.path.join(os.path.dirname(os.path.abspath(cli.__file__)), "web", "talos.png")
        bundle = cli.mac_app(workspace, package_parent, picture)
        self.assertEqual(bundle, str(self.user / "Applications" / "Talos.app"))
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
            facts = plistlib.load(handle)
        self.assertEqual((facts["CFBundleExecutable"], facts["CFBundleShortVersionString"], facts["LSUIElement"]),
                         ("Talos", __version__, True))
        program = os.path.join(bundle, "Contents", "MacOS", "Talos")
        if os.name != "nt":
            self.assertTrue(os.access(program, os.X_OK))
            # run the launcher itself, with a stand-in for "app": it must reach this Python, this package, that folder
            show = "import os, talos; print(os.path.realpath(os.getcwd())); print(talos.__version__)"
            with open(program, encoding="utf-8") as handle:
                script = handle.read()
            self.assertTrue(script.rstrip().endswith("-m talos app"))
            stand_in = script.replace("-m talos app", "-c " + '"' + show + '"')
            done = subprocess.run(["/bin/sh", "-c", stand_in], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.stdout.split("\n")[:2], [os.path.realpath(str(workspace)), __version__], done.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "macOS makes the icon and reads the app")
    def test_on_a_mac_the_app_has_its_logo_and_macos_accepts_it(self):
        os.environ["TALOS_WORKSPACE"] = str(self.user / "Talos")
        self.closers.append(lambda: os.environ.pop("TALOS_WORKSPACE", None))
        bundle = cli.install_shortcut()[0]
        icon = os.path.join(bundle, "Contents", "Resources", "Talos.icns")
        self.assertTrue(os.path.getsize(icon) > 1000)
        with open(icon, "rb") as handle:
            self.assertEqual(handle.read(4), b"icns")
        check = subprocess.run(["plutil", "-lint", os.path.join(bundle, "Contents", "Info.plist")],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout)
