"""Tests for the weather-adjusted comparisons, step detection, carbon and tips.

Every home here is invented: a heating slope and a balance point are chosen,
bills are generated from them, and the analysis has to find its way back.
"""

import json
import math
import unittest
from dataclasses import dataclass
from datetime import date, timedelta

from metermatters.analytics import insights

START = date(2023, 1, 1)


def climate(day: date) -> float:
    """A smooth year: about 27F in late January, about 73F in late July."""
    return 50 - 23 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 25) / 365.25)


def weather(days=1300, shift=None):
    """Daily temperatures. `shift(day)` adds degrees to chosen days."""
    out = {}
    for n in range(days):
        day = START + timedelta(n)
        out[day] = climate(day) + (shift(day) if shift else 0.0)
    return out


@dataclass
class Period:
    account: str
    start: date
    end: date
    kwh: float
    cost: float
    unit: str = "kWh"

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def kwh_per_day(self):
        return self.kwh / self.days


def bills(per_day, count=36, unit="kWh", price=0.30, fixed=10.0, account="111", length=30):
    """`per_day(period_start, period_end)` -> usage per day for that period."""
    out, start = [], START
    for _ in range(count):
        end = start + timedelta(days=length - 1)
        used = max(0.0, per_day(start, end)) * length
        out.append(Period(account, start, end, used, fixed + price * used, unit))
        start = end + timedelta(days=1)
    return out


def heating(temps, slope, base, floor=0.0):
    def per_day(start, end):
        days = [start + timedelta(n) for n in range((end - start).days + 1)]
        return floor + slope * sum(max(0.0, base - temps[d]) for d in days) / len(days)
    return per_day


class TestWeatherModel(unittest.TestCase):
    def test_it_finds_the_homes_own_balance_point_and_slope(self):
        temps = weather()
        gas = bills(heating(temps, slope=0.17, base=59), unit="therms", price=2.5)
        block = insights.build(gas, temps)["fuels"]["Gas"]
        w = block["weather"]
        self.assertEqual(w["heat"], 59)
        self.assertAlmostEqual(w["per_heating_degree"], 0.17, places=3)
        self.assertGreater(w["r2"], 0.99)
        self.assertTrue(w["matters"])

    def test_a_home_the_weather_does_not_move_is_said_to_be_one(self):
        temps = weather()
        flat = bills(lambda s, e: 26 + (s.toordinal() % 7) * 0.3)
        w = insights.build(flat, temps)["fuels"]["Electric"]["weather"]
        self.assertFalse(w["matters"])
        self.assertLess(w["r2"], insights.WEATHER_MATTERS)

    def test_air_conditioning_is_found_on_the_cooling_side(self):
        temps = weather()

        def cooled(start, end):
            days = [start + timedelta(n) for n in range(30)]
            return 20 + 1.2 * sum(max(0.0, temps[d] - 68) for d in days) / 30
        w = insights.build(bills(cooled), temps)["fuels"]["Electric"]["weather"]
        self.assertTrue(w["matters"])
        self.assertAlmostEqual(w["per_cooling_degree"], 1.2, places=1)

    def test_using_more_when_it_is_mild_is_never_called_heating(self):
        temps = weather()
        backwards = bills(lambda s, e: 10 + 0.2 * temps[s])        # more when warm
        w = insights.build(backwards, temps, )["fuels"]["Electric"]["weather"]
        self.assertGreaterEqual(w["per_heating_degree"], 0)

    def test_too_little_history_makes_no_claim_about_the_weather(self):
        temps = weather()
        few = bills(heating(temps, 0.17, 59), count=insights.MIN_TO_MODEL - 1, unit="therms")
        self.assertIsNone(insights.build(few, temps)["fuels"]["Gas"]["weather"])


def dryness(days=1300):
    """Daily watering need, inches: nothing in winter, up to a fifth of an inch a day in July,
    with a dry spell every third summer."""
    need, rain = {}, {}
    for n in range(days):
        day = START + timedelta(n)
        summer = max(0.0, -math.cos(2 * math.pi * (day.timetuple().tm_yday - 15) / 365.25))
        drought = 1.6 if day.year % 3 == 0 else 1.0
        need[day] = 0.2 * summer * drought
        rain[day] = 0.1 * (1 - summer) + 0.03 / drought
    return need, rain


