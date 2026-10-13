"""Getting usage and bills in from any utility.

The rest of the app reads two shapes: a statement history (one row per bill)
and usage periods (start, end, amount used, cost). Every route in is a
translation into those, so nothing downstream knows or cares who the utility is.

    Green Button XML    the standard's own format (ESPI), where a utility offers it
    any CSV             columns found by name; anything unsure is put to the person
                        as a form (``Unsure``) rather than guessed at
    by hand             one bill typed in (``manual``)

Usage files are rewritten into the layout ``greenbutton.py`` already reads, so
they are filed and loaded exactly like the ones that arrive in that layout.

Nothing here knows about a particular provider. Where a file is ambiguous - is
it kWh or therms, is that column the cost or the balance - the answer is asked
for, never assumed: a wrong unit would make every figure after it quietly wrong.
"""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
import xml.etree.ElementTree as ET
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from metermatters.analytics import analysis
from metermatters import fuels

FUELS = fuels.NAMES
MAX_ROWS = 200_000

# unit as written -> (the unit the app uses, factor to convert into it)
UNITS = {
    "kwh": ("kWh", 1.0), "kw-h": ("kWh", 1.0), "mwh": ("kWh", 1000.0), "wh": ("kWh", 0.001),
    "therm": ("therms", 1.0), "therms": ("therms", 1.0), "thm": ("therms", 1.0),
    "dth": ("therms", 10.0), "dekatherm": ("therms", 10.0), "dekatherms": ("therms", 10.0),
    "mmbtu": ("therms", 10.0), "btu": ("therms", 1e-5), "kbtu": ("therms", 1e-2),
    "ccf": ("ccf", 1.0), "hcf": ("ccf", 1.0), "mcf": ("ccf", 10.0),
    "cf": ("ccf", 0.01), "ft3": ("ccf", 0.01), "cubic feet": ("ccf", 0.01),
    "m3": ("ccf", 0.353147), "cubic meters": ("ccf", 0.353147),
    # Water. CCF is also a gas unit: which it is depends on what the file says
    # about itself - see _water_units.
    "gal": ("gal", 1.0), "gallon": ("gal", 1.0), "gallons": ("gal", 1.0),
    "kgal": ("gal", 1000.0), "kgallons": ("gal", 1000.0), "hgal": ("gal", 100.0),
}
UNIT_FUEL = {"kWh": "Electric", "therms": "Gas", "ccf": "Gas", "gal": "Water"}


def _water_units(unit, fuel):
    """A water meter's cubic feet are gallons to the app."""
    if fuel == "Water" and unit and unit[0] == "ccf":
        return ("gal", unit[1] * fuels.GALLONS_PER_CCF)
    return unit

# ESPI codes (Green Button Alliance / NAESB): uom 72 is Wh, 169 therm, 42 m3,
# 119 ft3; commodity 7 is natural gas. Cost is carried in units of 1e-5 of the
# currency.
ESPI_UOM = {"72": "wh", "169": "therm", "42": "m3", "119": "cf"}
ESPI_COST_SCALE = 1e-5
BAD_READING = "that Green Button file has a reading that isn't a number or a date"


class ImportFailed(ValueError):
    """A file that can't be used, said in words the person can act on."""


class Unsure(Exception):
    """The file might be usable, but a person has to say how. Carries the form's contents."""

    def __init__(self, headers: list[str], sample: list[list[str]], guess: dict, reason: str):
        super().__init__(reason)
        self.headers, self.sample, self.guess, self.reason = headers, sample, guess, reason


@dataclass
class Table:
    preamble: list[list[str]]
    headers: list[str]
    rows: list[list[str]]


@dataclass
class Row:
    """One usage period, in the app's own units."""
    account: str
    start: date
    end: date
    amount: float
    cost: float | None
    # What a solar home sent back to the grid over the period, same unit.
    exported: float = 0.0


@dataclass
class Result:
    kind: str                           # "usage" | "bills"
    fuel: str
    unit: str | None = None
    text: str = ""                      # usage: the file, rewritten
    bills: list[dict] = field(default_factory=list)
    periods: int = 0
    accounts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # True when the file named no account and one was made up for it. The
    # caller may know better - the account its bills already use.
    account_invented: bool = False


