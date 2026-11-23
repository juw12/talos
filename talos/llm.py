"""Talks to any OpenAI-compatible chat API, with or without tool calling.

Models that cannot call tools (many small local ones) get the tools described in plain text
and answer with <tool>{"name": ..., "arguments": {...}}</tool> blocks (a ```tool fence or
Hermes-style <tool_call> tags are understood too). The agent loop never
sees the difference.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from . import util

# what models actually write: <tool>{..}</tool>, <tool_call>{..}</tool_call> (Hermes-style
# models), or the JSON inside a ```tool / ```json fence. Small models mix the two: gemma3:1b
# wrote ```tool\n{..}</tool>\n``` in most rounds on the ASUS (2026-11-23), and once ```toolring, so a
# stray tag inside a fence is allowed and any fence label that starts with 'tool' is read.
TOOL_BLOCK = re.compile(
    r"<tool(?:_call)?>\s*(\{.*?\})\s*</tool(?:_call)?>"
    r"|```(?:tool\w*|json)?[ \t]*\n\s*(?:<tool(?:_call)?>\s*)?(\{\s*\"name\"\s*:.*?\})\s*"
    r"(?:</tool(?:_call)?>\s*)?```", re.S)

TEXT_TOOL_GUIDE = """
# Tools

You can act by calling tools. To call one, write a block exactly like this and then stop:

<tool>{"name": "TOOL_NAME", "arguments": {"key": "value"}}</tool>

Rules: valid JSON on one or more lines, several blocks allowed, no text after the last block.
The result comes back in a <tool_result> message. When the work is finished, answer in plain
text with no tool block.

