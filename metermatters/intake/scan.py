"""A scanned or photographed bill: read the text off it, then find the bill in it.

Two steps, kept apart. ``read_text`` sends the image or PDF to Google's Cloud
Vision API and gets the words back; it is the only thing here that touches
the network, and the only thing that needs a key (``ELEC_VISION_KEY``). The
image is sent, read, and not kept anywhere - not by this app, and Vision does
not store what it is sent.

``guess`` is pure: given the text of a bill, any bill, it finds what it can -
which utility, the amount due, the due date, the service dates and the usage
- and says how sure it is. Nothing is filed from here. The page shows the
guesses in the by-hand form and the person checks them before pressing Add.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date

from metermatters import fuels

VISION_URL = "https://vision.googleapis.com/v1"
TIMEOUT = 30
MAX_BYTES = 10 * 1024 * 1024
PDF_PAGES = 5

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
               ".tif": "image/tiff", ".tiff": "image/tiff", ".pdf": "application/pdf"}


class Unreadable(Exception):
    """The scan couldn't be read, for a reason worth telling the person."""


def enabled() -> bool:
    return bool(os.environ.get("ELEC_VISION_KEY"))


def mime_of(filename: str) -> str | None:
    """The type a scan is, from its name. None means 'not a scan'."""
    dot = (filename or "").lower().rfind(".")
    return IMAGE_TYPES.get((filename or "").lower()[dot:]) if dot >= 0 else None


# ------------------------------------------------------------------ Vision

def _post(path: str, body: dict) -> dict:
    key = os.environ.get("ELEC_VISION_KEY", "")
    request = urllib.request.Request(
        f"{VISION_URL}/{path}?key={key}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "meter-matters"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            why = json.loads(exc.read() or b"{}").get("error", {}).get("message", "")
        except ValueError:
            why = ""
        raise Unreadable(f"the text-reading service refused ({exc.code} {why})".rstrip()) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unreadable(f"the text-reading service didn't answer ({type(exc).__name__})") from None


def read_text(data: bytes, mime: str) -> str:
    """Every word on the scan, in reading order. Raises Unreadable."""
    if not enabled():
        raise Unreadable("reading scans isn't set up on this server")
    if len(data) > MAX_BYTES:
        raise Unreadable("that file is over 10 MB")
    content = base64.b64encode(data).decode()
    feature = {"type": "DOCUMENT_TEXT_DETECTION"}
    if mime == "application/pdf":
        answer = _post("files:annotate", {"requests": [{
            "inputConfig": {"content": content, "mimeType": mime},
            "features": [feature], "pages": list(range(1, PDF_PAGES + 1))}]})
        pages = (answer.get("responses") or [{}])[0].get("responses") or []
        text = "\n".join((p.get("fullTextAnnotation") or {}).get("text", "") for p in pages)
    else:
        answer = _post("images:annotate", {"requests": [{
            "image": {"content": content}, "features": [feature]}]})
        first = (answer.get("responses") or [{}])[0]
        if first.get("error"):
            raise Unreadable(first["error"].get("message", "the scan couldn't be read"))
        text = (first.get("fullTextAnnotation") or {}).get("text", "")
    if not text.strip():
        raise Unreadable("no text was found on it - try a sharper, straighter scan")
    return text


# ------------------------------------------------------------------- guess

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE = (rf"(?:\d{{1,2}}[/\-.]\d{{1,2}}[/\-.]\d{{2,4}}"
        rf"|(?:{MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+\d{{4}}"
        rf"|\d{{1,2}}\s+(?:{MONTHS})[a-z]*\.?,?\s+\d{{4}})")
RE_DATE = re.compile(DATE, re.I)
RE_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})")
# "Amount due", "Total due", "Please pay", "Balance due", "Current charges" - and
# whatever sits within a short reach after it.
# Text read off a scan keeps the page's line breaks, and a table's label and
# its value are often on different lines - so a label is allowed to reach
# across a few short lines to its number, but never across another dollar
# amount or date.
RE_DUE_AMOUNT = re.compile(
    r"(?:total\s+amount\s+due|amount\s+due|total\s+due|please\s+pay|pay\s+this\s+amount"
    r"|balance\s+due|amount\s+payable|total\s+current\s+charges|current\s+charges)"
    r"[^$]{0,40}?\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})", re.I)
