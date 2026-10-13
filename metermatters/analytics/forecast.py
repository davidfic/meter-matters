"""What the next statement will probably be.

The one number people open a bill app for. Two ways to get it, in order of
how much there is to go on:

* **From usage.** The same weeks last year, scaled by how the home has been
  running lately (the last three periods against the same three a year
  earlier), priced at today's unit price plus the standing charge.
* **From bills alone.** The same month last year, scaled the same way.

Either way the range comes from the method's own record: the estimate is run
for each of the last twelve statements as if it hadn't happened yet, and the
typical miss becomes the width of the range. A method that has been 8% out
says so; one that has been 30% out says that instead.

Nothing here is a promise. The page says "about".
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean, median

MIN_HISTORY = 4
# How far the "lately" scaling may pull the estimate from last year.
SCALE_FLOOR, SCALE_CEIL = 0.6, 1.5


def _ratio(recent: list[float], then: list[float]) -> float:
    if not recent or not then or not sum(then):
        return 1.0
    return max(SCALE_FLOOR, min(SCALE_CEIL, mean(recent) / mean(then)))


def _due_after(series: list[dict]) -> tuple[date | None, int]:
    """When the next statement falls due, and how long its period is."""
    dues = [date.fromisoformat(r["due"]) for r in series[-7:] if r.get("due")]
    gaps = [(b - a).days for a, b in zip(dues, dues[1:])] or [30]
    step = int(median(gaps))
    days = [r["days"] for r in series[-7:] if r.get("days")]
    return (dues[-1] + timedelta(days=step)) if dues else None, int(median(days)) if days else step


def _from_bills(series: list[dict], upto: int) -> float | None:
    """The estimate for series[upto], using only what came before it."""
    before = series[:upto]
    if len(before) < MIN_HISTORY:
        return None
    month = series[upto]["month"]
    same = [r for r in before if r["month"] == month]
    recent = [r["amount"] for r in before[-3:]]
    # A credit last year is not a bill to scale: "about −$30" is no estimate.
    if same and same[-1]["amount"] > 0:
        last_year = same[-1]
        idx = before.index(last_year)
        then = [r["amount"] for r in before[max(0, idx - 2):idx + 1]]
        return last_year["amount"] * _ratio(recent, then)
    return mean(recent)


def _from_usage(series: list[dict], upto: int, price: float, standing_per_day: float) -> float | None:
    before = [r for r in series[:upto] if r.get("kwh_per_day")]
    if len(before) < MIN_HISTORY or not price:
        return None
    month = series[upto]["month"]
    days = series[upto].get("days") or 30
    same = [r for r in before if r["month"] == month]
    recent = [r["kwh_per_day"] for r in before[-3:]]
    if same:
        last_year = same[-1]
        idx = before.index(last_year)
        then = [r["kwh_per_day"] for r in before[max(0, idx - 2):idx + 1]]
        per_day = last_year["kwh_per_day"] * _ratio(recent, then)
    else:
        per_day = mean(recent)
    return per_day * days * price + standing_per_day * days


def _typical_miss(series: list[dict], estimate) -> float | None:
    """Median absolute error of `estimate` over the last twelve statements, as a fraction."""
    misses = []
    for i in range(max(MIN_HISTORY, len(series) - 12), len(series)):
        guess = estimate(i)
        actual = series[i]["amount"]
        if guess is not None and actual:
            misses.append(abs(guess - actual) / actual)
    return median(misses) if len(misses) >= 3 else None


def next_bill(series: list[dict], price: float | None = None,
              standing_per_day: float = 0.0) -> dict | None:
    """The next statement: amount, a range, when, and what it rests on."""
    if len(series) < MIN_HISTORY:
        return None
    due, days = _due_after(series)
    last = series[-1]
    month = last["month"] % 12 + 1
    year = last["year"] + (1 if month == 1 else 0)
    pretend = {"month": month, "year": year, "days": days, "amount": 0.0}
    extended = series + [pretend]
    at = len(series)

    usage_ready = price and sum(1 for r in series if r.get("kwh_per_day")) >= MIN_HISTORY
    if usage_ready:
        amount = _from_usage(extended, at, price, standing_per_day)
        miss = _typical_miss(series, lambda i: _from_usage(series, i, price, standing_per_day))
        basis = "usage"
    else:
        amount = None
    if amount is None:
        amount = _from_bills(extended, at)
        miss = _typical_miss(series, lambda i: _from_bills(series, i))
        basis = "bills"
    if amount is None:
        return None
    spread = miss if miss is not None else 0.2
    # Kept in order whatever the sign of the estimate.
    low, high = sorted((amount * (1 - spread), amount * (1 + spread)))
    return {
        "amount": round(amount, 2),
        "low": round(low, 2),
        "high": round(high, 2),
        "due": due.isoformat() if due else None,
        "days": days,
        "basis": basis,
        "typical_miss_pct": round(spread * 100) if miss is not None else None,
        "backtested": miss is not None,
    }
