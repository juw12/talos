"""Small helpers shared by every module. Standard library only."""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__


def home() -> Path:
    """Folder for Talos's own data. TALOS_HOME overrides it (tests use this)."""
    return Path(os.environ.get("TALOS_HOME") or Path.home() / ".talos")


def user_home() -> Path:
    """The user's home, where OpenCode and Hermes keep their files."""
    return Path(os.environ.get("TALOS_USER_HOME") or Path.home())


def install_folders() -> list:
    """Where command-line tools put themselves. An app opened from the Desktop, the Dock or a
    scheduled job does not get the PATH a terminal has, so PATH alone misses them."""
    base = user_home()
    folders = [base / ".opencode" / "bin", base / ".local" / "bin", base / ".bun" / "bin",
               base / ".npm-global" / "bin", base / ".cargo" / "bin", base / "bin"]
    if os.name == "nt":
        for name, tail in (("APPDATA", "npm"), ("LOCALAPPDATA", "Programs")):
            if os.environ.get(name):
                folders.append(os.path.join(os.environ[name], tail))
    else:
        folders += ["/opt/homebrew/bin", "/usr/local/bin"]
    return [str(folder) for folder in folders]


def find_program(program: str):
    """Full path of a program: on PATH first, then in the usual install folders. None if absent."""
    program = str(program)
    return shutil.which(program) or shutil.which(program, path=os.pathsep.join(install_folders()))


def hide_child_windows() -> bool:
    """Windows, started without a console (the app shortcut): the programs Talos starts
    (commands, MCP servers, gh) must not each open a black console window."""
    if os.name != "nt":
        return False
    import ctypes
    import subprocess
    if ctypes.windll.kernel32.GetConsoleWindow() or getattr(subprocess.Popen, "_talos_quiet", False):
        return False
    original = subprocess.Popen.__init__

    def quiet(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = quiet
    subprocess.Popen._talos_quiet = True
    return True


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def clip(text: str, limit: int) -> str:
    """Keep the head and the tail of long output so the model sees both ends."""
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n... [{dropped} characters left out] ...\n{text[-half:]}"


# ---------------------------------------------------------------- JSON with comments

def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, leaving strings alone."""
    out = []
    i = 0
    in_string = False
    while i < len(text):
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(char)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_json_file(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(strip_jsonc(read_text(path)))
    except (json.JSONDecodeError, OSError):
        return default


def save_json_file(path, data) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- a small YAML subset
# Enough for SKILL.md / agent / command front matter and Hermes' config.yaml:
# nested maps, lists of scalars or maps, inline [a, b], block scalars (| and >).

def _scalar(text: str):
    text = text.strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1] == text[0] and len(text) >= 2:
        return text[1:-1]
    if text[0] == "[" and text[-1] == "]":
        inner = text[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    if text[0] == "{" and text[-1] == "}":
        result = {}
        for part in text[1:-1].split(","):
            if ":" in part:
                key, value = part.split(":", 1)
                result[key.strip().strip("\"'")] = _scalar(value)
        return result
    text = re.sub(r"\s+#.*$", "", text)
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _split_key(text: str):
    """Return (key, rest) for 'key: rest', or None when the text is not a map entry."""
    if text[:1] in "\"'[{":
        return None
    match = re.match(r"^([^:#]+?):(\s+(.*))?$", text)
    if not match:
        return None
    return match.group(1).strip(), (match.group(3) or "").strip()


def _yaml_block(lines, index, indent):
    if lines[index][1].startswith("- ") or lines[index][1] == "-":
        items = []
        while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("-"):
            item = lines[index][1][1:].strip()
            if not item:
                value, index = _yaml_block(lines, index + 1, lines[index + 1][0])
                items.append(value)
            elif _split_key(item):
                child_indent = indent + 2
                lines[index] = (child_indent, item)
                value, index = _yaml_block(lines, index, child_indent)
                items.append(value)
            else:
                items.append(_scalar(item))
                index += 1
        return items, index
    result = {}
    while index < len(lines) and lines[index][0] == indent:
        pair = _split_key(lines[index][1])
        if not pair:
            index += 1
            continue
        key, rest = pair
        index += 1
        has_child = index < len(lines) and (
            lines[index][0] > indent
            or (lines[index][0] == indent and lines[index][1].startswith("- ") and not rest)
        )
        if rest in ("|", ">", "|-", ">-"):
            parts = []
            while index < len(lines) and lines[index][0] > indent:
                parts.append(lines[index][1])
                index += 1
            result[key] = ("\n" if rest[0] == "|" else " ").join(parts)
        elif not rest and has_child:
            result[key], index = _yaml_block(lines, index, lines[index][0])
        else:
            result[key] = _scalar(rest) if rest else None
    return result, index


def parse_yaml(text: str):
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))
    if not lines:
        return {}
    value, _ = _yaml_block(lines, 0, lines[0][0])
    return value


def front_matter(text: str):
    """Split a Markdown file into (meta dict, body)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta = parse_yaml(text[3:end])
    body = text[end + 4:].lstrip("\r\n")
    return (meta if isinstance(meta, dict) else {}), body


# ---------------------------------------------------------------- secrets

_SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{30,}",
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b",
    r"Bearer\s+[A-Za-z0-9._\-]{20,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
]
_SECRET_RE = re.compile("|".join(_SECRET_PATTERNS), re.S)
_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd)(\"?\s*[:=]\s*[\"']?)([^\s\"',}]{12,})"
)