def garden(need, per_inch, indoor):
    def per_day(start, end):
        days = [start + timedelta(n) for n in range((end - start).days + 1)]
        return indoor + per_inch * sum(need[d] for d in days) / len(days)
    return per_day


class TestWaterModel(unittest.TestCase):
    def test_it_finds_how_much_the_garden_gets_per_inch(self):
        need, rain = dryness()
        wx = insights.Weather({}, rain, need)
        periods = bills(garden(need, per_inch=900.0, indoor=150.0), unit="gal", price=0.01)
        model = insights.Model.learn(wx, periods, "Water")
        self.assertEqual(model.kind, "dryness")
        self.assertTrue(model.matters)
        self.assertAlmostEqual(model.beta[1], 900.0, delta=5)
        self.assertAlmostEqual(model.beta[0], 150.0, delta=2)
        self.assertIsNone(model.heat)

    def test_a_home_that_does_not_water_is_said_to_be_one(self):
        need, rain = dryness()
        wx = insights.Weather({}, rain, need)
        periods = bills(lambda s, e: 150.0 + (e.day % 5), unit="gal", price=0.01)
        model = insights.Model.learn(wx, periods, "Water")
        self.assertFalse(model.matters)

    def test_a_dry_summer_is_not_blamed_on_the_household(self):
        need, rain = dryness()
        wx = insights.Weather({}, rain, need)
        periods = bills(garden(need, per_inch=900.0, indoor=150.0), unit="gal", price=0.01)
        model = insights.Model.learn(wx, periods, "Water")
        rows = insights.compare(periods, model, wx)
        # The same garden, watered to the same rule: once the dryness is taken
        # out, nothing is left for the household to answer for.
        for row in rows:
            self.assertLess(abs(row["you"]), 3.0, row["label"])
        self.assertIsNotNone(rows[-1]["rain_now"])

    def test_the_watering_tip_prices_one_in_four(self):
        need, rain = dryness()
        wx = insights.Weather({}, rain, need)
        periods = bills(garden(need, per_inch=900.0, indoor=150.0), unit="gal", price=0.01)
        model = insights.Model.learn(wx, periods, "Water")
        tip = insights.watering_tip(periods, model, wx)
        self.assertEqual(tip["id"], "watering")
        self.assertIn("900 gal for every inch", tip["body"])
        self.assertIsNone(insights.thermostat_tip(periods, model, wx))

    def test_one_bill_still_says_what_the_weather_did(self):
        need, rain = dryness()
        periods = bills(garden(need, per_inch=900.0, indoor=150.0), count=1, unit="gal", price=0.01)
        block = insights.build(periods, {}, rain=rain, need=need)["fuels"]["Water"]
        own = block["latest_weather"]
        self.assertAlmostEqual(own["rain"], sum(rain[d] for d in insights.Weather.days(periods[0])))
        self.assertAlmostEqual(own["need"], sum(need[d] for d in insights.Weather.days(periods[0])))
        self.assertIsNone(own["temp"])
        self.assertIsNone(block["latest"])

    def test_without_rain_on_file_water_says_nothing_about_the_weather(self):
        periods = bills(lambda s, e: 150.0, unit="gal", price=0.01)
        self.assertIsNone(insights.Model.learn(insights.Weather(weather()), periods, "Water"))


