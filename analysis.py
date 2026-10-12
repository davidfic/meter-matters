"""Parse the utility's AccountHistory.csv and turn it into trend analytics.

The CSV is the export you get from the electric provider's account history page.
Two row types matter:

  * ``Current Bill`` - the amount billed, dated by the statement's due date.
  * ``Payment``      - what you actually paid, and whether it cleared.

Bills are the metric we track; payments only tell us whether a bill is settled.
Everything here is plain stdlib so the analysis stays testable without a server.
"""

from __future__ import annotations

import csv
import html

import fuels
import io
import re
from dataclasses import dataclass, field
from datetime import date
from statistics import mean

BILL_TYPE = "Current Bill"
PAYMENT_TYPE = "Payment"

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


# --------------------------------------------------------------------------
# Low-level field parsing
# --------------------------------------------------------------------------

def clean_cell(value: str) -> str:
    """Strip the HTML the export smuggles into the account column.

    The provider writes ``55500010001&lt;br&gt;(electric10)`` - an escaped ``<br>``
    - so we unescape first, then flatten the break into a space.
    """
    text = html.unescape(value or "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def parse_money(value: str) -> float | None:
    """``"$1,248.26"`` -> ``1248.26``. Returns None for blanks and placeholders."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "N/A"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", ".", "-."}:
        return None
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_date(value: str) -> date | None:
    """The export uses ``YYYY/MM/DD``; we accept a couple of common variants."""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def split_account(raw: str) -> tuple[str, str]:
    """``"55500010001 (electric10)"`` -> ``("55500010001", "electric10")``."""
    cleaned = clean_cell(raw)
    match = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", cleaned)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return cleaned, cleaned


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class Entry:
    account: str
    nickname: str
    account_type: str
    when: date
    kind: str
    amount: float
    status: str
    source: str = ""

    @property
    def period(self) -> str:
        return f"{self.when.year:04d}-{self.when.month:02d}"


@dataclass
class Bill:
    """One statement, plus whatever payment we can match to it."""

    period: str
    due: date
    amount: float
    account: str
    nickname: str
    fuel: str = "Electric"
    paid: float | None = None
    paid_on: date | None = None
    status: str = "OPEN"
    usage: object | None = None  # the greenbutton.Period this bill charges for
    # True when the period's charge equals the bill to the cent. False when the
    # two were paired by their dates alone - the export left out a tax, the
    # account is budget-billed - and the figures should be read as approximate.
    usage_exact: bool = True

    @property
    def label(self) -> str:
        year, month = self.period.split("-")
        return f"{MONTH_NAMES[int(month) - 1]} {year}"

    @property
    def short_label(self) -> str:
        year, month = self.period.split("-")
        return f"{MONTH_NAMES[int(month) - 1]} '{year[2:]}"

    @property
    def month(self) -> int:
        return int(self.period.split("-")[1])

    @property
    def year(self) -> int:
        return int(self.period.split("-")[0])


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def read_entries(text: str) -> list[Entry]:
    """Parse the CSV text into flat entries, skipping anything unusable."""
    reader = csv.DictReader(io.StringIO(text))
    entries: list[Entry] = []
    for row in reader:
        # The header has a trailing comma, so DictReader hands us a stray
        # ``None`` key on every row. Normalising the keys drops it.
        row = {(k or "").strip(): v for k, v in row.items() if k}
        account, nickname = split_account(row.get("Account# or Nickname", ""))
        when = parse_date(row.get("Pay/Due Date", ""))
        kind = clean_cell(row.get("Type", ""))
        amount = parse_money(row.get("Amount", ""))
        if when is None or not kind or amount is None:
            continue
        entries.append(
            Entry(
                account=account,
                nickname=nickname,
                account_type=clean_cell(row.get("Account Type", "")),
                when=when,
                kind=kind,
                amount=amount,
                status=clean_cell(row.get("Status", "")) or "-",
                source=clean_cell(row.get("Source", "")),
            )
        )
    return entries


def load_entries(path) -> list[Entry]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return read_entries(handle.read())


def _month_before(period: str) -> str:
    year, month = (int(x) for x in period.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def build_bills(entries: list[Entry]) -> list[Bill]:
    """Fold entries into one Bill per (account, fuel, due-date month).

    The fuel is part of the key: a combined account bills electric and gas
    separately, and one must never replace the other. Two statements due in
    one calendar month - a 30-day cycle brushing a month boundary - are both
    kept, the earlier taking the month before when that month has none; only
    two due dates within ten days of each other are the same statement twice.

    A payment is matched to a bill when it lands in the same month, or in the
    month before the due date - utilities routinely bill on the 16th and take
    the autopay on the 12th, and some providers post it a month early.
    """
    groups: dict[tuple[str, str], list[Entry]] = {}
    for entry in entries:
        if entry.kind == BILL_TYPE:
            groups.setdefault((entry.account, entry.account_type or "Electric"), []).append(entry)

    bills: dict[tuple[str, str, str], Bill] = {}
    for (account, fuel), group in groups.items():
        placed: dict[str, Bill] = {}
        for entry in sorted(group, key=lambda e: e.when):
            bill = Bill(period=entry.period, due=entry.when, amount=entry.amount,
                        account=account, nickname=entry.nickname, fuel=fuel)
            other = placed.get(entry.period)
            if other is None:
                placed[entry.period] = bill
            elif (entry.when - other.due).days < 10:
                placed[entry.period] = bill             # the same statement, seen again: keep the later
            elif _month_before(entry.period) not in placed:
                other.period = _month_before(entry.period)
                placed[other.period] = other
                placed[entry.period] = bill
            else:
                placed[entry.period] = bill
        for bill in placed.values():
            bills[(account, fuel, bill.period)] = bill

    payments = [e for e in entries if e.kind == PAYMENT_TYPE and e.amount > 0]
    used: set[int] = set()
    for bill in bills.values():
        for index, payment in enumerate(payments):
            if index in used or payment.account != bill.account:
                continue
            same_month = payment.period == bill.period
            near = abs((bill.due - payment.when).days) <= 40
            if (same_month or near) and abs(payment.amount - bill.amount) < 0.01:
                bill.paid = payment.amount
                bill.paid_on = payment.when
                bill.status = payment.status.upper() or "PAID"
                used.add(index)
                break

    ordered = sorted(bills.values(), key=lambda b: (b.account, b.due))
    return ordered


def attach_usage(bills: list[Bill], periods: list) -> int:
    """Link each bill to the service period it charges for.

    Eversource bills roughly a month in arrears: the statement due 2026-10-16
    covers usage metered 2026-08-21..2026-09-17. Matching on the amount is
    exact and unambiguous, so we use that and require the lag to be sane rather
    than guessing from dates alone.

    Returns how many bills were matched. Duck-typed so this module stays
    independent of the export parser.
    """
    # Grouped by fuel, not by account. Eversource renumbers an account when
    # service is re-opened, and its export carries the pre-renumber history -
    # so the gas export for 55500030003 contains periods whose bills arrived
    # under the old masked number. Matching on the amount to the cent, inside a
    # sane lag window, and consuming each period once, makes the account number
    # unnecessary for the join.
    by_fuel: dict[str, list] = {}
    for period in periods:
        fuel = fuels.of_unit(getattr(period, "unit", "kWh")).name
        by_fuel.setdefault(fuel, []).append(period)

    taken: set[int] = set()
    matched = 0
    for bill in bills:
        bill.usage, bill.usage_exact = None, True
        for period in by_fuel.get(bill.fuel, []):
            if id(period) in taken or not getattr(period, "priced", True):
                continue
            lag = (bill.due - period.end).days
            if not (10 <= lag <= 50):
                continue
            if abs(period.cost - bill.amount) >= 0.01:
                continue
            bill.usage = period
            taken.add(id(period))
            matched += 1
            break

    # What the amounts can't pair, the dates may: a period that ended 10-50
    # days before exactly one bill, which in turn has exactly one such period.
    # That is a budget-billed account, an export without the taxes, a file
    # with no cost at all. Mutual uniqueness keeps it from ever being a guess.
    def window(bill, period):
        return 10 <= (bill.due - period.end).days <= 50

    loose_bills = [b for b in bills if b.usage is None]
    for bill in loose_bills:
        free = [p for p in by_fuel.get(bill.fuel, []) if id(p) not in taken and window(bill, p)]
        if len(free) != 1:
            continue
        period = free[0]
        if sum(1 for b in loose_bills if b.usage is None and window(b, period)) != 1:
            continue
        bill.usage, bill.usage_exact = period, False
        taken.add(id(period))
        matched += 1
    return matched


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------

def _round(value, digits=2):
    return None if value is None else round(value, digits)


def monthly_series(bills: list[Bill]) -> list[dict]:
    """The core series: one row per statement, with month-over-month deltas."""
    rows = []
    for index, bill in enumerate(bills):
        prev = bills[index - 1] if index else None
        delta = bill.amount - prev.amount if prev else None
        pct = (delta / prev.amount * 100) if prev and prev.amount else None
        use, prev_use = bill.usage, prev.usage if prev else None
        # A change in the bill splits exactly into a usage part and a rate part:
        #   d(cost) = rate_prev * d(kWh)  +  kWh_now * d(rate)
        usage_effect = rate_effect = None
        if use and prev_use and prev_use.rate is not None and use.rate is not None:
            usage_effect = prev_use.rate * (use.kwh - prev_use.kwh)
            rate_effect = use.kwh * (use.rate - prev_use.rate)

        rows.append(
            {
                "period": bill.period,
                "label": bill.label,
                "short": bill.short_label,
                "due": bill.due.isoformat(),
                "month": bill.month,
                "year": bill.year,
                "amount": _round(bill.amount),
                "delta": _round(delta),
                "pct": _round(pct, 1),
                "improved": bool(delta is not None and delta < 0),
                "paid": bill.paid is not None,
                "paid_on": bill.paid_on.isoformat() if bill.paid_on else None,
                "status": bill.status,
                # True when a metered period's own charge matched this amount
                # exactly - i.e. the figure is the month's charge, not a balance.
                "verified": use is not None and bill.usage_exact,
                # Usage, when a matching export row was found.
                "kwh": _round(use.kwh, 1) if use else None,
                "unit": getattr(use, "unit", "kWh") if use else None,
                "kwh_per_day": _round(use.kwh_per_day, 2) if use else None,
                "exported": _round(getattr(use, "exported", 0.0), 1) if use else None,
                "rate": _round(use.rate, 4) if use and use.rate else None,
                "days": use.days if use else None,
                "service_start": use.start.isoformat() if use else None,
                "service_end": use.end.isoformat() if use else None,
                "service_label": use.label if use else None,
                "kwh_delta": _round(use.kwh - prev_use.kwh, 1) if use and prev_use else None,
                "usage_effect": _round(usage_effect),
                "rate_effect": _round(rate_effect),
            }
        )
    return rows


def usage_streak(series: list[dict]) -> int:
    """Consecutive statements, ending at the latest, that used fewer kWh.

    This is the honest streak: unlike the dollar figure it cannot be faked by a
    rate cut.
    """
    streak = 0
    for row in reversed(series):
        if row.get("kwh_delta") is not None and row["kwh_delta"] < 0:
            streak += 1
        else:
            break
    return streak


def down_streak(series: list[dict]) -> int:
    """How many consecutive statements, ending at the latest, came in lower."""
    streak = 0
    for row in reversed(series):
        if row["delta"] is not None and row["delta"] < 0:
            streak += 1
        else:
            break
    return streak


def honest_streak(series: list[dict]) -> tuple[int, int, str]:
    """The streak that can't be flattered, and what it is made of.

    In order of honesty: statements below the same weeks last year with the
    weather taken out (``you_delta``, set where the temperatures are known);
    failing that, fewer units than the statement before; failing that, fewer
    dollars. Returns (current, best, basis).
    """
    for key, basis in (("you_delta", "weather"), ("kwh_delta", "usage"), ("delta", "dollars")):
        known = [r for r in series if r.get(key) is not None]
        if key != "delta" and len(known) < 2:
            continue
        wins = [r.get(key) is not None and r[key] < 0 for r in series]
        current = 0
        for win in reversed(wins):
            if not win:
                break
            current += 1
        best = run = 0
        for win in wins:
            run = run + 1 if win else 0
            best = max(best, run)
        return current, best, basis
    return 0, 0, "dollars"


def best_streak(series: list[dict]) -> int:
    best = run = 0
    for row in series:
        if row["delta"] is not None and row["delta"] < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def baseline_amount(series: list[dict], window: int) -> float | None:
    """Your starting average - the bar every later month is measured against.

    Using the *first* N statements (rather than a trailing average) keeps the
    target fixed, so banked savings can't be erased by the baseline drifting
    down as you improve.
    """
    if not series:
        return None
    window = max(1, min(window, len(series)))
    return mean(row["amount"] for row in series[:window])


def savings_series(series: list[dict], window: int) -> tuple[list[dict], float | None]:
    """Cumulative dollars saved against the starting baseline."""
    base = baseline_amount(series, window)
    if base is None:
        return [], None
    window = max(1, min(window, len(series)))
    running = 0.0
    out = []
    for row in series[window:]:
        saved = base - row["amount"]
        running += saved
        out.append(
            {
                "period": row["period"],
                "label": row["label"],
                "short": row["short"],
                "saved": _round(saved),
                "cumulative": _round(running),
            }
        )
    return out, _round(base)


def year_over_year(series: list[dict]) -> list[dict]:
    """Same calendar month, this year vs last - the seasonally fair comparison."""
    by_key = {(row["year"], row["month"]): row for row in series}
    out = []
    for row in series:
        prior = by_key.get((row["year"] - 1, row["month"]))
        if not prior:
            continue
        delta = row["amount"] - prior["amount"]
        kwh_delta = kwh_pct = None
        if row.get("kwh") is not None and prior.get("kwh"):
            kwh_delta = row["kwh"] - prior["kwh"]
            kwh_pct = kwh_delta / prior["kwh"] * 100
        out.append(
            {
                "period": row["period"],
                "label": row["label"],
                "month": row["month"],
                "month_name": MONTH_NAMES[row["month"] - 1],
                "this_year": row["amount"],
                "last_year": prior["amount"],
                "delta": _round(delta),
                "pct": _round(delta / prior["amount"] * 100, 1) if prior["amount"] else None,
                "kwh_this_year": row.get("kwh"),
                "kwh_last_year": prior.get("kwh"),
                "kwh_delta": _round(kwh_delta, 1),
                "kwh_pct": _round(kwh_pct, 1),
            }
        )
    return out


def seasonal_profile(series: list[dict]) -> list[dict]:
    """Average bill per calendar month - shows what the weather is costing you."""
    buckets: dict[int, list[float]] = {}
    for row in series:
        buckets.setdefault(row["month"], []).append(row["amount"])
    return [
        {
            "month": m,
            "month_name": MONTH_NAMES[m - 1],
            "avg": _round(mean(buckets[m])),
            "count": len(buckets[m]),
        }
        for m in sorted(buckets)
    ]


def combined_energy(bills: list[Bill]) -> list[dict]:
    """Total energy spend per month, split by fuel.

    Electric alone is only part of the bill - in some years gas has been the
    larger half - so the honest headline is the two added together.
    """
    months: dict[str, dict] = {}
    for bill in bills:
        row = months.setdefault(bill.period, {"period": bill.period, "total": 0.0})
        key = bill.fuel.lower()
        row[key] = row.get(key, 0.0) + bill.amount
        row["total"] += bill.amount
    out = []
    for period in sorted(months):
        row = months[period]
        year, month = period.split("-")
        row["label"] = f"{MONTH_NAMES[int(month) - 1]} {year}"
        row["short"] = f"{MONTH_NAMES[int(month) - 1]} '{year[2:]}"
        for key in [f.key for f in fuels.ORDER] + ["total"]:
            row[key] = _round(row.get(key, 0.0))
        out.append(row)
    return out


def baseload(series: list[dict], settings: dict) -> dict | None:
    """Estimate the always-on floor, and split each period around it.

    Usage never drops to zero: a fridge, a router, standby draw and anything
    left running set a floor you pay for every hour of the year. The estimate
    is the 10th percentile of daily usage - robust to one freak low month,
    where a plain minimum is not - and it is an **upper bound** on true
    baseload, since even the quietest month still contains some deliberate use.
    Only interval data can pin the real figure down.
    """
    rows = [r for r in series if r.get("kwh_per_day")]
    if len(rows) < 6:
        return None

    daily = sorted(r["kwh_per_day"] for r in rows)
    idx = max(0, int(len(daily) * 0.10) - 1)
    estimate = daily[idx]
    observed_min = daily[0]
    average = mean(r["kwh_per_day"] for r in rows)

    rates = [r["rate"] for r in rows if r.get("rate")]
    rate = rates[-1] if rates else None

    split = []
    for row in rows:
        days = row.get("days") or 30
        base_kwh = min(estimate * days, row["kwh"])
        split.append({
            "period": row["period"], "label": row["label"], "short": row["short"],
            "baseload_kwh": _round(base_kwh, 1),
            "variable_kwh": _round(row["kwh"] - base_kwh, 1),
            "kwh": row["kwh"],
        })

    units = {r.get("unit") for r in rows if r.get("unit")}
    return {
        "unit": units.pop() if len(units) == 1 else "kWh",
        "kwh_per_day": _round(estimate, 2),
        "observed_min": _round(observed_min, 2),
        "average_kwh_per_day": _round(average, 2),
        "share": _round(estimate / average * 100, 1) if average else None,
        "monthly_cost": _round(estimate * 30.4 * rate) if rate else None,
        "annual_cost": _round(estimate * 365 * rate) if rate else None,
        "rate": rate,
        "months": len(rows),
        "split": split,
    }


def _least_squares(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """(slope, intercept, r_squared), or None if it can't be fitted."""
    n = len(xs)
    if n < 4:
        return None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    return slope, my - slope * mx, (0.0 if syy == 0 else sxy ** 2 / (sxx * syy))


def tariff(series: list[dict]) -> dict | None:
    """Separate the standing charge from the per-unit price.

    A bill is a fixed customer charge plus usage x rate. Dividing cost by usage
    conflates the two, which barely matters for electricity (usage is always
    substantial) and is nonsense for gas: a summer month of 1 therm against a
    $10 standing charge reads as "$10 per therm". Fitting cost against usage
    recovers both numbers honestly.
    """
    rows = [r for r in series if r.get("kwh") is not None and r.get("amount") is not None]
    fitted = _least_squares([r["kwh"] for r in rows], [r["amount"] for r in rows])
    if fitted is None:
        return None
    slope, intercept, r2 = fitted
    if slope <= 0:
        return None
    return {
        "unit_rate": _round(slope, 4),
        "standing_charge": _round(max(intercept, 0.0)),
        "r2": _round(r2, 3),
        "n": len(rows),
        "unit": rows[-1].get("unit") or "kWh",
    }


def rate_history(series: list[dict], settings: dict) -> dict | None:
    """The price half of the bill, and the moments it jumped.

    Rate moves here have been far larger than any plausible behaviour change,
    so knowing when one happened is the difference between "I did well" and
    "the price fell".
    """
    rows = [r for r in series if r.get("rate")]
    if len(rows) < 2:
        return None

    threshold = float(settings.get("rate_change_pct", 3) or 3) / 100.0
    changes = []
    for prev, cur in zip(rows, rows[1:]):
        move = (cur["rate"] - prev["rate"]) / prev["rate"]
        if abs(move) >= threshold:
            changes.append({
                "period": cur["period"], "label": cur["label"],
                "from": prev["rate"], "to": cur["rate"],
                "pct": _round(move * 100, 1),
                # What that move alone did to the bill, holding usage fixed.
                "dollars": _round((cur["rate"] - prev["rate"]) * cur["kwh"]) if cur.get("kwh") else None,
            })

    # A month of almost no usage makes cost/usage meaningless - the standing
    # charge swamps it. Quote the range only over months that actually used
    # something.
    typical = _median([r["kwh"] for r in rows if r.get("kwh")]) if any(r.get("kwh") for r in rows) else 0
    meaningful = [r for r in rows if r.get("kwh") and r["kwh"] >= max(typical * 0.25, 1)]
    rates = [r["rate"] for r in (meaningful or rows)]
    compare = settings.get("compare_rate")
    savings = None
    if compare:
        try:
            compare = float(compare)
            last12 = [r for r in rows[-12:] if r.get("kwh")]
            actual = sum(r["kwh"] * r["rate"] for r in last12)
            hypothetical = sum(r["kwh"] * compare for r in last12)
            savings = {
                "rate": compare,
                "actual": _round(actual),
                "hypothetical": _round(hypothetical),
                "delta": _round(hypothetical - actual),
                "months": len(last12),
            }
        except (TypeError, ValueError):
            compare = None

    return {
        "series": [{"period": r["period"], "label": r["label"], "short": r["short"],
                    "rate": r["rate"]} for r in (meaningful or rows)],
        "current": rates[-1], "min": _round(min(rates), 4), "max": _round(max(rates), 4),
        "changes": changes,
        "compare": savings,
        "unit": rows[-1].get("unit") or "kWh",
        "excluded": len(rows) - len(meaningful or rows),
        "tariff": tariff(rows),
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def anomalies(series: list[dict], settings: dict, recent: list[dict] | None = None) -> list[dict]:
    """Statements that don't look like the same month in *nearby* years.

    Two constraints keep this from crying wolf:

    * peers are the same calendar month within a few years, not the whole
      history - bills have tripled since 2018, so an all-time median flags
      inflation rather than anything wrong;
    * comparison is on **kWh per day** wherever usage is known, because that
      is independent of the rate. A 20% price move is not an anomaly in how
      you live.

    Only statements in ``recent`` are reported, since an odd month in 2019
    isn't something you can act on.
    """
    threshold = float(settings.get("anomaly_pct", 25) or 25)
    span = int(settings.get("anomaly_year_span", 3) or 3)
    scope = recent if recent is not None else series
    scope_periods = {r["period"] for r in scope}

    # A percentage on a near-zero base explodes: summer gas going from 0.01 to
    # 0.03 therms/day is "+100%" and means nothing. Ignore months whose usage
    # is a small fraction of typical - there is no signal down there.
    daily = [r["kwh_per_day"] for r in series if r.get("kwh_per_day")]
    floor = _median(daily) * 0.25 if daily else 0

    out = []
    for row in series:
        if row["period"] not in scope_periods:
            continue
        use_kwh = row.get("kwh_per_day") is not None
        if use_kwh and row["kwh_per_day"] < floor:
            continue
        metric = "kwh_per_day" if use_kwh else "amount"
        peers = [r[metric] for r in series
                 if r["month"] == row["month"]
                 and r["period"] != row["period"]
                 and abs(r["year"] - row["year"]) <= span
                 and r.get(metric) is not None
                 and (not use_kwh or r[metric] >= floor)]
        if len(peers) < 2:
            continue
        expected = _median(peers)
        if not expected:
            continue
        deviation = (row[metric] - expected) / expected * 100
        if abs(deviation) < threshold:
            continue
        out.append({
            "period": row["period"], "label": row["label"],
            "basis": "usage" if use_kwh else "cost",
            "unit": f"{row.get('unit') or 'kWh'}/day" if use_kwh else "$",
            "actual": _round(row[metric], 2), "expected": _round(expected, 2),
            "pct": _round(deviation, 1),
            "amount": row["amount"],
            "high": deviation > 0,
            "peers": len(peers),
        })
    out.sort(key=lambda r: r["period"], reverse=True)
    return out


def charge_discrepancies(bills: list[Bill], periods: list) -> list[dict]:
    """Metered periods whose charge doesn't equal what the bill asked for.

    Run after ``attach_usage``: anything left unmatched had a bill nearby but
    a different number on it. That gap is real information - a credit, an
    adjustment, or a balance - and the export is the authoritative side, since
    it states the period's own charge.
    """
    out = []
    for bill in bills:
        period = bill.usage
        if period is None or bill.usage_exact or not getattr(period, "priced", True):
            continue
        fuel = fuels.of_unit(getattr(period, "unit", "kWh")).name
        out.append({
            "fuel": fuel,
            "service": period.label,
            "due": bill.due.isoformat(),
            "label": bill.label,
            "charge": _round(period.cost),
            "billed": _round(bill.amount),
            "diff": _round(period.cost - bill.amount),
            "usage": _round(period.kwh, 1),
            "unit": getattr(period, "unit", "kWh"),
        })
    out.sort(key=lambda r: r["due"], reverse=True)
    return out


def suspect_balances(bills: list[Bill]) -> list[dict]:
    """Stretches where 'amount due' looks like a rolled-up balance.

    The bill emails report **Total Amount Due**, which carries anything unpaid
    from earlier months. The signature is specific: three or more consecutive
    rises, then a collapse once a payment lands. Summing those months
    double-counts the part that was carried, so they are not monthly cost.

    Detected by shape rather than size - comparing against a median would just
    rediscover that bills have tripled since 2018.
    """
    out = []
    for fuel in sorted({b.fuel for b in bills}):
        rows = sorted((b for b in bills if b.fuel == fuel), key=lambda b: b.due)
        run: list[Bill] = []
        for prev, cur in zip(rows, rows[1:]):
            if cur.amount > prev.amount:
                if not run:
                    run = [prev]
                run.append(cur)
                continue
            # The run ended - was it a climb that then collapsed?
            if len(run) >= 4 and cur.amount < run[-1].amount * 0.5:
                out.append({
                    "fuel": fuel,
                    "from": run[0].period, "to": run[-1].period,
                    "label": f"{MONTH_NAMES[run[0].month - 1]} {run[0].year}"
                             f" – {MONTH_NAMES[run[-1].month - 1]} {run[-1].year}",
                    "months": len(run),
                    "low": _round(run[0].amount), "peak": _round(run[-1].amount),
                    "then": _round(cur.amount),
                })
            run = []
    out.sort(key=lambda r: r["from"])
    return out


def raw_rows(entries: list[Entry], bills: list[Bill]) -> list[dict]:
    """Every record in the CSV, flattened, with whatever we derived from it.

    This is the escape hatch: the charts answer the questions we anticipated,
    and this answers the rest. Nothing is filtered or rounded away - if a row
    is in the file it is here, with its provenance attached.
    """
    # Bills carry the derived fields; index them so entries can borrow.
    derived: dict[tuple[str, str, float], Bill] = {}
    for bill in bills:
        derived[(bill.account, bill.period, round(bill.amount, 2))] = bill

    out = []
    for entry in entries:
        bill = derived.get((entry.account, entry.period, round(entry.amount, 2))) \
            if entry.kind == BILL_TYPE else None
        use = bill.usage if bill else None
        row = {
            "date": entry.when.isoformat(),
            "period": entry.period,
            "fuel": entry.account_type or "Electric",
            "account": entry.account,
            "nickname": entry.nickname,
            "type": entry.kind,
            "amount": _round(entry.amount),
            "status": entry.status,
            "source": entry.source or "",
            "kwh": _round(use.kwh, 1) if use else None,
            "unit": getattr(use, "unit", "kWh") if use else None,
            "days": use.days if use else None,
            "kwh_per_day": _round(use.kwh_per_day, 2) if use else None,
            "rate": _round(use.rate, 4) if use and use.rate else None,
            "service_start": use.start.isoformat() if use else None,
            "service_end": use.end.isoformat() if use else None,
            "verified": bool(use),
        }
        out.append(row)
    out.sort(key=lambda r: (r["date"], r["fuel"], r["type"]), reverse=True)
    return out


def usage_rows(periods: list) -> list[dict]:
    """The metered periods, as read from the usage exports."""
    rows = []
    for p in periods:
        rows.append({
            "account": p.account,
            "start": p.start.isoformat(),
            "end": p.end.isoformat(),
            "days": p.days,
            "kwh": _round(p.kwh, 1),
            "unit": getattr(p, "unit", "kWh"),
            "kwh_per_day": _round(p.kwh_per_day, 2),
            "cost": _round(p.cost),
            "rate": _round(p.rate, 4) if p.rate else None,
            "avg_temp": p.avg_temp,
            "read_type": p.read_type or "",
            "estimated": p.estimated,
            "source": p.source,
        })
    rows.sort(key=lambda r: r["end"], reverse=True)
    return rows


def summarise(series: list[dict], settings: dict) -> dict:
    """Headline numbers for the stat tiles and the hero figure."""
    if not series:
        return {}

    amounts = [row["amount"] for row in series]
    latest = series[-1]
    previous = series[-2] if len(series) > 1 else None
    last12 = series[-12:]
    # Only compare against a *full* prior year. With 13 statements the slice
    # below would otherwise average a single month and label it "the prior 12".
    prior12 = series[-24:-12] if len(series) >= 24 else []

    lowest = min(series, key=lambda r: r["amount"])
    highest = max(series, key=lambda r: r["amount"])

    rolling12 = _round(mean(row["amount"] for row in last12))
    prior_rolling = _round(mean(row["amount"] for row in prior12)) if prior12 else None

    goal = settings.get("goal")
    goal_hit = None
    if goal:
        goal_hit = latest["amount"] <= goal

    with_usage = [r for r in series if r.get("kwh") is not None]
    last12_usage = [r for r in last12 if r.get("kwh") is not None]
    rates = [r["rate"] for r in with_usage if r.get("rate")]

    # Split the whole tracked run into the part you caused and the part the
    # rate caused - they sum to the total change, exactly.
    usage_effect_total = sum(r["usage_effect"] for r in series if r.get("usage_effect") is not None)
    rate_effect_total = sum(r["rate_effect"] for r in series if r.get("rate_effect") is not None)

    return {
        "latest": latest,
        "previous": previous,
        "count": len(series),
        "usage_count": len(with_usage),
        "kwh_12": _round(sum(r["kwh"] for r in last12_usage), 1) if last12_usage else None,
        "exported_12": _round(sum(r.get("exported") or 0 for r in last12_usage), 1)
        if any(r.get("exported") for r in last12_usage) else None,
        "kwh_latest": latest.get("kwh"),
        "rate_latest": latest.get("rate"),
        "rate_min": _round(min(rates), 4) if rates else None,
        "rate_max": _round(max(rates), 4) if rates else None,
        "rate_first": with_usage[0]["rate"] if with_usage else None,
        "rate_change_pct": _round(
            (rates[-1] - rates[0]) / rates[0] * 100, 1) if len(rates) > 1 and rates[0] else None,
        "usage_streak": usage_streak(series),
        "usage_effect_total": _round(usage_effect_total),
        "rate_effect_total": _round(rate_effect_total),
        "first_period": series[0]["period"],
        "last_period": latest["period"],
        "total_12": _round(sum(row["amount"] for row in last12)),
        "total_all": _round(sum(amounts)),
        "avg_12": rolling12,
        "avg_prior_12": prior_rolling,
        "rolling_delta": _round(rolling12 - prior_rolling) if prior_rolling else None,
        "lowest": lowest,
        "highest": highest,
        "is_record_low": latest["period"] == lowest["period"] and len(series) > 1,
        "streak": honest_streak(series)[0],
        "best_streak": honest_streak(series)[1],
        "streak_basis": honest_streak(series)[2],
        "dollar_streak": down_streak(series),
        "goal": goal,
        "goal_hit": goal_hit,
        "months_below_goal": sum(1 for r in series if goal and r["amount"] <= goal),
        "unpaid": [r for r in series if not r["paid"]],
    }


def project(series: list[dict], summary: dict, target_pct: float) -> dict:
    """What a sustained percentage cut is worth over the next twelve months.

    Each projected month is the same calendar month's average so far, reduced by
    ``target_pct`` - so the estimate respects the seasonal shape instead of
    flat-lining at the current bill.
    """
    if not series:
        return {}
    profile = {row["month"]: row["avg"] for row in seasonal_profile(series)}
    overall = mean(row["amount"] for row in series[-12:])

    last = series[-1]
    year, month = last["year"], last["month"]
    factor = 1 - (target_pct / 100.0)

    months = []
    for _ in range(12):
        month += 1
        if month > 12:
            month = 1
            year += 1
        typical = profile.get(month, overall)
        months.append(
            {
                "period": f"{year:04d}-{month:02d}",
                "short": f"{MONTH_NAMES[month - 1]} '{str(year)[2:]}",
                "baseline": _round(typical),
                "projected": _round(typical * factor),
            }
        )

    baseline_total = sum(m["baseline"] for m in months)
    projected_total = sum(m["projected"] for m in months)
    return {
        "target_pct": target_pct,
        "months": months,
        "baseline_total": _round(baseline_total),
        "projected_total": _round(projected_total),
        "saved": _round(baseline_total - projected_total),
    }


def analyse(bills: list[Bill], settings: dict, periods: list | None = None) -> dict:
    """Everything the dashboard needs, in one JSON-ready dict.

    Two views of the same statements:

    * ``series`` is the **whole** history - what the charts and the
      year-over-year comparison are drawn from.
    * ``game_series`` is a recent window. Records, streaks, badges and the
      savings baseline score against this, because measuring today's bill
      against 2019 prices makes the game unwinnable rather than motivating.
    """
    matched = attach_usage(bills, periods or [])
    series = monthly_series(bills)

    game_window = int(settings.get("game_window_months", 24) or 24)
    game_series = series[-game_window:] if 0 < game_window < len(series) else series

    window = int(settings.get("baseline_months", 6) or 6)
    savings, base = savings_series(game_series, window)
    summary = summarise(game_series, settings)

    return {
        "series": series,
        "game_series": game_series,
        "game_window": len(game_series),
        "game_first_period": game_series[0]["period"] if game_series else None,
        "scored_on_all": len(game_series) == len(series),
        "summary": summary,
        "usage_matched": matched,
        "savings": savings,
        "baseline": base,
        "baseline_months": min(window, len(game_series)) if game_series else 0,
        "banked": savings[-1]["cumulative"] if savings else 0.0,
        # Trend and seasonality want every statement there is.
        "yoy": year_over_year(series),
        "seasonal": seasonal_profile(series),
        "projection": project(series, summary, float(settings.get("target_pct", 5) or 5)),
        "baseload": baseload(series, settings),
        "rates": rate_history(series, settings),
        "tariff": tariff(series),
        "anomalies": anomalies(series, settings, game_series),
    }
