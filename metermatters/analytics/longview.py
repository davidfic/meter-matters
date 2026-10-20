"""The long view: why a bill that was $100 a month is $300 now.

Year by year, the average statement is split into what changed in usage and
what changed in price, against the first full year on file:

    bill_y - bill_b  =  (kwh_y - kwh_b) * rate_b  +  kwh_y * (rate_y - rate_b)
                        \\___ usage added ___/      \\____ price added ____/

The two parts add up to the change exactly. Everything is per statement, so
a year still in progress can sit beside the full ones.

Usage is read off the metered periods where they exist. Where they don't -
the usage export reaches back a few years, the bills further - it can be
estimated from a published rate for the area: usage = (bill - customer
charge) / price per kWh. Those years say so. Where there is no published
rate either, the year shows its dollars and nothing more.

Weather, where temperatures are on file: how much of a year's usage the
weather accounts for against a typical year, so the usage part can be read
as "the weather" and "you".
"""

from __future__ import annotations

from datetime import date, datetime
from statistics import mean

FULL_YEAR = 11          # statements; one can spill into the next year by its due date
ENOUGH = 6              # fewer and a year says nothing
MATCH_WITHIN = 0.15     # a published rate counts as "your tariff" within this of the metered years


def _round(value, places=2):
    return None if value is None else round(value, places)


def _mid(r: dict) -> date | None:
    """The middle of the statement's metered period, else its due date."""
    if r.get("service_start") and r.get("service_end"):
        a, b = date.fromisoformat(r["service_start"]), date.fromisoformat(r["service_end"])
        return a + (b - a) / 2
    if r.get("due"):
        return datetime.fromisoformat(r["due"]).date()
    return None


def published_matches(series: list[dict], published) -> bool:
    """Does the published rate describe this account? Checked on the metered
    statements: their own effective price has to sit near the published one."""
    pairs = []
    for r in series:
        if not r.get("kwh") or not r.get("amount"):
            continue
        day = _mid(r)
        p = published(day) if day else None
        if p is None:
            continue
        own = (r["amount"] - p["customer"]) / r["kwh"] if r["kwh"] else None
        if own is not None and own > 0:
            pairs.append(abs(own - p["per_kwh"]) / p["per_kwh"])
    return len(pairs) >= 6 and mean(pairs) <= MATCH_WITHIN


def _year_rows(series: list[dict], published, weather) -> list[dict]:
    by_year: dict[int, list[dict]] = {}
    for r in series:
        by_year.setdefault(r["year"], []).append(r)
    out = []
    for year in sorted(by_year):
        rows = by_year[year]
        if len(rows) < ENOUGH:
            continue
        spend = sum(r["amount"] for r in rows)
        kwh, estimated, unknown = 0.0, 0, 0
        for r in rows:
            if r.get("kwh") is not None:
                kwh += r["kwh"]
                continue
            day = _mid(r)
            p = published(day) if (published and day) else None
            if p is None or p["per_kwh"] <= 0:
                unknown += 1
                continue
            kwh += max(0.0, r["amount"] - p["customer"]) / p["per_kwh"]
            estimated += 1
        n = len(rows)
        usage_known = unknown == 0 and kwh > 0
        row = {
            "year": year, "statements": n, "full": n >= FULL_YEAR,
            "spend": _round(spend), "bill": _round(spend / n),
            "kwh": _round(kwh / n, 1) if usage_known else None,
            "rate": _round(spend / kwh, 4) if usage_known else None,
            "_bill": spend / n, "_kwh": kwh / n if usage_known else None, "_rate": spend / kwh if usage_known else None,
            "basis": "metered" if usage_known and not estimated else "estimated" if usage_known else "dollars",
            "estimated_statements": estimated,
            "weather_kwh": None,
        }
        if weather is not None and usage_known:
            shares = [weather(r) for r in rows]
            if all(s is not None for s in shares):
                row["weather_kwh"] = _round(sum(shares) / n, 1)
                row["_weather_kwh"] = sum(shares) / n
        out.append(row)
    return out


def build(series: list[dict], published=None, weather=None, source: dict | None = None) -> dict | None:
    """The years, each split against the first full one.

    *published(day)* -> {"per_kwh", "customer"} or None, for the area's
    published rate on that day. *weather(row)* -> kWh of that statement's
    usage the weather accounts for against a typical year, or None.
    """
    if published is not None and not published_matches(series, published):
        published = None
    years = _year_rows(series, published, weather)
    if len(years) < 2:
        return None
    base = next((y for y in years if y["full"] and y["kwh"] is not None), None)
    if base is None:
        return None
    for y in years:
        y["baseline"] = y is base
        if y["kwh"] is None or y is base:
            y.update({"usage_added": None, "price_added": None, "weather_added": None, "change": None})
            if y is not base:
                y["change"] = _round(y["_bill"] - base["_bill"])
            continue
        y["change"] = _round(y["_bill"] - base["_bill"])
        y["usage_added"] = _round((y["_kwh"] - base["_kwh"]) * base["_rate"])
        y["price_added"] = _round(y["_kwh"] * (y["_rate"] - base["_rate"]))
        if y.get("_weather_kwh") is not None:
            y["weather_added"] = _round((y["_weather_kwh"] - base.get("_weather_kwh", 0.0)) * base["_rate"])
        else:
            y["weather_added"] = None
    # The newest year with usage that is not the base - a full one if there is one, else the one in progress.
    after = [y for y in years if y["_kwh"] is not None and y is not base and y["year"] > base["year"]]
    latest = next((y for y in reversed(after) if y["full"]), after[-1] if after else None)
    what_if = None
    if latest is not None:
        what_if = {
            "latest": latest["year"], "base": base["year"], "partial": not latest["full"],
            # Per statement: today's usage at the old price, the old usage at today's price.
            "usage_now_price_then": _round(latest["_kwh"] * base["_rate"]),
            "usage_then_price_now": _round(base["_kwh"] * latest["_rate"]),
            "bill_then": _round(base["_bill"]), "bill_now": _round(latest["_bill"]),
            "usage_pct": _round((latest["_kwh"] - base["_kwh"]) / base["_kwh"] * 100, 1),
            "price_pct": _round((latest["_rate"] - base["_rate"]) / base["_rate"] * 100, 1),
        }
    for y in years:
        for key in [k for k in y if k.startswith("_")]:
            del y[key]
    return {
        "years": years,
        "base": base["year"],
        "what_if": what_if,
        "estimated": any(y["basis"] == "estimated" for y in years),
        "dollars_only": [y["year"] for y in years if y["basis"] == "dollars"],
        "weather": any(y.get("weather_added") is not None for y in years),
        "source": source if published is not None else None,
    }
