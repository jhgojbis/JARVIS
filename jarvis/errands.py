"""Errands too slow for a phone call. Jarvis answers at once, researches in the background and WhatsApps the result.
Links in the messages are built here (maps) or whitelisted (Uber Eats store pages), never copied from free LLM text:
a web page that tries to steer the assistant cannot make it send you to a link of its own."""
from __future__ import annotations
import json, logging, re, subprocess, threading, urllib.parse
from .config import Config

log = logging.getLogger("jarvis.errands")

PLACES_PROMPT = ("Use web search. Task: {request}\n"
                 "Pick at most 3 well reviewed places that fit. Prefer ones that deliver on Uber Eats and give their ubereats.com store "
                 'page URL only if you actually found it, else "". Return ONLY JSON: '
                 '{{"places":[{{"name":str,"address":str,"why":"max 12 words, plain text","ubereats":str}}]}}')
UBER = re.compile(r"https://www\.ubereats\.com/[A-Za-z0-9/_\-.%?=&]+")


def _ask_web(cfg: Config, prompt: str, timeout: float = 150) -> str:
    """One-off `claude -p` with web search on the owner's own Claude login (personal use). Too slow for a live call."""
    args = ["claude", "-p", "--model", cfg["llm"].get("research_model", "sonnet"), "--tools", "WebSearch", "--allowedTools", "WebSearch",
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence", "--disable-slash-commands", "--output-format", "text", prompt]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude failed: {r.stderr[:200]}")
    return r.stdout


def _plain(s, n: int) -> str:
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", str(s))).strip()[:n]


def maps_link(name: str, address: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(f"{name} {address}".strip())


def parse_places(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)).get("places", []) if m else []
    except json.JSONDecodeError:
        return []
    out = []
    for p in raw[:3] if isinstance(raw, list) else []:
        if isinstance(p, dict) and _plain(p.get("name", ""), 80):
            u = str(p.get("ubereats", "")).strip()
            out.append({"name": _plain(p["name"], 80), "address": _plain(p.get("address", ""), 120),
                        "why": _plain(p.get("why", ""), 100), "ubereats": u if UBER.fullmatch(u) else ""})
    return out


def places_message(request: str, places: list[dict]) -> str:
    if not places:
        return f"I couldn't find anything solid for: {request}"
    lines = [f"Ideas for: {request}"]
    for i, p in enumerate(places, 1):
        lines.append(f"\n{i}. {p['name']}" + (f" - {p['why']}" if p["why"] else ""))
        if p["address"]:
            lines.append(p["address"])
        lines.append("Map: " + maps_link(p["name"], p["address"]))
        lines.append("Uber Eats: " + (p["ubereats"] or "no delivery page found"))
    return "\n".join(lines)


def grocery_message(items: list[str]) -> str:
    """Hemköp has no cart link, so each item gets a search link: one tap to add. Your own list lives in Hemköp > Mina listor."""
    lines = ["Hemköp: tap an item to find it and add it to the cart (I can't fill the cart or pay for you):"]
    lines += [f"- {i}: https://www.hemkop.se/sok?q={urllib.parse.quote(i)}" for i in items]
    return "\n".join(lines)


def places_job(cfg: Config, notifier, request: str) -> None:
    try:
        text = places_message(request, parse_places(_ask_web(cfg, PLACES_PROMPT.format(request=request))))
    except Exception:
        log.exception("places search failed")
        text = f"Sorry, my search for '{request}' failed. Ask me again in a minute."
    try:
        notifier.message(text)
    except Exception:
        log.exception("could not deliver errand result")


def run_async(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()
