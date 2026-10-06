"""FastAPI app: Twilio voice/WhatsApp webhooks + browser chat. Run with `jarvis run`."""
from __future__ import annotations
import asyncio, hmac, logging, re, threading, time
from contextlib import asynccontextmanager
from pathlib import Path
from xml.sax.saxutils import escape
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from . import receipts, stt, tts
from .connectors import lights
from .telegram import Telegram, secret_for
from .brain import Brain
from .config import Config, norm_number
from .llm import LLM
from .notify import Notifier, say_and_listen
from .scheduler import Scheduler
from .screening import classify, vip_name
from .skills import Skills
from .store import Store

log = logging.getLogger("jarvis")

# Twilio abandons a webhook after 15 s, but the claude_cli backend can take longer. Voice replies not ready
# within FAST_WAIT are finished in the background while Twilio polls /voice/owner_wait (each poll < HOLD_WAIT).
FAST_WAIT, HOLD_WAIT, MAX_WAIT, SCREEN_TIMEOUT = 3.0, 9.0, 90.0, 12.0
CALLBACK_DELAY, CALLBACK_COOLDOWN = 4.0, 30.0       # seconds: wait before ringing back / minimum gap between two call-backs
PHRASES = ("Goodbye.", "One moment, sir.", "Good day. One moment while I check your mail.", "I did not hear anything. Goodbye.",
           "One moment, let me see if they are available.", "Thank you. Goodbye.", "Very good. Goodbye.",
           "Sorry, I lost my train of thought. What was that?", "Sorry. Goodbye.")


def xml(body: str) -> Response:
    return Response(f'<?xml version="1.0" encoding="UTF-8"?>{body}', media_type="application/xml")


