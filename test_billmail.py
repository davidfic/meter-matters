"""Tests for the bill-ready email parser and the history backfill."""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

import analysis
import backfill
import billmail
import greenbutton
import ingest

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "fixtures" / "bill_ready.eml"
FIXTURE_13 = HERE / "fixtures" / "account_history_13.csv"
USAGE_DIR = HERE / "usage"

# Everything above is personal and never committed. These have the same shape
# and invented contents, and are what a clean checkout is tested against - see
# fixtures/synthetic/generate.py for where every number in them comes from.
SYNTHETIC = HERE / "fixtures" / "synthetic"
SYNTHETIC_SAMPLE = SYNTHETIC / "bill_ready.eml"
SYNTHETIC_13 = SYNTHETIC / "account_history_13.csv"
SYNTHETIC_USAGE = SYNTHETIC / "usage"

# The fields we depend on live only in the HTML part; plain text omits the date.
HTML_MAIL = """From: Eversource Do Not Reply <noreply@notifications.eversource.com>
Subject: New Electric Bill from Eversource
Date: Tue, 22 Sep 2026 19:55:26 +0000
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><body><table><tr><td>Account Number: ******0001</td></tr>
<tr><td>Bill date: 09/21/2026</td></tr><tr><td>Due date: 10/16/2026</td></tr>
<tr><td>Total&nbsp;Amount Due: $1,248.26</td></tr></table></body></html>
"""


class TestParsing(unittest.TestCase):
    def test_fields_come_out_of_the_html(self):
        notice = billmail.parse_message(
            __import__("email").message_from_string(
                HTML_MAIL, policy=__import__("email").policy.default))
        self.assertAlmostEqual(notice.amount, 1248.26)   # comma survives
        self.assertEqual(notice.due, date(2026, 10, 16))
        self.assertEqual(notice.bill_date, date(2026, 9, 21))
        self.assertEqual(notice.account_suffix, "0001")

    @unittest.skipIf(not SAMPLE.exists(), "sample email not present")
    def test_the_real_email(self):
        notice = billmail.parse_file(SAMPLE)
        self.assertAlmostEqual(notice.amount, 248.26)
        self.assertEqual(notice.due, date(2026, 10, 16))
        self.assertRegex(notice.account_suffix, r"^\d{4}$")
        self.assertEqual(notice.received, date(2026, 9, 22))

    def test_a_whole_message_as_it_arrives(self):
        # Two parts, quoted-printable, Latin-1 markup, nested tables: what a
        # mailbox actually holds, which the inline sample above is not.
        notice = billmail.parse_file(SYNTHETIC_SAMPLE)
        self.assertAlmostEqual(notice.amount, 211.19)
        self.assertEqual(notice.due, date(2026, 10, 16))
        self.assertEqual(notice.bill_date, date(2026, 9, 20))
        self.assertEqual(notice.account_suffix, "0001")
        self.assertEqual(notice.received, date(2026, 9, 21))
        self.assertEqual(notice.fuel, "electric")
        self.assertEqual(notice.message_id, "<synthetic-bill-ready-0001@mail-vendor.example>")

    def test_the_plain_part_alone_is_not_enough(self):
        # It names the amount and no due date, so it must never be filed.
        from email import message_from_bytes, policy
        from email.message import EmailMessage
        whole = message_from_bytes(SYNTHETIC_SAMPLE.read_bytes(), policy=policy.default)
        plain = whole.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("total amount due of $211.19", " ".join(plain.split()))
        only = EmailMessage()
        only["Subject"] = whole["Subject"]
        only.set_content(plain)
        with self.assertRaises(billmail.ParseError):
            billmail.parse_message(only)

    def test_visible_text_strips_markup_and_entities(self):
        self.assertEqual(
            billmail.visible_text("<p>a&nbsp;<b>b</b></p><style>x{}</style>"),
            "a b")

    def test_a_missing_amount_raises(self):
        mail = HTML_MAIL.replace("Total&nbsp;Amount Due: $1,248.26", "nothing here")
        with self.assertRaises(billmail.ParseError):
            billmail.parse_message(__import__("email").message_from_string(
                mail, policy=__import__("email").policy.default))

    def test_a_missing_due_date_raises_rather_than_guessing(self):
        # Guessing would file the statement in the wrong month and corrupt
        # every trend silently - so this must be loud.
        mail = HTML_MAIL.replace("Due date: 10/16/2026", "")
        with self.assertRaises(billmail.ParseError):
            billmail.parse_message(__import__("email").message_from_string(
                mail, policy=__import__("email").policy.default))