Available tools:
"""


class ModelError(Exception):
    pass


def estimate_tokens(messages) -> int:
    return sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) // 4


def _describe_tools(tools) -> str:
    lines = []
    for tool in tools:
        spec = tool["function"]
        properties = spec.get("parameters", {}).get("properties", {})
        required = set(spec.get("parameters", {}).get("required", []))
        arguments = ", ".join(
            f"{name}{'' if name in required else '?'}: {info.get('type', 'any')}"
            for name, info in properties.items()
        )
        lines.append(f"- {spec['name']}({arguments}): {spec.get('description', '')}")
    return "\n".join(lines)


def to_text_protocol(messages, tools):
    """Rewrite a native tool conversation as plain text turns."""
    result = []
    for message in messages:
        role = message["role"]
        content = message.get("content") or ""
        if role == "system":
            if tools:
                content = content + TEXT_TOOL_GUIDE + _describe_tools(tools)
            result.append({"role": "system", "content": content})
        elif role == "assistant":
            text = content if isinstance(content, str) else ""
            for call in message.get("tool_calls") or []:
                block = {"name": call["name"], "arguments": call["arguments"]}
                text += f"\n<tool>{json.dumps(block, ensure_ascii=False)}</tool>"
            result.append({"role": "assistant", "content": text.strip() or "(no text)"})
        elif role == "tool":
            text = f"<tool_result name=\"{message.get('name', '')}\">\n{content}\n</tool_result>"
            if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], str):
                result[-1]["content"] += "\n" + text
            else:
                result.append({"role": "user", "content": text})
        else:
            result.append({"role": "user", "content": content})
    return result


def to_native(messages):
    """Our stored shape -> the OpenAI wire shape."""
    result = []
    for message in messages:
        item = {"role": message["role"], "content": message.get("content") or ""}
        if message["role"] == "assistant" and message.get("tool_calls"):
            item["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    },
                }
                for call in message["tool_calls"]
            ]
        if message["role"] == "tool":
            item["tool_call_id"] = message.get("tool_call_id", "")
        result.append(item)
    return result


def parse_text_calls(text: str):
    """Pull <tool> blocks out of a text answer. Returns (remaining text, calls)."""
    calls, used = [], []
    for index, match in enumerate(TOOL_BLOCK.finditer(text)):
        fenced = match.group(1) is None
        try:
            block = json.loads(match.group(1) or match.group(2), strict=False)   # real line breaks in strings are fine
        except json.JSONDecodeError:
            if not fenced:
                used.append(match.span())
            continue
        arguments = block.get("arguments") if isinstance(block, dict) else None
        if fenced and not isinstance(arguments, dict):
            continue                                    # an ordinary JSON example, not a call
        used.append(match.span())
        if block.get("name"):
            calls.append({
                "id": f"call_{int(time.time() * 1000)}_{index}",
                "name": str(block["name"]),
                "arguments": arguments if isinstance(arguments, dict) else {},
            })
    for begin, end in reversed(used):
        text = text[:begin] + text[end:]
    return text.strip(), calls


class LLM:
    def __init__(self, config: dict):
        self.config = config
        self.text_only = set()      # models found to reject native tool calls

    # one HTTP round trip, streaming when the server streams
    def _post(self, model, payload, on_text, abort):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   "User-Agent": util.USER_AGENT}
        headers.update(model.get("headers") or {})
        if model.get("api_key"):
            headers["Authorization"] = "Bearer " + model["api_key"]
        url = model["base_url"] + "/chat/completions"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        opener = util._opener(url)
        with opener.open(request, timeout=float(self.config.get("timeout", 600))) as response:
            content_type = response.headers.get("Content-Type", "")
            if "event-stream" not in content_type:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                text = message.get("content") or ""
                if text and on_text:
                    on_text(text)
                calls = [
                    {
                        "id": call.get("id") or f"call_{index}",
                        "name": call["function"]["name"],
                        "arguments_text": call["function"].get("arguments") or "{}",
                    }
                    for index, call in enumerate(message.get("tool_calls") or [])
                ]
                return text, calls, data.get("usage") or {}
            text_parts, calls, usage = [], {}, {}
            for raw in response:
                if abort is not None and abort.is_set():
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk_text = line[5:].strip()
                if chunk_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_text)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                        if on_text:
                            on_text(delta["content"])
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(
                            call.get("index", 0), {"id": "", "name": "", "arguments_text": ""}
                        )
                        slot["id"] = call.get("id") or slot["id"]
                        function = call.get("function") or {}
                        slot["name"] = function.get("name") or slot["name"]
                        slot["arguments_text"] += function.get("arguments") or ""
            return "".join(text_parts), [calls[key] for key in sorted(calls)], usage

    def chat(self, model: dict, messages, tools=None, on_text=None, abort=None, temperature=None):
        """Returns {"content", "tool_calls", "usage": {"in", "out"}}."""
        mode = model.get("tool_mode", "auto")
        use_text = bool(tools) and (mode == "text" or model["name"] in self.text_only)
        last_error = ""
        for attempt in range(4):
            payload = {"model": model["id"], "stream": True}
            if temperature is not None:
                payload["temperature"] = temperature
            if use_text or not tools:
                payload["messages"] = to_text_protocol(messages, tools if use_text else None)
            else:
                payload["messages"] = to_native(messages)
                payload["tools"] = tools
            try:
                # buffer text in text mode so <tool> blocks are not shown to the user
                text, raw_calls, usage = self._post(
                    model, payload, None if use_text else on_text, abort
                )
            except urllib.error.HTTPError as error:
                body = error.read(65536).decode("utf-8", errors="replace")    # enough to say why, never megabytes
                last_error = util.explain_http_error(error.code, body)
                if error.code == 400 and tools and not use_text and mode == "auto":
                    self.text_only.add(model["name"])     # the model has no tool calling
                    use_text = True
                    continue
                if error.code in (408, 429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(min(2 ** attempt, 8) * float(self.config.get("retry_wait", 1)))
                    continue
                raise ModelError(f"{model['name']}: {last_error}")
            except (urllib.error.URLError, OSError) as error:
                last_error = str(getattr(error, "reason", error))
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 8) * float(self.config.get("retry_wait", 1)))
                    continue
                raise ModelError(f"{model['name']}: cannot reach {model['base_url']} ({last_error})")
            calls = []
            for index, call in enumerate(raw_calls):
                try:
                    arguments = json.loads(call["arguments_text"] or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": call["arguments_text"]}
                calls.append({
                    "id": call["id"] or f"call_{int(time.time() * 1000)}_{index}",
                    "name": call["name"],
                    "arguments": arguments if isinstance(arguments, dict) else {},
                })
            # a call written as text: <tool>, <tool_call>, a stray </tool>, or a ```tool fence (gemma3:1b
            # wrote only fences, and they were never read because this looked for "<tool>" alone)
            if tools and not calls and ("<tool" in text or "</tool" in text or "```" in text):
                text, calls = parse_text_calls(text)
            if use_text and on_text and text:
                on_text(text)
            return {
                "content": text,
                "tool_calls": calls,
                "usage": {
                    "in": int(usage.get("prompt_tokens") or estimate_tokens(payload["messages"])),
                    "out": int(usage.get("completion_tokens") or len(text) // 4),
                },
            }
        raise ModelError(f"{model['name']}: {last_error or 'no answer'}")


def cost_of(model: dict, usage: dict) -> float:
    """Money for one answer, from the per-million prices in the config (0 when not given)."""
    price = model.get("cost") or {}
    return (
        usage.get("in", 0) * float(price.get("input", 0))
        + usage.get("out", 0) * float(price.get("output", 0))
    ) / 1_000_000
