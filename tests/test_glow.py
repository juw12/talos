"""Glow's free AI as a model (several keyless services, one after the other) and the spare model."""
import json

from talos import config as config_module, glow, llm, tune
from talos.fake import call, say
from tests.base import Case


class GlowTest(Case):
    def setUp(self):
        super().setUp()
        llm.RESTING.clear()
        llm.STRIKES.clear()
        llm._rests_read[0] = ""
        self.closers.append(llm.RESTING.clear)
        self.closers.append(llm.STRIKES.clear)

    def two_services(self, first_steps, second_steps, **extra):
        first, second = self.model(first_steps), self.model(second_steps)
        settings = {"model": "glow/auto", "retry_wait": 0, "proof": "off",
                    "provider": {"glow": {"routes": [[first.url, "one"], [second.url, "two"]]}}}
        settings.update(extra)
        return first, second, config_module.load(self.work, settings)

    def test_the_names(self):
        loaded = config_module.load(self.work, {"model": ""})
        auto = config_module.resolve_model(loaded, "glow/auto")
        self.assertEqual((auto["name"], auto["api_key"], auto["context"]), ("glow/auto", "", glow.CONTEXT))
        self.assertGreaterEqual(len(auto["routes"]), 5)
        self.assertTrue(all(route["base_url"].startswith("https://") for route in auto["routes"]))
        only = config_module.resolve_model(loaded, "glow/deepseek")["routes"]
        self.assertTrue(only and all("deepseek" in route["id"].lower() for route in only))
        self.assertTrue(all("qwen" in route["id"].lower() for route in config_module.resolve_model(loaded, "glow/qwen")["routes"]))
        with self.assertRaises(ValueError) as refused:
            config_module.resolve_model(loaded, "glow/nothing")
        self.assertIn("glow/auto", str(refused.exception))
        self.assertIn("glow/auto", config_module.list_models(loaded))
        # a free service is never picked by itself, and never measured unless it is named
        self.assertFalse(config_module.pick_default_model(loaded).startswith("glow/"))
        measured = []
        kept, tune.measure = tune.measure, lambda config, store, model, report=None: measured.append(model) or {
            "model": model, "passed": 0, "of": 0, "seconds": 0, "jobs": []}
        try:
            tune.run(config_module.load(self.work, {"model": "shop/a"}), self.store)
        finally:
            tune.measure = kept
        self.assertIn("shop/a", measured)
        self.assertFalse([name for name in measured if name.startswith("glow/")])

    def test_a_busy_service_hands_the_request_to_the_next_one(self):
        first, second, loaded = self.two_services(
            [{"status": 429, "error": "too many"}],
            [call("write", filePath="note.txt", content="hi\n"), say("Written by the second.")])
        from talos import agent as agent_module
        events = []
        runner = agent_module.Agent(loaded, self.store, auto_yes=True, emit=lambda event, data: events.append((event, data)))
        self.closers.append(runner.close)
        result = runner.run("write a note")
        self.assertEqual((result.text, result.stopped), ("Written by the second.", ""))
        self.assertEqual((self.work / "note.txt").read_text(encoding="utf-8"), "hi\n")
        self.assertEqual(len(first.requests), 1)             # asked once, then left alone while it rests
        self.assertEqual(second.requests[0]["model"], "two")
        self.assertTrue(any(name.startswith("glow/one@") for name in llm.RESTING))

    def test_a_service_rests_as_long_as_it_asks(self):
        self.assertEqual(llm.stated_wait({"Retry-After": "3176734"}, ""), 3176734)
        self.assertEqual(llm.stated_wait({}, '{"error":{"message":"Please retry in 87832 seconds.","retry_after":87832}}'), 87832)
        self.assertEqual(llm.stated_wait({}, "Free limit reached: 1 reply every 45 seconds per IP. Retry after 26s, or"), 26)
        self.assertEqual(llm.stated_wait({}, "rate limit exceeded"), 0)
        # 402 is how one free service says "limit reached"; it names a long wait, so its whole server rests
        first, second, loaded = self.two_services(
            [{"status": 402, "error": "Free limit reached. Retry after 9000s."}], [say("from the second"), say("again")])
        model = config_module.resolve_model(loaded)
        model["routes"].insert(1, dict(model["routes"][0], name="glow/other@first", id="other"))
        notices = []
        client = llm.LLM(loaded)
        client.notice = notices.append
        reply = client.chat(model, [{"role": "user", "content": "hello"}])
        self.assertEqual((reply["content"], reply["route"].split("@")[0]), ("from the second", "glow/two"))
        self.assertEqual(len(first.requests), 1)             # the second model of the resting server was not asked
        kept = json.loads((self.home / llm.REST_FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(kept), 1)
        self.assertTrue(list(kept)[0].startswith("@127.0.0.1"))
        llm.RESTING.clear()                                   # a new run reads what the last one wrote down
        llm._rests_read[0] = ""
        self.assertEqual(llm.LLM(loaded).chat(model, [{"role": "user", "content": "hello"}])["content"], "again")
        self.assertEqual(len(first.requests), 1)
        # when all rest for a short while, Talos waits for the first one and says so
        llm.RESTING.clear()
        first.steps[:] = [{"status": 429, "error": "Too many. Retry after 6s."}, say("after the wait")]
        second.steps[:] = [{"status": 429, "error": "Too many. Retry after 30s."}]
        model = config_module.resolve_model(loaded)
        self.assertEqual(client.chat(model, [{"role": "user", "content": "hello"}])["content"], "after the wait")
        self.assertTrue(notices and "busy" in notices[0])

    def test_all_busy_ends_with_a_plain_message(self):
        first, second, loaded = self.two_services([{"status": 429}], [{"status": 503}], glow_wait=0)
        model = config_module.resolve_model(loaded)
        with self.assertRaises(llm.ModelError) as refused:
            llm.LLM(loaded).chat(model, [{"role": "user", "content": "hello"}])
        self.assertIn("every free service is busy", str(refused.exception))
        self.assertEqual((len(first.requests), len(second.requests)), (1, 1))

    def test_the_spare_model_takes_over_for_the_rest_of_the_run(self):
        main = self.model([{"status": 401, "error": "bad key"}, say("the main model again")])
        spare = self.model([call("write", filePath="note.txt", content="hi\n"), say("Done by the spare.")])
        settings = main.config(fallback_model="spare/script", proof="off")
        settings["provider"]["spare"] = {"options": {"baseURL": spare.url}}
        from talos import agent as agent_module
        events = []
        runner = agent_module.Agent(config_module.load(self.work, settings), self.store, auto_yes=True,
                                    emit=lambda event, data: events.append((event, data)))
        self.closers.append(runner.close)
        result = runner.run("write a note")
        self.assertEqual((result.text, result.stopped), ("Done by the spare.", ""))
        self.assertTrue(any(event == "warning" and "carries on with spare/script" in data for event, data in events))
        self.assertEqual((len(main.requests), len(spare.requests)), (1, 2))      # the spare did every later step
        self.assertEqual(runner.model_name, "fake/script")
        self.assertEqual(runner.run("and again").text, "the main model again")   # the next run starts with the chosen one

    def test_a_list_of_spare_models_is_tried_in_its_order(self):
        main = self.model([{"status": 401, "error": "bad key"}])
        first = self.model([{"status": 429, "error": "the day's free requests are used"}] * 2)
        second = self.model([say("Answered by the second spare.")])
        settings = main.config(fallback_model=["first/script", "nothing-here/x", "second/script"], proof="off", retry_wait=0)
        settings["provider"]["first"] = {"options": {"baseURL": first.url}}
        settings["provider"]["second"] = {"options": {"baseURL": second.url}}
        from talos import agent as agent_module
        events = []
        runner = agent_module.Agent(config_module.load(self.work, settings), self.store, auto_yes=True,
                                    emit=lambda event, data: events.append((event, data)))
        self.closers.append(runner.close)
        result = runner.run("hello")
        self.assertEqual((result.text, result.stopped), ("Answered by the second spare.", ""))
        said = [data for event, data in events if event == "warning"]
        self.assertTrue(any("carries on with first/script" in line for line in said), said)
        self.assertTrue(any("carries on with second/script" in line for line in said), said)
        # a busy spare is asked twice, not four times, while another one stands by
        self.assertEqual((len(main.requests), len(first.requests), len(second.requests)), (1, 2, 1))
        self.assertEqual(config_module.spares({"fallback_model": "a/b, c/d ,a/b"}), ["a/b", "c/d"])
        self.assertEqual(config_module.spares({"fallback_model": ""}), [])

    def test_a_server_that_is_not_there_is_not_waited_for_when_a_spare_stands_by(self):
        spare = self.model([say("Answered by the spare.")])
        settings = {"model": "gone/m", "fallback_model": "spare/script", "retry_wait": 30, "proof": "off",
                    "provider": {"gone": {"options": {"baseURL": "http://127.0.0.1:9/v1"}},
                                 "spare": {"options": {"baseURL": spare.url}}}}
        from talos import agent as agent_module
        runner = agent_module.Agent(config_module.load(self.work, settings), self.store, auto_yes=True)
        self.closers.append(runner.close)
        self.assertEqual(runner.run("hello").text, "Answered by the spare.")     # retry_wait 30 would take minutes
