"""Glow's free AI as a Talos model: services that answer with no key and no account.

They are the ones built into Glow (juw12.github.io/projects/glow). Each one counts requests per
network (everyone behind the same router shares the count) and allows very few, so one service
alone cannot carry an agent. A `glow/...` model is therefore a list of routes: when one says "too
many" or does not answer, the same request goes to the next one at once, and the busy one is left
alone for as long as it asks. Your prompts go to these services; they are not private like a
local model. Expect it to be slow: it is the spare wheel, not the engine.

Measured on 2026-11-25 from an office network:
    OVHcloud       one request, then "come back in 36 days" for every model at once
    LLM7           one or two answers, then "free allowance reached", back in 20 s to 24 h
    Pollinations   one answer every 45 s, steady (this is "Glow Free" in Glow)

    glow/auto       every route, best first
    glow/deepseek   only DeepSeek (OVHcloud)
    glow/qwen       only Qwen (OVHcloud)

Your own list, in talos.json:  "provider": {"glow": {"routes": [["https://host/v1", "model-name"]]}}
or with more said:  {"baseURL": "https://host/v1", "model": "model-name", "apiKey": "", "every": 46}
"""
from __future__ import annotations

OVH = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
LLM7 = "https://api.llm7.io/v1"
POLLINATIONS = "https://text.pollinations.ai/openai"

# best first: (family, base URL of an OpenAI-compatible API, model name there, seconds between two
# answers when the service says so). Checked 2026-11-25.
ROUTES = [
    ("deepseek", OVH, "DeepSeek-V4.1-Flash-1017"),
    ("qwen", OVH, "Qwen3.8-27B"),
    ("qwen", OVH, "Qwen3.5-397B-A17B"),
    ("other", OVH, "gpt-oss-120b"),
    ("other", OVH, "Meta-Llama-3_3-70B-Instruct"),
    ("other", OVH, "Mistral-Small-3.2-24B-Instruct-2506"),
    ("other", LLM7, "default"),
    ("other", POLLINATIONS, "openai", 46),     # one answer in 45 s; a request sent sooner is held, then dropped
]
NAMES = ("auto", "deepseek", "qwen")
CONTEXT = 64000     # tokens Talos plans with; the smallest of these models takes more


def _route(base_url: str, model: str, api_key: str = "", every: float = 0) -> dict:
    base_url = str(base_url).rstrip("/")
    host = base_url.split("//")[-1].split("/")[0]
    return {"name": f"glow/{model}@{host}", "id": str(model), "base_url": base_url, "api_key": api_key,
            "every": float(every or 0)}


def routes(config: dict, name: str) -> list:
    """The routes of glow/<name>, best first. Empty when there is no such name."""
    own = (((config.get("provider") or {}).get("glow") or {}).get("routes")) or []
    if own:
        found = []
        for item in own:
            if isinstance(item, dict) and item.get("baseURL") and item.get("model"):
                found.append(_route(item["baseURL"], item["model"], str(item.get("apiKey") or ""), item.get("every") or 0))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                found.append(_route(item[0], item[1], str(item[2]) if len(item) > 2 else ""))
        return found if name == "auto" else [route for route in found if name.lower() in route["id"].lower()]
    if name not in NAMES:
        return []
    return [_route(item[1], item[2], "", item[3] if len(item) > 3 else 0) for item in ROUTES if name in ("auto", item[0])]
