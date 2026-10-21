"""Tests for the long view: the average statement year by year, split into usage and price."""

import unittest
from datetime import date

from metermatters.analytics import longview
from tests.test_importers import WebCase, daily


def statement(year, month, amount, kwh=None, rate=None):
    due = date(year, month, 20)
    return {"year": year, "month": month, "label": f"{due:%b %Y}", "amount": amount, "due": due.isoformat(),
            "kwh": kwh, "rate": rate,
            "service_start": date(year, month, 1).isoformat() if kwh else None,
            "service_end": date(year, month, 28).isoformat() if kwh else None}


def published(day):
    """A flat published rate: 20 cents a kWh over a $7 customer charge."""
    return {"per_kwh": 0.20, "customer": 7.0}


class TestTheSplit(unittest.TestCase):
    def series(self):
        rows = []
        for m in range(1, 13):                              # 2019: $107 a statement, no usage on file
            rows.append(statement(2019, m, 107.0))
        for m in range(1, 13):                              # 2024: 800 kWh at 30 cents, metered
            rows.append(statement(2024, m, 7.0 + 800 * 0.30, kwh=800.0, rate=0.30))
        return rows

    def test_the_two_parts_add_up_to_the_change(self):
        rows = self.series()
        # 2024 is on a 30-cent tariff, so a 20-cent published rate is not this account's: nothing is estimated.
        out = longview.build(rows, published)
        self.assertEqual(out["base"], 2024)                 # the first full year WITH usage
        self.assertEqual([y["basis"] for y in out["years"]], ["dollars", "metered"])
        self.assertIsNone(out["what_if"])                   # nothing after the base to compare

    def test_old_years_are_estimated_from_a_published_rate_that_fits(self):
        rows = self.series()
        thirty = lambda day: {"per_kwh": 0.30, "customer": 7.0}
        out = longview.build(rows, thirty, source={"text": "a table"})
        base, later = out["years"]
        self.assertEqual((base["year"], base["basis"], base["kwh"]), (2019, "estimated", 333.3))
        self.assertEqual(later["basis"], "metered")
        self.assertAlmostEqual(later["usage_added"] + later["price_added"], later["change"], places=1)
        self.assertGreater(later["usage_added"], 0)
        # The same tariff all along: the only price movement is the $7 customer charge spread over more kWh.
        self.assertLess(later["price_added"], 0)
        self.assertLess(abs(later["price_added"]), 12)
        self.assertEqual(out["what_if"]["latest"], 2024)
        self.assertTrue(out["estimated"])
        self.assertEqual(out["source"]["text"], "a table")

    def test_a_year_short_of_statements_is_left_out(self):
        rows = self.series() + [statement(2025, m, 250.0, kwh=800.0, rate=0.30) for m in range(1, 4)]
        out = longview.build(rows, None)
        self.assertEqual([y["year"] for y in out["years"]], [2019, 2024])

    def test_a_year_in_progress_sits_beside_the_full_ones(self):
        rows = self.series() + [statement(2025, m, 7.0 + 850 * 0.32, kwh=850.0, rate=0.32) for m in range(1, 9)]
        out = longview.build(rows, None)
        last = out["years"][-1]
        self.assertEqual((last["year"], last["full"], last["statements"]), (2025, False, 8))
        self.assertGreater(last["usage_added"], 0)
        self.assertGreater(last["price_added"], 0)
        # With no full year after the base, the what-ifs use the one in progress and say so.
        self.assertEqual((out["what_if"]["latest"], out["what_if"]["partial"]), (2025, True))

    def test_the_weather_split_needs_the_base_years_weather(self):
        rows = [statement(2023, m, 7.0 + 800 * 0.30, kwh=800.0, rate=0.30) for m in range(1, 13)] + \
               [statement(2024, m, 7.0 + 900 * 0.30, kwh=900.0, rate=0.30) for m in range(1, 13)]
        known_from_2024 = lambda r: 40.0 if r["year"] >= 2024 else None
        out = longview.build(rows, None, weather=known_from_2024)
        base, later = out["years"]
        self.assertEqual(base["year"], 2023)
        self.assertIsNone(base["weather_kwh"])
        self.assertEqual(later["weather_kwh"], 40.0)
        # 2024 against 2023, like the bars beside it - and 2023's weather is unknown.
        self.assertIsNone(later["weather_added"])
        self.assertFalse(out["weather"])
        known_throughout = lambda r: 40.0 if r["year"] >= 2024 else 10.0
        later = longview.build(rows, None, weather=known_throughout)["years"][1]
        self.assertAlmostEqual(later["weather_added"], 30.0 * (7.0 + 800 * 0.30) / 800, places=2)   # at the base year's own rate

    def test_two_years_without_usage_is_nothing_to_split(self):
        rows = [statement(2019, m, 100.0) for m in range(1, 13)] + [statement(2020, m, 120.0) for m in range(1, 13)]
        self.assertIsNone(longview.build(rows, None))


class TestThePage(WebCase):
    def test_the_fuel_page_offers_the_long_view_once_there_are_two_years(self):
        self.upload("export.csv", daily(start=date(2023, 1, 1)))
        page = self.client.get("/electric").get_data(as_text=True)
        self.assertIn('href="/electric/years"', page)
        html = self.client.get("/electric/years").get_data(as_text=True)
        for needed in ("year by year", "The average statement, by year", "Where the change came from", 'id="long-view"'):
            self.assertIn(needed, html, needed)
        self.assertEqual(self.client.get("/water/years").status_code, 404)

    def test_one_year_offers_nothing(self):
        self.upload("export.csv", daily())                  # Jan 2024 - Mar 2025: one full year
        page = self.client.get("/electric").get_data(as_text=True)
        self.assertIn('href="/electric/years"', page)       # two calendar years touched...
        self.assertEqual(self.client.get("/electric/years").status_code, 404)   # ...but only one with enough in it


if __name__ == "__main__":
    unittest.main()
