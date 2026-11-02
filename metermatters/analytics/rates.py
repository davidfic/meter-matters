"""Your price per kWh, taken apart.

A statement shows one all-in price. Underneath it are two: *supply*, the
electricity itself, and *delivery*, the wires, the poles and the meter, set
by the utility and the same whoever supplies the power. In states with retail
choice the supply half can be shopped for; everywhere, knowing the split says
what a change in one half does to the bill.

Nothing here reaches outside the app. The supply price is the one the person
reads off their bill's supply line. With it the all-in price splits, and the
Rates page can try another supply price over the past year's usage. Without
it the page still shows the price, its history and its seasons.
"""

from __future__ import annotations


def last_year(series: list[dict]) -> tuple[float, int]:
    """kWh over the most recent twelve statements that have usage, and how many."""
    rows = [r for r in series if r.get("kwh")][-12:]
    return sum(r["kwh"] for r in rows), len(rows)


def breakdown(rate: float | None, supply_cents: float | None, tariff: dict | None) -> dict | None:
    """Where one kWh's price goes: supply, delivery and everything else.

    ``rate`` is the all-in price in dollars per kWh from the latest statement.
    Delivery is what is left once supply is taken out, so it carries any
    taxes and charges that ride on the kWh as well - the estimate is only as
    good as the supply figure it starts from.
    """
    if not rate or supply_cents is None:
        return None
    supply = supply_cents / 100
    if supply >= rate:
        return None
    return {"all_in": round(rate, 4), "supply": round(supply, 4),
            "delivery": round(rate - supply, 4),
            "supply_share": round(supply / rate * 100, 1),
            "standing_charge": tariff.get("standing_charge") if tariff else None}


def seasons(series: list[dict]) -> list[dict]:
    """The average all-in price in each calendar month, over the years held."""
    by_month: dict[int, list[float]] = {}
    for row in series:
        if row.get("rate") and row.get("kwh") and row.get("month"):
            by_month.setdefault(row["month"], []).append(row["rate"])
    return [{"month": m, "rate": round(sum(v) / len(v), 4), "n": len(v)}
            for m, v in sorted(by_month.items())]


def standing(series: list[dict], rate: float | None) -> dict | None:
    """Where the latest price sits among every price paid, and the past year's."""
    rates = [r["rate"] for r in series if r.get("rate") and r.get("kwh")]
    if not rate or len(rates) < 3:
        return None
    below = sum(1 for r in rates if r < rate)
    last = [r for r in series if r.get("rate") and r.get("kwh")]
    year_ago = next((r["rate"] for r in reversed(last[:-1])
                     if r.get("period") and last[-1].get("period")
                     and r["year"] == last[-1]["year"] - 1 and r["month"] == last[-1]["month"]), None)
    return {"percentile": round(below / len(rates) * 100),
            "of": len(rates),
            "year_ago": year_ago,
            "year_pct": round((rate - year_ago) / year_ago * 100, 1) if year_ago else None}