def create_app(cfg: Config, store: Store | None = None, llm: LLM | None = None,
               notifier: Notifier | None = None, verify_twilio: bool | None = None, telegram=None) -> FastAPI:
    store = store or Store()
    llm = llm or LLM(cfg, store)
    tg_token = Config.env("TELEGRAM_BOT_TOKEN")
    tg = telegram or (Telegram(tg_token) if tg_token else None)
    notifier = notifier or Notifier(cfg, store=store, telegram=tg)
    skills = Skills(cfg, store, llm)
    brain = Brain(cfg, store, llm, skills, notifier)
    sched = Scheduler(cfg, store, skills, notifier)
    pending: dict[str, tuple[asyncio.Future, float]] = {}   # CallSid -> (owner reply in progress, start time)
    @asynccontextmanager
    async def lifespan(app):
        testing = bool(Config.env("PYTEST_CURRENT_TEST"))
        if not testing:      # render the fixed phrases now so calls never wait for the voice
            greeting = cfg["screening"]["greeting"].format(assistant=cfg["voice"]["assistant_name"], owner=cfg["owner"]["name"])
            threading.Thread(target=lambda: [tts.render(cfg, t) for t in (*PHRASES, greeting)], daemon=True).start()
        if not testing and tg is not None:
            def register():
                try:
                    tg.set_webhook(url("/telegram"))
                except Exception:
                    log.exception("could not register the Telegram webhook")
            threading.Thread(target=register, daemon=True).start()
        if not testing and cfg["lights"]["enabled"]:     # reading the gateway takes ~15 s: keep the lamp layout warm in the background
            def keep_warm():
                while True:
                    try:
                        lights.devices(cfg, fresh=True)
                    except Exception:
                        log.exception("could not read the lights")
                    time.sleep(lights.TTL - 60)
            threading.Thread(target=keep_warm, daemon=True).start()
        task = None if testing else asyncio.create_task(sched.loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Jarvis", lifespan=lifespan)
    app.state.brain, app.state.sched, app.state.store = brain, sched, store
    verify = bool(Config.env("TWILIO_AUTH_TOKEN")) if verify_twilio is None else verify_twilio

    def say(text: str) -> str:
        return tts.tag(cfg, text)

    def url(path: str) -> str:
        return Config.env("PUBLIC_URL").rstrip("/") + path

    async def check_twilio(request: Request) -> dict:
        form = dict((await request.form()).items())
        if verify:
            from twilio.request_validator import RequestValidator
            sig = request.headers.get("X-Twilio-Signature", "")
            if not RequestValidator(Config.env("TWILIO_AUTH_TOKEN")).validate(url(request.url.path), form, sig):
                raise HTTPException(403, "bad twilio signature")
        return form

    def dial_owner(caller: str, whisper: str = "") -> str:
        num = f'<Number url="{escape(url("/voice/whisper") + "?t=" + whisper)}">{norm_number(cfg["owner"]["phone"])}</Number>' \
            if whisper else f'<Number>{norm_number(cfg["owner"]["phone"])}</Number>'
        return f'<Dial callerId="{escape(caller)}" timeout="15" action="{url("/voice/after_dial")}">{num}</Dial>'

    # ---------------- incoming call (forward your phone to the Twilio number) ----------------
    @app.post("/voice/incoming")
    async def incoming(request: Request):
        f = await check_twilio(request)
        caller = f.get("From", "")
        if norm_number(caller) and norm_number(caller) == norm_number(Config.env("TWILIO_NUMBER")):
            return xml("<Response><Hangup/></Response>")      # our own number: an unanswered alert call forwarded back to us
        if norm_number(caller) and norm_number(caller) == norm_number(cfg["owner"]["phone"]):
            if cfg["calls"]["callback"]:
                # Reject (not billed, so it costs the owner nothing) and ring back. Spoofing the owner's number is harmless
                # here: the call-back always goes to the real owner number, and repeats are rate-limited.
                if time.time() - store.get("callback_ts", 0) > CALLBACK_COOLDOWN:
                    store.set("callback_ts", time.time())
                    loop = asyncio.get_running_loop()
                    loop.call_later(CALLBACK_DELAY, lambda: loop.run_in_executor(None, notifier.callback))
                return xml('<Response><Reject reason="busy"/></Response>')
            # Caller ID can be spoofed, so a keypad PIN (JARVIS_PHONE_PIN in .env) guards owner mode when set.
            if Config.env("JARVIS_PHONE_PIN"):
                return xml(f'<Response><Gather input="dtmf" finishOnKey="#" timeout="8" action="{url("/voice/owner_pin")}">'
                           f'{say("Good day. Your PIN, please.")}</Gather><Hangup/></Response>')
            return await owner_digest(f.get("CallSid", ""))
        name = vip_name(cfg, caller)
        if name or not cfg["screening"]["enabled"]:
            store.log_call(caller, name or "", "", "vip" if name else "passthrough")
            return xml(f"<Response>{dial_owner(caller)}</Response>")
        greeting = cfg["screening"]["greeting"].format(assistant=cfg["voice"]["assistant_name"], owner=cfg["owner"]["name"])
        return xml(f'<Response><Gather input="speech" action="{url("/voice/screen")}" speechTimeout="auto" '
                   f'timeout="6" language="{cfg["voice"]["language"]}">{say(greeting)}</Gather>'
                   f'{say("I did not hear anything. Goodbye.")}<Hangup/></Response>')

    @app.post("/voice/screen")
    async def screen(request: Request):
        f = await check_twilio(request)
        caller, speech = f.get("From", ""), f.get("SpeechResult", "")
        if not speech.strip():
            store.log_call(caller, "", "silence", "spam")
            return xml("<Response><Hangup/></Response>")
        try:
            r = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, classify, llm, cfg, speech),
                                       SCREEN_TIMEOUT)
        except asyncio.TimeoutError:           # too slow: take a message, never connect
            r = {"name": "", "reason": speech[:160], "verdict": "message"}
        if r["verdict"] == "connect":
            store.log_call(caller, r["name"], r["reason"], "connected")
            note = f"{r['name'] or 'Someone'} says: {r['reason']}".replace("&", "and")[:150]
            return xml(f'<Response>{say("One moment, let me see if they are available.")}{dial_owner(caller, note)}</Response>')
        if r["verdict"] == "spam" and cfg["screening"]["block_unknown_spam"]:
            store.log_call(caller, r["name"], r["reason"], "spam")
            return xml(f'<Response>{say("Thank you. Goodbye.")}<Hangup/></Response>')
        store.log_call(caller, r["name"], r["reason"], "message")
        try:
            notifier.message(f"Call from {r['name'] or caller} ({caller}): {r['reason']}")
        except Exception:
            log.exception("could not notify owner")
        return xml(f'<Response>{say(cfg["owner"]["name"] + " is unavailable right now, but I have passed on your message. Goodbye.")}<Hangup/></Response>')

    @app.post("/voice/whisper")
    async def whisper(request: Request, t: str = ""):
        await check_twilio(request)
        return xml(f'<Response><Gather numDigits="1" action="{url("/voice/whisper_ok")}" timeout="6">'
                   f'{say("Screened call. " + t + ". Press 1 to accept.")}</Gather><Hangup/></Response>')

    @app.post("/voice/whisper_ok")
    async def whisper_ok(request: Request):
        f = await check_twilio(request)
        return xml("<Response/>" if f.get("Digits") == "1" else "<Response><Hangup/></Response>")

    @app.post("/voice/after_dial")
    async def after_dial(request: Request):
        f = await check_twilio(request)
        if f.get("DialCallStatus") == "completed":
            return xml("<Response/>")
        return xml(f'<Response>{say(cfg["owner"]["name"] + " could not pick up. Your message has been noted. Goodbye.")}<Hangup/></Response>')

    # ---------------- owner talks to Jarvis by phone (Jarvis calls you, or you call the Twilio number from your own phone) ----------------
    @app.post("/voice/owner")
    async def owner_turn(request: Request):
        f = await check_twilio(request)
        speech = f.get("SpeechResult", "").strip()
        if not speech or speech.lower().strip(".") in ("goodbye", "bye", "thanks", "that's all", "thank you"):
            return xml(f'<Response>{say("Very good. Goodbye.")}<Hangup/></Response>')
        sid = f.get("CallSid", "")
        work = asyncio.get_running_loop().run_in_executor(None, brain.chat, "voice", speech)
        pending[sid] = (work, time.monotonic())
        return await owner_reply(sid, FAST_WAIT, hold="One moment, sir.")

    @app.post("/voice/owner_pin")
    async def owner_pin(request: Request):
        f = await check_twilio(request)
        if hmac.compare_digest(f.get("Digits", ""), Config.env("JARVIS_PHONE_PIN")):
            return await owner_digest(f.get("CallSid", ""))
        return xml(f'<Response>{say("Sorry. Goodbye.")}<Hangup/></Response>')

    @app.post("/voice/owner_wait")
    async def owner_wait(request: Request):
        f = await check_twilio(request)
        sid = f.get("CallSid", "")
        if sid not in pending:
            return xml(say_and_listen(cfg, "Sorry, I lost my train of thought. What was that?", url("/voice/owner")))
        return await owner_reply(sid, HOLD_WAIT)

    async def owner_digest(sid: str) -> Response:
        """First thing when the owner phones in: read the inbox, say what needs them, then listen."""
        def run() -> str:
            text, summary = skills.digest_with_summary()
            if summary:                       # the numbered list goes to your phone before Jarvis starts reading it out
                try:
                    notifier.message(summary, buttons=skills.summary_buttons())
                except Exception:
                    log.exception("could not send the numbered summary")
            store.add_history("voice", "assistant", text)
            return text
        pending[sid] = (asyncio.get_running_loop().run_in_executor(None, run), time.monotonic())
        return await owner_reply(sid, FAST_WAIT, hold="Good day. One moment while I check your mail.")

    def tg_trash(which: str) -> str:
        """Button press: deterministic, no LLM. 'all' = every item of the latest summary that is still there."""
        items = skills.numbered()[:skills.summary_count()]
        ns = [i for i, m in enumerate(items, 1) if not m.get("gone")] if which == "all" else [int(which)]
        if not ns:
            return "Nothing left to delete."
        try:
            names = skills.trash_inbox_items(ns)
        except Exception:
            log.exception("telegram delete failed")
            return "I could not move all of those to the trash."
        return "Moved to the trash: " + "; ".join(names)

    def tg_reply_draft(n: int) -> None:
        """Button "Reply N": write a draft, show it in full and let the owner send or discard it. Nothing leaves without the Send button."""
        owner = int(cfg["owner"]["telegram_chat_id"])
        tg.typing(owner)
        try:
            d = skills.suggest_reply(n)
        except Exception:
            log.exception("reply draft failed")
            tg.send_message(owner, "I could not write that reply.")
            return
        tg.send_message(owner, f"Draft (not sent) from {d['account']}\nTo: {d['to']}\nSubject: {d['subject']}\n\n{d['body']}",
                        [[("Send", "draft:send"), ("Discard", "draft:discard")]])

    def tg_draft_action(what: str) -> str:
        if what == "discard":
            store.set("draft", None)
            return "Draft discarded."
        try:
            return "Sent to " + skills.send_draft()
        except Exception:
            log.exception("sending the draft failed")
            return "I could not send that. There may be no draft waiting (they expire after 30 minutes)."

    def tg_text(owner: int, text: str) -> None:
        t0 = time.monotonic()
        tg.typing(owner)
        reply = brain.chat("telegram", text[:1000])
        t1 = time.monotonic()
        tg.send_message(owner, reply)
        # latest timing, readable from the database: how long the brain took vs the whole turn
        store.set("tg_timing", {"brain_s": round(t1 - t0, 2), "total_s": round(time.monotonic() - t0, 2), "text": text[:30]})

    def tg_voice(owner: int, file_id: str) -> None:
        """A voice note: transcribed here (Swedish or English), then handled like typed text."""
        tg.typing(owner)
        try:
            text = stt.transcribe(tg.download(file_id))
        except Exception:
            log.exception("voice note failed")
            tg.send_message(owner, "I could not understand that voice note.")
            return
        if not text:
            tg.send_message(owner, "I heard nothing in that voice note.")
            return
        tg.send_message(owner, f"Heard: {text}")
        tg_text(owner, text)

    def tg_receipt(owner: int, file_id: str, media_type: str) -> None:
        tg.typing(owner)
        try:
            image = tg.download(file_id)
            d = receipts.read(image, media_type)
            receipts.save(d, image)
        except Exception:
            log.exception("receipt failed")
            tg.send_message(owner, "I could not read that receipt. Try a sharper, straight-on photo.")
            return
        tg.send_message(owner, "Logged (draft for the bookkeeping, check it against the receipt):\n" + receipts.describe(d))

    def tg_handle(update: dict) -> None:
        owner = int(cfg["owner"].get("telegram_chat_id") or 0)
        cb, msg = update.get("callback_query"), update.get("message") or {}
        sender = ((cb or msg).get("from") or {}).get("id")
        if not owner or sender != owner:
            return                                      # everyone but the owner is ignored, even if they find the bot
        try:
            if cb:
                tg.answer_callback(cb["id"])
                data = str(cb.get("data", ""))
                if data == "del:all" or re.fullmatch(r"del:\d{1,2}", data):
                    tg.send_message(owner, tg_trash(data.split(":")[1]))
                elif re.fullmatch(r"rep:\d{1,2}", data):
                    tg_reply_draft(int(data.split(":")[1]))
                elif data in ("draft:send", "draft:discard"):
                    tg.send_message(owner, tg_draft_action(data.split(":")[1]))
            elif msg.get("voice") or msg.get("audio"):
                tg_voice(owner, (msg.get("voice") or msg["audio"])["file_id"])
            elif msg.get("photo"):
                tg_receipt(owner, msg["photo"][-1]["file_id"], "image/jpeg")           # the largest size Telegram made
            elif str((msg.get("document") or {}).get("mime_type", "")).startswith("image/"):
                tg_receipt(owner, msg["document"]["file_id"], msg["document"]["mime_type"])
            elif str(msg.get("text", "")).strip():
                tg_text(owner, str(msg["text"]))
        except Exception:
            log.exception("telegram update failed")

    @app.post("/telegram")
    async def telegram_hook(request: Request):
        if tg is None:
            raise HTTPException(404)
        if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret_for(getattr(tg, "token", "") or tg_token)):
            raise HTTPException(403, "bad telegram secret")
        update = await request.json()
        uid = int(update.get("update_id", 0))
        if uid and uid <= store.get("tg_update", 0):      # Telegram retries: handle each update once
            return {"ok": True}
        store.set("tg_update", uid)
        await asyncio.get_running_loop().run_in_executor(None, tg_handle, update)
        return {"ok": True}

    @app.post("/voice/owner_start")
    async def owner_start(request: Request):
        """The call-back Jarvis placed to the owner was answered: just ask what they want, no mail digest."""
        await check_twilio(request)
        return xml(say_and_listen(cfg, f"Good day, {cfg['owner']['name']}. What can I do for you?", url("/voice/owner")))

    @app.post("/voice/call_status")
    async def call_status(request: Request):
        """Twilio reports how an alert call ended; if you did not hear it, a WhatsApp voice clip goes out instead."""
        f = await check_twilio(request)
        try:
            duration = int(f.get("CallDuration") or 0)
        except ValueError:
            duration = 0
        await asyncio.get_running_loop().run_in_executor(
            None, notifier.call_finished, f.get("CallSid", ""), f.get("CallStatus", ""), duration)
        return Response(status_code=204)

    @app.post("/whatsapp/status")
    async def whatsapp_status(request: Request):
        """Delivery receipts for what Jarvis sent: an undelivered WhatsApp message is resent as SMS."""
        f = await check_twilio(request)
        await asyncio.get_running_loop().run_in_executor(
            None, notifier.whatsapp_status, f.get("MessageSid", ""), f.get("MessageStatus", ""))
        return Response(status_code=204)

    @app.get("/audio/{name}")
    async def audio(name: str):
        path = tts.AUDIO_DIR / name
        if not re.fullmatch(r"[0-9a-f]{32}\.mp3", name) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/mpeg")

    async def owner_reply(sid: str, wait: float, hold: str = "") -> Response:
        work, started = pending[sid]
        try:
            reply = await asyncio.wait_for(asyncio.shield(work), wait)
        except asyncio.TimeoutError:
            if time.monotonic() - started < MAX_WAIT:
                return xml(f'<Response>{say(hold) if hold else ""}<Redirect method="POST">{url("/voice/owner_wait")}</Redirect></Response>')
            pending.pop(sid, None)
            return xml(say_and_listen(cfg, "Sorry, sir, that is taking far too long. Anything else?", url("/voice/owner")))
        pending.pop(sid, None)
        return xml(say_and_listen(cfg, reply, url("/voice/owner")))

    # ---------------- WhatsApp (Twilio sandbox or approved sender) ----------------
    @app.post("/whatsapp")
    async def whatsapp(request: Request):
        f = await check_twilio(request)
        if norm_number(f.get("From", "")) != norm_number(cfg["owner"]["whatsapp"] or cfg["owner"]["phone"]):
            return xml("<Response/>")               # ignore everyone but the owner
        reply = await asyncio.get_running_loop().run_in_executor(None, brain.chat, "whatsapp", f.get("Body", ""))
        return xml(f"<Response><Message>{escape(reply)}</Message></Response>")

    # ---------------- browser chat (talk like ChatGPT; free browser STT/TTS) ----------------
    @app.get("/", response_class=HTMLResponse)
    async def page():
        return (Path(__file__).parent / "web" / "index.html").read_text().replace("__NAME__", cfg["voice"]["assistant_name"])

    @app.post("/chat")
    async def chat(request: Request):
        body = await request.json()
        token = Config.env("JARVIS_CHAT_TOKEN")
        if not token or not hmac.compare_digest(str(body.get("token", "")), token):
            raise HTTPException(401, "bad token")
        text = str(body.get("text", ""))[:1000]
        reply = await asyncio.get_running_loop().run_in_executor(None, brain.chat, "web", text)
        return {"reply": reply}

    @app.get("/health")
    async def health():
        return {"ok": True, "tokens_today": store.tokens_today()}

    return app