class TestComparedWithLastYear(unittest.TestCase):
    def setUp(self):
        # The third winter is eight degrees colder than the ones before it.
        self.temps = weather(shift=lambda d: -8.0 if d >= date(2025, 1, 1) and climate(d) < 45 else 0.0)

    def test_a_cold_year_is_not_blamed_on_the_household(self):
        gas = bills(heating(self.temps, 0.17, 59), unit="therms", price=2.5)
        t = insights.build(gas, self.temps)["fuels"]["Gas"]["totals"]
        self.assertGreater(t["raw_pct"], 5)             # the bills really did go up
        self.assertAlmostEqual(t["you_pct"], 0, delta=1)    # and none of it was them
        self.assertAlmostEqual(t["weather_pct"], t["raw_pct"], delta=1)

    def test_a_real_saving_shows_through_a_cold_year(self):
        honest = heating(self.temps, 0.17, 59)

        def thrifty(start, end):        # 15% less from 2025 on, whatever the weather
            return honest(start, end) * (0.85 if start >= date(2025, 1, 1) else 1.0)
        t = insights.build(bills(thrifty, unit="therms", price=2.5),
                           self.temps)["fuels"]["Gas"]["totals"]
        self.assertLess(t["you_pct"], -8)
        self.assertGreater(t["weather_pct"], 5)
        self.assertGreater(t["flips"], 0)       # bills that went up, though they used less

    def test_the_parts_add_up_to_the_change_exactly(self):
        gas = bills(heating(self.temps, 0.17, 59, floor=0.1), unit="therms", price=2.5)
        block = insights.build(gas, self.temps)["fuels"]["Gas"]
        for row in block["compare"]:
            self.assertAlmostEqual(row["weather"] + row["you"], row["raw"], places=9)
            self.assertAlmostEqual(row["d_weather"] + row["d_you"] + row["d_rate"],
                                   row["d_total"], places=6)
        d = block["totals"]["dollars"]
        self.assertAlmostEqual(d["weather"] + d["you"] + d["rate"], d["total"], places=6)

    def test_a_price_rise_is_charged_to_the_rate(self):
        temps = weather()
        steady = bills(lambda s, e: 25.0)
        for p in steady:
            if p.start >= date(2025, 1, 1):
                p.cost = 10 + 0.40 * p.kwh          # was 0.30
        d = insights.build(steady, temps)["fuels"]["Electric"]["totals"]["dollars"]
        self.assertGreater(d["rate"], 0)
        self.assertAlmostEqual(d["you"], 0, delta=1)
        self.assertAlmostEqual(d["rate"], d["total"], delta=1)

    def test_each_bill_is_matched_to_the_same_weeks_a_year_earlier(self):
        temps = weather()
        block = insights.build(bills(lambda s, e: 25.0), temps)["fuels"]["Electric"]
        self.assertEqual(len(block["compare"]), insights.COMPARE_LAST)
        first = block["compare"][0]
        self.assertAlmostEqual(first["temp_now"], first["temp_then"], delta=3)

    def test_percentages_of_next_to_nothing_are_flagged(self):
        temps = weather()
        gas = bills(heating(temps, 0.17, 59, floor=0.01), unit="therms", price=2.5)
        rows = insights.build(gas, temps)["fuels"]["Gas"]["compare"]
        summer = [r for r in rows if r["then"] < 0.1]
        self.assertTrue(summer)
        self.assertFalse(any(r["pct_ok"] for r in summer))
        self.assertTrue(any(r["pct_ok"] for r in rows))

    def test_without_weather_the_comparison_is_still_made(self):
        gas = bills(heating(self.temps, 0.17, 59), unit="therms", price=2.5)
        out = insights.build(gas, None, weather_status="unavailable")
        block = out["fuels"]["Gas"]
        self.assertEqual(out["weather_status"], "unavailable")
        self.assertIsNone(block["weather"])
        self.assertEqual(block["totals"]["weather"], 0)
        self.assertAlmostEqual(block["totals"]["you_pct"], block["totals"]["raw_pct"])

    def test_less_than_a_year_says_what_is_missing(self):
        block = insights.build(bills(lambda s, e: 25.0, count=11), weather())["fuels"]["Electric"]
        self.assertEqual(block["status"], "short-history")
        self.assertIn("more than a year", block["need"])
        self.assertIsNone(block["totals"])


