import json
import os

from talos import config
from tests.base import Case


class ConfigTest(Case):
    def test_model_resolution(self):
        loaded = config.load(self.work, {
            "model": "shop/big",
            "provider": {"shop": {"options": {"baseURL": "http://127.0.0.1:9/v1/", "apiKey": "k1", "headers": {"X-A": "1"}},
                                  "models": {"big": {"id": "big-2025", "limit": {"context": 64000},
                                                     "cost": {"input": 1, "output": 2}}}}},
        })
        model = config.resolve_model(loaded)
        self.assertEqual((model["name"], model["id"], model["base_url"], model["api_key"], model["context"]),
                         ("shop/big", "big-2025", "http://127.0.0.1:9/v1", "k1", 64000))
        self.assertEqual(config.resolve_model(loaded, "gemma3:12b")["base_url"], "http://127.0.0.1:11434/v1")
        os.environ["OPENROUTER_API_KEY"] = "from-env"
        try:
            router = config.resolve_model(loaded, "openrouter/meta/llama")
        finally:
            del os.environ["OPENROUTER_API_KEY"]
        self.assertEqual((router["id"], router["api_key"]), ("meta/llama", "from-env"))
        self.assertGreaterEqual(len(config.PRESETS), 13)
        with self.assertRaises(ValueError):
            config.resolve_model(loaded, "nowhere/model")
        with self.assertRaises(ValueError):
            config.resolve_model(config.load(self.work, {"model": ""}))

    def test_reads_opencode_and_hermes(self):
        opencode = self.user / ".config" / "opencode"
        opencode.mkdir(parents=True)
        (opencode / "secret.txt").write_text("file-key\n", encoding="utf-8")
        (opencode / "opencode.jsonc").write_text("""{
  // comments and trailing commas are fine
  "model": "lab/coder",
  "provider": {"lab": {"options": {"baseURL": "http://127.0.0.1:9/v1", "apiKey": "{file:secret.txt}"}}},
  "mcp": {"docs": {"type": "local", "command": ["docs-server"]}},
  "permission": {"edit": "ask"},
}""", encoding="utf-8")
        hermes = self.user / ".hermes"
        hermes.mkdir()
        (hermes / "config.yaml").write_text(
            "model:\n  default: hermes-4\n  provider: nous\n  base_url: http://127.0.0.1:8/v1\n"
            "mcp_servers:\n  files:\n    command: npx\n    args:\n      - files-mcp\n"
            "  cloud:\n    url: https://mcp.example/sse\n    headers:\n      Authorization: Bearer abc\n",
            encoding="utf-8")

        loaded = config.load(self.work)
        self.assertEqual(loaded["model"], "lab/coder")           # OpenCode is read after Hermes
        self.assertEqual(config.resolve_model(loaded)["api_key"], "file-key")
        self.assertEqual(loaded["permission"], {"edit": "ask"})
        self.assertEqual(sorted(loaded["mcp"]), ["cloud", "docs", "files"])
        self.assertEqual(loaded["mcp"]["files"]["command"], ["npx", "files-mcp"])
        self.assertEqual(loaded["mcp"]["cloud"]["headers"], {"Authorization": "Bearer abc"})
        self.assertEqual(len(loaded["_sources"]), 2)

        (self.work / "talos.json").write_text(json.dumps({"use": {"opencode": False}}), encoding="utf-8")
        only_hermes = config.load(self.work)
        self.assertEqual(only_hermes["model"], "nous/hermes-4")
        self.assertEqual(sorted(only_hermes["mcp"]), ["cloud", "files"])
        self.assertEqual(only_hermes["permission"], {})

        neither = config.load(self.work, {"use": {"hermes": False}})
        self.assertEqual((neither["model"], neither["mcp"]), ("", {}))

    def test_window_size_from_ollama(self):
        def reply(method, path, body, headers):
            if path == "/api/ps":
                return 200, {"models": [{"name": "loaded:1b", "context_length": 2048}]}
            if path == "/api/show" and "tuned" in body:
                return 200, {"parameters": "temperature 0.2\nnum_ctx 8192"}
            return 200, {"parameters": ""}
        server = self.http(reply)
        settings = {"provider": {"ollama": {"options": {"baseURL": server.url + "/v1"},
                                            "models": {"named": {"limit": {"context": 64000}}}}}}
        loaded = config.load(self.work, settings)
        config._CONTEXT.clear()
        sizes = [config.context_of(loaded, config.resolve_model(loaded, "ollama/" + name))
                 for name in ("loaded:1b", "tuned", "plain", "named")]
        self.assertEqual(sizes, [2048, 8192, 4096, 64000])
        self.assertEqual(config.context_of(loaded, config.resolve_model(loaded, "openai/gpt")), 128000)
        loaded["context"] = 16000
        self.assertEqual(config.context_of(loaded, config.resolve_model(loaded, "ollama/plain")), 16000)
        config._CONTEXT.clear()
