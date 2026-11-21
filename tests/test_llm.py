import json

from talos import llm
from talos.fake import say
from tests.base import Case


class LlmTest(Case):
    def test_text_protocol_fallback(self):
        block = "<tool>" + json.dumps({"name": "write", "arguments": {"filePath": "note.txt", "content": "hi\n"}}) + "</tool>"
        runner, model, _ = self.agent([say("I will write it. " + block), say("Written.")],
                                      model_options={"native_tools": False}, proof="off")
        result = runner.run("write a note")
        self.assertEqual(result.text, "Written.")
        self.assertEqual((self.work / "note.txt").read_text(encoding="utf-8"), "hi\n")
        self.assertIn("tools", model.requests[0])            # tried the normal way first
        self.assertNotIn("tools", model.requests[1])         # then switched to the text protocol
        self.assertIn("<tool>", model.requests[1]["messages"][0]["content"])

    def test_parse_text_calls(self):
        text, calls = llm.parse_text_calls('ok <tool>{"name": "read", "arguments": {"filePath": "a"}}</tool> <tool>{broken</tool>')
        self.assertEqual([item["name"] for item in calls], ["read"])
        self.assertNotIn("<tool>{\"name\"", text)

        # the other ways models write a call: a fence, Hermes-style tags, real line breaks in a string
        fenced = 'Sure.\n```tool\n{"name": "write", "arguments": {"filePath": "a.py", "content": "x = 1\nprint(x)\n"}}\n```'
        text, calls = llm.parse_text_calls(fenced)
        self.assertEqual((text, calls[0]["name"], calls[0]["arguments"]["content"]), ("Sure.", "write", "x = 1\nprint(x)\n"))
        text, calls = llm.parse_text_calls('<tool_call>{"name": "list", "arguments": {}}</tool_call>')
        self.assertEqual([item["name"] for item in calls], ["list"])
        example = 'An example file:\n```json\n{"name": "my-package", "version": "1.0"}\n```'
        self.assertEqual(llm.parse_text_calls(example), (example, []))             # plain JSON is left alone

    def test_without_streaming(self):
        runner, _, _ = self.agent([say("plain")], model_options={"stream": False})
        self.assertEqual(runner.run("hi").text, "plain")
