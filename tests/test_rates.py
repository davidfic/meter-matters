"""Tests for the Rates page: the price taken apart. Nothing here reaches the network."""

import io
import unittest

from metermatters.analytics import rates
from tests.test_importers import WebCase, daily


def series(n=14, kwh=800.0, rate=0.34, year=2025):
    return [{"period": f"{year}-{m:02d}", "kwh": kwh, "rate": rate, "year": year, "month": m,
             "amount": kwh * rate} for m in range(1, 13)][:n]


class TestOwnPrice(unittest.TestCase):
    def test_only_the_last_twelve_statements_count(self):
        self.assertEqual(rates.last_year(series(12) * 2), (9600.0, 12))
        self.assertEqual(rates.last_year([]), (0.0, 0))

    def test_the_breakdown_leaves_delivery_what_supply_does_not_cover(self):
        b = rates.breakdown(0.34, 14.84, {"standing_charge": 36.0})
        self.assertAlmostEqual(b["delivery"], 0.1916, places=4)
        self.assertEqual(b["standing_charge"], 36.0)
        self.assertIsNone(rates.breakdown(0.10, 14.84, None))    # supply above the all-in price
        self.assertIsNone(rates.breakdown(None, 14.84, None))
        self.assertIsNone(rates.breakdown(0.34, None, None))     # no supply price given

    def test_seasons_average_each_month_over_the_years(self):
        rows = series(12, rate=0.34, year=2024) + series(12, rate=0.30, year=2025)
        out = rates.seasons(rows)
        self.assertEqual(len(out), 12)
        self.assertAlmostEqual(out[0]["rate"], 0.32)
        self.assertEqual(out[0]["n"], 2)

    def test_standing_places_the_latest_price_among_the_rest(self):
        rows = series(12, year=2024) + series(12, year=2025)
        rows[-1]["rate"] = 0.40
        out = rates.standing(rows, 0.40)
        self.assertEqual((out["percentile"], out["of"]), (96, 24))
        self.assertEqual(out["year_ago"], 0.34)
        self.assertAlmostEqual(out["year_pct"], 17.6)
        self.assertIsNone(rates.standing(rows[:2], 0.40))


class TestApi(WebCase):
    """The page, for a home whose weather is somebody else's business (WebCase mocks it)."""

    def upload(self, name, body):
        body = body if isinstance(body, bytes) else body.encode()
        res = self.client.post("/api/upload", content_type="multipart/form-data",
                               data={"file": [(io.BytesIO(body), name)]})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])

    def test_nothing_to_show_without_bills_is_not_an_error(self):
        res = self.client.get("/api/rates")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["series"], [])

    def test_bills_without_usage_say_that_usage_is_needed(self):
        rows = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                + "".join(f"74000011111,Electric,2026/{m:02d}/16,Current Bill,$100.00,-\n"
                          for m in range(1, 7)))
        self.upload("AccountHistory.csv", rows)
        out = self.client.get("/api/rates").get_json()
        self.assertEqual(out["series"], [])
        self.assertIsInstance(out["error"], str)
        self.assertIn("usage", out["error"].lower())

    def test_the_page_needs_signing_in(self):
        res = webapp_client().get("/rates")
        self.assertEqual(res.status_code, 302)

    def test_the_page_renders_for_a_signed_in_user(self):
        res = self.client.get("/rates")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"rates.js", res.data)
        self.assertNotIn(b"supplier", res.data.lower())

    def test_a_supply_price_is_kept_and_a_bad_one_is_not(self):
        def kept(value):
            self.client.post("/api/rates/supply", json={"supply_cents": value})
            return self.client.get("/api/rates").get_json()["settings"]["supply_cents"]
        self.assertEqual(kept("15.5"), 15.5)
        self.assertIsNone(kept("banana"))
        self.assertIsNone(kept(4000))
        self.assertIsNone(kept(-3))

    def test_the_split_appears_once_a_supply_price_is_given_and_nothing_is_asked_outside(self):
        self.upload("export.csv", daily())
        before = self.client.get("/api/rates").get_json()
        self.assertIsNone(before["breakdown"])
        self.assertEqual(before["last_year"]["months"], 12)      # of the 14 statements on file
        self.assertGreater(before["last_year"]["kwh"], 0)
        for gone in ("status", "comparison", "offers", "source", "zip"):
            self.assertNotIn(gone, before)
        self.client.post("/api/rates/supply", json={"supply_cents": 15.5})
        after = self.client.get("/api/rates").get_json()
        self.assertAlmostEqual(after["breakdown"]["supply"], 0.155)
        self.assertGreater(after["breakdown"]["delivery"], 0)


def webapp_client():
    from metermatters.web import app as webapp
    return webapp.app.test_client()


if __name__ == "__main__":
    unittest.main()
