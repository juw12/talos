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


class Busy(ModelError):
    """The server cannot answer now: too many requests, an error on its side, or no connection.
    wait: the seconds it asked for, 0 when it did not say."""

    def __init__(self, text, wait=0.0):
        super().__init__(text)
        self.wait = float(wait or 0)


RESTING = {}     # route name, or "@host" for every route of a server -> until when it is left alone
STRIKES = {}     # route name -> how many times in a row it was busy
REST_FILE = "glow.json"      # long rests are kept here, so the next run does not ask again
LONG_REST = 600              # a server that asks for more than this is resting as a whole
LONGEST_REST = 6 * 3600      # ask again after this at the latest: the limit counts per network, and that can change
_rests_read = [""]


def stated_wait(headers, body: str) -> float:
    """The seconds a busy server asks us to wait, from Retry-After or from its message. 0: not said."""
    value = str((headers or {}).get("Retry-After") or "").strip()
    if value.isdigit():
        return float(value)
    found = (re.search(r'"retry_after"\s*:\s*(\d+)', body or "")
             or re.search(r"retry\s+(?:in|after)\s+(\d+)\s*s", body or "", re.I))
    return float(found.group(1)) if found else 0.0


def spoken_time(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def host_of(route: dict) -> str:
    return "@" + route["base_url"].split("//")[-1].split("/")[0]


def read_rests() -> None:
    """Once for each Talos folder: the long rests an earlier run wrote down."""
    path = util.home() / REST_FILE
    if _rests_read[0] == str(path):
        return
    _rests_read[0] = str(path)
    for name, until in (util.load_json_file(path, {}) or {}).items():
        if isinstance(until, (int, float)) and until > time.time():
            RESTING[name] = max(RESTING.get(name, 0), float(until))


def rest(route: dict, seconds: float, whole_server=False) -> None:
    seconds = min(seconds, LONGEST_REST)
    RESTING[host_of(route) if whole_server else route["name"]] = time.time() + seconds
    if seconds > LONG_REST:
        long = {name: until for name, until in RESTING.items() if until > time.time() + LONG_REST}
        try:
            util.save_json_file(util.home() / REST_FILE, long)
        except OSError:
            pass


def rests_until(route: dict) -> float:
    return max(RESTING.get(route["name"], 0), RESTING.get(host_of(route), 0))


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
        self.notice = None          # function(text): told when the free services make us wait

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

    def chat(self, model: dict, messages, tools=None, on_text=None, abort=None, temperature=None, spare=False):
        """Returns {"content", "tool_calls", "usage": {"in", "out"}}.

        spare: another model stands by, so a server that cannot be reached is not waited for long."""
        if model.get("routes"):
            return self._chat_routes(model, messages, tools, on_text, abort, temperature)
        return self._chat_one(model, messages, tools, on_text, abort, temperature, quick=spare)

    def _chat_routes(self, model, messages, tools, on_text, abort, temperature):
        """A model that is several free services (see glow.py): the first one that is not resting
        answers. A busy one rests as long as it asks for (a minute when it does not say, longer each
        time in a row); one that refuses for another reason (the model is gone, a key is needed now)
        rests an hour. When all rest, Talos waits for the first one, at most `glow_wait` seconds."""
        read_rests()
        deadline = time.time() + float(self.config.get("glow_wait", 300))
        last_error, told = "", False
        while True:
            for route in model["routes"]:
                if abort is not None and abort.is_set():
                    raise ModelError(f"{model['name']}: stopped")
                name = route["name"]
                if rests_until(route) > time.time():
                    continue
                one = {key: value for key, value in model.items() if key != "routes"}
                one.update(name=name, id=route["id"], base_url=route["base_url"], api_key=route.get("api_key") or "")
                try:
                    reply = self._chat_one(one, messages, tools, on_text, abort, temperature, patient=False)
                except Busy as error:
                    STRIKES[name] = STRIKES.get(name, 0) + 1
                    if error.wait:
                        rest(route, error.wait + 1, whole_server=error.wait > LONG_REST)
                    else:
                        rest(route, min(60 * 2 ** (STRIKES[name] - 1), 600))
                    last_error = str(error)
                    continue
                except ModelError as error:
                    rest(route, 3600)
                    last_error = str(error)
                    continue
                STRIKES.pop(name, None)
                if route.get("every"):      # it answers once in so many seconds: asking sooner only loses time
                    RESTING[name] = time.time() + float(route["every"])
                reply["route"] = name
                return reply
            soonest = min(rests_until(route) for route in model["routes"])
            if soonest > deadline:
                raise ModelError(f"{model['name']}: every free service is busy or refuses. The next one is free again "
                                 f"in {spoken_time(soonest - time.time())}. The last one said: {last_error}")
            if not told and soonest - time.time() > 5 and self.notice:
                told = True
                self.notice(f"The free services are busy. The next one answers in about {int(soonest - time.time()) + 1} s."
                            + (f" ({util.clip(last_error, 200)})" if last_error else ""))
            time.sleep(min(max(soonest - time.time(), 0.05), 2))

    def _chat_one(self, model, messages, tools, on_text, abort, temperature, patient=True, quick=False):
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
                if not patient and error.code in (402, 408, 429, 500, 502, 503, 504):      # 402: a free tier's limit
                    raise Busy(f"{model['name']}: {last_error}", stated_wait(error.headers, body))
                if error.code in (408, 429, 500, 502, 503, 504):
                    if attempt < (1 if quick else 3):      # quick: one more try, then the spare model has its turn
                        time.sleep(min(2 ** attempt, 8) * float(self.config.get("retry_wait", 1)))
                        continue
                raise ModelError(f"{model['name']}: {last_error}")
            except (urllib.error.URLError, OSError) as error:
                last_error = str(getattr(error, "reason", error))
                if not patient:
                    raise Busy(f"{model['name']}: cannot reach {model['base_url']} ({last_error})")
                if attempt < (1 if quick else 3):      # quick: one more try after a second, then the spare model
                    time.sleep(1 if quick else min(2 ** attempt, 8) * float(self.config.get("retry_wait", 1)))
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
