"""What Jarvis can do for you. Each skill is plain Python; the LLM is only used for judgement/wording."""
from __future__ import annotations
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import Config
from .connectors import calendar as cal, email as mail
from .llm import LLM
from .store import Store


class Skills:
    def __init__(self, cfg: Config, store: Store, llm: LLM):
        self.cfg, self.store, self.llm = cfg, store, llm

    @property
    def tz(self):
        return ZoneInfo(self.cfg["owner"]["timezone"])

    # ---- email -------------------------------------------------------------
    def important_email(self, mark_seen: bool = True) -> list[dict]:
        """Unread mail that matters. Only NEW mail is sent to the LLM, as one tiny batch."""
        if not self.cfg["email"]["enabled"]:
            return []
        seen = set(self.store.get("seen_mail", []))
        new = [m for m in mail.fetch_unread(self.cfg) if m["id"] not in seen]
        if not new:
            return []
        vip = [s.lower() for s in self.cfg["email"]["important_senders"]]
        hits = {i for i, m in enumerate(new) if any(s in m["from"].lower() for s in vip)}
        try:
            listing = "\n".join(f"{i}|{m['from']}|{m['subject']}|{m['snippet'][:120]}" for i, m in enumerate(new))
            r = self.llm.ask_json(
                "Triage email for a busy person. Mark important only: needs a reply/action soon, money, "
                "deadlines, people they know. Ignore newsletters, promos, notifications. "
                'Return {"important":[index,...]}.', listing, 120)
            hits |= {i for i in r.get("important", []) if isinstance(i, int) and 0 <= i < len(new)}
        except Exception:
            pass  # budget/API trouble: fall back to sender rules only
        if mark_seen:
            self.store.set("seen_mail", list(seen | {m["id"] for m in new})[-500:])
        return [new[i] for i in sorted(hits)]

    def email_text(self, items: list[dict]) -> str:
        return "; ".join(f"{m['from'].split('<')[0].strip()}: {m['subject']}" for m in items)

    # ---- calendar ----------------------------------------------------------
    def calendar_text(self, hours: float = 24) -> str:
        if not self.cfg["calendar"]["enabled"]:
            return ""
        return cal.fmt(cal.upcoming(self.cfg, hours), self.tz)

    def calendar_alerts(self, minutes: int = 20) -> list[str]:
        if not self.cfg["calendar"]["enabled"]:
            return []
        now = datetime.now(self.tz)
        done, out = set(self.store.get("alerted", [])), []
        for e in cal.upcoming(self.cfg, minutes / 60, now):
            key = f"{e['title']}@{e['start'].isoformat()}"
            if key not in done and e["start"] >= now:
                out.append(f"In {int((e['start'] - now).total_seconds() // 60)} min: {e['title']}")
                done.add(key)
        self.store.set("alerted", list(done)[-200:])
        return out

    # ---- briefing ----------------------------------------------------------
    def briefing(self) -> str:
        o = self.cfg["owner"]
        since = self.store.get("last_briefing", time.time() - 86400)
        missed = [f"{c['name'] or c['number']}: {c['reason']}" for c in self.store.calls_since(since)
                  if c["outcome"] in ("message", "spam")]
        facts = {"calendar": self.calendar_text(24) or "n/a",
                 "tasks": "; ".join(t["text"] + (f" (due {t['due']})" if t["due"] else "") for t in self.store.open_tasks()) or "none",
                 "email": self.email_text(self.important_email()) or "nothing important",
                 "calls": "; ".join(missed) or "none"}
        self.store.set("last_briefing", time.time())
        try:
            return self.llm.ask(
                f"You are {self.cfg['voice']['assistant_name']}, a concise, dry-witted British butler-style assistant. "
                f"Write a spoken morning briefing for {o['name']} in at most 4 short sentences. No lists, no markdown.",
                str(facts), 220)
        except Exception:
            return "Good morning. " + " ".join(f"{k}: {v}." for k, v in facts.items())
