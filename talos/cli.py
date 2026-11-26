"""The `talos` command."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__, agent as agent_module, config as config_module, cron, engines
from . import plugins, repl, skills, store as store_module, util

WORKFLOW = """name: talos
on:
  issue_comment:
    types: [created]
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  talos:
    # only people who can already push here may start it
    if: >-
      startsWith(github.event.comment.body, '/talos') &&
      contains(fromJSON('["OWNER", "MEMBER", "COLLABORATOR"]'), github.event.comment.author_association)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install git+https://github.com/juw12/talos
      - name: Run Talos on the comment
        env:
          COMMENT: ${{ github.event.comment.body }}
          TALOS_CONFIG_CONTENT: ${{ secrets.TALOS_CONFIG }}
          GH_TOKEN: ${{ github.token }}
        run: |
          python -m talos run "${COMMENT#/talos}" --yes > answer.md
          gh issue comment ${{ github.event.issue.number }} --body-file answer.md
"""

GITLAB = """talos:
  image: python:3.12
  rules:
    - if: $TALOS_PROMPT
  script:
    - pip install git+https://github.com/juw12/talos
    - python -m talos run "$TALOS_PROMPT" --yes
"""


def overrides_from(args) -> dict:
    data = {}
    content = os.environ.get("TALOS_CONFIG_CONTENT", "").strip()
    if content:
        data = json.loads(content)
    if getattr(args, "model", ""):
        data["model"] = args.model
    return data


def out(text=""):
    sys.stdout.write(str(text) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- doctor

def doctor_rows(config: dict) -> list:
    """What Talos found on this PC: [{name, ok, detail}]."""
    user, rows = util.user_home(), []

    def add(name, ok, detail):
        rows.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    add("Python", sys.version_info >= (3, 9), sys.version.split()[0])
    model = config_module.pick_default_model(config)
    add("Model", model, model or "none: start Ollama, or set \"model\" in " + str(util.home() / "talos.json"))
    models = config_module.list_models(config)
    add("Models available", models, ", ".join(models[:8]) or "none")
    add("Config files read", True, ", ".join(config.get("_sources") or []) or "none (defaults)")
    add("OpenCode setup", True, "found" if (user / ".config" / "opencode").is_dir() else "not found")
    add("Hermes setup", True, "found" if (user / ".hermes").is_dir() else "not found")
    found = skills.discover(config.get("_cwd", "."))
    held = [name for name, skill in found.items() if skill["blocked"]]
    add("Skills", True, f"{len(found)} found" + (f", {len(held)} held back by the guard: {', '.join(held[:6])}" if held else ""))
    installed = engines.available(config)
    add("Engines", True, ", ".join(installed) or "none installed (optional)")
    try:
        probe = store_module.Store(Path(tempfile.gettempdir()) / f"talos-probe-{os.getpid()}.db")
        fts = probe.fts
        probe.close()
        probe.path.unlink()
    except Exception:
        fts = False
    add("Session search", True, "full text (FTS5)" if fts else "simple (no FTS5 in this Python)")
    add("Data folder", True, util.home())
    return rows


# ---------------------------------------------------------------- app window and shortcut

def find_browser() -> str:
    names = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    roots = [os.environ.get(name, "") for name in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
    for root in roots:
        for name in names:
            path = Path(root) / name if root else None
            if path and path.is_file():
                return str(path)
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    if sys.platform == "darwin":
        return mac_browser([Path("/Applications"), util.user_home() / "Applications"])
    return ""


MAC_BROWSERS = ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser")


def mac_browser(folders) -> str:
    """On a Mac a browser is an .app folder, never a command on PATH: the program is inside it."""
    for folder in folders:
        for name in MAC_BROWSERS:
            program = Path(folder) / f"{name}.app" / "Contents" / "MacOS" / name
            if program.is_file():
                return str(program)
    return ""


def set_window_icon(title: str = "Talos", tries: int = 40) -> bool:
    """Windows only: put Talos' logo on the app window (and so on its taskbar button)."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    icon = str(Path(__file__).resolve().parent / "web" / "talos.ico")
    done = False
    for _ in range(tries):
        window = user32.FindWindowW("Chrome_WidgetWin_1", title)
        if window:
            for which, size in ((0, 16), (1, 48)):            # ICON_SMALL, ICON_BIG
                handle = user32.LoadImageW(None, icon, 1, size, size, 0x10)      # IMAGE_ICON, LR_LOADFROMFILE
                if handle:
                    user32.SendMessageW(window, 0x80, which, handle)             # WM_SETICON
                    done = True
            if done:
                return True
        time.sleep(0.5)
    return False


