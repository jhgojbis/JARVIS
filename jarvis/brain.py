"""Conversation brain: one LLM call per user turn, returns a spoken reply plus optional setup actions."""
from __future__ import annotations
import logging, re
from . import errands
from .config import Config, norm_number
from .llm import LLM, BudgetExceeded
from .skills import Skills
from .store import Store

SYSTEM = """You are {name}, {owner}'s personal voice assistant: concise, warm, dry British wit. Replies are SPOKEN: max 2 short sentences, no markdown.
Exception: when asked to read or summarise an email, pick the best match from the inbox below (by sender, subject or topic, even if
the name is approximate) and give the gist in at most 3 short sentences: who it is from, what it says, and whether anything must be
done. Skip reference numbers, IDs, links and footers unless asked. Translate into English if it is in another language.
You can configure yourself when asked. Put changes in "actions" (only these types):
 {{"type":"add_vip","name":str,"number":str}} | {{"type":"remove_vip","number":str}}
 {{"type":"set_job","name":str,"action":"email_check|briefing|calendar_alert","every":"3h|30m" OR "at":"HH:MM","notify":"alert|call|message"}}   (alert = phone me, WhatsApp voice clip if I miss it)
 {{"type":"remove_job","name":str}} | {{"type":"add_task","text":str,"due":str}} | {{"type":"done_task","id":int}}
 {{"type":"set_screening","enabled":bool}} | {{"type":"set_quiet_hours","start":"HH:MM","end":"HH:MM"}}
 {{"type":"send_whatsapp","text":str,"voice":bool}} = send {owner} a WhatsApp message (only ever to them). Use it when asked to send, forward or
 WhatsApp something. "text" is the COMPLETE message (e.g. the whole summary, in English, max 900 chars), never a placeholder; "voice":true also attaches it as a spoken clip.
 {{"type":"trash_email","n":int}} = move numbered email [n] to the trash (recoverable). Only when {owner} tells you to delete/remove/trash
 it; no confirmation needed. n is the [number] in the numbered list below, the same numbers as in the summary sent to WhatsApp and read out ("delete all" = every
 number of the latest summary, [1]..[N]; "delete 2" = [2]). If the owner names an email instead of a number, find it by sender/subject. One trash_email action each. If two emails match, ask which one instead of guessing.
 {{"type":"draft_email","n":int,"forward":bool,"to":str,"subject":str,"body":str,"account":str}} = prepare an email, NEVER sends. Reply to numbered email [n] ("reply 2 ..."): give n and the
 body (to/subject/account are filled in: it leaves from the account that received it). Forward [n] ("forward 3 to ..."): n, forward:true, to, and a short body. New mail: to (a real address seen in the listing or given by {owner}), subject, body, and "account" = one of the email accounts listed below (default: the first).
 Write the body in the language of the person it goes to, as {owner} would, short, signed with {owner}'s name only. After drafting, your reply MUST read out the
 recipient address, the account it is sent from, the subject and the complete text, then ask whether to send it. Say it is a draft, never that it was sent.
 {{"type":"send_email"}} = send the pending draft. ONLY when {owner} says send/yes/go ahead AFTER hearing the draft in an earlier turn, never in the same turn as draft_email.
 {{"type":"discard_draft"}}
 {{"type":"find_places","request":str}} = research restaurants/places on the web in the background and WhatsApp {owner} the options with map and Uber Eats links.
 Use it for "find me a good restaurant in X". Reply at once with one short sentence like "On it, I'll WhatsApp you the options in a minute." Never list places yourself.
 {{"type":"add_favorite","item":str}} | {{"type":"remove_favorite","item":str}} = {owner}'s favourite groceries (Swedish item names, e.g. "havregryn")
 {{"type":"send_groceries","items":[str]}} = WhatsApp {owner} Hemköp links for the items (default: all favourites). You cannot fill the cart, order or pay: say so if asked.
Never ask which mailbox: every mailbox is already in the numbered list. When asked to check or read email, use the list; do not ask what to check.
Confirm what you changed in the reply. If unsure, ask. Never invent facts: use only the context given.
Return JSON: {{"reply":str,"actions":[...]}}"""

log = logging.getLogger("jarvis.brain")
EMAIL_WORDS = re.compile(r"\b(e-?mails?|inbox|mail|read|said|says|write|writes|wrote|written|sent|messages?)\b", re.I)
CAL_WORDS = re.compile(r"\b(calendar|schedule|meeting|meetings|today|tomorrow|agenda|busy|free)\b", re.I)