# --------------------------------------------------------------------------
# Reading a table out of whatever it is
# --------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def sniff(text: str) -> Table:
    """Find the header row, skipping any lines of preamble above it."""
    lines = [l for l in text.splitlines()]
    if not any(l.strip() for l in lines):
        raise ImportFailed("that file is empty")
    sample = "\n".join(lines[:50])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    parsed = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if len(parsed) > MAX_ROWS:
        raise ImportFailed("that file has more rows than can be read")
    # The header is a row of 3+ named cells followed by a row of the same width.
    # A row of names that isn't is remembered, so the refusal can say what was
    # wrong under it rather than that nothing was found.
    problem = None
    for i, row in enumerate(parsed):
        cells = [c for c in row if c.strip()]
        if not (len(cells) >= 3 and any(re.search(r"[A-Za-z]", c) for c in cells)
                and not any(re.fullmatch(r"[\d.,$\-/: ]+", c.strip()) for c in cells if c.strip())):
            continue
        below = [(j, r) for j, r in enumerate(parsed[i + 1:], i + 1) if any(c.strip() for c in r)]
        if below and len(below[0][1]) == len(row):
            return Table(parsed[:i], [c.strip() for c in row], [r for _, r in below])
        if not below:
            problem = "that file has column names but no rows under them"
        else:
            j, first = below[0]
            problem = (f"row {j + 1} has {len(first)} cells but the column names on row {i + 1} "
                       f"have {len(row)}")
    raise ImportFailed(problem or "couldn't find a row of column names in that file")


ROLES = {
    "start": [r"^(billing |service |period |bill |usage )?(start|from|begin|beginning)( date| time)?$",
              r"^start date$"],
    "end": [r"^(billing |service |period |bill |usage )?(end|to|through|thru)( date| time)?$",
            r"^(read|reading|meter read) date$"],
    "due": [r"^(payment |pay |bill )?due( date)?$", r"^pay(ment)? date due$"],
    "billdate": [r"^(bill|billing|statement|invoice|issue)( date)?$", r"^statement date$"],
    "date": [r"^(usage |interval |reading )?(date|day)$", r"^date( time)?$"],
    "usage": [r"\b(usage|consumption|used|kwh|therms?|ccf|mcf|volume|quantity|energy)\b"],
    # Never a "balance": that is what is owed, not what the month cost.
    "money": [r"^(total )?(amount|charges?|cost|price)( due)?( \(?\$\)?)?$",
              r"^(total )?(bill|amount due|current charges|total charges)$",
              r"\b(cost|charges?|amount|total)\b"],
    "units": [r"^(units?|uom|unit of measure|measure)$"],
    "account": [r"\b(account|acct|premise|service point)\b"],
    "fuel": [r"^(fuel|commodity|service( type)?|utility|type|meter type)$"],
}


def _find(headers: list[str], role: str, taken: set[int]) -> int | None:
    for pattern in ROLES[role]:
        for i, h in enumerate(headers):
            if i not in taken and re.search(pattern, _norm(h)):
                return i
    return None


def _guess_columns(table: Table) -> dict[str, int]:
    found: dict[str, int] = {}
    taken: set[int] = set()
    for role in ("start", "end", "due", "billdate", "units", "account", "fuel",
                 "usage", "money", "date"):
        i = _find(table.headers, role, taken)
        if i is not None:
            found[role] = i
            taken.add(i)
    return found


def _num(value) -> float | None:
    return analysis.parse_money(value) if not isinstance(value, (int, float)) else float(value)


