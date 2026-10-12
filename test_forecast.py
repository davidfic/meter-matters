"""Tests for the next-statement estimate and the streak that can't be flattered."""

import unittest
from datetime import date, timedelta

import analysis
import forecast
import gamify


def series(n=18, base=100.0, season=True, usage=True, start=date(2024, 1, 16)):
    rows = []
    for i in range(n):
        d = date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1, 16)
        amount = base + (30 if season and d.month in (1, 2, 12) else 0) + i * 0.5
        row = {"period": f"{d:%Y-%m}", "label": f"{d:%b %Y}", "short": f"{d:%b '%y}", "due": d.isoformat(),
               "amount": amount, "month": d.month, "year": d.year, "days": 30, "delta": None,
               "paid": True, "improved": False, "kwh": None, "kwh_per_day": None, "rate": None,
               "pct": None, "status": "PAID"}
        if usage:
            row["kwh_per_day"] = 20 + (8 if d.month in (1, 2, 12) else 0)
            row["kwh"] = row["kwh_per_day"] * 30
            row["rate"] = amount / row["kwh"]
        if rows:
            row["delta"] = amount - rows[-1]["amount"]
            row["pct"] = row["delta"] / rows[-1]["amount"] * 100
            row["improved"] = row["delta"] < 0
            row["kwh_delta"] = (row["kwh"] - rows[-1]["kwh"]) if usage else None
        rows.append(row)
    return rows


class TestNextBill(unittest.TestCase):
    def test_too_little_history_gives_nothing(self):
        self.assertIsNone(forecast.next_bill(series(3)))

    def test_from_bills_alone_it_follows_the_season(self):
        rows = series(23, usage=False)                 # ends Nov 2025; next is December, a dear month
        f = forecast.next_bill(rows)
        self.assertEqual(f["basis"], "bills")
        self.assertGreater(f["amount"], 125)
        self.assertIn(f["due"], ("2025-12-15", "2025-12-16", "2025-12-17"))
        self.assertLess(f["low"], f["amount"])
        self.assertGreater(f["high"], f["amount"])
        self.assertTrue(f["backtested"])

    def test_from_usage_it_prices_last_years_usage_at_todays_rate(self):
        rows = series(23)
        price = 0.25
        f = forecast.next_bill(rows, price=price, standing_per_day=0.0)
        self.assertEqual(f["basis"], "usage")
        self.assertAlmostEqual(f["amount"], 28 * 30 * price, delta=28 * 30 * price * 0.25)

    def test_the_range_comes_from_how_wrong_the_method_has_been(self):
        steady = series(23, season=False, usage=False)
        f = forecast.next_bill(steady)
        self.assertLessEqual(f["typical_miss_pct"], 5)


class TestHonestStreak(unittest.TestCase):
    def test_weather_adjusted_wins_when_known(self):
        rows = series(6)
        for r in rows:
            r["you_delta"] = -1.0
        rows[2]["you_delta"] = 2.0
        self.assertEqual(analysis.honest_streak(rows), (3, 3, "weather"))

    def test_then_usage_then_dollars(self):
        rows = series(6)
        for r in rows[1:]:
            r["kwh_delta"] = -5
        self.assertEqual(analysis.honest_streak(rows)[2], "usage")
        for r in rows:
            r["kwh_delta"] = None
        self.assertEqual(analysis.honest_streak(rows)[2], "dollars")


class TestRecordsStayEarned(unittest.TestCase):
    def test_a_record_set_earlier_is_still_a_record(self):
        rows = series(8, season=False, usage=False)
        rows[3]["amount"] = 50.0                        # a record low, then back up
        report = {"series": rows, "game_series": rows,
                  "summary": analysis.summarise(rows, {}), "banked": 0}
        badge = next(b for b in gamify.achievements(report) if b["id"] == "record-low")
        self.assertTrue(badge["earned"])
        self.assertEqual(badge["earned_on"], rows[3]["label"])


if __name__ == "__main__":
    unittest.main()