def open_window(url: str) -> str:
    """Open the app in its own window (no address bar) when a Chromium browser is there."""
    browser = "" if os.environ.get("TALOS_NO_WINDOW") else find_browser()
    if os.environ.get("TALOS_NO_WINDOW"):
        return "not opened (TALOS_NO_WINDOW)"
    if browser:
        # its own browser profile: the window is then its own taskbar button with Talos' logo,
        # not a tab group under the browser's icon
        profile = util.home() / "window"
        subprocess.Popen([browser, f"--app={url}", "--window-size=1240,860", f"--user-data-dir={profile}",
                          "--no-first-run", "--no-default-browser-check"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=set_window_icon, daemon=True).start()
        return "window"
    webbrowser.open(url)
    return "browser tab"


def port_in_use(port: int) -> bool:
    import socket
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return True
    except OSError:
        return False


def is_talos(port: int, patience: float = 2) -> bool:
    """Is a Talos already answering on this port? `patience` is how long a busy PC is given to answer."""
    deadline = time.time() + patience
    while True:
        try:
            status, data = util.http_json("GET", f"http://127.0.0.1:{port}/api/ping",
                                          timeout=max(2, min(patience, 10)))
            return status == 200 and data.get("app") == "talos"
        except (OSError, ValueError):
            if time.time() >= deadline:
                return False
            time.sleep(0.3)


def shortcut_script(target: str, arguments: str, icon: str, folder: str, links) -> str:
    """The PowerShell that makes the shortcuts. Kept separate so it can be tested."""
    def quote(text):
        return "'" + str(text).replace("'", "''") + "'"
    lines = ["$shell = New-Object -ComObject WScript.Shell"]
    for link in links:
        lines += [
            f"$link = $shell.CreateShortcut({quote(link)})",
            f"$link.TargetPath = {quote(target)}",
            f"$link.Arguments = {quote(arguments)}",
            f"$link.WorkingDirectory = {quote(folder)}",
            f"$link.IconLocation = {quote(icon)}",
            "$link.Description = 'Talos - one agent for your computer'",
            "$link.Save()",
        ]
    return "\n".join(lines)


MAC_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Talos</string>
<key>CFBundleDisplayName</key><string>Talos</string>
<key>CFBundleIdentifier</key><string>io.github.juw12.talos</string>
<key>CFBundleExecutable</key><string>Talos</string>
<key>CFBundleIconFile</key><string>Talos</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>{version}</string>
<key>LSUIElement</key><true/>
</dict></plist>
"""


def mac_app(workspace, package_parent, picture) -> str:
    """Talos.app in the user's own Applications folder: Launchpad and Spotlight find it there.

    It is a small launcher around this Python, so it needs no build and no signing. The window it
    opens belongs to the browser; the launcher itself stays out of the Dock.
    """
    bundle = util.user_home() / "Applications" / "Talos.app"
    program = bundle / "Contents" / "MacOS" / "Talos"
    quoted = [shlex.quote(str(part)) for part in (workspace, package_parent, sys.executable)]
    util.write_text(program, "#!/bin/sh\ncd {} || exit 1\nPYTHONPATH={} exec {} -m talos app\n".format(*quoted))
    program.chmod(0o755)
    util.write_text(bundle / "Contents" / "Info.plist", MAC_PLIST.format(version=__version__))
    resources = bundle / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    if shutil.which("sips") and Path(picture).is_file():      # macOS' own tool; without it the app has no logo
        subprocess.run(["sips", "-s", "format", "icns", str(picture), "--out", str(resources / "Talos.icns")],
                       capture_output=True)
    return str(bundle)


def install_shortcut() -> list:
    """Start menu and Desktop shortcuts (Windows), Talos.app (Mac) or a menu entry (Linux): the app window, no console."""
    workspace = Path(os.environ.get("TALOS_WORKSPACE") or util.user_home() / "Talos")
    workspace.mkdir(parents=True, exist_ok=True)
    package_parent = Path(__file__).resolve().parent.parent
    icon = Path(__file__).resolve().parent / "web" / "talos.ico"
    if sys.platform == "darwin":
        return [mac_app(workspace, package_parent, icon.with_suffix(".png"))]
    if os.name != "nt":
        launcher = util.home() / "talos-app.sh"
        util.write_text(launcher, f"#!/bin/sh\ncd '{workspace}'\nPYTHONPATH='{package_parent}' exec '{sys.executable}' -m talos app\n")
        launcher.chmod(0o755)
        desktop = util.user_home() / ".local" / "share" / "applications" / "talos.desktop"
        util.write_text(desktop, "[Desktop Entry]\nType=Application\nName=Talos\n"
                        f"Exec={launcher}\nIcon={icon.with_suffix('.png')}\nTerminal=false\nCategories=Development;\n")
        return [str(desktop)]
    if getattr(sys, "frozen", False):
        target, arguments = sys.executable, "app"
    else:
        launcher = util.home() / "talos-app.pyw"
        util.write_text(launcher, "import sys\n"
                        f"sys.path.insert(0, {str(package_parent)!r})\n"
                        "from talos.cli import main\nmain(['app'])\n")
        windowless = Path(sys.executable).with_name("pythonw.exe")
        target = str(windowless if windowless.is_file() else sys.executable)
        arguments = f'"{launcher}"'
    code, output = subprocess.getstatusoutput(
        'powershell -NoProfile -Command "[Environment]::GetFolderPath(\'Desktop\'); '
        '[Environment]::GetFolderPath(\'Programs\')"')
    folders = [line.strip() for line in output.splitlines() if line.strip()]
    if code != 0 or len(folders) < 2:
        raise RuntimeError("Could not find the Desktop and Start menu folders: " + output)
    links = [str(Path(folder) / "Talos.lnk") for folder in folders[:2]]
    script = shortcut_script(target, arguments, str(icon), str(workspace), links)
    done = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError("Could not create the shortcuts: " + done.stderr.strip())
    return links


# ---------------------------------------------------------------- commands

def command_run(args, config, store):
    text = " ".join(args.prompt).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    for name in args.file or []:
        text += f" @{name}"
    if not text:
        out("Give a prompt: talos run \"what to do\"")
        return 2
    session = ""
    if args.session or getattr(args, "continue_", False):
        session = store.find_session(args.session or "last") or ""
    text, agent, model = repl.prepare(text, config["_cwd"], config, args.agent, "")
    events = getattr(args, "events", False)
    if events:
        args.json = True                         # nothing but JSON on the output
    printer = repl.Printer(quiet=args.json, out=open(os.devnull, "w") if args.json else None)
    if events:
        printer = event_lines
    interactive = sys.stdin.isatty() and not args.json and not args.yes
    runner = agent_module.Agent(
        config, store, session=session, agent=agent or "build", model=model, auto_yes=args.yes,
        asker=repl.console_asker if interactive else None,
        ask=repl.console_ask if interactive else None, emit=printer)
    if events:
        event_lines("session", {"session": runner.session, "model": runner.model_name})
    try:
        result = runner.run(text)
    except ValueError as error:                  # no usable model: say so in the format that was asked for
        if not args.json:
            raise
        answer = {"text": str(error), "session": runner.session, "stopped": "model error",
                  "proof": {"level": "nothing changed"}, "usage": {}, "steps": 0}
        out(json.dumps({"event": "result", "data": answer} if events else answer, ensure_ascii=False))
        return 1
    finally:
        runner.close()
    if args.json:
        answer = {"text": result.text, "session": result.session, "stopped": result.stopped,
                  "proof": result.proof, "usage": result.usage, "steps": result.steps}
        out(json.dumps({"event": "result", "data": answer} if events else answer, ensure_ascii=False))
    else:
        if result.stopped:
            out(result.text if result.stopped == "model error" else f"Stopped: {result.stopped}")
        if result.stopped == "model error":
            out(other_ways(config))
        if result.proof_line():
            out(result.proof_line())
    return 1 if result.stopped else 0


def event_lines(event, data):
    """talos run --events: each thing the agent does as one line of JSON, for a program that shows
    the work while it runs (a phone page, an editor). The pieces of streamed text are left out; the
    whole message comes as "assistant". The last line is the event "result"."""
    if event == "text":
        return
    if event.endswith("tool_result") and isinstance(data, dict):
        data = dict(data, result=util.clip(str(data.get("result", "")), 2000))
    out(json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str))


def other_ways(config) -> str:
    """What to try when the model did not answer: the agents already on this computer, or a model."""
    installed = list(engines.available(config))
    if installed:
        return (f"Another way now: talos engine {installed[0]} \"your task\" hands it to an agent that is "
                f"installed here ({', '.join(installed)}). Or pick another model: talos models.")
    return "Pick another model: talos models. With none: start Ollama, or talos auth set <provider> <key>."


def command_serve(args, config, store, mode="serve"):
    from . import server as server_module
    overrides = overrides_from(args)
    port = int(getattr(args, "port", 0) or config.get("port") or 4747)
    host = getattr(args, "host", "") or "127.0.0.1"
    # Something already listens on our port: on a busy PC that is usually our own app answering
    # slowly. Wait for it instead of starting a second copy.
    if mode == "app" and port_in_use(port) and is_talos(port, patience=25):
        open_window(f"http://127.0.0.1:{port}/")
        return 0
    idle = 180 if mode == "app" else 0
    try:
        server, app = server_module.serve(config, store, host, port, getattr(args, "password", "") or "",
                                          overrides, idle)
    except OSError:
        server, app = server_module.serve(config, store, host, 0, getattr(args, "password", "") or "",
                                          overrides, idle)
    port = server.server_address[1]
    url = f"http://{'127.0.0.1' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    out(f"Talos is running at {url}  (Ctrl+C to stop)")
    if (config.get("gateway") or {}) and mode != "app":
        from . import gateway
        threading.Thread(target=gateway.Gateway(config, store).serve, daemon=True).start()
    else:
        def schedule():
            while True:
                try:
                    cron.tick(config, store=store)
                except Exception as error:
                    out(f"[schedule] {error}")
                time.sleep(30)
        threading.Thread(target=schedule, daemon=True).start()
    if mode in ("web", "app"):
        out("Opened: " + (open_window(url) if mode == "app" else str(webbrowser.open(url))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def command_demo(args, config, store):
    """Show a whole run with a scripted stand-in model: no model, key or network needed."""
    from . import fake
    folder = Path(tempfile.mkdtemp(prefix="talos-demo-"))
    script = [
        fake.call("write", filePath="area.py",
                  content="import math\n\ndef circle_area(radius):\n    return math.pi * radius\n\n"
                          "assert round(circle_area(2), 2) == 12.57, circle_area(2)\nprint('area ok')\n"),
        fake.call("bash", command=f'"{sys.executable}" area.py'),
        fake.call("edit", filePath="area.py", oldString="math.pi * radius", newString="math.pi * radius ** 2"),
        fake.call("bash", command=f'"{sys.executable}" area.py'),
        fake.say("area.py now computes the area correctly. The first run failed its own check "
                 "(it returned 6.28), I fixed the formula, and the second run printed 'area ok'."),
    ]
    model = fake.FakeModel(script)
    try:
        demo_config = config_module.load(folder, model.config(learn="off"))
        out(f"Demo (scripted stand-in model, no network). Working folder: {folder}\n")
        out("> Write area.py with a circle_area function and prove it works.\n")
        runner = agent_module.Agent(demo_config, store, source="demo", emit=repl.Printer())
        try:
            result = runner.run("Write area.py with a circle_area function and prove it works.")
        finally:
            runner.close()
        out(result.proof_line())
        undone = store.undo(result.session)
        out(f"\nUndo: took the turn back, area.py exists now: {(folder / 'area.py').exists()} "
            f"({len(undone['files'])} change(s) restored)")
        out("This was a script, not a real model. For real work: talos doctor, then talos.")
    finally:
        model.close()
    return 0 if result.proof["level"] == "checked" else 1


def command_batch(args, config, store):
    lines = [line.strip() for line in util.read_text(args.file).splitlines() if line.strip()]
    target = Path(args.output or Path(args.file).with_suffix(".out.jsonl"))
    rows = []
    for index, line in enumerate(lines, 1):
        try:
            prompt = json.loads(line)["prompt"] if line.startswith("{") else line
        except (ValueError, KeyError, TypeError):
            prompt = line
        runner = agent_module.Agent(config, store, source="batch", auto_yes=args.yes)
        try:
            result = runner.run(prompt)
        finally:
            runner.close()
        talk = []
        for message in store.messages(result.session):
            if message["role"] == "system" or message.get("internal"):
                continue
            value = message["content"] if isinstance(message.get("content"), str) else "(attachment)"
            for call in message.get("tool_calls") or []:
                value += f"\n<tool>{json.dumps({'name': call['name'], 'arguments': call['arguments']})}</tool>"
            talk.append({"from": {"user": "human", "assistant": "gpt"}.get(message["role"], "tool"),
                         "value": util.redact(value)})
        rows.append({"prompt": prompt, "answer": result.text, "stopped": result.stopped,
                     "proof": result.proof["level"], "usage": result.usage, "conversations": talk})
        out(f"{index}/{len(lines)} {result.proof['level']}{' (' + result.stopped + ')' if result.stopped else ''}")
    util.write_text(target, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    out(f"Saved {len(rows)} run(s) to {target}")
    return 0


def command_migrate(args, config, store):
    """Say what is used in place, and copy Hermes' memory entries that Talos does not have yet."""
    user = util.user_home()
    out("Used where they are (nothing is copied or changed):")
    for label, path in config_module.sources(config["_cwd"]):
        if path.exists() and label != "talos":
            out(f"  {label} config   {path}")
    for folder in skills.skill_dirs(config["_cwd"]):
        if folder.is_dir() and util.home() not in folder.parents:
            out(f"  skills           {folder}")
    copied = 0
    for target, name in skills.MEMORY_FILES.items():
        source = user / ".hermes" / "memories" / name
        if not source.is_file():
            continue
        have = set(skills.memory_entries(target))
        for entry in util.read_text(source).split(skills.SEPARATOR.strip("\n")):
            entry = entry.strip()
            if entry and entry not in have:
                outcome = skills.memory_edit("add", target, entry)
                copied += outcome.startswith(("Saved", "Added"))
                if not outcome.startswith(("Saved", "Added")):
                    out(f"  skipped a {target} entry: {outcome}")
    out(f"Copied {copied} memory entr{'y' if copied == 1 else 'ies'} from Hermes.")
    return 0


def edit_user_config(change) -> Path:
    path = util.home() / "talos.json"
    data = util.load_json_file(path, {}) or {}
    change(data)
    util.save_json_file(path, data)
    return path


def main(argv=None) -> int:
    if sys.stdout is None or sys.stderr is None:        # started without a console (pythonw)
        log = open(util.home() / "app.log", "a", encoding="utf-8")
        sys.stdout = sys.stderr = log
    util.hide_child_windows()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="talos", description="One agent that does what OpenCode and "
                                     "Hermes do, on any model, and proves its own work.")
    parser.add_argument("--version", action="version", version=f"talos {__version__}")
    parser.add_argument("--cwd", default=".", help="project folder (default: here)")
    parser.add_argument("-m", "--model", default="", help="provider/model")
    sub = parser.add_subparsers(dest="command")

    def add(name, help_text, *arguments):
        item = sub.add_parser(name, help=help_text)
        for flags, options in arguments:
            item.add_argument(*flags, **options)
        return item

    text = (("prompt",), {"nargs": "*"})
    yes = (("--yes",), {"action": "store_true", "help": "allow everything without asking"})
    add("run", "do one task and exit", text, yes,
        (("--agent",), {"default": ""}), (("-s", "--session"), {"default": ""}),
        (("-c", "--continue"), {"action": "store_true", "dest": "continue_"}),
        (("--json",), {"action": "store_true"}), (("--events",), {"action": "store_true"}),
        (("-f", "--file"), {"action": "append"}))
    for name, help_text in (("serve", "run the server (API + web app)"), ("web", "serve and open the browser"),
                            ("app", "open the app window")):
        add(name, help_text, (("--port",), {"type": int, "default": 0}), (("--host",), {"default": ""}),
            (("--password",), {"default": ""}))
    add("gateway", "answer on Telegram, Discord, Slack and the other chat platforms")
    add("relay", "your Talos chats on your phone, from anywhere (Talos Relay)",
        (("action",), {"nargs": "?", "default": "run"}),
        (("value",), {"nargs": "?", "default": "", "help": "mode: relay, local or both. keys: openrouter,groq,cerebras or none"}), yes,
        (("--new",), {"action": "store_true", "help": "pair: make a new link; the old phones stop working"}),
        (("--text",), {"action": "store_true", "help": "pair: show the code in the terminal"}))
    add("cron", "scheduled jobs", (("action",), {"nargs": "?", "default": "list"}),
        (("rest",), {"nargs": "*"}), (("--deliver",), {"default": "local"}), (("--name",), {"default": ""}))
    add("sessions", "list, search or delete sessions", (("action",), {"nargs": "?", "default": "list"}),
        (("rest",), {"nargs": "*"}))
    add("export", "save a session as JSON", (("session",), {"nargs": "?", "default": "last"}),
        (("-o", "--output"), {"default": ""}))
    add("import", "load a session from JSON", (("file",), {}))
    add("share", "save a session as one HTML page", (("session",), {"nargs": "?", "default": "last"}),
        (("-o", "--output"), {"default": ""}), (("--gist",), {"action": "store_true"}))
    add("models", "list the models that were found")
    add("agent", "list or create agents", (("action",), {"nargs": "?", "default": "list"}),
        (("rest",), {"nargs": "*"}))
    add("mcp", "list, add or remove MCP servers", (("action",), {"nargs": "?", "default": "list"}),
        (("rest",), {"nargs": "*"}))
    add("skills", "list, show, scan or trust skills", (("action",), {"nargs": "?", "default": "list"}),
        (("rest",), {"nargs": "*"}))
    add("memory", "show or add memory", (("action",), {"nargs": "?", "default": "show"}),
        (("rest",), {"nargs": "*"}))
    add("auth", "save or list API keys (set <provider> <key>, login openrouter, remove <provider>)", (("action",), {"nargs": "?", "default": "list"}),
        (("rest",), {"nargs": "*"}))
    add("doctor", "show what was found on this PC")
    add("parity", "feature table against OpenCode and Hermes", (("--write",), {"action": "store_true"}))
    add("tune", "measure your models and recommend one", (("models",), {"nargs": "*"}),
        (("--apply",), {"action": "store_true"}))
    add("stats", "tokens and cost")
    add("acp", "speak the Agent Client Protocol on stdin/stdout")
    add("batch", "run many prompts from a file and save the trajectories", (("file",), {}), yes,
        (("-o", "--output"), {"default": ""}))
    add("github", "install the GitHub workflow", (("action",), {"nargs": "?", "default": "install"}))
    add("gitlab", "install the GitLab CI job", (("action",), {"nargs": "?", "default": "install"}))
    add("engine", "hand a task to OpenCode, Hermes, Claude, Codex or Gemini", (("name",), {"nargs": "?", "default": ""}), text)
    add("demo", "see a full run with no model needed")
    add("install-shortcut", "add Talos to the Start menu and Desktop (Windows), Applications (Mac) or the menu (Linux)")
    add("migrate", "use what OpenCode and Hermes already have")
    add("upgrade", "how to update")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).expanduser().resolve()
    config = config_module.load(cwd, overrides_from(args))
    name = args.command
    if name == "upgrade":
        out("pip install --upgrade git+https://github.com/juw12/talos   (or: git pull, in a checkout)")
        return 0
    if name == "install-shortcut":
        for link in install_shortcut():
            out(f"Created {link}")
        return 0
    if name in ("github", "gitlab"):
        path = cwd / (".github/workflows/talos.yml" if name == "github" else ".gitlab/talos.gitlab-ci.yml")
        util.write_text(path, WORKFLOW if name == "github" else GITLAB)
        out(f"Wrote {path}\nAdd a secret TALOS_CONFIG with the model settings (JSON), then comment "
            "/talos <task> on an issue." if name == "github" else f"Wrote {path}\nInclude it from .gitlab-ci.yml "
            "and start a pipeline with the variable TALOS_PROMPT.")
        return 0
    if name == "parity":
        from . import parity
        if args.write:
            util.write_text(cwd / "PARITY.md", parity.markdown() + "\n")
            out(f"Wrote {cwd / 'PARITY.md'}")
        for source, group in parity.counts().items():
            out(f"{source:9} native {group['native']:3}  bridged {group['bridged']:3}  "
                f"untested {group['untested']:3}  missing {group['missing']:3}")
        return 0
    if name == "doctor":
        for row in doctor_rows(config):
            out(f"{'ok ' if row['ok'] else 'NO '} {row['name']:18} {row['detail']}")
        return 0
    if name == "models":
        out("\n".join(config_module.list_models(config)) or "No models found. Start Ollama or add a provider.")
        return 0
    if name == "engine":
        if not args.name or not args.prompt:
            found = engines.available(config)
            out("\n".join(f"{key:9} {value}" for key, value in found.items()) or "No engines installed.")
            return 0
        out(engines.run(config, args.name, " ".join(args.prompt), str(cwd)))
        return 0
    if name == "auth":
        providers = (util.load_json_file(util.home() / "talos.json", {}) or {}).get("provider") or {}
        if args.action == "set" and len(args.rest) == 2:
            provider, key = args.rest
            path = edit_user_config(lambda data: data.setdefault("provider", {}).setdefault(
                provider, {}).setdefault("options", {}).update(apiKey=key))
            out(f"Saved the key for {provider} in {path}")
        elif args.action == "login":
            if args.rest != ["openrouter"]:
                out("Sign-in in the browser works for OpenRouter: talos auth login openrouter\n"
                    "For the others: talos auth set <provider> <key>")
                return 1
            from . import login
            try:
                key = login.openrouter(out)
            except ValueError as error:
                out(str(error))
                return 1
            path = edit_user_config(lambda data: data.setdefault("provider", {}).setdefault(
                "openrouter", {}).setdefault("options", {}).update(apiKey=key))
            out(f"Saved the key for openrouter (...{key[-4:]}) in {path}\n"
                "Its free models end in :free, for example: talos -m openrouter/openrouter/free")
        elif args.action == "remove" and args.rest:
            edit_user_config(lambda data: data.get("provider", {}).get(args.rest[0], {}).get(
                "options", {}).pop("apiKey", None))
            out("Removed.")
        else:
            for provider, entry in providers.items():
                key = (entry.get("options") or {}).get("apiKey", "")
                out(f"{provider:12} {'key saved (...' + key[-4:] + ')' if key else 'no key'}")
            for provider, (_, variable) in config_module.PRESETS.items():
                if variable and os.environ.get(variable):
                    out(f"{provider:12} from environment variable {variable}")
        return 0
    if name == "mcp":
        if args.action == "add" and len(args.rest) >= 2:
            server, target = args.rest[0], args.rest[1:]
            entry = ({"type": "remote", "url": target[0]} if target[0].startswith("http")
                     else {"type": "local", "command": target})
            path = edit_user_config(lambda data: data.setdefault("mcp", {}).update({server: entry}))
            out(f"Added MCP server {server} to {path}")
        elif args.action == "remove" and args.rest:
            edit_user_config(lambda data: data.get("mcp", {}).pop(args.rest[0], None))
            out("Removed.")
        else:
            from . import mcp
            hub = mcp.Hub(config)
            try:
                found = hub.tools()
                for server in config.get("mcp") or {}:
                    count = sum(1 for tool in found if tool.startswith(server + "_"))
                    out(f"{server:16} {hub.problems.get(server) or str(count) + ' tool(s)'}")
                for skipped in hub.skipped:
                    out(f"  left out: {skipped}")
            finally:
                hub.close()
            if not config.get("mcp"):
                out("No MCP servers. Add one: talos mcp add <name> <command or URL>")
        return 0
    if name == "skills":
        found = skills.discover(cwd)
        if args.action == "trust" and args.rest:
            out("Trusted." if skills.trust(cwd, args.rest[0]) else "No such skill.")
        elif args.action == "show" and args.rest:
            out(skills.load(cwd, args.rest[0]))
        else:
            for skill in found.values():
                state = f"HELD BACK: {skill['blocked']}" if skill["blocked"] else "ok"
                if args.action == "scan" or True:
                    out(f"{skill['name']:28} {skill['origin']:9} {state:12} {util.clip(skill['description'], 70)}")
            out(f"{len(found)} skill(s), {sum(1 for skill in found.values() if skill['blocked'])} held back.")
        return 0
    if name == "memory":
        if args.action == "add" and args.rest:
            out(skills.memory_edit("add", "memory", " ".join(args.rest)))
        else:
            out(skills.memory_prompt() or "Nothing saved yet.")
        return 0
    if name == "agent":
        if args.action == "create" and args.rest:
            path = cwd / ".talos" / "agent" / f"{args.rest[0]}.md"
            description = " ".join(args.rest[1:]) or "Describe when to use this agent."
            util.write_text(path, f"---\ndescription: {description}\nmode: subagent\n---\n\n"
                            "Write this agent's instructions here.\n")
            out(f"Created {path}")
        else:
            for key, entry in plugins.agents(cwd, config).items():
                out(f"{key:12} {entry.get('mode', 'all'):9} {entry.get('description', '')}")
        return 0
    if name == "cron":
        if args.action == "add" and len(args.rest) >= 2:
            out(cron.create(args.rest[0], " ".join(args.rest[1:]), args.name, args.deliver, str(cwd)))
            return 0
        if args.action in ("remove", "pause", "resume") and args.rest:
            out(cron.change(args.action, args.rest[0]))
            return 0
        if args.action == "list":
            out(cron.describe())
            return 0

    store = store_module.Store()
    try:
        if name is None:
            if not sys.stdin.isatty():
                args.prompt, args.agent, args.session, args.json, args.file, args.yes = [], "", "", False, None, False
                return command_run(args, config, store)
            repl.Repl(config, store, model=args.model, overrides=overrides_from(args)).loop()
            return 0
        if name == "run":
            return command_run(args, config, store)
        if name in ("serve", "web", "app"):
            return command_serve(args, config, store, name)
        if name == "demo":
            return command_demo(args, config, store)
        if name == "batch":
            return command_batch(args, config, store)
        if name == "migrate":
            return command_migrate(args, config, store)
        if name == "relay":
            from . import relay
            return relay.command(args, config, store, out, overrides_from(args))
        if name == "gateway":
            from . import gateway
            runner = gateway.Gateway(config, store)
            if not runner.adapters:
                out("No chat platform is set up. Add a \"gateway\" section to " + str(util.home() / "talos.json")
                    + " (see README, Chat platforms).")
                return 1
            out("Gateway running for: " + ", ".join(runner.adapters) + "  (Ctrl+C to stop)")
            try:
                runner.serve()
            except KeyboardInterrupt:
                pass
            return 0
        if name == "cron":
            if args.action == "run" and args.rest:
                out(cron.run_job(config, args.rest[0], store))
            elif args.action == "tick":
                out(f"Ran: {', '.join(cron.tick(config, store=store)) or 'nothing due'}")
            else:
                out("Use: talos cron list | add \"<schedule>\" <prompt> | remove|pause|resume|run <id> | tick")
            return 0
        if name == "sessions":
            if args.action == "delete" and args.rest:
                found = store.find_session(args.rest[0])
                if found:
                    store.delete_session(found)
                out(f"Deleted {found}." if found else "No such session.")
            elif args.action == "search" and args.rest:
                for hit in store.search(" ".join(args.rest), 12):
                    out(f"{hit['when']} {hit['session']} {hit['title']}: {hit['hit']}")
            else:
                for item in store.sessions(40):
                    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["updated"]))
                    out(f"{item['id']}  {when}  {item['source']:8} {item['title'] or '(no title)'}")
            return 0
        if name in ("export", "share"):
            session = store.find_session(args.session)
            if not session:
                out("No such session.")
                return 1
            suffix = "json" if name == "export" else "html"
            content = (json.dumps(store.export(session), indent=2, ensure_ascii=False) if name == "export"
                       else store.share_html(session))
            if name == "export" and not args.output:
                out(content)
                return 0
            path = Path(args.output or f"{session}.{suffix}")
            util.write_text(path, content)
            out(f"Saved {path.resolve()}")
            if name == "share" and args.gist:
                if not shutil.which("gh"):
                    out("The GitHub CLI (gh) is needed for --gist.")
                    return 1
                done = subprocess.run(["gh", "gist", "create", str(path)], capture_output=True, text=True)
                out(done.stdout.strip() or done.stderr.strip())
            return 0
        if name == "import":
            out("Imported as " + store.import_(json.loads(util.read_text(args.file))))
            return 0
        if name == "stats":
            stats = store.stats()
            out(f"{stats['sessions']} session(s), {stats['messages']} message(s), "
                f"{stats['tokens_in']} tokens in, {stats['tokens_out']} out, cost {stats['cost']:.4f}")
            for row in stats["by_model"]:
                out(f"  {row['model'] or '(none)':32} {row['sessions']} session(s)")
            return 0
        if name == "tune":
            from . import tune
            outcome = tune.run(config, store, args.models or None, out)
            for row in outcome["results"]:
                out(f"{row['model']:36} {row['passed']}/{row['of']} jobs  {row['seconds']} s")
            out(f"Recommended: {outcome['recommended'] or 'none passed'}")
            if args.apply:
                out(tune.apply(outcome))
            return 0
        if name == "acp":
            from . import acp
            acp.Acp(store, overrides=overrides_from(args)).serve()
            return 0
    except ValueError as error:
        out(str(error))
        return 1
    finally:
        store.close()
    return 0
