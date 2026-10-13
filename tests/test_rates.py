"""Tests for the supply-offer comparison. Nothing here reaches the network."""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from metermatters.analytics import rates
from tests.test_insights_api import export
from tests.support import HostedCase

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

SITE = [
    {"rowType": "DistributionCompany", "supplierName": "Utility", "supplierProductId": 1,
     "pricePerUnit": "17.323 ¢/kWh|TBD", "contractTerm": "Aug '26 through Jan '27 |Feb '27 through Jul '27 "},
    {"rowType": "Supplier", "supplierName": "City of Exampleton Choice", "supplierProductId": 2,
     "pricePerUnit": "14.840 ¢/kWh ", "contractTerm": " through Nov '27", "isMunicipalAggregationDefault": True,
     "municipalAggregationTown": "Exampleton", "earlyTerminationDetail": "No cancellation fee",
     "renewableEnergyProductPercentage": 100.0, "hasAutomaticRenewal": True},
    {"rowType": "Supplier", "supplierName": "Acme Power", "supplierProductId": 3,
     "pricePerUnit": "16.290 ¢/kWh", "contractTerm": "36 months", "isNewCustomerOnly": True,
     "earlyTerminationDetail": "Cancellation fee: $50.00", "renewableEnergyProductPercentage": 0.0,
     "pricePerMonth": "$2.00", "productWebsiteUrl": "https://acme.example/plan"},
    {"rowType": "Supplier", "supplierName": "Broken", "supplierProductId": 4, "pricePerUnit": None},
]
WHERE = {"distributionCompanies": [{"distributionCompanyId": 2, "distributionCompanyName": "Utility"}],
         "towns": [{"townCode": "Exampleton"}]}


def series(n=14, kwh=800.0, rate=0.34):
    return [{"period": f"2025-{m:02d}", "kwh": kwh, "rate": rate, "year": 2025, "month": m,
             "amount": kwh * rate} for m in range(1, 13)][:n]


class FakeSite:
    def __init__(self):
        self.calls, self.fail = [], False

    def __call__(self, path, body):
        self.calls.append((path, dict(body)))
        if self.fail:
            raise rates.Unavailable("down")
        return WHERE if path.endswith("byzipcode") else SITE


class TestParse(unittest.TestCase):
    def test_the_first_price_is_the_one_in_force(self):
        offers = {o["id"]: o for o in rates.parse(SITE)}
        self.assertEqual(offers[1]["cents"], 17.323)
        self.assertEqual(offers[1]["kind"], "basic")
        self.assertEqual(offers[2]["kind"], "municipal")
        self.assertTrue(offers[2]["default"])
        self.assertEqual(offers[3]["term_months"], 36)
        self.assertEqual(offers[3]["exit_fee"], 50.0)
        self.assertEqual(offers[2]["exit_fee"], 0.0)
        self.assertEqual(offers[3]["monthly_fee"], 2.0)

    def test_an_offer_with_no_price_is_dropped(self):
        self.assertNotIn(4, [o["id"] for o in rates.parse(SITE)])

    def test_nonsense_is_unavailable_not_a_crash(self):
        for junk in (None, {}, "x", [], [{"pricePerUnit": None}]):
            with self.subTest(junk), self.assertRaises(rates.Unavailable):
                rates.parse(junk)


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.offers = rates.parse(SITE)

    def test_the_default_supply_is_the_town_program_else_basic(self):
        self.assertEqual(rates.default_supply(self.offers)["id"], 2)
        self.assertEqual(rates.default_supply([o for o in self.offers if o["id"] != 2])["id"], 1)

    def test_an_offer_changes_the_bill_by_the_supply_difference_over_usage(self):
        out = rates.compare(series(12), self.offers, None)
        by = {o["id"]: o for o in out["offers"]}
        self.assertEqual(out["kwh"], 9600.0)
        # (17.323 - 14.84)c over 9,600 kWh
        self.assertAlmostEqual(by[1]["change"], 238.37, places=2)
        self.assertEqual(by[2]["change"], 0.0)
        self.assertTrue(by[2]["current"])
        # Acme: (16.29 - 14.84)c * 9600 + $2 * 12 months
        self.assertAlmostEqual(by[3]["change"], 139.2 + 24, places=2)

    def test_a_chosen_supply_replaces_the_assumed_one(self):
        out = rates.compare(series(12), self.offers, 20.0)
        self.assertFalse(out["supply"]["assumed"])
        self.assertFalse(any(o["current"] for o in out["offers"]))
        self.assertLess({o["id"]: o for o in out["offers"]}[1]["change"], 0)

    def test_only_the_last_twelve_statements_count(self):
        self.assertEqual(rates.compare(series(12) * 2, self.offers, None)["months"], 12)

    def test_no_usage_means_no_comparison_rather_than_a_wrong_one(self):
        self.assertEqual(rates.compare([], self.offers, None)["offers"], [])

    def test_the_market_is_summarised_over_the_other_offers(self):
        market = rates.compare(series(12), self.offers, None)["market"]
        self.assertEqual((market["count"], market["cheaper_than_you"]), (1, 0))

    def test_the_breakdown_leaves_delivery_what_supply_does_not_cover(self):
        b = rates.breakdown(0.34, 14.84, {"standing_charge": 36.0})
        self.assertAlmostEqual(b["delivery"], 0.1916, places=4)
        self.assertIsNone(rates.breakdown(0.10, 14.84, None))
        self.assertIsNone(rates.breakdown(None, 14.84, None))


