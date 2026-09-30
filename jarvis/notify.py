"""Outbound channels through Twilio: WhatsApp/SMS message, or Jarvis phones you."""
from __future__ import annotations
from xml.sax.saxutils import escape
from .config import Config, norm_number


class Notifier:
    def __init__(self, cfg: Config, client=None):
        self.cfg, self._client = cfg, client

    @property
    def client(self):
        if self._client is None:
            from twilio.rest import Client
            self._client = Client(Config.env("TWILIO_ACCOUNT_SID"), Config.env("TWILIO_AUTH_TOKEN"))
        return self._client

    def message(self, text: str) -> None:
        o = self.cfg["owner"]
        if o.get("whatsapp"):
            self.client.messages.create(from_=Config.env("TWILIO_WHATSAPP_NUMBER"),
                                        to="whatsapp:" + norm_number(o["whatsapp"]), body=text[:1500])
        else:
            self.client.messages.create(from_=Config.env("TWILIO_NUMBER"), to=norm_number(o["phone"]), body=text[:1500])

    def call(self, text: str) -> None:
        """Phone the owner; Jarvis reads `text`, then keeps listening so you can talk back."""
        base = Config.env("PUBLIC_URL").rstrip("/")
        self.client.calls.create(from_=Config.env("TWILIO_NUMBER"), to=norm_number(self.cfg["owner"]["phone"]),
                                 twiml=say_and_listen(self.cfg, text, f"{base}/voice/owner"))

    def send(self, how: str, text: str) -> None:
        self.call(text) if how == "call" else self.message(text)


def say_and_listen(cfg: Config, text: str, action: str) -> str:
    v = cfg["voice"]
    return (f'<Response><Gather input="speech" action="{escape(action)}" speechTimeout="auto" language="{v["language"]}">'
            f'<Say voice="{v["twilio_voice"]}" language="{v["language"]}">{escape(text)}</Say></Gather>'
            f'<Say voice="{v["twilio_voice"]}">Goodbye.</Say></Response>')
