"""Tests for the importer - the seam every automated route depends on."""

import shutil
import tempfile
import unittest
from pathlib import Path

from metermatters.intake import ingest
from metermatters.intake import import_bill

EXPORT = """Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,
55500010001&lt;br&gt;(electric10),Electric,2026/03/20,Current Bill,$300.00,-
55500010001&lt;br&gt;(electric10),Electric,2026/02/16,Current Bill,$400.00,-
"""

NEWER = """Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,
55500010001&lt;br&gt;(electric10),Electric,2026/04/17,Current Bill,$250.00,-
55500010001&lt;br&gt;(electric10),Electric,2026/03/20,Current Bill,$300.00,-
"""


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.csv = self.dir / "AccountHistory.csv"
        self.csv.write_text(EXPORT, encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.dir)

    def test_overlapping_export_only_adds_the_new_row(self):
        result = ingest.merge_csv_text(self.csv, NEWER)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.total, 3)

    def test_reimporting_is_a_no_op(self):
        first = ingest.merge_csv_text(self.csv, NEWER)
        again = ingest.merge_csv_text(self.csv, NEWER)
        self.assertEqual(first.added, 1)
        self.assertEqual(again.added, 0)
        self.assertFalse(again.changed)

    def test_a_short_export_never_truncates_history(self):
        short = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                 "55500010001&lt;br&gt;(electric10),Electric,2026/04/17,Current Bill,$250.00,-\n")
        ingest.merge_csv_text(self.csv, short)
        rows, _ = ingest.read_rows(self.csv)
        self.assertEqual(len(rows), 3)  # the two originals survive

    def test_dry_run_writes_nothing(self):
        before = self.csv.read_text(encoding="utf-8")
        result = ingest.merge_csv_text(self.csv, NEWER, dry_run=True)
        self.assertEqual(result.added, 1)
        self.assertEqual(self.csv.read_text(encoding="utf-8"), before)

    def test_rows_are_written_newest_first(self):
        ingest.merge_csv_text(self.csv, NEWER)
        rows, _ = ingest.read_rows(self.csv)
        dates = [r["Pay/Due Date"] for r in rows]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_junk_csv_is_rejected(self):
        with self.assertRaises(ValueError):
            ingest.merge_csv_text(self.csv, "just,some,junk\n1,2,3\n")

    def test_empty_csv_is_rejected(self):
        with self.assertRaises(ValueError):
            ingest.merge_csv_text(self.csv, "a,b,c\n")

    def test_bill_row_matches_the_export_format(self):
        row = ingest.bill_row("55500010001&lt;br&gt;(electric10)", "2026-11-16", 232.1)
        self.assertEqual(row["Pay/Due Date"], "2026/11/16")
        self.assertEqual(row["Amount"], "$232.10")
        self.assertEqual(row["Type"], "Current Bill")

    def test_bill_row_rejects_a_bad_date(self):
        with self.assertRaises(ValueError):
            ingest.bill_row("acct", "not-a-date", 10.0)

    def test_default_account_comes_from_the_file(self):
        self.assertIn("55500010001", ingest.default_account(self.csv))

    def test_merge_creates_the_file_when_absent(self):
        fresh = self.dir / "new.csv"
        row = ingest.bill_row("acct", "2026-01-16", 100.0)
        result = ingest.merge(fresh, [row], list(row))
        self.assertEqual(result.added, 1)
        self.assertTrue(fresh.exists())


class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.csv = self.dir / "AccountHistory.csv"
        self.csv.write_text(EXPORT, encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.dir)

    def run_cli(self, *args):
        return import_bill.main([*args, "--csv", str(self.csv), "--quiet"])

    def test_exit_codes_distinguish_added_from_nothing_new(self):
        self.assertEqual(self.run_cli("--amount", "250", "--due", "2026-04-17"),
                         import_bill.EXIT_OK)
        self.assertEqual(self.run_cli("--amount", "250", "--due", "2026-04-17"),
                         import_bill.EXIT_NOTHING_NEW)

    def test_missing_due_date_is_an_error(self):
        self.assertEqual(self.run_cli("--amount", "250"), import_bill.EXIT_ERROR)

    def test_bad_date_is_an_error_not_a_crash(self):
        self.assertEqual(self.run_cli("--amount", "250", "--due", "whenever"),
                         import_bill.EXIT_ERROR)

    def test_dry_run_leaves_the_file_alone(self):
        before = self.csv.read_text(encoding="utf-8")
        self.run_cli("--amount", "250", "--due", "2026-04-17", "--dry-run")
        self.assertEqual(self.csv.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