class Held(HostedCase):
    def setUp(self):
        super().setUp()
        self.site = FakeSite()
        patch = mock.patch.object(rates, "_post", side_effect=self.site)
        patch.start()
        self.addCleanup(patch.stop)


class TestKeeping(Held):
    def test_only_the_zip_and_a_placeholder_usage_are_sent(self):
        rates.offers_for("01000", NOW)
        sent = self.site.calls[-1][1]
        self.assertEqual(sent, {"zipCode": "01000", "customerClassId": 1, "distributionCompanyId": 2,
                                "monthlyUsage": rates.PLACEHOLDER_KWH, "town": "Exampleton"})

    def test_a_days_old_answer_is_not_asked_for_again(self):
        rates.offers_for("01000", NOW)
        n = len(self.site.calls)
        rates.offers_for("01000", NOW + timedelta(hours=23))
        self.assertEqual(len(self.site.calls), n)
        rates.offers_for("01000", NOW + timedelta(hours=25))
        self.assertGreater(len(self.site.calls), n)

    def test_when_the_site_is_down_the_old_answer_is_served(self):
        rates.offers_for("01000", NOW)
        self.site.fail = True
        got = rates.offers_for("01000", NOW + timedelta(days=3))
        self.assertEqual(len(got["offers"]), 3)

    def test_a_failure_is_not_retried_on_every_page_view(self):
        self.site.fail = True
        for _ in range(2):
            with self.assertRaises(rates.Unavailable):
                rates.offers_for("01000", NOW)
        self.assertEqual(len(self.site.calls), 1)

    def test_what_is_not_a_zip_is_never_sent(self):
        for value in ("", "0100", "abcde", None):
            with self.subTest(value), self.assertRaises(rates.Unavailable):
                rates.offers_for(value)
        self.assertEqual(self.site.calls, [])


class TestApi(Held):
    def setUp(self):
        super().setUp()
        self.user = self.sign_up()

    def test_nothing_to_show_without_bills_is_not_an_error(self):
        res = self.client.get("/api/rates")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["series"], [])
        self.assertEqual(self.site.calls, [])

    def test_bills_without_usage_say_that_usage_is_needed(self):
        rows = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                + "".join(f"74000011111,Electric,2026/{m:02d}/16,Current Bill,$100.00,-\n"
                          for m in range(1, 7))).encode()
        res = self.client.post("/api/upload", content_type="multipart/form-data",
                               data={"file": [(io.BytesIO(rows), "AccountHistory.csv")]})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])
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

    def test_a_supply_price_is_kept_and_a_bad_one_is_not(self):
        def kept(value):
            self.client.post("/api/rates/supply", json={"supply_cents": value})
            return self.client.get("/api/rates").get_json()["settings"]["supply_cents"]
        self.assertEqual(kept("15.5"), 15.5)
        self.assertIsNone(kept("banana"))
        self.assertIsNone(kept(4000))
        self.assertIsNone(kept(-3))


def webapp_client():
    from metermatters.web import app as webapp
    return webapp.app.test_client()


if __name__ == "__main__":
    unittest.main()
