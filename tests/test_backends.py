import os
import unittest

from talos import backends
from tests.base import PYTHON, Case


class BackendsTest(Case):
    def test_command_lines(self):
        docker = backends.make({"type": "docker", "image": "python:3.13-slim"})
        start = docker.start_argv("/work/a")
        self.assertEqual(start[:2], ["docker", "run"])
        self.assertIn("python:3.13-slim", start)
        self.assertIn("/work/a:/workspace", start)
        run, cwd = docker.argv("pytest -q", "/work/a")
        self.assertEqual(run[:2] + run[-3:], ["docker", "exec", "sh", "-c", "pytest -q"])
        self.assertIsNone(cwd)
        self.assertEqual(docker.container("/work/a"), docker.container("/work/a"))
        self.assertNotEqual(docker.container("/work/a"), docker.container("/work/b"))

        ssh = backends.make({"type": "ssh", "host": "box.example", "user": "me", "port": 2222, "key": "id", "cwd": "/srv/app"})
        self.assertEqual(ssh.argv("ls", "/x")[0], ["ssh", "-o", "BatchMode=yes", "-p", "2222", "-i", "id",
                                                   "me@box.example", "cd /srv/app && ls"])
        image = backends.make({"type": "singularity"}).argv("ls", "/x")[0]
        self.assertEqual((image[:2], image[-3:]), (["singularity", "exec"], ["sh", "-c", "ls"]))
        self.assertEqual(backends.shell_argv("ls", "/bin/zsh"), ["/bin/zsh", "-c", "ls"])
        with self.assertRaises(ValueError):
            backends.make({"type": "moon"})

        fixed = backends.powershell_fix('"C:\\Program Files\\py\\python.exe" a.py && echo "x && y"')
        self.assertIn('& "C:\\Program Files\\py\\python.exe" a.py', fixed)
        self.assertIn('echo "x && y"', fixed)
        self.assertEqual(fixed.count("$global:LASTEXITCODE = $null"), 2)
        self.assertIn("&&", backends.powershell_fix("a && b", legacy=False))

        code, output = backends.run_argv([PYTHON, "-c", "print('out'); raise SystemExit(5)"])
        self.assertEqual((code, output.strip()), (5, "out"))
        code, output = backends.run_argv([PYTHON, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertEqual(code, -1)
        self.assertEqual(backends.run_argv(["no-such-program-talos"])[0], 127)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell only")
    def test_powershell_exit_codes(self):
        local = backends.make({})
        quoted = f'"{PYTHON}"'
        self.assertEqual(local.run(f'{quoted} -c "raise SystemExit(4)"', str(self.work))[0], 4)
        code, output = local.run(f'{quoted} -c "raise SystemExit(3)" && echo second', str(self.work))
        self.assertEqual(code, 3)
        self.assertNotIn("second", output)
        code, output = local.run(f'{quoted} -c "print(1)" && echo second', str(self.work))
        self.assertEqual(code, 0)
        self.assertIn("second", output)
        self.assertEqual(local.run("Get-Item no-such-file-here", str(self.work))[0], 1)
