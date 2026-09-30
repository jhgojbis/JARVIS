"""FastAPI app: Twilio voice/WhatsApp webhooks + browser chat. Run with `jarvis run`."""
from __future__ import annotations
import asyncio, hmac, logging
from contextlib import asynccontextmanager
from pathlib import Path
from xml.sax.saxutils import escape
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from .brain import Brain
from .config import Config, norm_number
from .llm import LLM
from .notify import Notifier, say_and_listen
from .scheduler import Scheduler
from .screening import classify, vip_name
from .skills import Skills
from .store import Store

log = logging.getLogger("jarvis")


def xml(body: str) -> Response:
    return Response(f'<?xml version="1.0" encoding="UTF-8"?>{body}', media_type="application/xml")


def create_app(cfg: Config, store: Store | None = None, llm: LLM | None = None,
               notifier: Notifier | None = None, verify_twilio: bool | None = None) -> FastAPI:
    store = store or Store()
    llm = llm or LLM(cfg, store)
    notifier = notifier or Notifier(cfg)
    skills = Skills(cfg, store, llm)
    brain = Brain(cfg, store, llm, skills)
    sched = Scheduler(cfg, store, skills, notifier)
    @asynccontextmanager
    async def lifespan(app):
        task = None if Config.env("PYTEST_CURRENT_TEST") else asyncio.create_task(sched.loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Jarvis", lifespan=lifespan)
    app.state.brain, app.state.sched, app.state.store = brain, sched, store
    verify = bool(Config.env("TWILIO_AUTH_TOKEN")) if verify_twilio is None else verify_twilio

    def say(text: str) -> str:
        v = cfg["voice"]
        return f'<Say voice="{v["twilio_voice"]}" language="{v["language"]}">{escape(text)}</Say>'

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
        return f'<Dial callerId="{escape(caller)}" timeout="25" action="{url("/voice/after_dial")}">{num}</Dial>'

    # ---------------- incoming call (forward your phone to the Twilio number) ----------------
    @app.post("/voice/incoming")
    async def incoming(request: Request):
        f = await check_twilio(request)
        caller = f.get("From", "")
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
        r = await asyncio.get_running_loop().run_in_executor(None, classify, llm, cfg, speech)
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
        reply = await asyncio.get_running_loop().run_in_executor(None, brain.chat, "voice", speech)
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
