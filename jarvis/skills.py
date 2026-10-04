"""What Jarvis can do for you. Each skill is plain Python; the LLM is only used for judgement/wording."""
from __future__ import annotations
import logging, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import Config
from .connectors import calendar as cal, email as mail
from .llm import LLM
from .store import Store


log = logging.getLogger("jarvis.skills")


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
    NUM_TTL = 12 * 3600         # how long "delete 1" / "reply 2" keep pointing at the last summary
    FIELDS = ("id", "account", "from", "subject", "why", "snippet", "reply_to", "unread")

    def _accounts(self) -> list[dict]:
        return mail.accounts(self.cfg)

    def account_names(self) -> str:
        return ", ".join(a["name"] + (" (work, high priority" + (", unread only" if a.get("unread_only") else "") + ")" if a["priority"] == "high" else "")
                         for a in self._accounts())

    def _fetch_all(self, getter, unread_getter=None, **kw) -> list[dict]:
        """Mail of every account (fetched in parallel), tagged with the account it came from, work mail first. One broken
        account must not hide the others; only when every account fails does this raise. Accounts marked unread_only use
        `unread_getter`."""
        accts = sorted(self._accounts(), key=lambda a: a["priority"] != "high")
        if not accts:
            raise RuntimeError("no email account has credentials")

        def one(a):
            fn = unread_getter if a.get("unread_only") and unread_getter else getter
            try:
                return [dict(m, account=a["name"]) for m in fn(self.cfg, account=a["name"], **kw)]
            except Exception:
                log.exception("email account %s failed", a["name"])
                return None
        with ThreadPoolExecutor(max_workers=len(accts)) as pool:
            results = list(pool.map(one, accts))
        if all(r is None for r in results):
            raise RuntimeError("no email account reachable")
        return [m for r in results if r for m in r]

    def important_email(self, mark_seen: bool = True, only_new: bool = True) -> list[dict]:
        """Unread mail that needs a reaction, each with a short `why`. A message is judged once (the verdict is remembered, so
        results stay stable and calls stay fast); only_new=False re-lists everything unread, e.g. when you phone in."""
        if not self.cfg["email"]["enabled"]:
            return []
        seen = set(self.store.get("seen_mail", []))
        key = lambda m: f"{m['account']}|{m['id']}"
        ignore = [x.lower() for x in self.cfg["email"].get("ignore_senders") or []]
        new = [m for m in self._fetch_all(mail.fetch_unread, snippet=500)
               if (not only_new or (key(m) not in seen and m["id"] not in seen)) and not any(x in m["from"].lower() for x in ignore)]
        if not new:
            return []
        verdicts = dict(self.store.get("triage", {}))                 # key -> [important 0/1, why]
        todo = [m for m in new if key(m) not in verdicts]
        if todo:
            high = [a["name"] for a in self._accounts() if a["priority"] == "high"]
            prompt = self.TRIAGE + (f" Mailbox {', '.join(high)} is the owner's work mail and matters more: treat messages from real people, "
                                    "customers, leads or inquiries there as important." if high else "")
            try:
                listing = "\n".join(f"{i}|{m['account']}|{m['from']}|{m['subject']}|{m['snippet'][:160]}" for i, m in enumerate(todo))
                said = {}
                for it in self.llm.ask_json(prompt, listing, 400).get("important", []):
                    if isinstance(it, int):
                        i, why = it, ""
                    elif isinstance(it, dict):
                        i, why = it.get("i"), str(it.get("why", ""))[:120]
                    else:
                        continue
                    if isinstance(i, int) and 0 <= i < len(todo):
                        said[i] = why
                for i, m in enumerate(todo):
                    verdicts[key(m)] = [int(i in said), said.get(i, "")]
                self.store.set("triage", dict(list(verdicts.items())[-400:]))
            except Exception:
                pass  # budget/API trouble: not remembered, so it is tried again next time; sender rules still apply
        vip = [x.lower() for x in self.cfg["email"]["important_senders"]]
        hits = {}
        for i, m in enumerate(new):
            v = verdicts.get(key(m), [0, ""])
            if v[0] or any(x in m["from"].lower() for x in vip):
                hits[i] = v[1]
        if mark_seen:
            self.store.set("seen_mail", list(seen | {key(m) for m in new})[-500:])
        return [dict(new[i], why=hits[i]) for i in sorted(hits)]

    def email_text(self, items: list[dict]) -> str:
        multi = len(self._accounts()) > 1
        return "; ".join(f"{m['account'] + ', ' if multi and m.get('account') else ''}{m['from'].split('<')[0].strip()}: "
                         f"{m.get('why') or m['subject']}" for m in items)

    def say_items(self, items: list[dict]) -> str:
        """The spoken version: the same numbers the WhatsApp summary and later commands use."""
        said = " ".join(f"Number {i}, {self.email_text([m])}." for i, m in enumerate(items[:4], 1))
        return said + (f" And {len(items) - 4} more." if len(items) > 4 else "")

    def summary_message(self, items: list[dict]) -> str:
        multi = len(self._accounts()) > 1
        lines = [f"{len(items)} email{'s' if len(items) > 1 else ''} need{'' if len(items) > 1 else 's'} you:"]
        lines += [f"{i}. {'[' + m['account'] + '] ' if multi and m.get('account') else ''}{m['from'].split('<')[0].strip()}: "
                  f"{m.get('why') or m['subject']}" for i, m in enumerate(items, 1)]
        lines.append('\nReply, e.g.: "delete all", "delete 1", "reply 2 yes, Thursday works", "forward 3 to bob@firma.se", "read 2".')
        return "\n".join(lines)

    # numbering: the items of the last summary are 1..N (same numbers on the phone and on WhatsApp), the rest of the recent
    # inbox follows after them. A deleted item keeps its number so the others never shift.
    def set_numbered(self, items: list[dict]) -> None:
        self.store.set("numbered", {"ts": time.time(), "items": [{k: m.get(k) for k in self.FIELDS} for m in items]})

    def summary_count(self) -> int:
        d = self.store.get("numbered")
        return len(d["items"]) if d and time.time() - d["ts"] < self.NUM_TTL else 0

    def numbered(self) -> list[dict]:
        d = self.store.get("numbered")
        head = d["items"] if d and time.time() - d["ts"] < self.NUM_TTL else []
        cached = getattr(self, "_inbox", None)
        taken = {(m.get("account"), m.get("id")) for m in head}
        return head + [m for m in (reversed(cached[1]) if cached else []) if (m.get("account"), m.get("id")) not in taken]

    def numbered_text(self) -> str:
        multi = len(self._accounts()) > 1
        n = self.summary_count()
        out = []
        for i, m in enumerate(self.numbered(), 1):
            if m.get("gone"):
                out.append(f"[{i}] (deleted)")
                continue
            tags = ([m["account"]] if multi and m.get("account") else []) + (["unread"] if m.get("unread") else [])
            out.append(f"[{i}]{' (' + ', '.join(tags) + ')' if tags else ''} From {m['from']} | Subject: {m['subject']} | "
                       f"{'Why: ' + m['why'] + ' | ' if m.get('why') else ''}{m['snippet']}")
        return (f"Latest summary = [1]..[{n}]; numbers below are what the owner's commands refer to.\n" if n else "") + "\n".join(out)

    def _warm_inbox(self) -> None:
        try:
            self.inbox_text()
        except Exception:
            pass

    def digest_with_summary(self) -> tuple[str, str]:
        """(what to say, what to WhatsApp first) when the owner phones in: mail that needs them, from every mailbox."""
        name, ask = self.cfg["owner"]["name"], "What can I do for you?"
        if not self.cfg["email"]["enabled"]:
            return f"Good day, {name}. {ask}", ""
        try:
            items = self.important_email(only_new=False)
        except Exception:
            return f"Good day, {name}. I could not reach your email just now. {ask}", ""
        self.set_numbered(items)
        # warm the rest of the inbox in the background so "read the Brevo one" works without delaying the digest
        threading.Thread(target=self._warm_inbox, daemon=True).start()
        if not items:
            return f"Good day, {name}. Nothing in your inbox needs you. {ask}", ""
        n = len(items)
        return (f"Good day, {name}. {n} email{'s' if n > 1 else ''} need{'' if n > 1 else 's'} you. {self.say_items(items)} {ask}",
                self.summary_message(items))

    def digest(self) -> str:
        return self.digest_with_summary()[0]

    def inbox_text(self, n: int = 10, snippet: int = 500) -> str:
        """The numbered list: last summary first, then the newest mail of every mailbox (read or unread; unread only for
        mailboxes marked unread_only). The recent-mail fetch is cached for a minute so follow-up questions stay fast."""
        if not self.cfg["email"]["enabled"]:
            return ""
        def refresh():
            fetched = self._fetch_all(mail.fetch_recent, mail.fetch_unread, limit=n, snippet=snippet)
            listing = []
            for name in dict.fromkeys(m["account"] for m in fetched):          # work mail first, newest first inside each mailbox
                listing += list(reversed([m for m in fetched if m["account"] == name]))
            self._inbox = (time.time(), list(reversed(listing)))

        cached = getattr(self, "_inbox", None)
        age = time.time() - cached[0] if cached else None
        if age is None or age > 600:                       # nothing usable: wait for the mailbox
            refresh()
        elif age > 60 and not getattr(self, "_refreshing", False):   # slightly stale: answer now, refresh behind the scenes
            self._refreshing = True

            def bg():
                try:
                    refresh()
                except Exception:
                    log.exception("background inbox refresh failed")
                finally:
                    self._refreshing = False
            threading.Thread(target=bg, daemon=True).start()
        return self.numbered_text()

    def _gone(self, picked: list[dict]) -> None:
        d = self.store.get("numbered")
        keys = {(m.get("account"), m.get("id")) for m in picked}
        if d:
            for m in d["items"]:
                if (m.get("account"), m.get("id")) in keys:
                    m["gone"] = True
            self.store.set("numbered", d)
        self._inbox = None                      # the recent listing has changed

    def trash_inbox_items(self, ns: list[int]) -> list[str]:
        """Move emails [n...] of the numbered list to the trash; returns their subjects. All numbers are resolved against
        the same list first, and one failure does not stop the rest."""
        items = self.numbered()
        picked = []
        for n in dict.fromkeys(ns):
            if not 1 <= n <= len(items) or items[n - 1].get("gone"):
                raise LookupError("no such email in the list")
            picked.append(items[n - 1])
        done, failed = [], []
        for m in picked:
            try:
                mail.trash(self.cfg, m["id"], account=m.get("account"))
                done.append(m)
            except Exception:
                failed.append(m)
                log.exception("could not trash %r", m["subject"])
        self._gone(done)
        if failed:
            raise RuntimeError(f"{len(failed)} of {len(picked)} emails could not be moved")
        return [m["subject"] for m in done]

    def trash_inbox_item(self, n: int) -> str:
        return self.trash_inbox_items([n])[0]

    DRAFT_TTL = 1800        # an unsent draft is forgotten after 30 minutes

    def pending_draft(self) -> dict | None:
        d = self.store.get("draft")
        return d if d and time.time() - d["ts"] < self.DRAFT_TTL else None

    def make_draft(self, a: dict) -> dict:
        """Prepare (never send) an email: a reply to numbered email [n], a forward of it, or a brand-new mail.
        Kept as the single pending draft and filed in the Gmail drafts folder of the sending mailbox."""
        body, to, subject = str(a.get("body", "")).strip()[:3000], str(a.get("to", "")).strip(), str(a.get("subject", "")).strip()[:200]
        items = self.numbered()
        n = a.get("n")
        src = items[n - 1] if isinstance(n, int) and 1 <= n <= len(items) and not items[n - 1].get("gone") else None
        if isinstance(n, int) and src is None:
            raise LookupError("no such email in the list")
        d = {"to": to, "subject": subject, "body": body, "in_reply_to": "", "ts": time.time(),
             "account": mail.account(self.cfg, (src or {}).get("account") or a.get("account") or None)["name"]}   # replies leave from the account that got the mail
        if src and a.get("forward") is not True:                          # reply
            d.update(to=src["reply_to"], subject=src["subject"] if src["subject"].lower().startswith("re:") else "Re: " + src["subject"],
                     in_reply_to=src["id"])
        elif src:                                                         # forward, with the original in full
            full = mail.fetch_body(self.cfg, src["id"], account=src.get("account"))
            d["subject"] = src["subject"] if src["subject"].lower().startswith("fwd:") else "Fwd: " + src["subject"]
            d["body"] = f"{body}\n\n---------- Forwarded message ----------\nFrom: {full['from']}\nSubject: {full['subject']}\n\n{full['snippet']}".strip()
        if not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", d["to"]) or not d["subject"] or not d["body"].strip():
            raise ValueError("need a valid recipient address, a subject and some text")
        self.store.set("draft", d)
        try:
            mail.save_draft(self.cfg, d)
        except Exception:
            pass                                  # the draft still exists here; the Gmail copy is a convenience
        return d

    def send_draft(self) -> str:
        d = self.pending_draft()
        if not d:
            raise LookupError("no draft is waiting")
        mail.send(self.cfg, d)
        self.store.set("draft", None)
        return d["to"]

    def inbox_recent(self, seconds: float = 300) -> bool:
        """True while a conversation about email is going on, so follow-ups ("and the next one?") keep the inbox."""
        return (bool(getattr(self, "_inbox", None)) and time.time() - self._inbox[0] < seconds) or self.summary_count() > 0

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
        mails = self.important_email()
        if mails:
            self.set_numbered(mails)
        facts = {"calendar": self.calendar_text(24) or "n/a",
                 "tasks": "; ".join(t["text"] + (f" (due {t['due']})" if t["due"] else "") for t in self.store.open_tasks()) or "none",
                 "email": self.email_text(mails) or "nothing important",
                 "calls": "; ".join(missed) or "none"}
        self.store.set("last_briefing", time.time())
        try:
            return self.llm.ask(
                f"You are {self.cfg['voice']['assistant_name']}, a concise, dry-witted British butler-style assistant. "
                f"Write a spoken morning briefing for {o['name']} in at most 4 short sentences. No lists, no markdown.",
                str(facts), 220)
        except Exception:
            return "Good morning. " + " ".join(f"{k}: {v}." for k, v in facts.items())
