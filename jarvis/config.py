"""Config = config.yaml (behaviour) + environment/.env (secrets)."""
from __future__ import annotations
import copy, os, re
from pathlib import Path
import yaml

DEFAULTS = {
    "owner": {"name": "Boss", "phone": "", "whatsapp": "", "timezone": "UTC", "quiet_hours": ["22:00", "07:00"]},
    "voice": {"twilio_voice": "Polly.Brian-Neural", "language": "en-GB", "assistant_name": "Jarvis",
              "engine": "polly"},   # polly (Twilio <Say>) | thomas | luke (edge-tts, played with <Play>)
    # Jarvis only phones inside this window; outside it (or when you don't pick up) you get a WhatsApp voice clip instead.
    "calls": {"start": "09:00", "end": "24:00", "weekdays_only": False,
              "callback": True,
              "sms_per_day": 6},     # cap on fallback SMS (a text message to Sweden costs about 0.65 kr per 160 characters)   # you ring Jarvis, it rejects the call (free) and rings you back (calls to a US number cost you)
    "llm": {"backend": "anthropic", "model": "claude-haiku-4-5-20251001", "max_tokens": 400, "daily_token_budget": 200000},
    "screening": {"enabled": True, "vip": [], "block_unknown_spam": True, "allow_urgent": True,
                  "greeting": "Hello, this is {assistant}, {owner}'s assistant. Who is calling, and what is it about?"},
    "email": {"enabled": False, "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com", "important_senders": [], "ignore_senders": [], "accounts": []},
    "calendar": {"enabled": False, "ics_urls": []},
    "jobs": [],
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split("  #")[0].strip().strip('"'))


class Config:
    def __init__(self, data: dict, path: str | None = None):
        self.data, self.path = data, path

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        load_dotenv()
        raw = yaml.safe_load(Path(path).read_text()) if Path(path).exists() else {}
        return cls(_merge(DEFAULTS, raw or {}), path)

    def save(self) -> None:
        if self.path:
            Path(self.path).write_text(yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True))

    def __getitem__(self, k):
        return self.data[k]

    @staticmethod
    def env(name: str, default: str = "") -> str:
        return os.environ.get(name, default)


def norm_number(n: str) -> str:
    """Normalise to digits with leading + so '+1 (555) 000-1111' == '+15550001111'."""
    n = re.sub(r"^(whatsapp:)", "", (n or "").strip())
    digits = re.sub(r"\D", "", n)
    return "+" + digits if digits else ""
