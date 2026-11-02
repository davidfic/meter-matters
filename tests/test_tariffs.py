"""Tests for the published state prices behind the long view's estimates. Nothing here reaches the network."""

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from metermatters.analytics import tariffs
from tests.support import HostedCase
from tests.test_importers import WebCase, daily

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

ANSWER = {"response": {"total": "4", "data": [
    {"period": "2026-01", "stateid": "MA", "sectorid": "RES", "price": "30.12",
     "price-units": "cents per kilowatt-hour"},
    {"period": "2026-02", "stateid": "MA", "sectorid": "RES", "price": 29.8},
    {"period": "2026-03", "stateid": "MA", "sectorid": "RES", "price": None},
    {"period": "2026-04", "stateid": "MA", "sectorid": "RES", "price": "28.5"},
]}}


class TestReading(unittest.TestCase):
    def test_months_come_out_in_cents(self):
        self.assertEqual(tariffs.parse(ANSWER), {"2026-01": 30.12, "2026-02": 29.8, "2026-04": 28.5})

    def test_an_error_or_nothing_is_unavailable_not_a_crash(self):
        for junk in (None, {}, "x", {"error": {"code": "API_KEY_MISSING"}},
                     {"response": {"data": []}}, {"response": {"data": "x"}},
                     {"response": {"data": [{"period": "2026-01", "price": "none"}]}}):
            with self.subTest(junk), self.assertRaises(tariffs.Unavailable):
                tariffs.parse(junk)

    def test_a_day_takes_its_month_else_the_newest_before_it_within_the_lag(self):
        months = tariffs.parse(ANSWER)
        self.assertEqual(tariffs.lookup(months, date(2026, 2, 14)),
                         {"per_kwh": 0.298, "customer": 0.0, "month": "2026-02"})
        self.assertEqual(tariffs.lookup(months, date(2026, 3, 1))["month"], "2026-02")    # March was blank
        self.assertEqual(tariffs.lookup(months, date(2026, 8, 31))["month"], "2026-04")   # four months of lag
        self.assertIsNone(tariffs.lookup(months, date(2026, 9, 1)))                        # five is too many
        self.assertIsNone(tariffs.lookup(months, date(2025, 12, 31)))                      # before the first month


class TestKeeping(HostedCase):
    env = {"ELEC_EIA_KEY": "test-key"}

    def setUp(self):
        super().setUp()
        self.calls, self.fail = [], False

        def fake(params):
            self.calls.append(dict(params))
            if self.fail:
                raise tariffs.Unavailable("down")
            return ANSWER
        patch = mock.patch.object(tariffs, "_get", side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_only_the_state_and_the_key_are_sent(self):
        tariffs.prices_for("ma", NOW)
        sent = self.calls[-1]
        self.assertEqual(sent["facets[stateid][]"], "MA")
        self.assertEqual(sent["facets[sectorid][]"], "RES")
        self.assertEqual(sent["api_key"], "test-key")
        self.assertEqual(set(sent), {"api_key", "frequency", "data[0]", "facets[stateid][]",
                                     "facets[sectorid][]", "start", "sort[0][column]",
                                     "sort[0][direction]", "length"})

    def test_a_months_old_answer_is_not_asked_for_again(self):
        tariffs.prices_for("MA", NOW)
        n = len(self.calls)
        tariffs.prices_for("MA", NOW + timedelta(days=29))
        self.assertEqual(len(self.calls), n)
        tariffs.prices_for("MA", NOW + timedelta(days=31))
        self.assertGreater(len(self.calls), n)

    def test_when_eia_is_down_the_old_answer_is_used(self):
        tariffs.prices_for("MA", NOW)
        self.fail = True
        self.assertEqual(tariffs.prices_for("MA", NOW + timedelta(days=40)), tariffs.parse(ANSWER))

    def test_a_failure_is_not_retried_on_every_page_view(self):
        self.fail = True
        self.assertIsNone(tariffs.prices_for("MA", NOW))
        self.assertIsNone(tariffs.prices_for("MA", NOW + timedelta(minutes=10)))
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(tariffs.prices_for("MA", NOW + timedelta(hours=2)))
        self.assertEqual(len(self.calls), 2)

    def test_nowhere_or_no_key_asks_nothing(self):
        self.assertIsNone(tariffs.prices_for("ZZ", NOW))
        self.assertIsNone(tariffs.prices_for(None, NOW))
        with mock.patch.dict(os.environ, {"ELEC_EIA_KEY": ""}):
            self.assertIsNone(tariffs.prices_for("MA", NOW))
        self.assertEqual(self.calls, [])

    def test_for_area_is_electric_only_and_names_the_source(self):
        published, source = tariffs.for_area("MA", "Electric")
        self.assertEqual(published(date(2026, 1, 15))["per_kwh"], 0.3012)
        self.assertEqual(source["text"], "EIA's average residential price for Massachusetts")
        self.assertEqual(tariffs.for_area("MA", "Gas"), (None, None))
        self.assertEqual(tariffs.for_area(None, "Electric"), (None, None))


class TestTheLongView(WebCase):
    """Bills older than the usage export get their usage estimated from the state's price."""
    env = {"ELEC_EIA_KEY": "test-key"}

    def setUp(self):
        super().setUp()
        self.asked = []

        def fake(params):
            self.asked.append(params["facets[stateid][]"])
            months = [f"{y}-{m:02d}" for y in range(2022, 2027) for m in range(1, 13)]
            return {"response": {"data": [{"period": p, "price": 31.0} for p in months]}}
        patch = mock.patch.object(tariffs, "_get", side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_old_year_without_usage_is_estimated_and_the_page_says_from_what(self):
        bills = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                 + "".join(f"9876543210,Electric,2022/{m:02d}/16,Current Bill,$248.00,-\n"
                           for m in range(1, 13)))
        self.upload("export.csv", daily(start=date(2023, 1, 1)))      # metered at 31 cents, like the state
        self.upload("AccountHistory.csv", bills.encode())               # 2022: dollars only
        html = self.client.get("/electric/years").get_data(as_text=True)
        self.assertEqual(self.asked, ["CA"])                            # the state read off the export's address
        self.assertIn("EIA&#39;s average residential price for California", html.replace("'", "&#39;"))
        self.assertIn("published rates", html)


if __name__ == "__main__":
    unittest.main()
