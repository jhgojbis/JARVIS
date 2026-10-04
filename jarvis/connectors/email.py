"""Email via IMAP/SMTP (works with Gmail/Outlook/iCloud/Yahoo using an app password - no OAuth needed).
Several accounts are supported: config.yaml `email.accounts`, credentials in .env as <PREFIX>EMAIL_ADDRESS / <PREFIX>EMAIL_APP_PASSWORD.
Every function takes `account=<name>`; without it the first account is used."""
from __future__ import annotations
import email, html, imaplib, re, smtplib, time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from email.header import decode_header, make_header
from email.utils import parseaddr
from ..config import Config


def _dec(s):
    return str(make_header(decode_header(s or "")))


def accounts(cfg: Config) -> list[dict]:
    """Configured accounts that actually have credentials. No `accounts:` in the config = one account from EMAIL_ADDRESS."""
    out = []
    for a in cfg["email"].get("accounts") or [{"name": "private"}]:
        prefix = a.get("env_prefix", "")
        user, pw = Config.env(f"{prefix}EMAIL_ADDRESS"), Config.env(f"{prefix}EMAIL_APP_PASSWORD").replace(" ", "")
        if user and pw:
            out.append({"name": a.get("name") or "private", "user": user, "pw": pw, "priority": a.get("priority", "normal"),
                        "imap": a.get("imap_host") or cfg["email"]["imap_host"], "smtp": a.get("smtp_host") or cfg["email"]["smtp_host"]})
    return out


def account(cfg: Config, name: str | None = None) -> dict:
    accts = accounts(cfg)
    if not accts:
        raise RuntimeError("no email account has credentials (EMAIL_ADDRESS / EMAIL_APP_PASSWORD)")
    if name:
        for a in accts:
            if a["name"].lower() == str(name).lower():
                return a
        raise LookupError(f"unknown email account {name!r}")
    return accts[0]


def fetch_unread(cfg: Config, limit: int = 30, snippet: int = 200, account: str | None = None) -> list[dict]:
    return _fetch(cfg, "UNSEEN", limit, snippet, account)


def fetch_recent(cfg: Config, limit: int = 10, snippet: int = 500, account: str | None = None) -> list[dict]:
    """Newest mail whether read or not - what the owner sees at the top of their inbox. Oldest first."""
    return _fetch(cfg, "ALL", limit, snippet, account)


def _connect(cfg: Config, name: str | None = None):
    a = account(cfg, name)
    M = imaplib.IMAP4_SSL(a["imap"])
    M.login(a["user"], a["pw"])
    return M


def _logout(M) -> None:
    try:
        M.logout()
    except Exception:
        pass


def _find(M, message_id: str, readonly: bool) -> list:
    M.select("INBOX", readonly=readonly)
    _, data = M.search(None, "HEADER", "Message-ID", '"%s"' % message_id.replace('"', ""))
    nums = data[0].split()
    if not nums:
        raise LookupError("message not found in the inbox")
    return nums


def trash(cfg: Config, message_id: str, account: str | None = None) -> None:
    """Move one message to the trash folder (Gmail keeps it 30 days; nothing is ever deleted for good).
    Found by Message-ID; the folder is whichever one the server flags \\Trash, so it works in any language."""
    if not message_id.strip("<> "):
        raise ValueError("no message id")
    M = _connect(cfg, account)
    try:
        nums = _find(M, message_id, readonly=False)
        folder = _folder(M, "\\Trash")
        for n in nums:
            M.copy(n, '"%s"' % folder)
            M.store(n, "+FLAGS", "\\Deleted")
        M.expunge()
    finally:
        _logout(M)


def _folder(M, attr: str) -> str:
    """The server's own name for a special folder (\\Trash, \\Drafts...), whatever the account language."""
    for line in M.list()[1]:
        text = line.decode() if isinstance(line, bytes) else str(line)
        if attr in text and ' "/" ' in text:
            return text.rsplit(' "/" ', 1)[-1].strip().strip('"')
    raise RuntimeError(f"no {attr} folder found")


def fetch_body(cfg: Config, message_id: str, snippet: int = 4000, account: str | None = None) -> dict:
    """One message in full (up to `snippet` characters of text), found by Message-ID. Never marks it read."""
    M = _connect(cfg, account)
    try:
        nums = _find(M, message_id, readonly=True)
        _, msg = M.fetch(nums[-1], "(BODY.PEEK[])")
        return parse_message(msg[0][1], snippet)
    finally:
        _logout(M)


def build(cfg: Config, d: dict) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = account(cfg, d.get("account"))["user"], d["to"], d["subject"]
    m["Date"], m["Message-ID"] = formatdate(localtime=True), make_msgid()
    if d.get("in_reply_to"):
        m["In-Reply-To"] = m["References"] = d["in_reply_to"]
    m.set_content(d["body"])
    return m


def save_draft(cfg: Config, d: dict) -> None:
    """Put the draft into the Gmail drafts folder of the sending account, so it shows up on the owner's phone too."""
    M = _connect(cfg, d.get("account"))
    try:
        M.append('"%s"' % _folder(M, "\\Drafts"), "\\Draft", imaplib.Time2Internaldate(time.time()), build(cfg, d).as_bytes())
    finally:
        _logout(M)


def send(cfg: Config, d: dict) -> None:
    """Send for real over SMTP from the draft's account (Gmail files it under Sent by itself)."""
    a = account(cfg, d.get("account"))
    with smtplib.SMTP_SSL(a["smtp"], 465, timeout=30) as S:
        S.login(a["user"], a["pw"])
        S.send_message(build(cfg, d))


def _fetch(cfg: Config, criteria: str, limit: int, snippet: int, name: str | None = None) -> list[dict]:
    M = _connect(cfg, name)
    try:
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
        _logout(M)


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
    rname, raddr = parseaddr(_dec(m.get("Reply-To")))
    return {"id": m.get("Message-ID", ""), "reply_to": raddr or addr, "from": f"{name} <{addr}>" if name else addr,
            "subject": _dec(m.get("Subject")), "snippet": " ".join(body.split())[:snippet]}
