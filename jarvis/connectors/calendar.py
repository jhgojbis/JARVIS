"""Calendar via iCal (.ics) secret URL: Google, Outlook and Apple calendars all provide one."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import urllib.request
from zoneinfo import ZoneInfo
import icalendar, recurring_ical_events
from ..config import Config


def parse_events(ics: bytes | str, start: datetime, end: datetime) -> list[dict]:
    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for e in recurring_ical_events.of(cal).between(start, end):
        s = e.get("DTSTART").dt
        if not isinstance(s, datetime):          # all-day event
            s = datetime(s.year, s.month, s.day, tzinfo=start.tzinfo)
        elif s.tzinfo is None:
            s = s.replace(tzinfo=start.tzinfo)
        out.append({"title": str(e.get("SUMMARY", "(no title)")), "start": s,
                    "location": str(e.get("LOCATION", "") or "")})
    return sorted(out, key=lambda x: x["start"])


def upcoming(cfg: Config, hours: float = 24, now: datetime | None = None) -> list[dict]:
    tz = ZoneInfo(cfg["owner"]["timezone"])
    now = now or datetime.now(tz)
    events = []
    for url in cfg["calendar"]["ics_urls"]:
        with urllib.request.urlopen(url, timeout=20) as r:
            events += parse_events(r.read(), now, now + timedelta(hours=hours))
    return sorted(events, key=lambda x: x["start"])


def fmt(events: list[dict], tz: ZoneInfo | None = None) -> str:
    return "; ".join(f"{e['start'].astimezone(tz).strftime('%a %H:%M') if tz else e['start'].strftime('%a %H:%M')} {e['title']}"
                     for e in events) or "nothing scheduled"
