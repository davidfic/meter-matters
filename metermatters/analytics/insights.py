"""What the usage says once the weather is taken out.

The bills answer "what did I pay". This answers the questions that lead to
using less:

* Against the same weeks last year, did I use more or less - and how much of
  that was the weather, how much the price, and how much me?
* Did something change? A new appliance, a habit, a fault: usage that steps to
  a new level and stays there.
* What does it come to in carbon, and what would a specific change be worth in
  this house?

Everything is worked out from the metered periods in the usage exports, not
from the bill amounts - only the meter says what was used. Pure functions, no
Flask and no network: the temperatures are handed in (see ``weather.py``), so
with none to hand the same comparisons are made without the weather part.

**How the weather is taken out.** Each home has a temperature below which the
heating comes on (the balance point) and an amount of fuel it burns for every
degree below that. Both are found by fitting the home's own history, not
assumed. What the home "should" have used in any period's weather follows from
that, and the difference between two periods' expectations is the weather's
share of the change between them. The rest is the household's.

**Water** follows the dry rather than the cold. Each day a lawn loses water to
sun and air (reference evapotranspiration) and gets some back as rain; what it
needed and didn't get is the day's watering need, in inches. A home that
waters uses so many gallons for every inch of it, and that slope is fitted the
same way. A home that doesn't water shows no slope, and the page says so.
"""

from __future__ import annotations

import contextlib
import contextvars
from datetime import date, timedelta
from math import log
from statistics import mean

from metermatters import fuels

# What a kilowatt-hour carries depends on the grid it came from, which is a
# matter of where the home is (``region.grid``). The page being built says
# which; anything not told uses the New England figure this started with.
KG_PER_KWH = 543.178 * 0.45359237 / 1000
_GRID = contextvars.ContextVar("grid", default=KG_PER_KWH)
# EPA Greenhouse Gas Equivalencies Calculator: 0.0053 t CO2 per therm, and
# 0.0548 t per thousand cubic feet.
KG_PER_UNIT = {"therms": 5.3, "therm": 5.3, "ccf": 5.48}


@contextlib.contextmanager
def grid(kg_per_kwh: float):
    """Work out carbon for a home on this grid, for the length of the block."""
    token = _GRID.set(kg_per_kwh)
    try:
        yield
    finally:
        _GRID.reset(token)

# Fewer periods than this and a fitted line is mostly noise.
MIN_TO_MODEL = 10
# Below this share of the variation, the weather is not what moves the usage
# and the page says so instead of drawing a relationship that isn't there.
WEATHER_MATTERS = 0.30
MIN_TO_DETECT_SHIFT = 14
COMPARE_LAST = 12


def fuel_of(period) -> str:
    return fuels.of_unit(getattr(period, "unit", "kWh")).name


def kg_per_unit(unit: str) -> float:
    """Kilograms of CO2e per unit. Water carries no published factor here: zero."""
    key = (unit or "kwh").lower()
    if key in KG_PER_UNIT:
        return KG_PER_UNIT[key]
    return _GRID.get() if fuels.of_unit(key) is fuels.ELECTRIC else 0.0


def timeline(periods: list) -> list:
    """One continuous run of periods from whatever accounts there are.

    A utility renumbers an account when service is re-opened, so one home
    often appears as two accounts end to end. Those are joined. An account
    that overlaps the newest one in time is a different meter, and is left
    out rather than added on top.
    """
    by_account: dict[str, list] = {}
    for p in periods:
        by_account.setdefault(p.account, []).append(p)
    runs = sorted(by_account.values(), key=lambda run: max(p.end for p in run), reverse=True)
    kept: list = []
    for run in runs:
        earliest = min((p.start for p in kept), default=None)
        kept += [p for p in run if earliest is None or p.end < earliest]
    return sorted(kept, key=lambda p: p.end)


# ----------------------------------------------------------------- weather