def redact(text: str) -> str:
    """Blank out things that look like keys or passwords. Used before anything leaves the PC."""
    text = _SECRET_RE.sub("[redacted]", text)
    return _ASSIGN_RE.sub(lambda match: match.group(1) + match.group(2) + "[redacted]", text)


# ---------------------------------------------------------------- HTTP

# Sent with every request. Without a name of its own Python says "Python-urllib", which the
# Cloudflare in front of many model providers turns away (HTTP 403, error code 1010).
USER_AGENT = "talos/" + __version__

# Where the trusted certificates are when Python itself has none. Python from python.org on a Mac
# is the usual case: it ships an empty list until "Install Certificates.command" is run.
CA_BUNDLES = (
    "/etc/ssl/cert.pem",                        # macOS, Alpine, the BSDs
    "/etc/ssl/certs/ca-certificates.crt",       # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",         # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",                   # openSUSE
)
_tls = None


def tls_context():
    """The context for every outgoing TLS connection. Certificates are always checked.

    When this Python knows no certificate authority at all, the system's own list is loaded, so
    HTTPS works on a Mac with a fresh python.org Python instead of failing every call.
    """
    global _tls
    if _tls is None:
        context = ssl.create_default_context()
        if not context.cert_store_stats().get("x509_ca"):
            for bundle in CA_BUNDLES:
                if os.path.isfile(bundle):
                    try:
                        context.load_verify_locations(cafile=bundle)
                        break
                    except (OSError, ssl.SSLError):
                        continue
        _tls = context
    return _tls


def _opener(url: str):
    host = url.split("/")[2].split(":")[0] if "://" in url else ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if url.lower().startswith("https:"):
        return urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()))
    return urllib.request.build_opener()


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def explain_http_error(status: int, body: str) -> str:
    """One readable line for a failed call, instead of a page of HTML or raw JSON."""
    body = (body or "").strip()
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error", data)
        message = error.get("message") if isinstance(error, dict) else error
        message = message or data.get("message") or data.get("detail")
        if message:
            return f"HTTP {status}: {clip(str(message), 400)}"
    if re.search(r"error code:? *1010", body, re.I):
        return (f"HTTP {status}: the provider's Cloudflare turned this program away "
                "(error 1010, it did not accept the User-Agent)")
    if "<html" in body[:2000].lower() or body.lower().startswith("<!doctype"):
        title = _TITLE_RE.search(body)
        page = re.sub(r"\s+", " ", title.group(1)).strip() if title else "an HTML page"
        return f"HTTP {status}: the server answered with a web page, not an API answer ({clip(page, 160)})"
    return f"HTTP {status}: {clip(body, 400)}" if body else f"HTTP {status}"


def http(method: str, url: str, body=None, headers=None, timeout: float = 30):
    """One HTTP call. Returns (status, text). Local addresses never go through a proxy."""
    data = None
    headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    headers.setdefault("User-Agent", USER_AGENT)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def http_json(method: str, url: str, body=None, headers=None, timeout: float = 30):
    status, text = http(method, url, body, headers, timeout)
    try:
        return status, json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return status, {"raw": text}
