import datetime as dt

from talos import cron
from talos.fake import call, say
from tests.base import Case


class CronTest(Case):
    def test_schedules(self):
        monday = dt.datetime(2026, 11, 23, 8, 0)            # a Monday
        self.assertEqual(cron.next_run("in 20m", monday), dt.datetime(2026, 11, 23, 8, 20))
        self.assertEqual(cron.next_run("every 2h", monday), dt.datetime(2026, 11, 23, 10, 0))
        self.assertEqual(cron.next_run("daily 09:30", monday), dt.datetime(2026, 11, 23, 9, 30))
        self.assertEqual(cron.next_run("daily 07:00", monday), dt.datetime(2026, 11, 24, 7, 0))
        self.assertEqual(cron.next_run("weekends 10:00", monday), dt.datetime(2026, 11, 28, 10, 0))
        self.assertEqual(cron.next_run("weekdays 08:30", dt.datetime(2026, 11, 27, 9, 0)), dt.datetime(2026, 11, 30, 8, 30))
        self.assertEqual(cron.next_run("*/15 9-17 * * 1-5", monday), dt.datetime(2026, 11, 23, 9, 0))
        self.assertEqual(cron.next_run("2026-12-01 08:00", monday), dt.datetime(2026, 12, 1, 8, 0))
        self.assertIsNone(cron.next_run("2020-01-01 08:00", monday))
        with self.assertRaises(ValueError):
            cron.next_run("whenever", monday)
        self.assertTrue(cron.repeats("every 2h") and cron.repeats("0 9 * * *"))
        self.assertFalse(cron.repeats("in 5m") or cron.repeats("2026-12-01 08:00"))

    def test_job_runs_and_reschedules(self):
        sent = []
        server = self.http(lambda method, path, body, headers: (sent.append((path, body)), (200, {"ok": True, "result": []}))[1])
        model = self.model([say("Report: 3 files changed."), say("One-off done."), say("(not used)")])
        loaded = self.config(model, gateway={"telegram": {"token": "T", "base": server.url, "allowed_users": ["7"]}})

        self.assertIn("Cannot read the schedule", cron.create("whenever", "x"))
        self.assertIn("needs a prompt", cron.create("every 1h", " "))
        made = cron.create("every 1h", "Write the hourly report", name="hourly", deliver="telegram:7", cwd=str(self.work))
        self.assertIn("Scheduled job", made)
        cron.create("in 5m", "One-off job", cwd=str(self.work))
        hourly, once = cron.jobs()
        self.assertIn("hourly", cron.describe())

        self.assertEqual(cron.tick(loaded, store=self.store), [])                    # nothing is due yet
        later = dt.datetime.now() + dt.timedelta(hours=1, minutes=5)
        self.assertEqual(cron.tick(loaded, now=later, store=self.store), [hourly["id"], once["id"]])

        after_hourly, after_once = cron.jobs()
        self.assertGreater(dt.datetime.fromisoformat(after_hourly["next_run"]), later)   # repeats
        self.assertEqual(after_once["next_run"], "")                                    # ran once, never again
        self.assertIn("ok", after_hourly["last_status"])
        self.assertIn("Sent to telegram:7", after_hourly["last_status"])
        self.assertTrue(any("sendMessage" in path and "3 files changed" in body for path, body in sent))
        saved = list((self.home / "cron" / "output" / hourly["id"]).glob("*.md"))
        self.assertEqual(saved[0].read_text(encoding="utf-8").strip(), "Report: 3 files changed.")
        self.assertEqual(cron.tick(loaded, now=later, store=self.store), [])            # not twice

        self.assertIn("pause done", cron.change("pause", hourly["id"]))
        far = later + dt.timedelta(days=2)
        self.assertEqual(cron.tick(loaded, now=far, store=self.store), [])
        self.assertIn("remove done", cron.change("remove", once["id"]))
        self.assertEqual(len(cron.jobs()), 1)

    def test_agent_schedules_and_sends(self):
        sent = []
        server = self.http(lambda method, path, body, headers: (sent.append((path, body)), (200, {"ok": True}))[1])
        runner, _, events = self.agent([
            call("cronjob", action="create", schedule="daily 07:00", prompt="Summarise the news", name="news"),
            call("cronjob", action="list"),
            call("send_message", platform="telegram", chat="7", text="Job is set. key sk-abcdefghijklmnopqrstuvwxyz123456"),
            call("send_message", platform="fax", chat="1", text="x"),
            say("scheduled"),
        ], gateway={"telegram": {"token": "T", "base": server.url}})
        runner.run("send me the news every morning")
        created, listed = self.results(events, "cronjob")
        self.assertIn("Scheduled job", created)
        self.assertIn("news", listed)
        self.assertEqual(cron.jobs()[0]["cwd"], str(self.work))
        ok, unknown = self.results(events, "send_message")
        self.assertIn("Sent to telegram:7", ok)
        self.assertIn("not set up", unknown)
        self.assertIn("Job is set.", sent[0][1])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", sent[0][1])       # secrets never leave in a message
