"""Scheduled prompts. Jobs live in ~/.talos/cron/jobs.json; `talos gateway` (or `talos cron tick`,
for the system's own scheduler) runs the ones that are due and delivers the answer.

Schedules: "in 20m", "every 2h", "daily 09:00", "weekdays 08:30", a 5-field cron line
("0 9 * * 1-5"), or one date and time ("2026-12-01 08:00").
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from . import util

UNITS = {"m": 60, "h": 3600, "d": 86400}


def _path():
    return util.home() / "cron" / "jobs.json"


def jobs() -> list:
    return util.load_json_file(_path(), []) or []


def _save(items) -> None:
    util.save_json_file(_path(), items)


def _field(text: str, low: int, high: int) -> set:
    """One cron field ('*/5', '1-5', '1,15', '*') as the set of allowed numbers."""
    allowed = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            start, end = (int(piece) for piece in part.split("-", 1))
        else:
            start = end = int(part)
        allowed.update(range(start, end + 1, step))
    return allowed


def next_run(schedule: str, after: dt.datetime):
    """The first time after `after` that the schedule fires, or None when it never will again.
    Raises ValueError for a schedule it cannot read."""
    text = schedule.strip().lower()
    match = re.fullmatch(r"(in|every)\s+(\d+)\s*([mhd])\w*", text)
    if match:
        return after + dt.timedelta(seconds=int(match.group(2)) * UNITS[match.group(3)])
    match = re.fullmatch(r"(daily|weekdays|weekends)\s+(?:at\s+)?(\d{1,2}):(\d{2})", text)
    if match:
        days = {"daily": "*", "weekdays": "1-5", "weekends": "0,6"}[match.group(1)]
        text = f"{int(match.group(3))} {int(match.group(2))} * * {days}"
    fields = text.split()
    if len(fields) == 5 and re.fullmatch(r"[\d*/,\- ]+", text):
        minutes, hours = _field(fields[0], 0, 59), _field(fields[1], 0, 23)
        days, months = _field(fields[2], 1, 31), _field(fields[3], 1, 12)
        weekdays = {day % 7 for day in _field(fields[4], 0, 7)}
        moment = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if (moment.minute in minutes and moment.hour in hours and moment.day in days
                    and moment.month in months and (moment.weekday() + 1) % 7 in weekdays):
                return moment
            moment += dt.timedelta(minutes=1)
        return None
    try:
        moment = dt.datetime.fromisoformat(schedule.strip())
    except ValueError:
        raise ValueError(
            f"Cannot read the schedule '{schedule}'. Examples: in 20m, every 2h, daily 09:00, "
            "weekdays 08:30, 0 9 * * 1-5, 2026-12-01 08:00")
    return moment if moment > after else None


def repeats(schedule: str) -> bool:
    text = schedule.strip().lower()
    if text.startswith("in "):
        return False
    try:
        dt.datetime.fromisoformat(schedule.strip())
        return False
    except ValueError:
        return True


def create(schedule: str, prompt: str, name: str = "", deliver: str = "local", cwd: str = ".") -> str:
    if not prompt.strip():
        return "The job needs a prompt."
    try:
        first = next_run(schedule, dt.datetime.now())
    except ValueError as error:
        return str(error)
    if first is None:
        return "That time is already in the past."
    job = {
        "id": uuid.uuid4().hex[:8], "name": name or prompt.strip()[:40], "schedule": schedule,
        "prompt": prompt, "deliver": deliver or "local", "cwd": cwd, "paused": False,
        "next_run": first.isoformat(timespec="minutes"), "last_run": "", "last_status": "",
    }
    items = jobs()
    items.append(job)
    _save(items)
    return f"Scheduled job {job['id']} \"{job['name']}\". First run: {job['next_run']}."


def describe() -> str:
    return "\n".join(
        f"{job['id']}  {'paused' if job['paused'] else 'next ' + job['next_run']}  "
        f"[{job['schedule']}] -> {job['deliver']}  {job['name']}"
        + (f"  (last: {job['last_status']})" if job["last_status"] else "")
        for job in jobs()
    ) or "No scheduled jobs."


def change(action: str, identifier: str) -> str:
    items = jobs()
    job = next((item for item in items if item["id"] == identifier), None)
    if not job:
        return f"No job with id '{identifier}'."
    if action == "remove":
        items.remove(job)
    elif action == "pause":
        job["paused"] = True
    elif action == "resume":
        job["paused"] = False
        upcoming = next_run(job["schedule"], dt.datetime.now())
        job["next_run"] = upcoming.isoformat(timespec="minutes") if upcoming else ""
    else:
        return "action must be create, list, remove, pause, resume or run."
    _save(items)
    return f"Job {identifier}: {action} done."


def run_job(config: dict, identifier: str, store=None) -> str:
    """Run one job now, in a fresh session, and deliver the answer."""
    from . import agent as agent_module, config as config_module, gateway, store as store_module
    job = next((item for item in jobs() if item["id"] == identifier), None)
    if not job:
        return f"No job with id '{identifier}'."
    job_config = config_module.load(job.get("cwd") or ".", {
        key: value for key, value in config.items()
        if key in ("model", "provider", "retry_wait", "gateway", "proof", "learn")
    })
    own_store = store is None
    store = store or store_module.Store()
    try:
        runner = agent_module.Agent(job_config, store, source="cron", auto_yes=False,
                                    deny_tools=("cronjob", "question"))
        result = runner.run(job["prompt"])
        text, status = result.text, ("ok" if not result.stopped else result.stopped)
    except Exception as error:
        text, status = f"The scheduled job failed: {error}", "failed"
    finally:
        if own_store:
            store.close()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    util.write_text(util.home() / "cron" / "output" / job["id"] / f"{stamp}.md", text + "\n")
    if job.get("deliver", "local") != "local" and ":" in job["deliver"]:
        platform, chat = job["deliver"].split(":", 1)
        status += "; " + gateway.send(config, platform, chat, text)
    items = jobs()
    for item in items:
        if item["id"] == identifier:
            item["last_run"] = dt.datetime.now().isoformat(timespec="minutes")
            item["last_status"] = status
    _save(items)
    return text


def tick(config: dict, now=None, store=None) -> list:
    """Run every job that is due. Returns the ids that ran."""
    now = now or dt.datetime.now()
    ran = []
    for job in jobs():
        if job["paused"] or not job["next_run"] or dt.datetime.fromisoformat(job["next_run"]) > now:
            continue
        items = jobs()          # move the clock first, so a crash cannot run the job twice
        for item in items:
            if item["id"] == job["id"]:
                upcoming = next_run(item["schedule"], now) if repeats(item["schedule"]) else None
                item["next_run"] = upcoming.isoformat(timespec="minutes") if upcoming else ""
        _save(items)
        run_job(config, job["id"], store)
        ran.append(job["id"])
    return ran
