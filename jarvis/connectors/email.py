"""Email via IMAP (works with Gmail/Outlook/iCloud/Yahoo using an app password - no OAuth needed)."""
from __future__ import annotations
import email, imaplib
from email.header import decode_header, make_header
from email.utils import parseaddr
from ..config import Config


def _dec(s):
    return str(make_header(decode_header(s or "")))


def fetch_unread(cfg: Config, limit: int = 30, snippet: int = 200) -> list[dict]:
    user, pw = Config.env("EMAIL_ADDRESS"), Config.env("EMAIL_APP_PASSWORD")
    if not (user and pw):
        raise RuntimeError("EMAIL_ADDRESS / EMAIL_APP_PASSWORD not set")
    M = imaplib.IMAP4_SSL(cfg["email"]["imap_host"])
    try:
        M.login(user, pw)
        M.select("INBOX", readonly=True)       # readonly: never marks mail as read
        _, data = M.search(None, "UNSEEN")
        ids = data[0].split()[-limit:]
        out = []
        for i in ids:
            _, msg = M.fetch(i, "(BODY.PEEK[])")
            out.append(parse_message(msg[0][1], snippet))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def parse_message(raw: bytes, snippet: int = 200) -> dict:
    m = email.message_from_bytes(raw)
    name, addr = parseaddr(_dec(m.get("From")))
    body = ""
    for part in (m.walk() if m.is_multipart() else [m]):
        if part.get_content_type() == "text/plain":
            body = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
            break
    return {"id": m.get("Message-ID", ""), "from": f"{name} <{addr}>" if name else addr,
            "subject": _dec(m.get("Subject")), "snippet": " ".join(body.split())[:snippet]}
