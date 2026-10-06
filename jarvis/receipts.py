"""Receipts sent as a photo on Telegram: read by Claude, logged to receipts/receipts.csv with the image kept next to it.
Everything here is a DRAFT for the bookkeeping (the numbers are read by a model, so always glance at the receipt)."""
from __future__ import annotations
import csv, hashlib, json, re
from datetime import datetime
from pathlib import Path
from .llm import ask_image

DIR = Path(__file__).resolve().parent.parent / "receipts"
FIELDS = ("logged", "date", "vendor", "total", "vat", "vat_rate", "currency", "category", "note", "image")
PROMPT = ('This is a receipt or invoice. Return ONLY a JSON object: {"date":"YYYY-MM-DD or empty","vendor":str,"total":number incl. VAT,'
          '"vat":number or null,"vat_rate":percent or null,"currency":"SEK/EUR/USD...","category":"short bookkeeping category in Swedish, '
          'e.g. Programvara, Resor, Kontorsmaterial, Mat, Annonsering","note":"one short line about what was bought"}. '
          "Use null/empty for what you cannot read; never guess amounts.")


def read(image: bytes, media_type: str = "image/jpeg") -> dict:
    m = re.search(r"\{.*\}", ask_image(PROMPT, image, media_type), re.S)
    d = json.loads(m.group(0)) if m else {}
    if not isinstance(d, dict) or not isinstance(d.get("total"), (int, float)):
        raise ValueError("could not read a total from the receipt")
    return d


def save(d: dict, image: bytes, now: datetime | None = None, directory: Path | None = None) -> Path:
    """Append the receipt to the CSV and keep the image; returns the image path. Same image twice = same file (no double entry)."""
    directory = directory or DIR
    directory.mkdir(exist_ok=True)
    now = now or datetime.now()
    name = f"{now:%Y%m%d}-{hashlib.sha1(image).hexdigest()[:8]}.jpg"
    path = directory / name
    if path.exists():
        return path
    path.write_bytes(image)
    csv_path = directory / "receipts.csv"
    new = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        w.writerow({"logged": now.isoformat(timespec="seconds"), "image": name,
                    **{k: ("" if d.get(k) is None else d.get(k)) for k in FIELDS if k not in ("logged", "image")}})
    return path


def describe(d: dict) -> str:
    vat = f", VAT {d['vat']} ({d.get('vat_rate') or '?'}%)" if d.get("vat") is not None else ", VAT not visible"
    return f"{d.get('vendor') or 'Unknown'}: {d['total']} {d.get('currency') or ''}{vat}, {d.get('date') or 'no date'}, {d.get('category') or 'no category'}".replace("  ", " ")
