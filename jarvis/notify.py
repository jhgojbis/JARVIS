"""Outbound channels through Twilio: WhatsApp/SMS message, voice clip, or Jarvis phones you."""
from __future__ import annotations
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo
from . import tts
from .config import Config, norm_number

log = logging.getLogger("jarvis.notify")
SMS_CHARS = 150
HEARD_SECONDS = 10      # a call that "completed" in less than this was almost certainly voicemail or a reject


def can_call(cfg: Config, now: datetime) -> bool:
    c = cfg["calls"]
    if c["weekdays_only"] and now.weekday() >= 5:
        return False
    return c["start"] <= now.strftime("%H:%M") < c["end"]


class Notifier:
    def __init__(self, cfg: Config, client=None, store=None):
        self.cfg, self._client, self.store = cfg, client, store

    @property
    def client(self):
        if self._client is None:
            from twilio.rest import Client
            self._client = Client(Config.env("TWILIO_ACCOUNT_SID"), Config.env("TWILIO_AUTH_TOKEN"))
        return self._client

    def message(self, text: str, media: list[str] | None = None) -> None:
        o = self.cfg["owner"]
        if o.get("whatsapp"):
            extra = {"media_url": media} if media else {}
            base = Config.env("PUBLIC_URL").rstrip("/")
            m = self.client.messages.create(from_=Config.env("TWILIO_WHATSAPP_NUMBER"), to="whatsapp:" + norm_number(o["whatsapp"]),
                                            body=text[:1500], status_callback=f"{base}/whatsapp/status", **extra)
            if self.store is not None and getattr(m, "sid", None):      # remember it so an undelivered one can go out as SMS
                self.store.set(f"wa:{m.sid}", {"text": text, "media": media or []})
        else:
            self.sms(text, media)

    def sms(self, text: str, media: list[str] | None = None) -> None:
        """Fallback SMS. Every 160 characters is a billed segment (about 0.65 kr), so it is cut short, a voice clip becomes a
        link, and at most calls.sms_per_day go out per day. The full text is on WhatsApp / ask Jarvis by phone."""
        if self.store is not None:
            day, c = date.today().isoformat(), self.store.get("sms_count") or {}
            n = c.get("n", 0) if c.get("day") == day else 0
            if n >= self.cfg["calls"]["sms_per_day"]:
                log.warning("SMS limit reached for today, not sending")
                return
            self.store.set("sms_count", {"day": day, "n": n + 1})
        body = (text if len(text) <= SMS_CHARS else text[:SMS_CHARS - 3].rstrip() + "...") + (f" Listen: {media[0]}" if media else "")
        self.client.messages.create(from_=Config.env("TWILIO_NUMBER"), to=norm_number(self.cfg["owner"]["phone"]), body=body)

    def whatsapp_status(self, sid: str, status: str) -> bool:
        """WhatsApp reported on a message. Undelivered (typically the 24 h window is closed) -> resend as SMS. True if resent."""
        pending = self.store.get(f"wa:{sid}") if self.store is not None else None
        if not pending:
            return False
        if status in ("undelivered", "failed"):
            self.store.set(f"wa:{sid}", None)
            self.sms(pending["text"], pending["media"])
            return True
        if status in ("delivered", "read"):
            self.store.set(f"wa:{sid}", None)
        return False

    def voice_clip(self, text: str) -> None:
        """WhatsApp message with the text and a spoken version attached (text only if the voice can't be rendered)."""
        name = tts.render(self.cfg, text)
        self.message(text, [tts.audio_url(name)] if name and self.cfg["owner"].get("whatsapp") else None)

    def call(self, text: str, status_callback: bool = False) -> str:
        """Phone the owner; Jarvis reads `text`, then keeps listening so you can talk back. Returns the call SID."""
        base = Config.env("PUBLIC_URL").rstrip("/")
        extra = {"status_callback": f"{base}/voice/call_status", "status_callback_event": ["completed"]} if status_callback else {}
        c = self.client.calls.create(from_=Config.env("TWILIO_NUMBER"), to=norm_number(self.cfg["owner"]["phone"]),
                                     twiml=say_and_listen(self.cfg, text, f"{base}/voice/owner"), **extra)
        return c.sid

    def callback(self) -> str:
        """Ring the owner back; the call opens with the same mail digest as when the owner phones in."""
        base = Config.env("PUBLIC_URL").rstrip("/")
        return self.client.calls.create(from_=Config.env("TWILIO_NUMBER"), to=norm_number(self.cfg["owner"]["phone"]),
                                        url=f"{base}/voice/owner_start", method="POST").sid

    def alert(self, text: str, now: datetime | None = None) -> str:
        """Something you must react to: phone you inside the call window; if you don't pick up (see
        /voice/call_status) or it is outside the window, send a WhatsApp voice clip instead."""
        now = now or datetime.now(ZoneInfo(self.cfg["owner"]["timezone"]))
        if can_call(self.cfg, now):
            try:
                sid = self.call(text, status_callback=True)
                if self.store is not None:
                    self.store.set(f"alert:{sid}", text)
                return "call"
            except Exception:
                log.exception("alert call failed, sending a voice clip instead")
        self.voice_clip(text)
        return "clip"

    def call_finished(self, sid: str, status: str, duration: int) -> bool:
        """Twilio says the alert call ended. Not heard (no answer, busy, failed, voicemail) -> voice clip. True if sent."""
        text = self.store.get(f"alert:{sid}") if self.store is not None else None
        if not text:
            return False
        self.store.set(f"alert:{sid}", None)
        if status == "completed" and duration >= HEARD_SECONDS:
            return False
        self.voice_clip(text)
        return True

    def send(self, how: str, text: str) -> None:
        if how == "alert":
            self.alert(text)
        elif how == "call":
            self.call(text)
        else:
            self.message(text)


def say_and_listen(cfg: Config, text: str, action: str) -> str:
    from xml.sax.saxutils import escape
    return (f'<Response><Gather input="speech" action="{escape(action)}" speechTimeout="auto" language="{cfg["voice"]["language"]}">'
            f'{tts.tag(cfg, text)}</Gather>{tts.tag(cfg, "Goodbye.")}</Response>')