class TestSomethingChanged(unittest.TestCase):
    def test_a_step_that_came_and_went_is_found_where_it_happened(self):
        def stepped(start, end):
            return 26 + (7 if date(2025, 3, 1) <= start < date(2025, 9, 1) else 0) \
                + (start.toordinal() % 5) * 0.4
        block = insights.build(bills(stepped), weather())["fuels"]["Electric"]
        s = block["shift"]
        self.assertIsNotNone(s)
        self.assertFalse(s["ongoing"])
        # The last period to run high began in August and was read in September.
        self.assertEqual((s["start"], s["end"]), ("March 2025", "September 2025"))
        self.assertAlmostEqual(s["per_day"], 7, delta=0.5)
        self.assertAlmostEqual(s["watts"], 7 / 24 * 1000, delta=25)
        self.assertAlmostEqual(s["after"], s["before"], delta=0.5)
        self.assertTrue(s["key"].startswith("Electric:2025-0"))

    def test_a_step_still_going_is_called_ongoing(self):
        def stepped(start, end):
            return 26 + (6 if start >= date(2025, 6, 1) else 0) + (start.toordinal() % 5) * 0.4
        s = insights.build(bills(stepped), weather())["fuels"]["Electric"]["shift"]
        self.assertTrue(s["ongoing"])
        self.assertIsNone(s["after"])
        self.assertEqual(s["start"], "June 2025")

    def test_what_it_cost_is_the_extra_at_the_price_paid(self):
        def stepped(start, end):
            return 20 + (10 if date(2025, 3, 1) <= start < date(2025, 9, 1) else 0)
        s = insights.build(bills(stepped, price=0.30, fixed=0), weather())["fuels"]["Electric"]["shift"]
        self.assertAlmostEqual(s["extra"], 10 * s["days"], delta=1)
        self.assertAlmostEqual(s["cost"], s["extra"] * 0.30, delta=1)
        self.assertAlmostEqual(s["kg"], s["extra"] * insights.KG_PER_KWH, delta=1)

    def test_ordinary_ups_and_downs_are_not_a_step(self):
        noisy = bills(lambda s, e: 26 + math.sin(s.toordinal()) * 2)
        self.assertIsNone(insights.build(noisy, weather())["fuels"]["Electric"]["shift"])

    def test_winter_is_not_a_step_in_a_home_with_heating(self):
        temps = weather()
        gas = bills(heating(temps, 0.17, 59), unit="therms", price=2.5)
        self.assertIsNone(insights.build(gas, temps)["fuels"]["Gas"]["shift"])

    def test_a_step_in_a_heated_home_is_found_under_the_seasons(self):
        temps = weather()
        honest = heating(temps, 0.17, 59, floor=0.2)

        def leaky(start, end):      # a window left open, say, from late 2024 to mid 2025
            return honest(start, end) + (1.0 if date(2024, 11, 1) <= start < date(2025, 5, 1) else 0)
        block = insights.build(bills(leaky, unit="therms", price=2.5), temps)["fuels"]["Gas"]
        s = block["shift"]
        self.assertIsNotNone(s)
        self.assertEqual(s["basis"], "against the weather")
        self.assertEqual(s["start"], "November 2024")
        self.assertIsNone(s["watts"])

    def test_an_unnamed_step_is_an_alert_and_a_named_one_is_not(self):
        def stepped(start, end):
            return 26 + (7 if date(2025, 3, 1) <= start < date(2025, 9, 1) else 0)
        periods = bills(stepped)
        out = insights.build(periods, weather())
        alert, = out["alerts"]
        self.assertEqual((alert["fuel"], alert["href"]), ("Electric", "/electric#changed"))
        self.assertIn("March 2025", alert["title"])
        key = out["fuels"]["Electric"]["shift"]["key"]

        named = insights.build(periods, weather(), notes={key: "space heater"})
        self.assertEqual(named["alerts"], [])
        self.assertEqual(named["fuels"]["Electric"]["shift"]["note"], "space heater")
        tip = next(t for t in named["fuels"]["Electric"]["tips"] if t["id"] == "shift")
        self.assertIn("space heater", tip["body"])


class TestCarbon(unittest.TestCase):
    def test_each_fuel_uses_its_own_factor(self):
        temps = weather()
        both = bills(lambda s, e: 30.0) + bills(lambda s, e: 2.0, unit="therms", account="222")
        c = insights.build(both, temps)["carbon"]
        self.assertAlmostEqual(c["fuels"]["Electric"]["kg"], 30 * 360 * insights.KG_PER_KWH, delta=1)
        self.assertAlmostEqual(c["fuels"]["Gas"]["kg"], 2 * 360 * 5.3, delta=1)
        self.assertAlmostEqual(c["total"], c["fuels"]["Electric"]["kg"] + c["fuels"]["Gas"]["kg"])
        self.assertEqual(len(c["months"]), 12)
        self.assertIsNotNone(c["prior_total"])

    def test_the_published_factors(self):
        self.assertAlmostEqual(insights.KG_PER_KWH, 0.2464, places=4)      # EPA eGRID2023, NEWE
        self.assertEqual(insights.kg_per_unit("therms"), 5.3)
        self.assertEqual(insights.kg_per_unit("ccf"), 5.48)

    def test_a_month_missing_one_fuel_is_left_out_not_counted_as_a_saving(self):
        temps = weather()
        electric = bills(lambda s, e: 30.0, count=36)
        gas = bills(lambda s, e: 2.0, count=30, unit="therms", account="222")   # six months behind
        c = insights.build(electric + gas, temps)["carbon"]
        self.assertEqual(c["months"][-1]["label"], insights.short(gas[-1]))
        self.assertTrue(all(m["gas"] > 0 and m["electric"] > 0 for m in c["months"]))

    def test_one_fuel_alone_still_has_a_footprint(self):
        c = insights.build(bills(lambda s, e: 30.0), weather())["carbon"]
        self.assertEqual(list(c["fuels"]), ["Electric"])