RE_DUE_DATE = re.compile(
    rf"(?:due\s+date|due\s+by|due\s+on|pay\s+by|paid\s+by|payment\s+due|date\s+due|due)"
    rf"\s*:?\s*(?:[^\d]{{0,25}}?)({DATE})", re.I)
RE_PERIOD = re.compile(
    rf"({DATE})\s*(?:-|–|—|to|through|thru|till|until)\s*({DATE})", re.I)
RE_PERIOD_WORDS = re.compile(
    rf"(?:service|billing|usage|read(?:ing)?)\s+(?:period|dates?|from)[^\d]{{0,20}}"
    rf"({DATE})\s*(?:-|–|—|to|through|thru|and)\s*({DATE})", re.I)
# Two dates side by side, as a bill's "previous read / current read" columns
# come out, with nothing between them.
RE_PERIOD_ADJACENT = re.compile(rf"({DATE})\s+({DATE})", re.I)
UNIT = (r"kwh|kilowatt[\s-]*hours?|therms?|ccf|hcf|hundred\s+cubic\s+feet|100\s+cu\.?\s*ft\.?"
        r"|cubic\s+feet|cu\.?\s*ft\.?|kgal|k\s*gal|thousand\s+gallons|gallons?|gal")
# A number next to a unit of usage. HCF/CCF are a hundred cubic feet.
RE_USAGE = re.compile(rf"(\d{{1,3}}(?:,\d{{3}})+|\d+(?:\.\d+)?)\s*({UNIT})\b", re.I)
# A "Usage (100 cu. ft.)" column heading, with the figure on the line after.
RE_USAGE_COLUMN = re.compile(
    rf"(?:usage|consumption|used)\s*\(?\s*({UNIT})\s*\)?\s*\n\s*(\d{{1,3}}(?:,\d{{3}})+|\d+(?:\.\d+)?)\b", re.I)
# "100 cu. ft. equals 748 gallons": a conversion note, not a reading.
RE_CONVERSION = re.compile(r"\b(?:equals|=|is)\s+\d", re.I)
UNIT_WORDS = {
    "kwh": ("kWh", 1.0), "kilowatt": ("kWh", 1.0), "therm": ("therms", 1.0),
    "ccf": ("gal", fuels.GALLONS_PER_CCF), "hcf": ("gal", fuels.GALLONS_PER_CCF),
    "hundred": ("gal", fuels.GALLONS_PER_CCF), "100": ("gal", fuels.GALLONS_PER_CCF),
    "cubic": ("gal", fuels.GALLONS_PER_CCF / 100), "cu": ("gal", fuels.GALLONS_PER_CCF / 100),
    "kgal": ("gal", 1000.0), "k gal": ("gal", 1000.0), "thousand": ("gal", 1000.0),
    "gal": ("gal", 1.0),
}
FUEL_WORDS = (("Water", r"\bwater\b|\bsewer\b|\bgallons?\b|\bhcf\b"),
              ("Gas", r"\bnatural\s+gas\b|\btherms?\b|\bgas\s+(?:service|usage|bill|charges)\b"),
              ("Electric", r"\bkwh\b|\belectric(?:ity)?\b|\bkilowatt\b"))


def _date(text: str) -> date | None:
    from datetime import datetime
    from metermatters.analytics import analysis
    text = re.sub(r"\s+", " ", text.replace("–", "-").replace(".", "/") if re.match(r"^\d", text)
                  else text.replace(",", "").replace(".", "")).strip()
    found = analysis.parse_date(text.replace("-", "/"))
    if found:
        return found
    text = re.sub(r"(?i)\bsept\b", "Sep", text)
    for fmt in ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _money(text: str) -> float:
    return float(text.replace(",", ""))


def _fuel(text: str) -> str | None:
    """The utility, by which words appear most. None when nothing fits."""
    scores = {name: len(re.findall(pattern, text, re.I)) for name, pattern in FUEL_WORDS}
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def _unit(word: str) -> tuple[str, float] | None:
    word = word.lower()
    key = next((k for k in UNIT_WORDS if word.startswith(k)), None)
    return UNIT_WORDS[key] if key else None