class Weather:
    def __init__(self, temps: dict, rain: dict | None = None, need: dict | None = None):
        self.temps = temps or {}
        # Inches per day: what fell, and what a garden lost and didn't get back.
        self.rain = rain or {}
        self.need = need or {}
        # Finding a balance point asks for the same sums hundreds of times over.
        self._kept: dict = {}

    @staticmethod
    def days(p):
        day = p.start
        while day <= p.end:
            yield day
            day += timedelta(days=1)

    def covers(self, p) -> bool:
        return bool(self.temps) and all(d in self.temps for d in self.days(p))

    def _degrees(self, p, base: float, sign: int) -> float:
        key = (p.start, p.end, base, sign)
        if key not in self._kept:
            self._kept[key] = sum(max(0.0, sign * (base - self.temps[d]))
                                  for d in self.days(p)) / p.days
        return self._kept[key]

    def heating(self, p, base: float) -> float:
        """Degrees below `base`, averaged over the days of the period."""
        return self._degrees(p, base, 1)

    def cooling(self, p, base: float) -> float:
        return self._degrees(p, base, -1)

    def average(self, p) -> float:
        return mean(self.temps[d] for d in self.days(p))

    def covers_dryness(self, p) -> bool:
        return bool(self.need) and all(d in self.need for d in self.days(p))

    def dryness(self, p) -> float:
        """Inches of watering need a day, averaged over the period."""
        key = (p.start, p.end, "need")
        if key not in self._kept:
            self._kept[key] = sum(self.need[d] for d in self.days(p)) / p.days
        return self._kept[key]

    def rainfall(self, p) -> float:
        """Inches of rain over the whole period."""
        return sum(self.rain[d] for d in self.days(p))


def _solve(a, b):
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


def fit(xs: list[tuple], ys: list[float]) -> dict | None:
    """Least squares with an intercept. `xs` is one tuple of features per row."""
    rows = [(1.0, *x) for x in xs]
    k = len(rows[0])
    if len(rows) <= k:
        return None
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    aty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(k)]
    beta = _solve(ata, aty)
    if beta is None:
        return None
    predicted = [sum(b * v for b, v in zip(beta, r)) for r in rows]
    centre = mean(ys)
    total = sum((y - centre) ** 2 for y in ys)
    left = sum((y - p) ** 2 for y, p in zip(ys, predicted))
    return {"beta": beta, "r2": max(0.0, 1 - left / total) if total else 0.0, "n": len(ys)}


class Model:
    """How one fuel in one home responds to the weather.

    For electric and gas the feature is degrees of cold (and, for electric,
    of heat) a day; `heat` and `cool` are the balance points. For water the
    one feature is inches of watering need a day, `heat` and `cool` are None,
    and `beta[1]` is gallons per inch.
    """

    def __init__(self, wx: Weather, heat: int | None, cool: int | None, beta: list, r2: float,
                 kind: str = "degrees"):
        self.wx, self.heat, self.cool, self.beta, self.r2 = wx, heat, cool, beta, r2
        self.kind = kind

    @classmethod
    def learn(cls, wx: Weather, periods: list, fuel: str) -> "Model | None":
        if fuel == "Water":
            return cls.learn_dryness(wx, periods)
        covered = [p for p in periods if wx.covers(p)]
        if len(covered) < MIN_TO_MODEL:
            return None
        ys = [p.kwh_per_day for p in covered]
        best = None
        # Gas heats. Electricity may heat, cool, both or neither, so it gets a
        # second balance point for the cooling.
        cools = [None] if fuel != "Electric" else range(60, 76)
        for heat in range(45, 71):
            for cool in cools:
                if cool is not None and cool < heat:
                    continue
                xs = [(wx.heating(p, heat),) if cool is None
                      else (wx.heating(p, heat), wx.cooling(p, cool)) for p in covered]
                found = fit(xs, ys)
                # More fuel in milder weather is not a heating system; it is a
                # coincidence the fit has latched on to.
                if found is None or any(b < 0 for b in found["beta"][1:]):
                    continue
                if best is None or found["r2"] > best[0]["r2"]:
                    best = (found, heat, cool)
        if best is None:
            # Nothing sensible fits: the weather explains none of it, which is
            # itself worth saying.
            return cls(wx, 65, None, [mean(ys), 0.0], 0.0)
        found, heat, cool = best
        return cls(wx, heat, cool, found["beta"], found["r2"])

    @classmethod
    def learn_dryness(cls, wx: Weather, periods: list) -> "Model | None":
        covered = [p for p in periods if wx.covers_dryness(p)]
        if len(covered) < MIN_TO_MODEL:
            return None
        ys = [p.kwh_per_day for p in covered]
        found = fit([(wx.dryness(p),) for p in covered], ys)
        # Less water when it is drier is not a garden; it is noise.
        if found is None or found["beta"][1] < 0:
            return cls(wx, None, None, [mean(ys), 0.0], 0.0, kind="dryness")
        return cls(wx, None, None, found["beta"], found["r2"], kind="dryness")

    @property
    def matters(self) -> bool:
        return self.r2 >= WEATHER_MATTERS

    def covers(self, p) -> bool:
        return self.wx.covers_dryness(p) if self.kind == "dryness" else self.wx.covers(p)

    def features(self, p) -> tuple:
        if self.kind == "dryness":
            return (self.wx.dryness(p),)
        if self.cool is None:
            return (self.wx.heating(p, self.heat),)
        return (self.wx.heating(p, self.heat), self.wx.cooling(p, self.cool))

    def expect(self, p) -> float:
        """Usage per day this home normally shows in this period's weather."""
        return sum(b * v for b, v in zip(self.beta, (1.0, *self.features(p))))


