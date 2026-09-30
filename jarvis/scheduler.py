"""Minimal scheduler: no dependencies, persists last-run in SQLite, honours quiet hours."""
from __future__ import annotations
import asyncio, logging, re, time
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import Config
from .notify import Notifier
from .skills import Skills
from .store import Store

log = logging.getLogger("jarvis.scheduler")

def parse_every(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([mh])\s*", str(s))
    if not m:
        raise ValueError(f"bad interval {s!r}")
    return int(m.group(1)) * (3600 if m.group(2) == "h" else 60)

def in_quiet_hours(cfg: Config, now: datetime) -> bool:
    s, e = cfg["owner"]["quiet_hours"]
    t = now.strftime("%H:%M")
    return (s <= t < e) if s <= e else (t >= s or t < e)

def is_due(job: dict, last: float, now: datetime) -> bool:
    if job.get("every"):
        return now.timestamp() - last >= parse_every(job["every"])
    if job.get("at"):   # once per day, at/after HH:MM, not yet run today
        return now.strftime("%H:%M") >= job["at"] and datetime.fromtimestamp(last, now.tzinfo).date() < now.date()
    return False

class Scheduler:
    def __init__(self, cfg: Config, store: Store, skills: Skills, notifier: Notifier):
        self.cfg, self.store, self.skills, self.notifier = cfg, store, skills, notifier

    def run_job(self, job: dict) -> str | None:
        """Returns text to tell the owner, or None when there is nothing worth saying."""
        a = job["action"]
        if a == "email_check":
            items = self.skills.important_email()
            return ("Important email: " + self.skills.email_text(items)) if items else None
        if a == "calendar_alert":
            return " | ".join(self.skills.calendar_alerts()) or None
        if a == "briefing":
            return self.skills.briefing()
        return None

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(ZoneInfo(self.cfg["owner"]["timezone"]))
        sent = []
        if in_quiet_hours(self.cfg, now):
            return sent
        for job in list(self.cfg["jobs"]):
            key = f"job:{job['name']}"
            last = self.store.get(key, 0)
            try:
                if not is_due(job, last, now):
                    continue
                self.store.set(key, now.timestamp())      # set first: a crash never causes a retry storm
                text = self.run_job(job)
                if text:
                    self.notifier.send("call" if job.get("notify") == "call" else "message", text)
                    sent.append(text)
            except Exception:
                log.exception("job %s failed", job.get("name"))
        return sent

    async def loop(self):
        while True:
            await asyncio.get_running_loop().run_in_executor(None, self.tick)
            await asyncio.sleep(60)