def _usage(text: str, fuel: str | None) -> tuple[tuple[float, str], bool] | None:
    """The usage figure and whether it came from a labelled column.

    A labelled column wins. Otherwise, the figures next to a unit, leaving
    out conversion notes ("100 cu. ft. equals 748 gallons"): with one or two,
    the larger; with more, the middle one, since a bill that shows both
    meter readings and their difference has readings that dwarf the usage.
    """
    for m in RE_USAGE_COLUMN.finditer(text):
        found = _unit(m.group(1))
        if found and not (fuel and fuels.of_unit(found[0]).name != fuel):
            return (_money(m.group(2)) * found[1], found[0]), True
    figures = []
    for m in RE_USAGE.finditer(text):
        found = _unit(m.group(2))
        if found is None or (fuel and fuels.of_unit(found[0]).name != fuel):
            continue
        line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end()) if text.find("\n", m.end()) >= 0 else len(text)]
        if RE_CONVERSION.search(line):
            continue
        unit, factor = found
        value = _money(m.group(1)) * factor
        # "x 1000 gallons" on the line: the figure is in thousands.
        if unit == "gal" and factor == 1.0 and re.search(r"(?:x|×)\s*1,?000", line, re.I):
            value *= 1000
        figures.append((value, unit))
    if not figures:
        return None
    figures.sort()
    return (figures[-1] if len(figures) <= 2 else figures[len(figures) // 2]), False


def _readings_check_out(text: str, used: float, unit: str) -> bool:
    """Do two meter readings on the page differ by exactly the usage? A bill
    that shows both readings and the difference lets the figure be checked."""
    whole = [int(n.replace(",", "")) for n in re.findall(r"(?<![\d/.-])(\d{1,3}(?:,\d{3})+|\d{1,9})(?![\d/.-])", text)]
    # The readings may be in the bill's own unit: gallons, hundred cubic
    # feet, or thousands of gallons.
    candidates = [used] + ([used / fuels.GALLONS_PER_CCF, used / 1000.0] if unit == "gal" else [])
    targets = {round(c) for c in candidates if abs(c - round(c)) < 0.02 and round(c) > 0}
    seen = set(whole)
    return any((a + t) in seen for a in whole for t in targets)


def guess(text: str) -> dict:
    """What a bill's text says. Every field may be None; `sure` lists which
    were found by a label rather than by looking around."""
    flat = re.sub(r"[ \t]+", " ", text)
    out: dict = {"fuel": None, "amount": None, "due": None, "start": None, "end": None,
                 "used": None, "unit": None, "sure": [], "text": text}

    out["fuel"] = _fuel(flat)
    if out["fuel"]:
        out["sure"].append("fuel")

    m = RE_DUE_AMOUNT.search(flat)
    if m:
        out["amount"], _ = _money(m.group(1)), out["sure"].append("amount")
    else:
        amounts = [_money(a) for a in RE_MONEY.findall(flat)]
        if amounts:
            out["amount"] = max(amounts)

    m = RE_DUE_DATE.search(flat)
    if m and _date(m.group(1)):
        out["due"], _ = _date(m.group(1)), out["sure"].append("due")

    for m in (*RE_PERIOD_WORDS.finditer(flat), *RE_PERIOD.finditer(flat),
              *RE_PERIOD_ADJACENT.finditer(flat)):
        first, last = _date(m.group(1)), _date(m.group(2))
        if first and last and first < last and 14 <= (last - first).days <= 120:
            out["start"], out["end"] = first, last
            out["sure"].append("period")
            break
    if out["due"] is None:
        # No labelled due date: the latest date on the page, after the period.
        dates = [d for d in (_date(x) for x in RE_DATE.findall(flat)) if d]
        if out["end"]:
            dates = [d for d in dates if d > out["end"]]
        if dates:
            out["due"] = max(dates)

    found = _usage(flat, out["fuel"])
    if found:
        (out["used"], out["unit"]), labelled = found
        if out["fuel"] is None:
            out["fuel"] = fuels.of_unit(out["unit"]).name
        if labelled or _readings_check_out(flat, out["used"], out["unit"]):
            out["sure"].append("used")

    for key in ("due", "start", "end"):
        if out[key] is not None:
            out[key] = out[key].isoformat()
    return out
