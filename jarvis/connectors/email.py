"""Email via IMAP (works with Gmail/Outlook/iCloud/Yahoo using an app password - no OAuth needed)."""
from __future__ import annotations
import email, html, imaplib, re
from email.header import decode_header, make_header
from email.utils import parseaddr
from ..config import Config


def _dec(s):
    return str(make_header(decode_header(s or "")))


def fetch_unread(cfg: Config, limit: int = 30, snippet: int = 200) -> list[dict]:
    return _fetch(cfg, "UNSEEN", limit, snippet)


def fetch_recent(cfg: Config, limit: int = 10, snippet: int = 500) -> list[dict]:
    """Newest mail whether read or not - what the owner sees at the top of their inbox. Oldest first."""
    return _fetch(cfg, "ALL", limit, snippet)


def _fetch(cfg: Config, criteria: str, limit: int, snippet: int) -> list[dict]:
    user, pw = Config.env("EMAIL_ADDRESS"), Config.env("EMAIL_APP_PASSWORD").replace(" ", "")
    if not (user and pw):
        raise RuntimeError("EMAIL_ADDRESS / EMAIL_APP_PASSWORD not set")
    M = imaplib.IMAP4_SSL(cfg["email"]["imap_host"])
    try:
        M.login(user, pw)
        M.select("INBOX", readonly=True)       # readonly: never marks mail as read
        _, data = M.search(None, criteria)
        ids = data[0].split()[-limit:]
        out = []
        for i in ids:
            _, msg = M.fetch(i, "(FLAGS BODY.PEEK[])")
            m = parse_message(msg[0][1], snippet)
            m["unread"] = b"\\Seen" not in msg[0][0]
            out.append(m)
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def parse_message(raw: bytes, snippet: int = 200) -> dict:
    m = email.message_from_bytes(raw)
    name, addr = parseaddr(_dec(m.get("From")))
    parts = {}
    for part in (m.walk() if m.is_multipart() else [m]):
        if part.get_content_type() in ("text/plain", "text/html") and part.get_content_type() not in parts:
            parts[part.get_content_type()] = (part.get_payload(decode=True) or b"").decode(
                part.get_content_charset() or "utf-8", "replace")
    body = parts.get("text/plain") or ""
    if not body.strip() and "text/html" in parts:   # many senders ship HTML only
        body = html.unescape(re.sub(r"<(style|script)\b.*?</\1>|<[^>]+>", " ", parts["text/html"], flags=re.S | re.I))
    return {"id": m.get("Message-ID", ""), "from": f"{name} <{addr}>" if name else addr,
            "subject": _dec(m.get("Subject")), "snippet": " ".join(body.split())[:snippet]}