class Brain:
    def __init__(self, cfg: Config, store: Store, llm: LLM, skills: Skills, notifier=None):
        self.cfg, self.store, self.llm, self.skills, self.notifier = cfg, store, llm, skills, notifier

    def chat(self, channel: str, text: str) -> str:
        ctx = []
        tasks = self.store.open_tasks()
        if tasks:
            ctx.append("Open tasks: " + "; ".join(f"#{t['id']} {t['text']}" for t in tasks))
        draft = self.skills.pending_draft()
        if draft:
            ctx.append(f"Pending draft, NOT sent yet. To: {draft['to']} | Subject: {draft['subject']} | Text: {draft['body']}")
        favs = self.store.get("favorites", [])
        if favs:
            ctx.append("Favourite groceries: " + ", ".join(favs))
        ctx.append("VIPs: " + ", ".join(v["name"] for v in self.cfg["screening"]["vip"]))
        ctx.append("Jobs: " + ", ".join(f"{j['name']}({j.get('every') or 'at ' + str(j.get('at'))})" for j in self.cfg["jobs"]))
        try:
            if self.cfg["email"]["enabled"]:
                ctx.append("Email accounts: " + (self.skills.account_names() or "none with credentials"))
            if self.cfg["email"]["enabled"]:
                if EMAIL_WORDS.search(text):           # asking about mail: refresh the recent mail behind the numbered list
                    ctx.append("Numbered emails (all mailboxes):\n" + (self.skills.inbox_text() or "empty"))
                elif self.skills.inbox_recent():       # "delete 1", "reply 2 ...": use the saved list, no slow mailbox fetch
                    ctx.append("Numbered emails (all mailboxes):\n" + (self.skills.numbered_text() or "empty"))
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
        failed = self.apply(r.get("actions") or [])
        if failed:                                      # an action failed: never claim it worked
            reply = failed
        self.store.add_history(channel, "user", text)
        self.store.add_history(channel, "assistant", reply)
        return reply

    # whitelisted config changes only - the LLM can never touch anything else
    def apply(self, actions: list) -> str:
        """Runs the whitelisted actions. Returns what to say instead of the LLM's reply if one failed, else ''."""
        c, changed, failed, drafted = self.cfg.data, False, "", False
        trash_ns = [a["n"] for a in actions if isinstance(a, dict) and a.get("type") == "trash_email" and isinstance(a.get("n"), int)]
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
                       "notify": a["notify"] if a.get("notify") in ("call", "alert") else "message"}
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
            elif t == "send_whatsapp" and a.get("text") and self.notifier is not None:
                try:
                    (self.notifier.voice_clip if a.get("voice") is True else self.notifier.message)(str(a["text"])[:1000])
                except Exception:
                    log.exception("send_whatsapp failed")
                    failed = "I tried to send that to your WhatsApp, but it was refused. You may need to message me there first."
            elif t == "draft_email":
                try:
                    self.skills.make_draft(a)
                    drafted = True
                except Exception as e:
                    log.exception("draft_email failed")
                    failed = "I need a proper email address, a subject and some text for that." if isinstance(e, ValueError) \
                        else "I could not prepare that email."
            elif t == "send_email":
                if drafted:                       # a draft is only ever sent after the owner has heard it
                    failed = "That is only a draft. Tell me to send it once you have heard it."
                else:
                    try:
                        log.info("sent email to %s", self.skills.send_draft())
                    except Exception:
                        log.exception("send_email failed")
                        failed = "I could not send that email. There may be no draft waiting."
            elif t == "discard_draft":
                self.store.set("draft", None)
            elif t == "find_places" and a.get("request") and self.notifier is not None:
                errands.run_async(errands.places_job, self.cfg, self.notifier, str(a["request"])[:200])
            elif t == "add_favorite" and a.get("item"):
                favs = self.store.get("favorites", [])
                item = str(a["item"]).strip()[:60]
                if item and item.lower() not in [f.lower() for f in favs]:
                    self.store.set("favorites", (favs + [item])[:100])
            elif t == "remove_favorite" and a.get("item"):
                self.store.set("favorites", [f for f in self.store.get("favorites", []) if f.lower() != str(a["item"]).strip().lower()])
            elif t == "send_groceries" and self.notifier is not None:
                given = a.get("items")
                items = [str(i).strip()[:60] for i in given if str(i).strip()][:30] if isinstance(given, list) and given else self.store.get("favorites", [])
                if not items:
                    failed = "There is nothing on your grocery list yet."
                else:
                    try:
                        self.notifier.message(errands.grocery_message(items))
                    except Exception:
                        log.exception("send_groceries failed")
                        failed = "I tried to send that to your WhatsApp, but it was refused. You may need to message me there first."
            elif t == "trash_email" and isinstance(a.get("n"), int):
                pass                                    # handled together below, so several deletions share one listing
        if trash_ns:
            try:
                log.info("trashed emails: %s", self.skills.trash_inbox_items(trash_ns))
            except Exception:
                log.exception("trash_email failed")
                failed = failed or "I could not move all of those emails to the trash."
        if changed:
            self.cfg.save()
        return failed