class TestBackfillSynthetic(unittest.TestCase):
    """13 statements on file, 26 periods in the export: 13 to fill in."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.csv = self.dir / "AccountHistory.csv"
        shutil.copy(SYNTHETIC_13, self.csv)
        self.addCleanup(shutil.rmtree, self.dir)

    def run_backfill(self, *extra):
        return backfill.main(["--csv", str(self.csv),
                              "--usage-dir", str(SYNTHETIC_USAGE), *extra])

    def test_dry_run_changes_nothing(self):
        before = self.csv.read_text(encoding="utf-8")
        self.assertEqual(self.run_backfill("--dry-run"), 0)
        self.assertEqual(self.csv.read_text(encoding="utf-8"), before)

    def test_backfill_extends_history_and_is_idempotent(self):
        self.assertEqual(self.run_backfill(), 0)
        bills = analysis.build_bills(analysis.load_entries(self.csv))
        self.assertEqual(len(bills), 26)
        self.assertEqual((bills[0].period, bills[-1].period), ("2024-09", "2026-10"))
        # Running again must find nothing left to do.
        self.assertEqual(self.run_backfill(), 2)
        self.assertEqual(len(analysis.build_bills(analysis.load_entries(self.csv))), 26)

    def test_the_account_is_the_number_not_the_digits_of_its_nickname(self):
        # The nickname is "demo42". Pulling digits out of the whole cell would
        # look for account 5550001000142 and find no periods at all.
        self.assertEqual(self.run_backfill(), 0)

    def test_gas_periods_are_never_filed_as_electric(self):
        self.run_backfill()
        rows, _ = ingest.read_rows(self.csv)
        self.assertEqual({r["Account Type"] for r in rows}, {"Electric"})

    def test_synthesised_rows_are_labelled(self):
        self.run_backfill()
        rows, _ = ingest.read_rows(self.csv)
        marked = [r for r in rows if r.get("Status") == backfill.BACKFILL_STATUS]
        # Both the statement and its payment are marked, so a later import
        # carrying the real due date can tell them apart from real rows.
        self.assertEqual(len(marked), 26)
        kinds = {r["Type"] for r in marked}
        self.assertEqual(kinds, {"Current Bill", "Payment"})
        self.assertEqual({r[ingest.SOURCE_FIELD] for r in marked}, {"usage-export"})

    def test_the_estimated_due_date_uses_the_observed_lag(self):
        # The 13 real pairs lag 27 to 29 days; the median is 28. The oldest
        # period closed 2024-08-19, so its statement is put at 2024-09-16.
        self.run_backfill()
        oldest = analysis.build_bills(analysis.load_entries(self.csv))[0]
        self.assertEqual(oldest.due, date(2024, 9, 16))
        self.assertAlmostEqual(oldest.amount, 250.43, places=2)        # 10.00 + 815 x 0.295

    def test_real_statements_are_left_untouched(self):
        original = analysis.build_bills(analysis.load_entries(SYNTHETIC_13))
        self.run_backfill()
        after = {b.period: b for b in analysis.build_bills(analysis.load_entries(self.csv))}
        for bill in original:
            self.assertAlmostEqual(after[bill.period].amount, bill.amount, places=2)
            self.assertEqual(after[bill.period].due, bill.due)

    def test_backfill_unlocks_year_over_year(self):
        self.run_backfill()
        bills = analysis.build_bills(analysis.load_entries(self.csv))
        periods = greenbutton.load_dir(SYNTHETIC_USAGE).periods
        report = analysis.analyse(bills, {"baseline_months": 6, "target_pct": 5}, periods)
        self.assertEqual(len(report["yoy"]), 14)                       # 26 months, less 12
        self.assertEqual(report["usage_matched"], 26)


@unittest.skipUnless(FIXTURE_13.exists(), "the personal export is not in this checkout")
class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.csv = self.dir / "AccountHistory.csv"
        shutil.copy(FIXTURE_13, self.csv)
        self.addCleanup(shutil.rmtree, self.dir)

    def run_backfill(self, *extra):
        return backfill.main(["--csv", str(self.csv),
                              "--usage-dir", str(USAGE_DIR), *extra])

    @unittest.skipIf(not USAGE_DIR.exists(), "usage exports not present")
    def test_dry_run_changes_nothing(self):
        before = self.csv.read_text(encoding="utf-8")
        self.run_backfill("--dry-run")
        self.assertEqual(self.csv.read_text(encoding="utf-8"), before)

    @unittest.skipIf(not USAGE_DIR.exists(), "usage exports not present")
    def test_backfill_extends_history_and_is_idempotent(self):
        self.assertEqual(self.run_backfill(), 0)
        bills = analysis.build_bills(analysis.load_entries(self.csv))
        self.assertEqual(len(bills), 28)
        # Running again must find nothing left to do.
        self.assertEqual(self.run_backfill(), 2)
        self.assertEqual(len(analysis.build_bills(analysis.load_entries(self.csv))), 28)

    @unittest.skipIf(not USAGE_DIR.exists(), "usage exports not present")
    def test_synthesised_rows_are_labelled(self):
        self.run_backfill()
        rows, _ = ingest.read_rows(self.csv)
        marked = [r for r in rows if r.get("Status") == backfill.BACKFILL_STATUS]
        # Both the statement and its payment are marked, so a later import
        # carrying the real due date can tell them apart from real rows.
        self.assertEqual(len(marked), 30)
        kinds = {r["Type"] for r in marked}
        self.assertEqual(kinds, {"Current Bill", "Payment"})

    @unittest.skipIf(not USAGE_DIR.exists(), "usage exports not present")
    def test_real_statements_are_left_untouched(self):
        original = analysis.build_bills(analysis.load_entries(FIXTURE_13))
        self.run_backfill()
        after = {b.period: b for b in analysis.build_bills(analysis.load_entries(self.csv))}
        for bill in original:
            self.assertAlmostEqual(after[bill.period].amount, bill.amount, places=2)
            self.assertEqual(after[bill.period].due, bill.due)

    @unittest.skipIf(not USAGE_DIR.exists(), "usage exports not present")
    def test_backfill_unlocks_year_over_year(self):
        self.run_backfill()
        bills = analysis.build_bills(analysis.load_entries(self.csv))
        periods = greenbutton.load_dir(USAGE_DIR).periods
        report = analysis.analyse(bills, {"baseline_months": 6, "target_pct": 5}, periods)
        self.assertEqual(len(report["yoy"]), 16)
        self.assertEqual(report["usage_matched"], 28)


if __name__ == "__main__":
    unittest.main()
