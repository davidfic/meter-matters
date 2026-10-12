#!/usr/bin/env python
"""Exploration: a fair comparison with last year, with the weather taken out.

Not part of the app. Reads the live usage exports, fetches daily temperatures
for the service area, fits how this home responds to weather, and writes a
self-contained page of what it found:

    .venv/bin/python explore/weather_explore.py        # -> explore/weather-adjusted.html

Questions it answers:

* How much of the usage does the weather explain? (Gas: nearly all. Electric: none.)
* Against the same weeks last year, how much of the change was weather, how
  much was the rate, and how much was the household?
* Would "your bill is tracking toward $X" have been right, had we said it?
* What is the carbon footprint, and what would specific changes be worth?

Weather is Open-Meteo's historical archive (CC BY 4.0, free for non-commercial
use). Only the ZIP code's town is sent, rounded to two decimal places.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import greenbutton  # noqa: E402

CACHE = HERE / "weather-daily.json"
TEMPLATE = HERE / "template.html"
OUT = HERE / "weather-adjusted.html"

# EPA eGRID2023, New England subregion (NEWE): 543.2 lb CO2e per MWh.
KG_PER_KWH = 543.2 * 0.45359237 / 1000
# EPA Greenhouse Gas Equivalencies Calculator: 0.0053 metric tons CO2 per therm.
KG_PER_THERM = 5.3


# ----------------------------------------------------------------- weather

def fetch_weather(zipcode: str, start: date, end: date) -> dict:
    def get(url):
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.load(response)

    found = get("https://geocoding-api.open-meteo.com/v1/search?"
                + urllib.parse.urlencode({"name": zipcode, "count": 1, "countryCode": "US"}))
    place = found["results"][0]
    lat, lon = round(place["latitude"], 2), round(place["longitude"], 2)
    daily = get("https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "start_date": start.isoformat(),
        "end_date": end.isoformat(), "daily": "temperature_2m_mean",
        "temperature_unit": "fahrenheit", "timezone": place.get("timezone", "auto")}))
    return {"place": f"{place['name']}, {place.get('admin1', '')}".strip(", "),
            "time": daily["daily"]["time"], "temp": daily["daily"]["temperature_2m_mean"]}


def load_weather(export) -> tuple[str, dict]:
    if not CACHE.exists():
        zipcode = re.search(r"\b(\d{5})\b\s*$", export.address or "")
        if not zipcode:
            raise SystemExit("no ZIP code in the usage export to look the weather up by")
        first = min(p.start for p in export.periods)
        CACHE.write_text(json.dumps(fetch_weather(zipcode.group(1), first, date.today())))
    raw = json.loads(CACHE.read_text())
    return raw["place"], {date.fromisoformat(d): t
                          for d, t in zip(raw["time"], raw["temp"]) if t is not None}


class Weather:
    def __init__(self, temps: dict):
        self.temps = temps

    def covers(self, p) -> bool:
        return all(d in self.temps for d in self.days(p))

    @staticmethod
    def days(p):
        d = p.start
        while d <= p.end:
            yield d
            d += timedelta(days=1)

    def heating(self, p, base) -> float:
        """Degrees below `base`, averaged per day of the period."""
        return sum(max(0.0, base - self.temps[d]) for d in self.days(p)) / p.days

    def cooling(self, p, base) -> float:
        return sum(max(0.0, self.temps[d] - base) for d in self.days(p)) / p.days

    def average(self, p) -> float:
        return mean(self.temps[d] for d in self.days(p))


# -------------------------------------------------------------- regression

def solve(a, b):
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        pivot = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[pivot][c]) < 1e-12:
            return None
        m[c], m[pivot] = m[pivot], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(xs, ys):
    """Least squares with an intercept; xs is a list of feature tuples."""
    rows = [(1.0, *x) for x in xs]
    k = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    aty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(k)]
    beta = solve(ata, aty)
    if beta is None:
        return None
    predicted = [sum(b * v for b, v in zip(beta, r)) for r in rows]
    centre = mean(ys)
    total = sum((y - centre) ** 2 for y in ys)
    left = sum((y - p) ** 2 for y, p in zip(ys, predicted))
    return {"beta": beta, "r2": 1 - left / total if total else 0.0,
            "rmse": (left / max(len(ys) - k, 1)) ** 0.5, "n": len(ys)}


class Model:
    """How one fuel in one home responds to the weather."""

    def __init__(self, wx: Weather, periods, fuel: str):
        self.wx, self.fuel = wx, fuel
        ys = [p.kwh_per_day for p in periods]
        best = None
        # The balance point - the outdoor temperature below which the heating
        # comes on - differs by house, so it is searched for, not assumed.
        if fuel == "Gas":
            for heat in range(50, 71):
                m = fit([(wx.heating(p, heat),) for p in periods], ys)
                if m and (best is None or m["r2"] > best["r2"]):
                    best = {**m, "heat": heat, "cool": None}
        else:
            for heat in range(45, 66):
                for cool in range(60, 76):
                    m = fit([(wx.heating(p, heat), wx.cooling(p, cool)) for p in periods], ys)
                    if m and (best is None or m["r2"] > best["r2"]):
                        best = {**m, "heat": heat, "cool": cool}
        self.__dict__.update(best)

    def features(self, p):
        if self.cool is None:
            return (self.wx.heating(p, self.heat),)
        return (self.wx.heating(p, self.heat), self.wx.cooling(p, self.cool))

    def expect(self, p) -> float:
        """Usage per day this home would normally show in this weather."""
        return sum(b * v for b, v in zip(self.beta, (1.0, *self.features(p))))


# ---------------------------------------------------------------- analysis

def short(p) -> str:
    return f"{p.end:%b} '{p.end:%y}"


def span(p) -> str:
    return f"{p.start:%b %-d} – {p.end:%b %-d, %Y}"


def middle(p) -> date:
    return p.start + (p.end - p.start) / 2


def year_before(p, periods):
    """The period covering the same weeks of the calendar, one year earlier."""
    target = middle(p) - timedelta(days=365)
    earlier = [q for q in periods if q.end < p.start]
    if not earlier:
        return None
    q = min(earlier, key=lambda q: abs((middle(q) - target).days))
    return q if abs((middle(q) - target).days) <= 16 else None


def compare(periods, model: Model, wx: Weather) -> list[dict]:
    rows = []
    for p in periods:
        q = year_before(p, periods)
        if q is None:
            continue
        raw = p.kwh_per_day - q.kwh_per_day
        weather = model.expect(p) - model.expect(q)
        you = raw - weather
        rate_now = p.cost / p.kwh if p.kwh else 0.0
        rate_then = q.cost / q.kwh if q.kwh else 0.0
        rows.append({
            "label": short(p), "span": span(p), "days": p.days,
            "now": p.kwh_per_day, "then": q.kwh_per_day,
            "temp_now": wx.average(p), "temp_then": wx.average(q),
            "raw": raw, "weather": weather, "you": you,
            # Dollars for the period. The three parts sum to the change exactly.
            "d_total": (p.cost / p.days - q.cost / q.days) * p.days,
            "d_weather": rate_then * weather * p.days,
            "d_you": rate_then * you * p.days,
            "d_rate": p.kwh_per_day * (rate_now - rate_then) * p.days,
        })
    return rows


def totals(rows: list[dict]) -> dict:
    then = sum(r["then"] * r["days"] for r in rows)
    part = lambda key: sum(r[key] * r["days"] for r in rows)  # noqa: E731
    return {
        "then": then, "now": sum(r["now"] * r["days"] for r in rows),
        "raw": part("raw"), "weather": part("weather"), "you": part("you"),
        "raw_pct": part("raw") / then * 100, "weather_pct": part("weather") / then * 100,
        "you_pct": part("you") / then * 100,
        "lower_raw": sum(1 for r in rows if r["raw"] < 0),
        "lower_adjusted": sum(1 for r in rows if r["you"] < 0),
        "flips": sum(1 for r in rows if (r["raw"] < 0) != (r["you"] < 0)),
        "periods": len(rows),
        "dollars": {k: sum(r[f"d_{k}"] for r in rows) for k in ("total", "weather", "you", "rate")},
    }


def level_shift(periods) -> dict | None:
    """A stretch where usage stepped to a new level and later stepped back.

    Found by trying every pair of break points and keeping the pair that
    leaves the least unexplained - reported only when it explains a good part
    of the variation and the step is big enough to matter.
    """
    xs = [p.kwh_per_day for p in periods]
    spread = sum((v - mean(xs)) ** 2 for v in xs)
    best = None
    for i in range(6, len(xs) - 6):
        for j in range(i + 4, len(xs) - 2):
            left = sum(sum((v - mean(s)) ** 2 for v in s) for s in (xs[:i], xs[i:j], xs[j:]))
            if best is None or left < best[0]:
                best = (left, i, j)
    if best is None:
        return None
    left, i, j = best
    before, during, after = mean(xs[:i]), mean(xs[i:j]), mean(xs[j:])
    explained = 1 - left / spread
    if explained < 0.35 or abs(during - before) / before < 0.10:
        return None
    stretch = periods[i:j]
    extra = sum(p.kwh - before * p.days for p in stretch)
    rate = sum(p.cost for p in stretch) / sum(p.kwh for p in stretch)
    return {
        "from": i, "to": j - 1, "before": before, "during": during, "after": after,
        "start": f"{stretch[0].start:%B %Y}", "end": f"{stretch[-1].end:%B %Y}",
        "days": sum(p.days for p in stretch), "explained": explained,
        "per_day": during - before, "watts": (during - before) / 24 * 1000,
        "extra": extra, "cost": extra * rate, "kg": extra * KG_PER_KWH,
    }


def price(recent) -> tuple[float, float]:
    """(fixed charge, price per unit) from the latest bills."""
    t = fit([(q.kwh,) for q in recent], [q.cost for q in recent])
    if t and t["beta"][1] > 0:
        return max(t["beta"][0], 0.0), t["beta"][1]
    return 0.0, recent[-1].cost / recent[-1].kwh


def backtest(periods, fuel, wx, last=12) -> list[dict]:
    """Forecast each recent bill using only what was known before it.

    The forecast is handed the period's real weather, so this is the best a
    mid-month "tracking toward" figure could do - a live one would also carry
    the error in the weather forecast for the days still to come.
    """
    rows = []
    for i in range(len(periods) - last, len(periods)):
        past, p = periods[:i], periods[i]
        if len(past) < 14:
            continue
        model = Model(wx, past, fuel)
        fixed, unit = price(past[-6:])
        usage = max(0.0, model.expect(p)) * p.days
        q = year_before(p, periods)
        rows.append({
            "label": short(p), "span": span(p),
            "actual": p.cost, "forecast": fixed + unit * usage,
            "last_year": q.cost / q.days * p.days if q else None,
            "last_bill": past[-1].cost / past[-1].days * p.days,
            "usage": p.kwh, "forecast_usage": usage,
        })
    return rows


def misses(rows, key) -> dict:
    gaps = [abs(r[key] - r["actual"]) for r in rows if r.get(key) is not None]
    whole = sum(r["actual"] for r in rows if r.get(key) is not None)
    return {"typical": mean(gaps), "median": median(gaps), "pct": sum(gaps) / whole * 100}


def thermostat(periods, model: Model, wx: Weather, unit_price: float) -> list[dict]:
    """What turning the heating down would have saved over the last year.

    Lowering the thermostat one degree moves the balance point down one
    degree, to a first approximation - so the saving is the heating slope
    times the degree-days that disappear.
    """
    recent = periods[-12:]
    days = [recent[0].start + timedelta(n)
            for n in range((recent[-1].end - recent[0].start).days + 1)]
    slope = model.beta[1]
    now = sum(max(0.0, model.heat - wx.temps[d]) for d in days)
    used = sum(p.kwh for p in recent)
    out = []
    for drop in (1, 2, 3):
        then = sum(max(0.0, model.heat - drop - wx.temps[d]) for d in days)
        saved = slope * (now - then)
        out.append({"degrees": drop, "therms": saved, "dollars": saved * unit_price,
                    "kg": saved * KG_PER_THERM, "share": saved / used * 100})
    return out


def carbon(electric, gas) -> dict:
    by_month: dict[str, dict] = {}
    for fuel, periods, factor in (("electric", electric, KG_PER_KWH), ("gas", gas, KG_PER_THERM)):
        for p in periods:
            row = by_month.setdefault(f"{p.end:%Y-%m}", {"label": short(p)})
            row[fuel] = row.get(fuel, 0.0) + p.kwh * factor
            row[f"{fuel}_cost"] = row.get(f"{fuel}_cost", 0.0) + p.cost
    # Only months where both fuels have been metered - a month missing one
    # would read as a saving.
    months = [by_month[k] for k in sorted(by_month)
              if "electric" in by_month[k] and "gas" in by_month[k]]
    year, prior = months[-12:], months[-24:-12]
    add = lambda rows, key: sum(r[key] for r in rows)  # noqa: E731
    return {
        "months": year,
        "electric": add(year, "electric"), "gas": add(year, "gas"),
        "total": add(year, "electric") + add(year, "gas"),
        "prior_total": (add(prior, "electric") + add(prior, "gas")) if len(prior) == 12 else None,
        "electric_cost": add(year, "electric_cost"), "gas_cost": add(year, "gas_cost"),
        "kg_per_kwh": KG_PER_KWH, "kg_per_therm": KG_PER_THERM,
        "first": year[0]["label"], "last": year[-1]["label"],
    }


def fuel_block(fuel, periods, wx) -> dict:
    model = Model(wx, periods, fuel)
    rows = compare(periods, model, wx)
    back = backtest(periods, fuel, wx)
    recent = set(id(p) for p in periods[-12:])
    return {
        "fuel": fuel, "unit": periods[-1].unit, "periods": len(periods),
        "first": f"{periods[0].start:%B %Y}", "last": f"{periods[-1].end:%B %Y}",
        "model": {"r2": model.r2, "heat": model.heat, "cool": model.cool,
                  "beta": model.beta, "rmse": model.rmse,
                  "average": mean(p.kwh_per_day for p in periods)},
        "points": [{"label": span(p), "x": wx.heating(p, model.heat), "y": p.kwh_per_day,
                    "fit": model.expect(p), "temp": wx.average(p), "recent": id(p) in recent}
                   for p in periods],
        "series": [{"label": short(p), "span": span(p), "value": p.kwh_per_day,
                    "temp": wx.average(p), "kwh": p.kwh, "days": p.days} for p in periods],
        "compare": rows[-12:],
        "totals": totals(rows[-12:]),
        "forecast": back,
        "accuracy": {k: misses(back, k) for k in ("forecast", "last_year", "last_bill")},
        "shift": level_shift(periods),
    }


def main() -> int:
    export = greenbutton.load_dir(ROOT / "usage")
    place, temps = load_weather(export)
    wx = Weather(temps)

    fuels = {"Electric": [], "Gas": []}
    for p in export.periods:
        if wx.covers(p):
            fuels["Gas" if p.unit.lower() in ("therms", "ccf") else "Electric"].append(p)
    for periods in fuels.values():
        periods.sort(key=lambda p: p.end)

    gas = fuel_block("Gas", fuels["Gas"], wx)
    electric = fuel_block("Electric", fuels["Electric"], wx)
    gas_model = Model(wx, fuels["Gas"], "Gas")
    _, gas_price = price(fuels["Gas"][-12:])
    _, electric_price = price(fuels["Electric"][-12:])
    footprint = carbon(fuels["Electric"], fuels["Gas"])

    data = {
        "place": place, "built": date.today().isoformat(),
        "gas": gas, "electric": electric, "carbon": footprint,
        "tips": {
            "thermostat": thermostat(fuels["Gas"], gas_model, wx, gas_price),
            "gas_price": gas_price, "electric_price": electric_price,
            "watts100": {"kwh": 0.1 * 24 * 365, "dollars": 0.1 * 24 * 365 * electric_price,
                         "kg": 0.1 * 24 * 365 * KG_PER_KWH},
            "kg_per_dollar": {"gas": KG_PER_THERM / gas_price,
                              "electric": KG_PER_KWH / electric_price},
        },
    }

    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("/*APP_STYLES*/", (ROOT / "static" / "styles.css").read_text(encoding="utf-8"))
    page = page.replace("/*DATA*/", json.dumps(data, default=float).replace("</", "<\\/"))
    OUT.write_text(page, encoding="utf-8")

    g, e = gas["totals"], electric["totals"]
    print(f"weather for {place}; {len(temps)} days")
    for block, t in ((gas, g), (electric, e)):
        m = block["model"]
        print(f"\n{block['fuel']}: weather explains {m['r2'] * 100:.0f}% "
              f"(balance point {m['heat']}F), {block['periods']} periods")
        print(f"  last 12 vs the 12 before: {t['raw_pct']:+.1f}% as billed = "
              f"weather {t['weather_pct']:+.1f}% + you {t['you_pct']:+.1f}%")
        d = t["dollars"]
        print(f"  dollars: {d['total']:+,.0f} = weather {d['weather']:+,.0f} "
              f"+ rate {d['rate']:+,.0f} + you {d['you']:+,.0f}")
        print(f"  lower than last year in {t['lower_raw']}/12 as billed, "
              f"{t['lower_adjusted']}/12 after weather; verdict flips in {t['flips']}")
        a = block["accuracy"]
        print("  forecast, typical miss per bill: "
              + ", ".join(f"{k} ${v['typical']:.0f} ({v['pct']:.0f}%)" for k, v in a.items()))
        if block["shift"]:
            s = block["shift"]
            print(f"  level shift {s['start']} - {s['end']}: {s['before']:.1f} -> {s['during']:.1f} "
                  f"-> {s['after']:.1f} per day; {s['extra']:,.0f} extra, ${s['cost']:,.0f}, "
                  f"{s['watts']:.0f} W; explains {s['explained'] * 100:.0f}%")
    c = footprint
    print(f"\ncarbon {c['first']} - {c['last']}: {c['total'] / 1000:.2f} t "
          f"(electric {c['electric'] / 1000:.2f}, gas {c['gas'] / 1000:.2f}); "
          f"year before {c['prior_total'] / 1000:.2f} t" if c["prior_total"] else "")
    print(f"  gas is {c['gas'] / c['total'] * 100:.0f}% of the carbon and "
          f"{c['gas_cost'] / (c['gas_cost'] + c['electric_cost']) * 100:.0f}% of the spend")
    for t in data["tips"]["thermostat"]:
        print(f"  thermostat {t['degrees']}F lower: {t['therms']:.0f} therms, "
              f"${t['dollars']:.0f}, {t['kg']:.0f} kg ({t['share']:.0f}%)")
    w = data["tips"]["watts100"]
    print(f"  100 W off for good: {w['kwh']:.0f} kWh, ${w['dollars']:.0f}, {w['kg']:.0f} kg a year")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
