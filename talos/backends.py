"""Where shell commands run: this PC, a Docker container, another machine over SSH, or a
Singularity image. Chosen with "backend": {"type": "local" | "docker" | "ssh" | "singularity"}.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time


def shell_argv(command: str, shell: str = "") -> list:
    """The argv that runs one command line in the user's shell."""
    if shell:
        return [shell, "-c", command]
    if os.name == "nt":
        program = shutil.which("pwsh") or "powershell"
        command = powershell_fix(command, legacy=not shutil.which("pwsh"))
        return [program, "-NoProfile", "-NonInteractive", "-Command", command]
    return [os.environ.get("SHELL") or "/bin/sh", "-c", command]


def powershell_fix(command: str, legacy: bool = True) -> str:
    """Repair the two habits from other shells that PowerShell rejects: a quoted program path
    at the start (needs &), and `a && b` (not in Windows PowerShell 5)."""
    parts, current, quote = [], "", ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif legacy and command.startswith("&&", index):
            parts.append(current.strip())
            current, index = "", index + 2
            continue
        current += char
        index += 1
    parts.append(current.strip())
    parts = [("& " + part) if part[:1] in "'\"" else part for part in parts if part]
    # Windows PowerShell answers 1 for any failure; this hands back the program's own exit code,
    # and stops an `a && b` chain at the first part that fails.
    check = ("$talosOk = $?; if ($null -ne $LASTEXITCODE) { if ($LASTEXITCODE -ne 0) "
             "{ exit $LASTEXITCODE } } elseif (-not $talosOk) { exit 1 }")
    return "\n".join(f"$global:LASTEXITCODE = $null\n{part}\n{check}" for part in parts) + "\nexit 0"


def shell_name(shell: str = "") -> str:
    if shell:
        return os.path.basename(shell)
    return "PowerShell" if os.name == "nt" else os.path.basename(os.environ.get("SHELL") or "sh")


def kill_tree(process) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
    else:
        process.kill()


def run_argv(argv, cwd=None, timeout=120, abort=None, env=None, stdin_text=None):
    """Run and wait. Returns (exit code, combined output). -1 = timed out, -2 = stopped."""
    if cwd and "PWD" in (env if env is not None else os.environ):
        # A shell keeps PWD in step with the folder; starting a program in another folder does not.
        # Programs that trust PWD (OpenCode does) would otherwise work in the folder Talos was started in.
        env = dict(env if env is not None else os.environ, PWD=str(cwd))
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        )
    except OSError as error:
        return 127, f"Could not start {argv[0]}: {error}"
    chunks = []

    def pump():
        if stdin_text is not None:
            try:
                process.stdin.write(stdin_text.encode("utf-8"))
                process.stdin.close()
            except OSError:
                pass
        for raw in iter(lambda: process.stdout.read(4096), b""):
            chunks.append(raw)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.time() + timeout
    code = None
    while code is None:
        code = process.poll()
        if code is not None:
            break
        if abort is not None and abort.is_set():
            kill_tree(process)
            code = -2
        elif time.time() > deadline:
            kill_tree(process)
            code = -1
        else:
            time.sleep(0.05)
    reader.join(timeout=3)
    output = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
    if code == -1:
        output += f"\n[stopped: no result after {timeout} s]"
    return code, output


class Local:
    name = "local"

    def __init__(self, settings=None):
        self.settings = settings or {}

    def argv(self, command, cwd):
        return shell_argv(command, self.settings.get("shell", "")), cwd

    def run(self, command, cwd, timeout=120, abort=None):
        argv, where = self.argv(command, cwd)
        return run_argv(argv, where, timeout, abort)


class Docker(Local):
    """One long-lived container per project folder, with the folder mounted at /workspace."""
    name = "docker"

    def container(self, cwd) -> str:
        import hashlib
        return self.settings.get("container") or "talos-" + hashlib.sha1(
            str(cwd).encode("utf-8")).hexdigest()[:8]

    def start_argv(self, cwd) -> list:
        image = self.settings.get("image", "python:3.12-slim")
        return ["docker", "run", "-d", "--name", self.container(cwd), "-v", f"{cwd}:/workspace",
                "-w", "/workspace", image, "sleep", "infinity"]

    def argv(self, command, cwd):
        return ["docker", "exec", "-w", "/workspace", self.container(cwd), "sh", "-c", command], None

    def run(self, command, cwd, timeout=120, abort=None):
        probe, _ = run_argv(["docker", "inspect", self.container(cwd)], None, 20)
        if probe != 0:
            code, output = run_argv(self.start_argv(cwd), None, 300)
            if code != 0:
                return code, "Could not start the Docker container:\n" + output
        return super().run(command, cwd, timeout, abort)


class SSH(Local):
    name = "ssh"

    def argv(self, command, cwd):
        settings = self.settings
        target = settings.get("host", "")
        if settings.get("user"):
            target = f"{settings['user']}@{target}"
        argv = ["ssh", "-o", "BatchMode=yes"]
        if settings.get("port"):
            argv += ["-p", str(settings["port"])]
        if settings.get("key"):
            argv += ["-i", str(settings["key"])]
        remote = settings.get("cwd") or "~"
        return argv + [target, f"cd {remote} && {command}"], None


class Singularity(Local):
    name = "singularity"

    def argv(self, command, cwd):
        image = self.settings.get("image", "docker://python:3.12-slim")
        return ["singularity", "exec", "--pwd", str(cwd), image, "sh", "-c", command], None


BACKENDS = {"local": Local, "docker": Docker, "ssh": SSH, "singularity": Singularity}


def make(settings=None):
    settings = settings or {}
    kind = settings.get("type", "local")
    if kind not in BACKENDS:
        raise ValueError(f"Unknown backend '{kind}'. Use one of: {', '.join(BACKENDS)}")
    return BACKENDS[kind](settings)
