"""The overview's headline: what the last twelve months add up to, whatever
shape the files are in."""

import unittest

from metermatters.analytics import analysis
from tests.test_importers import WebCase


def energy(*months):
    """Rows the way combined_energy makes them: (period, electric, gas, water)."""
    out = []
    for period, electric, gas, water in months:
        row = {"period": period, "electric": electric, "gas": gas, "water": water,
               "total": round(electric + gas + water, 2)}
        year, month = period.split("-")
        row["label"] = f"{analysis.MONTH_NAMES[int(month) - 1]} {year}"
        row["short"] = f"{analysis.MONTH_NAMES[int(month) - 1]} '{year[2:]}"
        out.append(row)
    return out


class Headline(unittest.TestCase):
    def test_a_lone_bill_in_one_utility_does_not_blank_it(self):
        rows = energy(*[(f"2026-{m:02d}", 200.0, 50.0, 0.0) for m in range(1, 11)],
                      ("2026-11", 210.0, 55.0, 197.65))
        h = analysis.overview_headline(rows)
        self.assertIsNotNone(h)
        self.assertEqual(h["months"], 11)
        self.assertEqual(h["total_12"], round(200 * 10 + 210 + 50 * 10 + 55 + 197.65, 2))
        self.assertEqual([f["key"] for f in h["by_fuel"]], ["electric", "gas", "water"])
        self.assertEqual(h["peak"]["label"], "Nov 2026")
        self.assertIsNone(h["prior_12"])

    def test_a_month_still_missing_a_utility_is_not_the_cheapest(self):
        rows = energy(*[(f"2026-{m:02d}", 200.0, 50.0, 0.0) for m in range(1, 11)],
                      ("2026-11", 0.0, 0.0, 197.65))        # water in, electric and gas not yet
        h = analysis.overview_headline(rows)
        self.assertEqual(h["low"]["label"], "Jan 2026")
        self.assertEqual(h["peak"]["label"], "Jan 2026")
        self.assertEqual(h["total_12"], round(250 * 10 + 197.65, 2))

    def test_water_billed_every_other_month_leaves_every_month_in_the_running(self):
        # Water arrives on the even months. The cheapest month is an odd one, with
        # no water bill in it, and that is the right answer, not a technicality.
        rows = energy(*[(f"2026-{m:02d}", 200.0 + m, 50.0, 90.0 if m % 2 == 0 else 0.0)
                        for m in range(1, 13)])
        h = analysis.overview_headline(rows)
        self.assertEqual(h["low"]["label"], "Jan 2026")
        self.assertEqual(h["peak"]["label"], "Dec 2026")

    def test_one_utility_still_gets_a_headline(self):
        rows = energy(*[(f"2026-{m:02d}", 100.0 + m, 0.0, 0.0) for m in range(1, 7)])
        h = analysis.overview_headline(rows)
        self.assertEqual(h["total_12"], sum(100.0 + m for m in range(1, 7)))
        self.assertEqual(h["by_fuel"], [{"key": "electric", "amount": 621.0, "pct": 100}])
        self.assertEqual(h["avg_month"], round(621.0 / 6, 2))

    def test_the_prior_twelve_need_twelve_months(self):
        rows = energy(*[(f"{2025 + (m - 1) // 12}-{(m - 1) % 12 + 1:02d}", 100.0, 20.0, 0.0)
                        for m in range(1, 25)])
        h = analysis.overview_headline(rows)
        self.assertEqual((h["total_12"], h["prior_12"]), (1440.0, 1440.0))
        self.assertIsNone(analysis.overview_headline(rows[1:])["prior_12"])

    def test_next_bills_add_up_the_forecasts(self):
        rows = energy(("2026-09", 200.0, 50.0, 0.0), ("2026-10", 210.0, 55.0, 0.0))
        h = analysis.overview_headline(rows, {"Electric": {"amount": 221.0}, "Gas": None,
                                              "Water": {"amount": 0}})
        self.assertEqual((h["next_bills"], h["next_count"]), (221.0, 1))
        self.assertIsNone(analysis.overview_headline(rows)["next_bills"])

    def test_nothing_gives_nothing(self):
        self.assertIsNone(analysis.overview_headline([]))
        self.assertEqual(analysis.season_rows([]), [])


class Season(unittest.TestCase):
    def test_each_utility_is_averaged_over_the_years_it_has(self):
        rows = energy(("2025-01", 300.0, 100.0, 0.0), ("2026-01", 200.0, 120.0, 40.0),
                      ("2026-02", 180.0, 90.0, 0.0))
        season = analysis.season_rows(rows)
        self.assertEqual([r["month"] for r in season], [1, 2])
        january = season[0]
        self.assertEqual((january["electric"], january["gas"], january["water"]), (250.0, 110.0, 40.0))
        self.assertEqual(january["total"], 400.0)
        self.assertEqual(january["n"], 2)
        self.assertEqual(season[1]["n"], 1)


class OnThePage(WebCase):
    HEADER = "Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status\n"

    def test_the_overview_carries_a_headline_with_a_lone_water_bill(self):
        rows = self.HEADER + "".join(
            f"1234567890,Electric,2026/{m:02d}/16,Current Bill,${100 + m}.00,-\n" for m in range(1, 11))
        self.assertEqual(self.upload("AccountHistory.csv", rows.encode()).status_code, 200)
        self.client.post("/bills/add", data={"fuel": "Water", "due": "2026-11-09", "amount": "197.65"})
        d = self.client.get("/api/overview").get_json()
        self.assertEqual([c["fuel"] for c in d["fuels"]], ["Electric", "Water"])
        h = d["headline"]
        self.assertEqual(h["months"], 11)
        self.assertEqual(h["total_12"], round(sum(100 + m for m in range(1, 11)) + 197.65, 2))
        self.assertEqual([f["key"] for f in h["by_fuel"]], ["electric", "water"])
        self.assertEqual(len(d["season"]), 11)
        self.assertEqual(d["season"][-1]["water"], 197.65)


if __name__ == "__main__":
    unittest.main()
