"""One config, read from Talos's own file plus what OpenCode and Hermes already have.

Order, later wins: built-in defaults, Hermes (~/.hermes/config.yaml), OpenCode (global, then
project opencode.json), Talos (~/.talos/talos.json, then project talos.json), then overrides.
Key names follow OpenCode's config so an existing opencode.json works unchanged.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

from . import util

# provider name -> (base URL of an OpenAI-compatible API, environment variable with the key)
PRESETS = {
    "ollama": ("http://127.0.0.1:11434/v1", ""),
    "lmstudio": ("http://127.0.0.1:1234/v1", ""),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "opencode": ("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "nous": ("https://inference-api.nousresearch.com/v1", "NOUS_API_KEY"),
}

DEFAULTS = {
    "model": "",
    "small_model": "",
    "provider": {},
    "agent": {},
    "command": {},
    "mcp": {},
    "permission": {},
    "instructions": [],
    "lsp": {},
    "formatter": {},
    "tool_mode": "auto",       # auto | native | text  (text = for models without tool calling)
    "proof": "nudge",          # off | nudge | require
    "learn": "auto",           # auto | off  (save skills and memory after long runs)
    "budget": {"steps": 60, "tokens": 0, "cost": 0},
    "backend": {"type": "local"},
    "gateway": {},
    "engines": {},
    "search": {"url": "https://html.duckduckgo.com/html/"},
    "image": {},
    "theme": "paper",
    "use": {"opencode": True, "hermes": True},   # read these tools' config files in place
    "personality": "",
}


def _merge(base: dict, extra: dict) -> dict:
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _substitute(value, folder: Path):
    """OpenCode-style {env:NAME} and {file:path} inside config strings."""
    if isinstance(value, dict):
        return {key: _substitute(item, folder) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, folder) for item in value]
    if not isinstance(value, str) or "{" not in value:
        return value

    def from_env(match):
        return os.environ.get(match.group(1), "")

    def from_file(match):
        path = Path(os.path.expanduser(match.group(1)))
        if not path.is_absolute():
            path = folder / path
        return util.read_text(path).strip() if path.exists() else ""

    value = re.sub(r"\{env:([^}]+)\}", from_env, value)
    return re.sub(r"\{file:([^}]+)\}", from_file, value)


def _from_hermes(data: dict) -> dict:
    """Map the parts of Hermes' config.yaml that have a direct meaning here."""
    result = {}
    model = data.get("model")
    if isinstance(model, dict):
        name = model.get("default") or model.get("name") or ""
        provider = model.get("provider") or ""
        base_url = model.get("base_url") or ""
        if base_url:
            provider = provider or "hermes"
            result["provider"] = {provider: {"options": {"baseURL": base_url}}}
        if name:
            result["model"] = f"{provider}/{name}" if provider and provider != "auto" else name
    elif isinstance(model, str) and model:
        result["model"] = model
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        result["mcp"] = {}
        for name, server in servers.items():
            if not isinstance(server, dict):
                continue
            if server.get("url"):
                result["mcp"][name] = {
                    "type": "remote", "url": server["url"], "headers": server.get("headers") or {},
                }
            elif server.get("command"):
                result["mcp"][name] = {
                    "type": "local",
                    "command": [server["command"]] + list(server.get("args") or []),
                    "environment": server.get("env") or {},
                }
    return result


def sources(cwd) -> list:
    """Config files in load order, as (label, path, loader)."""
    cwd = Path(cwd)
    user = util.user_home()
    opencode_dir = user / ".config" / "opencode"
    return [
        ("hermes", user / ".hermes" / "config.yaml"),
        ("opencode", opencode_dir / "opencode.json"),
        ("opencode", opencode_dir / "opencode.jsonc"),
        ("opencode", cwd / "opencode.json"),
        ("opencode", cwd / "opencode.jsonc"),
        ("talos", util.home() / "talos.json"),
        ("talos", cwd / "talos.json"),
        ("talos", cwd / ".talos" / "talos.json"),
    ]