def _day(value: str) -> date | None:
    text = (value or "").strip()
    d = analysis.parse_date(text.split("T")[0].split(" ")[0])
    if d:
        return d
    for fmt in ("%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%m/%d/%y %H:%M", "%Y-%m-%dT%H:%M:%S%z",
                "%d-%b-%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _unit_of(text: str) -> tuple[str, float] | None:
    key = _norm(text)
    for form in (key, key.replace(" ", "")):
        if form in UNITS:
            return UNITS[form]
    found = re.search(r"\b(kwh|mwh|therms?|thm|dth|mmbtu|ccf|hcf|mcf|kbtu|btu)\b", key)
    return UNITS[found.group(1)] if found else None


def _fuel_word(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"water|sewer|gallon|\bgal\b", t):
        return "Water"
    if re.search(r"gas|therm|ccf|propane", t):
        return "Gas"
    if re.search(r"elec|kwh|power|energy", t):
        return "Electric"
    return None


def _looks_like_records(table: Table) -> bool:
    """Is there a column of dates and a column of amounts? Otherwise there is nothing to map."""
    dates = numbers = 0
    for i in range(len(table.headers)):
        values = [r[i] for r in table.rows[:20] if i < len(r) and r[i].strip()]
        if not values:
            continue
        if sum(1 for v in values if _day(v)) >= 0.8 * len(values):
            dates += 1
        elif sum(1 for v in values if analysis.parse_money(v) is not None) >= 0.8 * len(values):
            numbers += 1
    return dates >= 1 and numbers >= 1


# --------------------------------------------------------------------------
# The guess, and the form that stands in for it
# --------------------------------------------------------------------------

def understand(table: Table, mapping: dict | None = None) -> dict:
    """What this file is: kind, fuel, unit and which column is which.

    With a ``mapping`` (what the person chose on the form) that is taken as given.
    Otherwise it is worked out, and ``Unsure`` raised when it can't be.
    """
    headers = table.headers
    cols = _guess_columns(table)
    sample_rows = table.rows[:5]

    if mapping:
        cols = {}
        # The form sends a table of role -> column name. Anything else is no choice at all.
        columns = mapping.get("columns")
        for role, name in (columns.items() if isinstance(columns, dict) else ()):
            if role in ROLES and isinstance(name, str) and name in headers:
                twice = next((r for r, i in cols.items() if i == headers.index(name)), None)
                if twice:
                    raise Unsure(headers, sample_rows, {"columns": {r: headers[i] for r, i in cols.items()}},
                                 f"{name} is picked as both {twice} and {role}. A column can only be one of them.")
                cols[role] = headers.index(name)
    guess = {role: headers[i] for role, i in cols.items()}

    if "fuel" in cols:
        # A combined export lists electric and gas rows side by side. One fuel
        # per upload keeps every figure on one axis; say which, as the XML reader does.
        said = {_fuel_word(_cell(r, cols, "fuel")) for r in table.rows} - {None}
        if len(said) > 1:
            raise ImportFailed("that file holds more than one fuel (" + " and ".join(sorted(said))
                               + "); export one at a time")

    kind = (mapping or {}).get("kind")
    if kind not in ("usage", "bills"):
        if "usage" in cols and ({"start", "end"} & cols.keys() or "date" in cols or "billdate" in cols):
            kind = "usage"
        elif "money" in cols and ({"due", "billdate", "date", "end"} & cols.keys()):
            kind = "bills"
    if kind is None:
        if not _looks_like_records(table):
            raise ImportFailed("that doesn't look like a bill or usage file - "
                               "there are no dates and amounts in it")
        raise Unsure(headers, sample_rows, {"columns": guess},
                     "We couldn't tell what's in this file. Tell us which column is which.")

    fuel = (mapping or {}).get("fuel")
    unit_name = (mapping or {}).get("unit")
    unit = _unit_of(unit_name) if unit_name else None
    if kind == "usage" and unit is None:
        if "units" in cols and table.rows:
            unit = _unit_of(table.rows[0][cols["units"]])
        if unit is None:
            unit = _unit_of(headers[cols["usage"]])
        if unit is None and "fuel" in cols and table.rows:
            fuel_guess = _fuel_word(table.rows[0][cols["fuel"]])
            unit = {"Electric": UNITS["kwh"], "Gas": UNITS["therm"], "Water": UNITS["gal"]}.get(fuel_guess)
    if fuel not in FUELS:
        # What the file says about itself - a "Service: Water" column, "water"
        # in the usage header - decides before the unit does: CCF is both.
        said = None
        if "fuel" in cols and table.rows:
            said = _fuel_word(table.rows[0][cols["fuel"]])
        said = said or _fuel_word(headers[cols["usage"]] if "usage" in cols else "") \
            or _fuel_word(" ".join(" ".join(p) for p in table.preamble))
        fuel = said or (UNIT_FUEL.get(unit[0]) if unit else None)
    unit = _water_units(unit, fuel)
    unsure = {"columns": guess, "kind": kind, "fuel": fuel, "unit": unit[0] if unit else None}
    if mapping and kind == "usage" and mapping.get("fuel") in FUELS and unit and UNIT_FUEL[unit[0]] != fuel:
        # "Gas" and "kWh" on one form: neither can be taken over the other, so ask.
        measures = UNIT_FUEL[unit[0]].lower()
        raise Unsure(headers, sample_rows, unsure,
                     f"{unit[0]} is a{'n' if measures[0] in 'aeiou' else ''} {measures} unit, not "
                     f"{fuel.lower()}. Pick the unit that matches the fuel.")

    # The columns a kind of file can't do without. With a mapping these may be
    # missing because the person left them blank, or named something that isn't there.
    if kind == "usage" and "usage" not in cols:
        raise Unsure(headers, sample_rows, unsure, "Pick the column that says how much was used.")
    if kind == "usage" and not ({"start", "end"} <= cols.keys()
                                or {"date", "end", "billdate"} & cols.keys()):
        raise Unsure(headers, sample_rows, unsure, "Pick the column with the dates.")
    if kind == "bills" and "money" not in cols:
        raise Unsure(headers, sample_rows, unsure, "Pick the column with the amount of each bill.")
    if kind == "bills" and not ({"due", "billdate", "date", "end"} & cols.keys()):
        raise Unsure(headers, sample_rows, unsure, "Pick the column with the dates.")

    if kind == "usage" and unit is None:
        raise Unsure(headers, sample_rows, unsure,
                     "We can't tell what unit this is measured in. Pick one.")
    if fuel is None:
        raise Unsure(headers, sample_rows, unsure,
                     "Is this electric or gas? Pick one.")
    return {"kind": kind, "fuel": fuel, "unit": unit, "cols": cols, "guess": unsure}


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------

def _cell(row: list[str], cols: dict, role: str) -> str:
    i = cols.get(role)
    return row[i].strip() if i is not None and i < len(row) else ""


def _say_skipped(notes: list[str], unreadable: int, reversed_rows: int = 0) -> None:
    """The module's promise: a row left out is a row said to be left out."""
    if unreadable:
        notes.append(f"{unreadable} row{'s' if unreadable != 1 else ''} with no readable date or amount "
                     f"{'were' if unreadable != 1 else 'was'} skipped.")
    if reversed_rows:
        notes.append(f"{reversed_rows} row{'s' if reversed_rows != 1 else ''} that ended before "
                     f"{'they' if reversed_rows != 1 else 'it'} started "
                     f"{'were' if reversed_rows != 1 else 'was'} skipped.")


def _usage_rows(table: Table, info: dict, default_account: str) -> tuple[list[Row], list[str]]:
    cols, (unit, factor) = info["cols"], info["unit"]
    notes: list[str] = []
    points: list[tuple[str, date, date | None, float, float | None]] = []
    unreadable = reversed_rows = 0
    for r in table.rows:
        amount = _num(_cell(r, cols, "usage"))
        if amount is None:
            unreadable += 1
            continue
        cost = _num(_cell(r, cols, "money")) if "money" in cols else None
        account = _cell(r, cols, "account") or default_account
        start = _day(_cell(r, cols, "start")) if "start" in cols else None
        end = _day(_cell(r, cols, "end")) if "end" in cols else None
        single = None
        if not (start and end):
            single = _day(_cell(r, cols, "date")) or _day(_cell(r, cols, "billdate"))
        if start and end:
            if end < start:
                reversed_rows += 1       # a period can't end before it starts; not a one-day reading either
                continue
            points.append((account, start, end, amount * factor, cost))
        elif end or single:
            points.append((account, end or single, None, amount * factor, cost))
        else:
            unreadable += 1
    if not points:
        if reversed_rows:
            raise ImportFailed("every row ends before it starts - are the start and end columns "
                               "the wrong way round?")
        raise ImportFailed("no rows with a date and an amount used could be read")
    _say_skipped(notes, unreadable, reversed_rows)

    out: list[Row] = []
    for account in dict.fromkeys(p[0] for p in points):
        mine = [p for p in points if p[0] == account]
        ranged = [p for p in mine if p[2] is not None]
        singles = [p for p in mine if p[2] is None]
        for a, s, e, amt, cost in ranged:
            out.append(Row(a, s, e, amt, cost))
        if singles:
            out += _from_dates(account, singles, notes)
    out.sort(key=lambda r: (r.account, r.end))
    return out, notes


def _from_dates(account: str, singles: list, notes: list[str]) -> list[Row]:
    """Rows that carry one date each: daily or hourly readings, or monthly reads."""
    days = sorted({p[1] for p in singles})
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    typical = statistics.median(gaps) if gaps else 0
    if typical <= 3:
        return _by_month(account, singles, notes)
    # Spaced like statements: each date closes a period that began after the last one.
    rows, previous = [], None
    for _, d, _, amt, cost in sorted(singles, key=lambda p: p[1]):
        start = previous + timedelta(days=1) if previous else d - timedelta(days=int(typical) - 1)
        rows.append(Row(account, start, d, amt, cost))
        previous = d
    return rows


def _by_month(account: str, points: list, notes: list[str]) -> list[Row]:
    """Add up daily or finer readings into calendar months, whole months only."""
    months: dict[tuple[int, int], dict] = {}
    for _, d, _, amt, cost in points:
        m = months.setdefault((d.year, d.month), {"days": set(), "amt": 0.0, "cost": 0.0, "priced": True})
        m["days"].add(d)
        m["amt"] += amt
        if cost is None:
            m["priced"] = False
        else:
            m["cost"] += cost
    rows, dropped = [], 0
    for (y, mo), m in sorted(months.items()):
        size = monthrange(y, mo)[1]
        if len(m["days"]) < size * 0.9:
            dropped += 1             # a part-month would read as a month of low use
            continue
        rows.append(Row(account, date(y, mo, 1), date(y, mo, size), m["amt"],
                        m["cost"] if m["priced"] else None))
    if dropped:
        notes.append(f"{dropped} part-month{'s' if dropped != 1 else ''} at the edges of the file "
                     "left out, because they would look like unusually low use.")
    if not rows:
        raise ImportFailed("that file doesn't cover a whole month")
    return rows


def _preamble_value(preamble: list[list[str]], *keys: str) -> str | None:
    """A ``key,value`` line above the header, such as 'Account Number,123'."""
    for parts in preamble:
        if len(parts) >= 2 and _norm(parts[0]) in keys and parts[1].strip():
            return parts[1].strip()
    return None


def usage_text(rows: list[Row], unit: str, account: str, address: str | None = None) -> str:
    """The rows in the layout greenbutton.py reads."""
    out = io.StringIO(newline="")
    out.write("Name,Imported\n")
    if address:
        csv.writer(out).writerow(["Address", address])
    out.write(f"Account Number,{account}\n\n")
    w = csv.writer(out)
    solar = any(r.exported for r in rows)
    w.writerow(["TYPE", "START DATE", "END DATE", f"USAGE ({unit})", "COST", "NOTES"]
               + ([f"EXPORTED ({unit})"] if solar else []))
    for r in rows:
        w.writerow(["Imported", r.start.isoformat(), r.end.isoformat(), f"{r.amount:.3f}",
                    f"${r.cost:.2f}" if r.cost is not None else "",
                    "" if r.cost is not None else "no cost in file"]
                   + ([f"{r.exported:.3f}"] if solar else []))
    return out.getvalue()


# --------------------------------------------------------------------------
# Bills
# --------------------------------------------------------------------------

def _bill_rows(table: Table, info: dict, default_account: str,
               notes: list[str]) -> list[dict]:
    from metermatters.intake import ingest
    cols = info["cols"]
    when = next((r for r in ("due", "billdate", "date", "end") if r in cols), None)
    rows, credits, unreadable = [], 0, 0
    for r in table.rows:
        amount = _num(_cell(r, cols, "money"))
        due = _day(_cell(r, cols, when))
        if amount is None or due is None:
            unreadable += 1
            continue
        if amount <= 0:
            credits += 1            # a solar credit, or a month that cost nothing: kept, and marked
        if when != "due":
            due += timedelta(days=21)        # a statement date isn't a due date; estimated, as backfill does
        account = _cell(r, cols, "account") or default_account
        rows.append(ingest.bill_row(account, due.isoformat(), amount, account_type=info["fuel"],
                                    status="CREDIT" if amount <= 0 else "IMPORTED" if when == "due" else "BACKFILL",
                                    source="import"))
    if not rows:
        raise ImportFailed("no rows with a date and an amount could be read")
    _say_skipped(notes, unreadable)
    if credits:
        notes.append(f"{credits} statement{'s' if credits != 1 else ''} with a credit or a zero amount "
                     "kept and marked CREDIT - a solar month that paid you back is still a month.")
    return rows


# --------------------------------------------------------------------------
# Green Button XML
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(node, name):
    return [c for c in node.iter() if _local(c.tag) == name]


def _first_text(node, name) -> str | None:
    found = _kids(node, name)
    return (found[0].text or "").strip() if found and found[0].text else None


def from_xml(data: bytes, default_account: str | None = None) -> Result:
    """A Green Button (ESPI) feed: interval readings, added up into months."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data[:65536].lower():
        raise ImportFailed("that XML file uses features that aren't accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ImportFailed("that isn't a readable Green Button file") from None

    kinds = _reading_types(root)
    if not kinds:
        raise ImportFailed("this Green Button file doesn't say what it measures")
    units = {k["unit"] for k in kinds.values()}
    if len(units) > 1:
        # PG&E's combined feed carries electric and gas in one file. One fuel
        # per upload keeps every figure on one axis; say which.
        raise ImportFailed("that Green Button file holds more than one fuel ("
                           + " and ".join(sorted(units)) + "); export one at a time")
    unit = units.pop()
    invented = default_account is None
    default_account = default_account or f"{UNIT_FUEL[unit]} account"

    used, sent = [], {}
    for entry in _entries(root):
        if _kids(entry, "IntervalBlock") == []:
            continue
        kind = kinds.get(_meter_reading(entry)) or next(iter(kinds.values()))
        for reading in _kids(entry, "IntervalReading"):
            period = _kids(reading, "timePeriod")
            start = _first_text(period[0], "start") if period else None
            value = _first_text(reading, "value")
            if not (start and value):
                continue
            cost = _first_text(reading, "cost")
            try:
                d = datetime.fromtimestamp(int(start), tz=timezone.utc).date()
                amount = float(value) * kind["scale"]
                paid = float(cost) * ESPI_COST_SCALE if cost else None
            except (ValueError, OverflowError, OSError):
                # A word where a number should be, or a date past the end of time.
                raise ImportFailed(BAD_READING) from None
            if not (math.isfinite(amount) and math.isfinite(paid or 0.0)):
                raise ImportFailed(BAD_READING)
            if kind["reverse"]:
                sent[d] = sent.get(d, 0.0) + amount
                continue
            used.append((default_account, d, None, amount, paid))
    if not used:
        raise ImportFailed("that Green Button file has no readings in it")
    notes: list[str] = []
    rows = _from_dates(default_account, used, notes)
    if sent:
        for row in rows:
            row.exported = sum(v for d, v in sent.items() if row.start <= d <= row.end)
        notes.append("This meter runs both ways: what you sent to the grid is kept apart "
                     "from what you used, not subtracted from it.")
    result = _result_usage(rows, unit, notes)
    result.account_invented = invented
    return result


def _entries(root):
    return [e for e in root.iter() if _local(e.tag) == "entry"]


def _meter_reading(entry) -> str | None:
    """Which MeterReading an entry belongs to, from its links."""
    for link in _kids(entry, "link"):
        found = re.search(r"/MeterReading/([^/]+)", link.get("href") or "")
        if found:
            return found.group(1)
    return None


def _reading_types(root) -> dict:
    """Every ReadingType in the feed, keyed by its MeterReading (or by order when unlinked)."""
    kinds = {}
    for i, entry in enumerate(_entries(root)):
        rt = _kids(entry, "ReadingType")
        if not rt:
            continue
        uom = _first_text(rt[0], "uom")
        if uom not in ESPI_UOM:
            raise ImportFailed(f"this Green Button file measures in an unrecognised unit (code {uom}); "
                               "send it to us and we'll add it")
        unit, factor = UNITS[ESPI_UOM[uom]]
        try:
            exponent = int(_first_text(rt[0], "powerOfTenMultiplier") or 0)
        except ValueError:
            raise ImportFailed(BAD_READING) from None
        if abs(exponent) > 15:
            # The standard's multipliers run from pico to tera. Past that it is
            # a mistake, not a scale, and 10 to the power of it is no number.
            raise ImportFailed(BAD_READING)
        # flowDirection 19 is "reverse": power the home sent out.
        reverse = (_first_text(rt[0], "flowDirection") or "1").strip() == "19"
        key = _meter_reading(entry) or f"#{i}"
        kinds[key] = {"unit": unit, "scale": factor * (10 ** exponent), "reverse": reverse}
    return kinds


def _result_usage(rows: list[Row], unit: str, notes: list[str],
                  address: str | None = None) -> Result:
    accounts = list(dict.fromkeys(r.account for r in rows))
    # One file per account: the loader reads the account out of the preamble.
    first = [r for r in rows if r.account == accounts[0]]
    text = usage_text(first, unit, accounts[0], address)
    if len(accounts) > 1:
        notes.append(f"{len(accounts) - 1} other account(s) in the file were set aside; "
                     "upload them separately.")
    if all(r.cost is None for r in first):
        notes.append("There's no cost in this file, so it can show how much you used but not "
                     "what it cost. Add the bills to see both.")
    return Result("usage", UNIT_FUEL[unit], unit, text=text, periods=len(first),
                  accounts=[accounts[0]], notes=notes)


# --------------------------------------------------------------------------
# The entry points
# --------------------------------------------------------------------------

def read(data: bytes, filename: str = "", mapping: dict | None = None,
         default_account: str | None = None) -> Result:
    """Turn an uploaded file the Eversource-shaped readers don't know into a Result.

    Raises ``Unsure`` when a person has to say what it is, ``ImportFailed`` when
    it can't be used at all.
    """
    if data.lstrip()[:5] == b"<?xml" or data.lstrip()[:1] == b"<":
        return from_xml(data, default_account)
    if data[:2] == b"PK":
        return _from_zip(data, filename, mapping, default_account)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")        # never fails; a Windows export, most likely
    if "\x00" in text[:4096]:
        raise ImportFailed("that file isn't text - a spreadsheet should be saved as CSV first")

    table = sniff(text)
    info = understand(table, mapping)
    named = default_account or _preamble_value(table.preamble, "account number", "account",
                                               "account no", "account #")
    account = named or f"{info['fuel']} account"
    invented = not named and "account" not in info["cols"]
    if info["kind"] == "bills":
        notes: list[str] = []
        rows = _bill_rows(table, info, account, notes)
        return Result("bills", info["fuel"], bills=rows, periods=len(rows), accounts=[account],
                      notes=notes, account_invented=invented)
    rows, notes = _usage_rows(table, info, account)
    result = _result_usage(rows, info["unit"][0], notes,
                           _preamble_value(table.preamble, "address", "service address"))
    result.account_invented = invented
    return result


def _from_zip(data: bytes, filename: str, mapping: dict | None, default_account) -> Result:
    """A zip from a portal that isn't the one we know: the first readable file inside."""
    import io as _io
    import zipfile
    from metermatters.intake import ingest
    try:
        archive = zipfile.ZipFile(_io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportFailed("that zip can't be opened") from None
    # The loader's own path has this cap, and the app comes here when that
    # path says no - so without it here, the cap is only ever a suggestion.
    if sum(i.file_size for i in archive.infolist()) > ingest.MAX_UNPACKED_BYTES:
        raise ImportFailed("that zip unpacks to far more than an export should")
    names = [n for n in archive.namelist() if n.lower().endswith((".csv", ".xml", ".txt"))
             and not n.startswith("__MACOSX") and not n.endswith("/")]
    if not names:
        raise ImportFailed("that zip holds no CSV or XML file")
    last: Exception | None = None
    for name in names:
        try:
            return read(archive.read(name), name, mapping, default_account)
        except Unsure:
            raise
        except ImportFailed as exc:
            last = exc
    raise ImportFailed(f"nothing in that zip could be read ({last})")


def manual(fuel: str, due: str, amount: str, account: str = "") -> dict:
    """One bill typed in by hand, as a statement row."""
    from metermatters.intake import ingest
    if fuel not in FUELS:
        raise ImportFailed("choose electric, gas or water")
    value = analysis.parse_money(amount)
    if value is None or value <= 0:
        raise ImportFailed("enter the amount of the bill")
    if analysis.parse_date(due) is None:
        raise ImportFailed("enter the date the bill is due")
    account = " ".join((account or "").split())[:60] or f"{fuel} (by hand)"
    return ingest.bill_row(account, due, value, account_type=fuel, status="-", source="manual")
