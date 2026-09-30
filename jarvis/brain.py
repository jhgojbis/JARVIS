"""Conversation brain: one LLM call per user turn, returns a spoken reply plus optional setup actions."""
from __future__ import annotations
import re
from .config import Config, norm_number
from .llm import LLM, BudgetExceeded
from .skills import Skills
from .store import Store

SYSTEM = """You are {name}, {owner}'s personal voice assistant: concise, warm, dry British wit. Replies are SPOKEN: max 2 short sentences, no markdown.
You can configure yourself when asked. Put changes in "actions" (only these types):
 {{"type":"add_vip","name":str,"number":str}} | {{"type":"remove_vip","number":str}}
 {{"type":"set_job","name":str,"action":"email_check|briefing|calendar_alert","every":"3h|30m" OR "at":"HH:MM","notify":"call|message"}}
 {{"type":"remove_job","name":str}} | {{"type":"add_task","text":str,"due":str}} | {{"type":"done_task","id":int}}
 {{"type":"set_screening","enabled":bool}} | {{"type":"set_quiet_hours","start":"HH:MM","end":"HH:MM"}}
Confirm what you changed in the reply. If unsure, ask. Never invent facts: use only the context given.
Return JSON: {{"reply":str,"actions":[...]}}"""

EMAIL_WORDS = re.compile(r"\b(e-?mails?|inbox|mail)\b", re.I)
CAL_WORDS = re.compile(r"\b(calendar|schedule|meeting|meetings|today|tomorrow|agenda|busy|free)\b", re.I)


class Brain:
    def __init__(self, cfg: Config, store: Store, llm: LLM, skills: Skills):
        self.cfg, self.store, self.llm, self.skills = cfg, store, llm, skills

    def chat(self, channel: str, text: str) -> str:
        ctx = []
        tasks = self.store.open_tasks()
        if tasks:
            ctx.append("Open tasks: " + "; ".join(f"#{t['id']} {t['text']}" for t in tasks))
        ctx.append("VIPs: " + ", ".join(v["name"] for v in self.cfg["screening"]["vip"]))
        ctx.append("Jobs: " + ", ".join(f"{j['name']}({j.get('every') or 'at ' + str(j.get('at'))})" for j in self.cfg["jobs"]))
        try:
            if EMAIL_WORDS.search(text) and self.cfg["email"]["enabled"]:
                ctx.append("Important unread email: " + (self.skills.email_text(self.skills.important_email(False)) or "none"))
            if CAL_WORDS.search(text) and self.cfg["calendar"]["enabled"]:
                ctx.append("Calendar next 48h: " + (self.skills.calendar_text(48) or "nothing"))
        except Exception as e:
            ctx.append(f"(a connector failed: {type(e).__name__})")
        system = SYSTEM.format(name=self.cfg["voice"]["assistant_name"], owner=self.cfg["owner"]["name"]) + "\n" + "\n".join(ctx)
        history = self.store.recent_history(channel)
        try:
            r = self.llm.ask_json(system, history + [{"role": "user", "content": text}])
        except BudgetExceeded:
            return "I've hit today's token budget, sir. Back tomorrow, or raise the limit in the config."
        except Exception:
            return "Sorry, I couldn't reach my brain just now. Try again in a moment."
        reply = str(r.get("reply") or "Sorry, I didn't catch that.")
        self.apply(r.get("actions") or [])
        self.store.add_history(channel, "user", text)
        self.store.add_history(channel, "assistant", reply)
        return reply

    # whitelisted config changes only - the LLM can never touch anything else
    def apply(self, actions: list) -> None:
        c, changed = self.cfg.data, False
        for a in actions if isinstance(actions, list) else []:
            if not isinstance(a, dict):
                continue
            t = a.get("type")
            if t == "add_vip" and norm_number(str(a.get("number", ""))):
                n = norm_number(a["number"])
                c["screening"]["vip"] = [v for v in c["screening"]["vip"] if norm_number(v["number"]) != n] + \
                                        [{"name": str(a.get("name", n))[:40], "number": n}]
                changed = True
            elif t == "remove_vip":
                n = norm_number(str(a.get("number", "")))
                c["screening"]["vip"] = [v for v in c["screening"]["vip"] if norm_number(v["number"]) != n]
                changed = True
            elif t == "set_job" and a.get("action") in ("email_check", "briefing", "calendar_alert") and (a.get("every") or a.get("at")):
                job = {"name": str(a.get("name") or a["action"])[:30], "action": a["action"],
                       "notify": "call" if a.get("notify") == "call" else "message"}
                job["every" if a.get("every") else "at"] = str(a.get("every") or a.get("at"))
                c["jobs"] = [j for j in c["jobs"] if j["name"] != job["name"]] + [job]
                changed = True
            elif t == "remove_job":
                c["jobs"] = [j for j in c["jobs"] if j["name"] != a.get("name")]
                changed = True
            elif t == "add_task" and a.get("text"):
                self.store.add_task(str(a["text"])[:200], str(a.get("due", ""))[:40])
            elif t == "done_task" and isinstance(a.get("id"), int):
                self.store.done_task(a["id"])
            elif t == "set_screening" and isinstance(a.get("enabled"), bool):
                c["screening"]["enabled"] = a["enabled"]
                changed = True
            elif t == "set_quiet_hours" and a.get("start") and a.get("end"):
                c["owner"]["quiet_hours"] = [str(a["start"]), str(a["end"])]
                changed = True
        if changed:
            self.cfg.save()