def load(cwd=".", overrides=None) -> dict:
    config = copy.deepcopy(DEFAULTS)
    config["_sources"] = []
    use = {"opencode": True, "hermes": True}
    for label, path in sources(cwd):        # Talos's own files say which other setups to read
        if label == "talos" and path.exists():
            use.update((util.load_json_file(path, {}) or {}).get("use") or {})
    use.update((overrides or {}).get("use") or {})
    for label, path in sources(cwd):
        if not path.exists() or use.get(label) is False:
            continue
        if label == "hermes":
            data = _from_hermes(util.parse_yaml(util.read_text(path)) or {})
        else:
            data = util.load_json_file(path, {}) or {}
        if not isinstance(data, dict):
            continue
        _merge(config, _substitute(data, path.parent))
        config["_sources"].append(str(path))
    _merge(config, overrides or {})
    config["_cwd"] = str(Path(cwd).resolve())
    return config


def resolve_model(config: dict, name: str = "") -> dict:
    """Turn 'provider/model' into what the client needs. Raises ValueError with a plain reason."""
    name = name or config.get("model") or ""
    if not name:
        raise ValueError(
            "No model is set. Start Ollama, or put \"model\": \"provider/model\" in "
            f"{util.home() / 'talos.json'}. Run 'talos doctor' to see what was found."
        )
    provider_name, _, model_id = name.partition("/")
    if not model_id:
        provider_name, model_id = "ollama", name
    provider = (config.get("provider") or {}).get(provider_name) or {}
    options = provider.get("options") or {}
    preset_url, preset_env = PRESETS.get(provider_name, ("", ""))
    base_url = (options.get("baseURL") or preset_url).rstrip("/")
    if not base_url:
        raise ValueError(f"Provider '{provider_name}' has no baseURL in the config.")
    api_key = options.get("apiKey") or (os.environ.get(preset_env, "") if preset_env else "")
    info = (provider.get("models") or {}).get(model_id) or {}
    return {
        "name": f"{provider_name}/{model_id}",
        "id": info.get("id") or model_id,
        "base_url": base_url,
        "api_key": api_key,
        "headers": options.get("headers") or {},
        "context": int((info.get("limit") or {}).get("context") or 0),
        "cost": info.get("cost") or {},
        "tool_mode": info.get("tool_mode") or config.get("tool_mode", "auto"),
    }


_CONTEXT = {}


def _ollama_context(base_url: str, model_id: str) -> int:
    """What a local Ollama will really give this model: loaded size, its own setting, or the default."""
    address = base_url[:-3] if base_url.endswith("/v1") else base_url
    try:
        status, data = util.http_json("GET", address + "/api/ps", timeout=2)
        for item in (data.get("models") or []) if status == 200 else []:
            if model_id in (item.get("name"), item.get("model")) and item.get("context_length"):
                return int(item["context_length"])
        status, data = util.http_json("POST", address + "/api/show", {"model": model_id}, timeout=3)
        found = re.search(r"num_ctx\s+(\d+)", str(data.get("parameters") or "")) if status == 200 else None
        if found:
            return int(found.group(1))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return int(os.environ.get("OLLAMA_CONTEXT_LENGTH") or 4096)
    except ValueError:
        return 4096


def context_of(config: dict, model: dict) -> int:
    """How many tokens fit in this model's window: the config's number, else what Ollama reports."""
    if model.get("context"):
        return int(model["context"])
    if config.get("context"):
        return int(config["context"])
    if model["name"] not in _CONTEXT:
        local = model["name"].startswith("ollama/") or ":11434" in model["base_url"]
        _CONTEXT[model["name"]] = _ollama_context(model["base_url"], model["id"]) if local else 128000
    return _CONTEXT[model["name"]]


def list_models(config: dict) -> list:
    """The chosen model, every model named in the config, plus what a running Ollama offers."""
    names = [config["model"]] if config.get("model") else []
    for provider_name, provider in (config.get("provider") or {}).items():
        for model_id in (provider.get("models") or {}):
            names.append(f"{provider_name}/{model_id}")
    ollama = ((config.get("provider") or {}).get("ollama") or {}).get("options") or {}
    address = (ollama.get("baseURL") or PRESETS["ollama"][0]).rstrip("/")
    address = address[:-3] if address.endswith("/v1") else address
    try:
        status, data = util.http_json("GET", address + "/api/tags", timeout=2)
        if status == 200:
            names += [f"ollama/{item['name']}" for item in data.get("models", [])]
    except OSError:
        pass
    return sorted(set(names))


def pick_default_model(config: dict) -> str:
    """The configured model, else the first local Ollama model, else ''."""
    if config.get("model"):
        return config["model"]
    local = [name for name in list_models(config) if name.startswith("ollama/")]
    return local[0] if local else ""
