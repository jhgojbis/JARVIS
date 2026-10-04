"""What Jarvis can do for you. Each skill is plain Python; the LLM is only used for judgement/wording."""
from __future__ import annotations
import threading, time
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
    TRIAGE = ("Triage email for a busy person. Important means the owner must REACT: an invoice or payment to make, "
              "a deadline, a question or request addressed to them, a booking to confirm, a security or account problem, "
              "or a message from a person they know. Skip everything that is only information: newsletters, promos, "
              "receipts and order confirmations, automatic notifications, status updates, social media. "
              "Mail may be in any language; write each reason in English, max 12 words, saying what it is and what to do "
              '(amounts and dates included). Return {"important":[{"i":index,"why":"reason"}]}.')

    def important_email(self, mark_seen: bool = True, only_new: bool = True) -> list[dict]:
        """Unread mail that needs a reaction, each with a short `why`. By default only mail Jarvis has not triaged
        before goes to the LLM (one tiny batch); only_new=False re-checks everything unread, e.g. when you phone in."""
        if not self.cfg["email"]["enabled"]:
            return []
        seen = set(self.store.get("seen_mail", []))
        new = [m for m in mail.fetch_unread(self.cfg) if not only_new or m["id"] not in seen]
        if not new:
            return []
        vip = [s.lower() for s in self.cfg["email"]["important_senders"]]
        hits = {i: "" for i, m in enumerate(new) if any(s in m["from"].lower() for s in vip)}
        try:
            listing = "\n".join(f"{i}|{m['from']}|{m['subject']}|{m['snippet'][:160]}" for i, m in enumerate(new))
            for it in self.llm.ask_json(self.TRIAGE, listing, 400).get("important", []):
                if isinstance(it, int):
                    i, why = it, ""
                elif isinstance(it, dict):
                    i, why = it.get("i"), str(it.get("why", ""))[:120]
                else:
                    continue
                if isinstance(i, int) and 0 <= i < len(new):
                    hits[i] = why or hits.get(i, "")
        except Exception:
            pass  # budget/API trouble: fall back to sender rules only
        if mark_seen:
            self.store.set("seen_mail", list(seen | {m["id"] for m in new})[-500:])
        return [dict(new[i], why=hits[i]) for i in sorted(hits)]

    def email_text(self, items: list[dict]) -> str:
        return "; ".join(f"{m['from'].split('<')[0].strip()}: {m.get('why') or m['subject']}" for m in items)

    def _warm_inbox(self) -> None:
        try:
            self.inbox_text()
        except Exception:
            pass

    def digest(self) -> str:
        """What Jarvis says when the owner phones in: the mail that needs them, or the all-clear."""
        name, ask = self.cfg["owner"]["name"], "What can I do for you?"
        if not self.cfg["email"]["enabled"]:
            return f"Good day, {name}. {ask}"
        try:
            items = self.important_email(only_new=False)
            # warm the inbox cache in the background so "read the second one" works as a follow-up without delaying the digest
            threading.Thread(target=self._warm_inbox, daemon=True).start()
        except Exception:
            return f"Good day, {name}. I could not reach your email just now. {ask}"
        if not items:
            return f"Good day, {name}. Nothing in your inbox needs you. {ask}"
        shown = items[:4]
        said = " ".join(f"{('First', 'Second', 'Third', 'Fourth')[i]}, {self.email_text([m])}." for i, m in enumerate(shown))
        more = f" And {len(items) - 4} more." if len(items) > 4 else ""
        n = len(items)
        return f"Good day, {name}. {n} email{'s' if n > 1 else ''} need{'' if n > 1 else 's'} you. {said}{more} {ask}"

    def inbox_text(self, n: int = 10, snippet: int = 500) -> str:
        """Newest mail (read or unread) with body snippets, so the owner can ask for one to be read out.
        No LLM call; the IMAP fetch is cached for a minute so follow-up questions stay fast."""
        if not self.cfg["email"]["enabled"]:
            return ""
        cached = getattr(self, "_inbox", None)
        if not cached or time.time() - cached[0] > 60:
            cached = self._inbox = (time.time(), mail.fetch_recent(self.cfg, limit=n, snippet=snippet))
        return "\n".join(f"[{i + 1}]{' (unread)' if m.get('unread') else ''} From {m['from']} | Subject: {m['subject']} | "
                         f"{m['snippet']}" for i, m in enumerate(reversed(cached[1])))

    def trash_inbox_item(self, n: int) -> str:
        """Move email [n] of the inbox listing last read out to the trash; returns its subject."""
        cached = getattr(self, "_inbox", None)
        items = list(reversed(cached[1])) if cached else []
        if not 1 <= n <= len(items):
            raise LookupError("no such email in the listing")
        m = items[n - 1]
        mail.trash(self.cfg, m["id"])
        self._inbox = None                      # the listing has changed
        return m["subject"]

    def inbox_recent(self, seconds: float = 300) -> bool:
        """True while a conversation about email is going on, so follow-ups ("and the next one?") keep the inbox."""
        return bool(getattr(self, "_inbox", None)) and time.time() - self._inbox[0] < seconds

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
