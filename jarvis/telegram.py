"""Telegram Bot API: free, no 24 h window, no sandbox that expires. Jarvis talks to its owner (and only its owner) here.
Updates arrive on a webhook (/telegram, protected by a secret header); replies go out through the Bot API."""
from __future__ import annotations
import hashlib
from pathlib import Path
import requests


def secret_for(token: str) -> str:
    """Value Telegram echoes in X-Telegram-Bot-Api-Secret-Token, so only Telegram can post to our webhook."""
    return hashlib.sha256(("jarvis-tg|" + token).encode()).hexdigest()[:48]


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, timeout: float = 25, **params):
        r = requests.post(f"{self.base}/{method}", json=params, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"telegram {method}: HTTP {r.status_code}")
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description', r.status_code)}")
        return data["result"]

    def send_message(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        """`buttons` = rows of (label, callback_data)."""
        extra = {"reply_markup": {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}} if buttons else {}
        self.call("sendMessage", chat_id=chat_id, text=text[:4096], **extra)

    def send_audio(self, chat_id: int, path: Path, caption: str = "") -> None:
        with open(path, "rb") as f:
            r = requests.post(f"{self.base}/sendAudio", data={"chat_id": chat_id, "caption": caption[:1000], "title": "Jarvis"},
                              files={"audio": ("jarvis.mp3", f, "audio/mpeg")}, timeout=60)
        if not r.json().get("ok"):
            raise RuntimeError(f"telegram sendAudio: {r.json().get('description', r.status_code)}")

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", timeout=5, chat_id=chat_id, action="typing")
        except Exception:
            pass

    def answer_callback(self, callback_id: str) -> None:
        try:
            self.call("answerCallbackQuery", timeout=5, callback_query_id=callback_id)
        except Exception:
            pass

    def set_webhook(self, url: str) -> None:
        self.call("setWebhook", url=url, secret_token=secret_for(self.token), allowed_updates=["message", "callback_query"],
                  drop_pending_updates=False)