# ------------------------------------------------------------- comparisons

def short(p) -> str:
    return f"{p.end:%b} '{p.end:%y}"


def span(p) -> str:
    return f"{p.start:%b} {p.start.day} – {p.end:%b} {p.end.day}, {p.end.year}"


def month_year(day: date) -> str:
    return f"{day:%B %Y}"


def _middle(p) -> date:
    return p.start + (p.end - p.start) / 2


def year_before(p, periods: list):
    """The period covering the same weeks of the calendar, one year earlier."""
    target = _middle(p) - timedelta(days=365)
    earlier = [q for q in periods if q.end < p.start]
    if not earlier:
        return None
    q = min(earlier, key=lambda q: abs((_middle(q) - target).days))
    return q if abs((_middle(q) - target).days) <= 16 else None


def compare(periods: list, model: Model | None, wx: Weather) -> list[dict]:
    """Each period against a year earlier, split into weather, price and you."""
    typical = mean(p.kwh_per_day for p in periods) if periods else 0.0
    rows = []
    for p in periods:
        q = year_before(p, periods)
        if q is None:
            continue
        raw = p.kwh_per_day - q.kwh_per_day
        known = model is not None and model.covers(p) and model.covers(q)
        weather = model.expect(p) - model.expect(q) if known else 0.0
        you = raw - weather
        rate_now = p.cost / p.kwh if p.kwh else 0.0
        rate_then = q.cost / q.kwh if q.kwh else rate_now
        rows.append({
            "label": short(p), "span": span(p), "end": p.end.isoformat(), "days": p.days,
            "now": p.kwh_per_day, "then": q.kwh_per_day,
            "raw": raw, "weather": weather, "you": you, "weather_known": known,
            "temp_now": wx.average(p) if wx.covers(p) else None,
            "temp_then": wx.average(q) if wx.covers(q) else None,
            "rain_now": wx.rainfall(p) if wx.covers_dryness(p) else None,
            "rain_then": wx.rainfall(q) if wx.covers_dryness(q) else None,
            # A percentage of next to nothing is noise: gas going from 0.01 to
            # 0.03 therms a day in July is "+200%" and means nothing.
            "pct_ok": q.kwh_per_day >= typical * 0.10,
            "raw_pct": raw / q.kwh_per_day * 100 if q.kwh_per_day else None,
            "you_pct": you / q.kwh_per_day * 100 if q.kwh_per_day else None,
            # Dollars over the period; the three parts sum to the change exactly.
            "d_total": (p.cost / p.days - q.cost / q.days) * p.days,
            "d_weather": rate_then * weather * p.days,
            "d_you": rate_then * you * p.days,
            "d_rate": p.kwh_per_day * (rate_now - rate_then) * p.days,
        })
    return rows


