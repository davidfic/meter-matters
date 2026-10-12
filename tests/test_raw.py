"""Tests for the raw-data explorer's payload."""

import unittest
from pathlib import Path

from metermatters.analytics import analysis
from metermatters.intake import greenbutton
from metermatters.intake import ingest

HERE = Path(__file__).resolve().parent
CSV = HERE.parent / "AccountHistory.csv"   # your own history, in the checkout
USAGE = HERE.parent / "usage"

# Those are your own bills, and are never committed. These have the same shape
# and invented contents, and are what a clean checkout is tested against - see
# fixtures/synthetic/generate.py for where every number in them comes from.
SYNTHETIC = HERE / "fixtures" / "synthetic"


class RawRowsChecks:
    """What must hold for any history. A subclass says which one."""

    csv: Path
    usage: Path

    @classmethod
    def setUpClass(cls):
        cls.entries = analysis.load_entries(cls.csv)
        cls.bills = analysis.build_bills(cls.entries)
        cls.periods = greenbutton.load_dir(cls.usage).periods if cls.usage.exists() else []
        analysis.attach_usage(cls.bills, cls.periods)
        cls.rows = analysis.raw_rows(cls.entries, cls.bills)

    def test_every_entry_is_represented(self):
        # The point of the raw view is that nothing is filtered away.
        self.assertEqual(len(self.rows), len(self.entries))

    def test_rows_are_newest_first(self):
        dates = [r["date"] for r in self.rows]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_provenance_is_present_on_every_row(self):
        self.assertTrue(all(r["source"] for r in self.rows),
                        "every row should say where it came from")
        self.assertTrue({"portal", "email"} <= {r["source"] for r in self.rows})

    def test_only_bills_carry_derived_usage(self):
        for row in self.rows:
            if row["type"] != "Current Bill":
                self.assertIsNone(row["kwh"])
                self.assertFalse(row["verified"])

    def test_verified_rows_have_a_metered_period(self):
        for row in self.rows:
            if row["verified"]:
                self.assertIsNotNone(row["service_start"])
                self.assertIsNotNone(row["service_end"])
                self.assertIsNotNone(row["kwh"])

    def test_both_fuels_appear(self):
        self.assertEqual({r["fuel"] for r in self.rows}, {"Electric", "Gas"})


@unittest.skipIf(not CSV.exists(), "no data file")
class TestRawRows(RawRowsChecks, unittest.TestCase):
    """Your own history, when it is here."""

    csv, usage = CSV, USAGE


class TestRawRowsSynthetic(RawRowsChecks, unittest.TestCase):
    """The invented history, which always is - and whose contents are known."""

    csv, usage = SYNTHETIC / "account_history_full.csv", SYNTHETIC / "usage"

    def test_what_is_in_the_file(self):
        self.assertEqual(len(self.rows), 71)
        kinds = [r["type"] for r in self.rows]
        self.assertEqual((kinds.count("Current Bill"), kinds.count("Payment")), (58, 13))
        sources = [r["source"] for r in self.rows]
        self.assertEqual((sources.count("portal"), sources.count("email")), (26, 45))

    def test_every_statement_finds_its_metered_period(self):
        # 26 current electric, 8 under the old number, 24 gas - each statement
        # is one period's charge to the cent, so each is verified.
        self.assertEqual(sum(r["verified"] for r in self.rows), 58)
        units = {(r["fuel"], r["unit"]) for r in self.rows if r["verified"]}
        self.assertEqual(units, {("Electric", "kWh"), ("Gas", "therms")})

    def test_a_masked_account_still_joins_on_the_amount(self):
        # Early gas bills show only "******0004"; no export names that number.
        masked = [r for r in self.rows if r["account"].startswith("*")]
        self.assertEqual(len(masked), 8)
        self.assertTrue(all(r["verified"] and r["unit"] == "therms" for r in masked))

    def test_the_newest_statement_in_full(self):
        newest = next(r for r in self.rows if r["type"] == "Current Bill")
        self.assertEqual((newest["date"], newest["fuel"], newest["account"], newest["nickname"]),
                         ("2026-10-16", "Electric", "55500010001", "demo42"))
        self.assertAlmostEqual(newest["amount"], 211.19)
        self.assertEqual((newest["service_start"], newest["service_end"], newest["days"]),
                         ("2026-08-20", "2026-09-18", 30))
        self.assertAlmostEqual(newest["kwh"], 590.0)


class UsageRowsChecks:
    usage: Path

    def setUp(self):
        self.rows = analysis.usage_rows(greenbutton.load_dir(self.usage).periods)

    def test_one_row_per_metered_period(self):
        self.assertEqual(len(self.rows), len(greenbutton.load_dir(self.usage).periods))

    def test_sorted_newest_first_and_self_consistent(self):
        self.assertEqual([r["end"] for r in self.rows],
                         sorted([r["end"] for r in self.rows], reverse=True))
        for r in self.rows:
            self.assertLess(r["start"], r["end"])
            if r["rate"] and r["kwh"]:
                self.assertAlmostEqual(r["rate"], r["cost"] / r["kwh"], places=3)

    def test_each_row_names_its_file(self):
        self.assertTrue(all(r["source"].endswith(".csv") for r in self.rows))


@unittest.skipIf(not USAGE.exists(), "no usage exports")
class TestUsageRows(UsageRowsChecks, unittest.TestCase):
    """Your own exports, when they are here."""

    usage = USAGE


class TestUsageRowsSynthetic(UsageRowsChecks, unittest.TestCase):
    usage = SYNTHETIC / "usage"

    def test_what_is_in_the_folder(self):
        self.assertEqual(len(self.rows), 58)                           # 26 + 8 + 24
        self.assertEqual(sum(r["estimated"] for r in self.rows), 1)
        self.assertEqual(sum(r["avg_temp"] is not None for r in self.rows), 8)


class TestSourceColumn(unittest.TestCase):
    def test_bill_row_records_its_source(self):
        row = ingest.bill_row("acct", "2026-01-16", 100.0, source="email")
        self.assertEqual(row[ingest.SOURCE_FIELD], "email")

    def test_source_defaults_to_blank_not_missing(self):
        self.assertEqual(ingest.bill_row("acct", "2026-01-16", 100.0)[ingest.SOURCE_FIELD], "")


if __name__ == "__main__":
    unittest.main()