class TestTips(unittest.TestCase):
    def test_one_degree_on_the_thermostat(self):
        temps = weather()
        gas = bills(heating(temps, 0.17, 59), unit="therms", price=2.5, fixed=12)
        tip = next(t for t in insights.build(gas, temps)["fuels"]["Gas"]["tips"]
                   if t["id"] == "thermostat")
        # 0.17 therms for each of the days colder than the balance point.
        recent = gas[-12:]
        cold = sum(1 for p in recent for n in range(p.days)
                   if temps[p.start + timedelta(n)] < 58)
        saved = float(tip["worth"][1]["value"].split()[0])
        self.assertAlmostEqual(saved, 0.17 * cold, delta=3)
        dollars = float(tip["worth"][0]["value"].lstrip("$"))
        self.assertAlmostEqual(dollars, saved * 2.5, delta=3)

    def test_a_home_without_heating_is_not_told_to_turn_it_down(self):
        tips = insights.build(bills(lambda s, e: 26.0), weather())["fuels"]["Electric"]["tips"]
        self.assertNotIn("thermostat", [t["id"] for t in tips])
        self.assertIn("always-on", [t["id"] for t in tips])

    def test_the_price_of_a_unit_leaves_the_standing_charge_out(self):
        temps = weather()
        gas = insights.timeline(bills(heating(temps, 0.17, 59, floor=0.005),
                                      unit="therms", price=2.5, fixed=12))
        self.assertAlmostEqual(insights.standing_charge(gas) * 30, 12, delta=0.5)
        self.assertAlmostEqual(insights.unit_price(gas), 2.5, delta=0.05)


class TestAccounts(unittest.TestCase):
    def test_a_renumbered_account_is_one_history(self):
        periods = bills(lambda s, e: 25.0, count=36)
        for p in periods[:20]:
            p.account = "old"
        block = insights.build(periods, weather())["fuels"]["Electric"]
        self.assertEqual(block["periods"], 36)

    def test_a_second_meter_running_alongside_is_not_added_on_top(self):
        mine = bills(lambda s, e: 25.0, count=36, account="mine")
        other = bills(lambda s, e: 90.0, count=34, account="other")       # ends a little earlier
        block = insights.build(mine + other, weather())["fuels"]["Electric"]
        self.assertEqual(block["periods"], 36)
        self.assertAlmostEqual(block["average"], 25.0, delta=0.1)


class TestShape(unittest.TestCase):
    def test_no_usage_at_all(self):
        out = insights.build([], None, weather_status="no-usage")
        self.assertEqual((out["fuels"], out["carbon"], out["alerts"]), ({}, None, []))

    def test_everything_is_json(self):
        temps = weather()
        both = bills(heating(temps, 0.17, 59), unit="therms", price=2.5) \
            + bills(lambda s, e: 26 + (8 if s >= date(2025, 6, 1) else 0), account="e")
        json.dumps(insights.build(both, temps, place="Anytown", notes={"x": "y"}))


class TestTempsForCompare(unittest.TestCase):
    def test_every_covered_period_has_a_temperature_for_the_compare_card(self):
        from tests.test_insights_api import export as _export  # noqa: F401  (same fixtures family)
        from metermatters.intake import greenbutton
        from datetime import date, timedelta
        periods = []
        start = date(2024, 1, 1)
        for _ in range(16):
            end = start + timedelta(days=29)
            periods.append(greenbutton.Period("1", start, end, 600.0, 150.0))
            start = end + timedelta(days=1)
        temps = {d: climate(d) for d in (date(2024, 1, 1) + timedelta(n) for n in range(600))}
        block = insights.build(periods, temps)["fuels"]["Electric"]
        self.assertEqual(len(block["temps"]), 16)
        self.assertIn(periods[0].end.isoformat(), block["temps"])


if __name__ == "__main__":
    unittest.main()