def totals(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    then = sum(r["then"] * r["days"] for r in rows)
    if not then:
        return None
    part = lambda key: sum(r[key] * r["days"] for r in rows)  # noqa: E731
    return {
        "periods": len(rows),
        "then": then, "now": sum(r["now"] * r["days"] for r in rows),
        "raw": part("raw"), "weather": part("weather"), "you": part("you"),
        "raw_pct": part("raw") / then * 100,
        "weather_pct": part("weather") / then * 100,
        "you_pct": part("you") / then * 100,
        "lower": sum(1 for r in rows if r["you"] < 0),
        "flips": sum(1 for r in rows if (r["raw"] < 0) != (r["you"] < 0)),
        "dollars": {k: sum(r[f"d_{k}"] for r in rows) for k in ("total", "weather", "you", "rate")},
    }


# ------------------------------------------------------------ level shifts

def standing_charge(periods: list) -> float:
    """Dollars a day paid whatever is used, as far as the bills reveal it.

    Read off the periods when next to nothing was used - a summer gas bill is
    nearly all standing charge. Where usage never drops that low (electricity)
    there is nothing to read it from, and it is taken as zero: the price per
    unit then comes out a few percent high rather than being guessed at.
    """
    typical = mean(p.kwh_per_day for p in periods)
    quiet = [p for p in periods[-24:] if p.kwh_per_day < typical * 0.10]
    return min((p.cost / p.days for p in quiet), default=0.0)


def unit_price(periods: list, over: list | None = None) -> float:
    """What one more unit cost, across `over` (by default the last twelve periods).

    Not cost divided by usage, which folds the standing charge in - a month of
    almost no gas would read as "$10 a therm". And not the slope of cost
    against usage: prices change through the year, and the slope mistakes
    "cheaper in the months I use more" for a lower price.
    """
    over = [p for p in (over if over is not None else periods[-12:])
            if p.kwh and getattr(p, "priced", True)]
    used = sum(p.kwh for p in over)
    if not used:
        return 0.0
    fixed = standing_charge(periods)
    return max(0.0, sum(p.cost - fixed * p.days for p in over)) / used


def _spread(values: list[float]) -> float:
    centre = mean(values)
    return sum((v - centre) ** 2 for v in values)


def level_shift(periods: list, model: Model | None, wx: Weather) -> dict | None:
    """A stretch where usage stepped to a new level - and, perhaps, stepped back.

    Looked for in what the weather does *not* explain, where there is a model:
    otherwise every winter would be "a step up" in a house with heating. Two
    shapes are tried - a step that is still going, and one that has ended -
    and nothing is reported unless the step explains a good part of the
    variation and is big enough to matter.
    """
    if model is not None and model.matters:
        periods = [p for p in periods if model.covers(p)]
        values = [p.kwh_per_day - model.expect(p) for p in periods]
        basis = "against the weather"
    else:
        values = [p.kwh_per_day for p in periods]
        basis = "usage"
    n = len(values)
    if n < MIN_TO_DETECT_SHIFT:
        return None
    whole = _spread(values)
    if not whole:
        return None

    # Three stories, each costing more to tell than the last: it stepped and
    # stayed (one break, two levels); it stepped and came back (two breaks,
    # two levels); it stepped and then settled somewhere new (two breaks,
    # three levels). A story with more in it always fits a little better, so
    # each is charged for what it adds and the cheapest overall wins.
    def price_of(left, told):
        return n * log(max(left, 1e-12) / n) + told * log(n)

    stories = []
    for i in range(6, n - 2):
        stories.append((price_of(_spread(values[:i]) + _spread(values[i:]), 3),
                        _spread(values[:i]) + _spread(values[i:]), i, n))
        for j in range(i + 3, n - 1):
            during = _spread(values[i:j])
            back = _spread(values[:i] + values[j:]) + during
            stories.append((price_of(back, 4), back, i, j))
            moved = _spread(values[:i]) + during + _spread(values[j:])
            stories.append((price_of(moved, 5), moved, i, j))
    if not stories:
        return None
    _, left, i, j = min(stories)
    before, during = mean(values[:i]), mean(values[i:j])
    after = mean(values[j:]) if j < n else None
    typical = mean(p.kwh_per_day for p in periods)
    if 1 - left / whole < 0.35 or abs(during - before) < typical * 0.10:
        return None

    stretch = periods[i:j]
    extra = sum((v - before) * p.days for v, p in zip(values[i:j], stretch))
    price = unit_price(periods, stretch)
    return {
        "key": f"{fuel_of(periods[0])}:{stretch[0].start.isoformat()}",
        "basis": basis, "from": i, "to": j - 1, "ongoing": j >= n,
        "start": month_year(stretch[0].start), "end": month_year(stretch[-1].end),
        "days": sum(p.days for p in stretch),
        "before": before, "during": during, "after": after,
        "per_day": during - before,
        "explained": 1 - left / whole,
        "extra": extra, "cost": extra * price,
        "kg": extra * kg_per_unit(periods[0].unit),
        # What would have to run around the clock to account for it.
        "watts": (during - before) / 24 * 1000 if fuel_of(periods[0]) == "Electric" else None,
        "series": [{"label": short(p), "span": span(p), "value": v}
                   for p, v in zip(periods, values)],
    }


# ------------------------------------------------------------------ carbon

def carbon_months(periods: list) -> list[dict]:
    months: dict[str, dict] = {}
    for p in periods:
        row = months.setdefault(f"{p.end:%Y-%m}", {"period": f"{p.end:%Y-%m}", "label": short(p),
                                                   "kg": 0.0, "cost": 0.0, "usage": 0.0})
        row["kg"] += p.kwh * kg_per_unit(p.unit)
        row["cost"] += p.cost
        row["usage"] += p.kwh
    return [months[k] for k in sorted(months)]


def carbon(usage: dict[str, list]) -> dict | None:
    """Carbon dioxide from every fuel there is usage for, month by month."""
    # Only fuels with a carbon factor: water has none here.
    per_fuel = {fuel: {m["period"]: m for m in carbon_months(periods)}
                for fuel, periods in usage.items()
                if periods and kg_per_unit(periods[0].unit) > 0}
    if not per_fuel:
        return None
    # Only months every fuel has been metered for: a month missing one would
    # read as a saving.
    shared = sorted(set.intersection(*(set(m) for m in per_fuel.values())))
    if not shared:
        return None

    def rows(keys):
        out = []
        for key in keys:
            row = {"period": key, "label": next(iter(per_fuel.values()))[key]["label"]}
            for fuel, months in per_fuel.items():
                row[fuel.lower()] = months[key]["kg"]
                row[f"{fuel.lower()}_cost"] = months[key]["cost"]
            row["total"] = sum(months[key]["kg"] for months in per_fuel.values())
            out.append(row)
        return out

    year, prior = rows(shared[-12:]), rows(shared[-24:-12])
    by_fuel = {fuel: {"kg": sum(r[fuel.lower()] for r in year),
                      "cost": sum(r[f"{fuel.lower()}_cost"] for r in year)}
               for fuel in per_fuel}
    return {
        "months": year, "fuels": by_fuel,
        "total": sum(r["total"] for r in year),
        "prior_total": sum(r["total"] for r in prior) if len(prior) == 12 else None,
        "first": year[0]["label"], "last": year[-1]["label"], "count": len(year),
        "factors": {"kwh": _GRID.get(), "therm": KG_PER_UNIT["therms"]},
        "fuels_without_factor": [f for f, ps in usage.items() if ps and kg_per_unit(ps[0].unit) <= 0],
    }


# -------------------------------------------------------------------- tips

def _money(value: float) -> str:
    return f"${value:,.0f}"


def _kg(value: float) -> str:
    return f"{value:,.0f} kg"


def thermostat_tip(periods: list, model: Model, wx: Weather) -> dict | None:
    """What one degree lower on the heating would have saved over the last year.

    Turning the thermostat down a degree moves the balance point down a
    degree, near enough - so the saving is the fuel burnt per degree, times
    the degrees that disappear.
    """
    if model.kind != "degrees" or not model.matters or model.beta[1] <= 0:
        return None
    recent = [p for p in periods[-12:] if wx.covers(p)]
    if len(recent) < 6:
        return None
    days = [d for p in recent for d in wx.days(p)]
    gone = sum(max(0.0, model.heat - wx.temps[d]) - max(0.0, model.heat - 1 - wx.temps[d])
               for d in days)
    saved = model.beta[1] * gone
    used = sum(p.kwh for p in recent)
    unit = recent[-1].unit
    if saved <= 0 or not used:
        return None
    return {
        "id": "thermostat", "fuel": fuel_of(recent[0]),
        "title": "Turn the heating down one degree",
        "body": f"This home uses {model.beta[1]:.3g} {unit} for every degree the day averages "
                f"below {model.heat}°F. One degree lower on the thermostat, over the last "
                f"year's weather, comes to:",
        "worth": [{"value": _money(saved * unit_price(periods)), "label": "a year"},
                  {"value": f"{saved:,.0f} {unit}", "label": f"{saved / used * 100:.0f}% of what you used"},
                  {"value": _kg(saved * kg_per_unit(unit)), "label": "of carbon"}],
    }


def watering_tip(periods: list, model: Model, wx: Weather) -> dict | None:
    """What one watering in four, skipped, would have saved over the last year.

    The slope says how many gallons this home puts on the garden for every
    inch the weather called for. Over a year's weather that is the outdoor
    water; a quarter of it is one watering in four.
    """
    if model.kind != "dryness" or not model.matters or model.beta[1] <= 0:
        return None
    recent = [p for p in periods[-12:] if wx.covers_dryness(p)]
    if len(recent) < 4:
        return None
    inches = sum(wx.dryness(p) * p.days for p in recent)
    outdoor = model.beta[1] * inches
    used = sum(p.kwh for p in recent)
    unit = recent[-1].unit
    if outdoor <= 0 or not used:
        return None
    saved = outdoor / 4
    return {
        "id": "watering", "fuel": "Water",
        "title": "Skip one watering in four",
        "body": f"This home uses about {model.beta[1]:,.0f} {unit} for every inch of watering the "
                f"weather calls for. Over the last year that came to {inches:.1f} inches, about "
                f"{outdoor:,.0f} {unit} on the garden. Skipping one watering in four, or watering "
                f"only when the soil is dry, comes to:",
        "worth": [{"value": _money(saved * unit_price(periods)), "label": "a year"},
                  {"value": f"{saved:,.0f} {unit}", "label": f"{saved / used * 100:.0f}% of what you used"}],
    }


def always_on_tip(periods: list, model: Model | None) -> dict | None:
    """What 100 watts, switched off for good, is worth."""
    if not periods or fuel_of(periods[0]) != "Electric":
        return None
    kwh = 0.1 * 24 * 365
    steady = model is not None and not model.matters
    return {
        "id": "always-on", "fuel": "Electric",
        "title": "Switch off 100 watts for good",
        "body": ("Electricity here barely moves with the seasons, so most of it is things "
                 "that are always on. " if steady else
                 "Anything that runs day and night costs the same every hour of the year. ")
                + "Every 100 watts you find and switch off comes to:",
        "worth": [{"value": _money(kwh * unit_price(periods)), "label": "a year"},
                  {"value": f"{kwh:,.0f} kWh", "label": "a year"},
                  {"value": _kg(kwh * _GRID.get()), "label": "of carbon"}],
    }


def shift_tip(shift: dict | None, unit: str, note: str | None) -> dict | None:
    if not shift or shift["per_day"] <= 0:
        return None
    size = (f"about {shift['watts']:,.0f} watts running day and night"
            if shift.get("watts") else f"about {shift['per_day']:.2g} {unit} a day")
    state = ("It is still running." if shift["ongoing"]
             else f"It stopped in {shift['end']}.")
    return {
        "id": "shift", "fuel": shift["key"].split(":")[0],
        "title": f"{'Find' if not note else 'Remember'} what came on in {shift['start']}",
        "body": (f"You noted: “{note}”. " if note else "")
                + f"Usage stepped up by {size}. {state} "
                + ("So far it has cost:" if shift["ongoing"] else "While it ran it cost:"),
        "worth": [{"value": _money(shift["cost"]), "label": f"in {shift['days']:,} days"},
                  {"value": f"{shift['extra']:,.0f} {unit}", "label": "extra"},
                  {"value": _kg(shift["kg"]), "label": "of carbon"}],
    }


# -------------------------------------------------------------------- build

def fuel_block(fuel: str, periods: list, wx: Weather, notes: dict) -> dict:
    periods = timeline(periods)
    unit = periods[-1].unit if periods else fuels.by_name(fuel).unit
    block = {"fuel": fuel, "unit": unit, "periods": len(periods), "status": "ok",
             "weather_relevant": fuels.by_name(fuel).weather,
             # What the weather means for this fuel: the cold, or the dry.
             "weather_kind": "dryness" if fuel == "Water" else "degrees"}
    if not periods:
        return {**block, "status": "no-usage",
                "need": "Upload a usage export to see how you compare with last year."}

    model = Model.learn(wx, periods, fuel) if fuels.by_name(fuel).weather else None
    rows = compare(periods, model, wx)
    shift = level_shift(periods, model, wx)
    if shift:
        shift["note"] = (notes or {}).get(shift["key"], "")
    block.update({
        # The average temperature of every period the weather is known for,
        # keyed by the day it ended: the Compare card shows it beside the bill.
        "temps": {p.end.isoformat(): round(wx.average(p), 1) for p in periods if wx.covers(p)},
        "first": month_year(periods[0].start), "last": month_year(periods[-1].end),
        "average": mean(p.kwh_per_day for p in periods),
        "weather": None if model is None else {
            "r2": model.r2, "matters": model.matters, "kind": model.kind,
            "heat": model.heat, "cool": model.cool,
            "base": model.beta[0], "per_heating_degree": model.beta[1],
            "per_cooling_degree": model.beta[2] if model.cool is not None else None,
            "per_inch": model.beta[1] if model.kind == "dryness" else None,
        },
        "points": [{"label": span(p), "x": model.features(p)[0], "y": p.kwh_per_day,
                    "fit": model.expect(p),
                    "temp": wx.average(p) if wx.covers(p) else None,
                    "rain": wx.rainfall(p) if wx.covers_dryness(p) else None,
                    "recent": p in periods[-COMPARE_LAST:]}
                   for p in periods if model.covers(p)] if model is not None and model.matters else [],
        "compare": rows[-COMPARE_LAST:],
        "totals": totals(rows[-COMPARE_LAST:]),
        "latest": rows[-1] if rows else None,
        "shift": shift,
        "carbon": {"kg": sum(m["kg"] for m in carbon_months(periods)[-12:]),
                   "months": len(carbon_months(periods)[-12:])},
    })
    if not rows:
        block["status"] = "short-history"
        block["need"] = ("Comparisons start once there is more than a year of usage on file. "
                         f"So far it runs from {block['first']} to {block['last']}.")

    tips = [shift_tip(shift, unit, shift["note"] if shift else None)]
    if model is not None:
        tips.append(thermostat_tip(periods, model, wx))
        tips.append(watering_tip(periods, model, wx))
    tips.append(always_on_tip(periods, model))
    block["tips"] = [t for t in tips if t]
    return block


def build(periods: list, temps: dict | None, *, rain: dict | None = None,
          need: dict | None = None, notes: dict | None = None,
          place: str | None = None, weather_status: str = "ok") -> dict:
    """Everything the pages need, JSON-ready.

    `weather_status` says why there are no temperatures when there are none:
    "off", "no-location" or "unavailable".
    """
    wx = Weather(temps or {}, rain, need)
    by_fuel: dict[str, list] = {}
    for p in periods:
        by_fuel.setdefault(fuel_of(p), []).append(p)

    fuels = {fuel: fuel_block(fuel, by_fuel[fuel], wx, notes or {}) for fuel in sorted(by_fuel)}
    alerts = []
    for fuel, block in fuels.items():
        shift = block.get("shift")
        # Once it has been given a name it is a known thing, not an alert.
        if shift and not shift["note"]:
            size = (f"about {shift['watts']:,.0f} watts, day and night" if shift.get("watts")
                    else f"about {abs(shift['per_day']):.2g} {block['unit']} a day")
            alerts.append({
                "fuel": fuel, "kind": "shift", "href": f"/{fuel.lower()}#changed",
                "title": f"{shift['start']}: usage stepped "
                         f"{'up' if shift['per_day'] > 0 else 'down'} and "
                         f"{'has stayed there' if shift['ongoing'] else 'later came back'}",
                "detail": f"{size.capitalize()}: {abs(shift['extra']):,.0f} {block['unit']} "
                          f"{'more' if shift['extra'] > 0 else 'less'}, about "
                          f"{_money(abs(shift['cost']))}, over {shift['days']:,} days. "
                          f"The weather doesn't explain it.",
            })
    return {
        "place": place, "weather_status": weather_status if not temps else "ok",
        "fuels": fuels,
        "carbon": carbon({fuel: timeline(ps) for fuel, ps in by_fuel.items()}),
        "alerts": alerts,
    }

