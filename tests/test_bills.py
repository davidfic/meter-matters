"""Tests for how statements, usage and backfill fit together - across fuels, utilities and files."""

import io
import json
import unittest
import zipfile
from datetime import date
from pathlib import Path
import tempfile

from metermatters.analytics import analysis
from metermatters.intake import backfill
from metermatters.intake import billmail
from metermatters.intake import greenbutton
from metermatters.intake import importers
from metermatters.intake import ingest
from tests.test_importers import daily, bills as bill_csv, WebCase

HEAD = "Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,Source\n"


def row(account, fuel, due, amount, kind="Current Bill", status="-"):
    return f"{account},{fuel},{due},{kind},${amount:.2f},{status},\n"


def entries(text):
    return analysis.build_bills(analysis.read_entries(HEAD + text))


class TestKeyingBills(unittest.TestCase):
    def test_electric_and_gas_on_one_account_are_both_kept(self):
        got = entries(row("123", "Electric", "2026/03/16", 200) + row("123", "Gas", "2026/03/18", 50))
        self.assertEqual(sorted((b.fuel, b.amount) for b in got), [("Electric", 200.0), ("Gas", 50.0)])

    def test_two_statements_due_in_one_month_are_both_kept(self):
        got = entries(row("123", "Electric", "2026/03/02", 200) + row("123", "Electric", "2026/03/31", 210)
                      + row("123", "Electric", "2026/04/30", 220))
        self.assertEqual([(b.period, b.amount) for b in got],
                         [("2026-02", 200.0), ("2026-03", 210.0), ("2026-04", 220.0)])

    def test_the_same_statement_seen_twice_is_one_bill(self):
        got = entries(row("123", "Electric", "2026/03/16", 200) + row("123", "Electric", "2026/03/17", 200))
        self.assertEqual([(b.period, b.due) for b in got], [("2026-03", date(2026, 3, 17))])

    def test_the_month_before_is_only_borrowed_when_free(self):
        got = entries(row("123", "Electric", "2026/02/20", 190) + row("123", "Electric", "2026/03/02", 200)
                      + row("123", "Electric", "2026/03/31", 210))
        self.assertEqual([b.amount for b in got], [190.0, 210.0])     # the 03/02 one had nowhere to go


def period(start, end, kwh, cost, unit="kWh", priced=True):
    return greenbutton.Period("123", start, end, kwh, cost, unit=unit, priced=priced)


class TestAttachingUsage(unittest.TestCase):
    def bills(self, *amounts_and_due):
        return entries("".join(row("123", "Electric", due, amt) for amt, due in amounts_and_due))

    def test_an_exact_amount_match_is_verified(self):
        bills = self.bills((100.0, "2026/03/16"))
        p = period(date(2026, 1, 20), date(2026, 2, 18), 500, 100.0)
        self.assertEqual(analysis.attach_usage(bills, [p]), 1)
        self.assertTrue(bills[0].usage_exact)
        self.assertTrue(analysis.monthly_series(bills)[0]["verified"])

    def test_amounts_that_differ_still_pair_by_dates_when_there_is_only_one_way(self):
        bills = self.bills((112.5, "2026/03/16"))                 # taxes the export leaves out
        p = period(date(2026, 1, 20), date(2026, 2, 18), 500, 100.0)
        self.assertEqual(analysis.attach_usage(bills, [p]), 1)
        self.assertFalse(bills[0].usage_exact)
        self.assertFalse(analysis.monthly_series(bills)[0]["verified"])
        self.assertEqual(analysis.monthly_series(bills)[0]["kwh"], 500.0)

    def test_an_ambiguous_pairing_is_left_alone(self):
        bills = self.bills((112.5, "2026/03/16"), (113.0, "2026/03/30"))
        p = period(date(2026, 1, 20), date(2026, 2, 18), 500, 100.0)
        self.assertEqual(analysis.attach_usage(bills, [p]), 0)

    def test_usage_without_a_cost_pairs_by_dates_and_is_never_priced(self):
        bills = self.bills((112.5, "2026/03/16"))
        p = period(date(2026, 1, 20), date(2026, 2, 18), 500, 0.0, priced=False)
        self.assertEqual(analysis.attach_usage(bills, [p]), 1)
        self.assertIsNone(p.rate)
        self.assertEqual(analysis.charge_discrepancies(bills, [p]), [])

    def test_a_date_pairing_with_a_different_charge_is_the_discrepancy(self):
        bills = self.bills((112.5, "2026/03/16"))
        p = period(date(2026, 1, 20), date(2026, 2, 18), 500, 100.0)
        analysis.attach_usage(bills, [p])
        found, = analysis.charge_discrepancies(bills, [p])
        self.assertEqual((found["charge"], found["billed"]), (100.0, 112.5))


class TestUnpricedFiles(unittest.TestCase):
    def test_a_blank_cost_is_read_as_unpriced_not_free(self):
        text = ("Account Number,123\n\nTYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES\n"
                "x,2026-01-01,2026-01-31,500,,no cost in file\n")
        p, = greenbutton.parse(text).periods
        self.assertEqual((p.cost, p.priced, p.rate), (0.0, False, None))

    def test_the_importer_writes_no_cost_as_blank(self):
        text = importers.read(daily(cost=False), "x.csv").text
        self.assertFalse(all(p.priced for p in greenbutton.parse(text).periods))


class TestRetiringBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv, self.usage = self.tmp / "AccountHistory.csv", self.tmp / "usage"
        self.usage.mkdir()
        (self.usage / "x.csv").write_text(importers.read(daily(end=date(2024, 6, 30)), "x.csv").text)
        self.made = backfill.fill_missing(self.csv, self.usage, today=date(2026, 10, 10))

    def bills(self):
        return analysis.build_bills(analysis.load_entries(self.csv))

    def test_bills_are_made_from_usage_when_there_are_none(self):
        self.assertEqual(self.made, 6)
        self.assertTrue(all(b.status == "BACKFILL" for b in self.bills()))

    def test_a_real_bill_for_the_same_month_replaces_the_estimate(self):
        march = next(b for b in self.bills() if b.period == "2024-03")
        real = ingest.bill_row("9876543210", (march.due.replace(day=5)).isoformat(), 140.0, status="-")
        ingest.merge(self.csv, [real], list(real))
        bills = self.bills()
        self.assertEqual(len(bills), 6)                             # not seven
        self.assertEqual([b.status for b in bills if b.period == "2024-03"], ["OPEN"])
        self.assertNotIn("BACKFILL", [b.status for b in bills if b.period == "2024-03"])

    def test_a_real_bill_for_another_month_retires_nothing(self):
        real = ingest.bill_row("9876543210", "2024-09-15", 140.0, status="-")
        ingest.merge(self.csv, [real], list(real))
        self.assertEqual(len(self.bills()), 7)

    def test_filling_is_idempotent(self):
        self.assertEqual(backfill.fill_missing(self.csv, self.usage, today=date(2026, 10, 10)), 0)


class TestEmailFiling(unittest.TestCase):
    def test_a_notice_with_no_account_goes_to_its_own_fuels_account(self):
        tmp = Path(tempfile.mkdtemp())
        csv, usage = tmp / "AccountHistory.csv", tmp / "usage"
        csv.write_text(HEAD + row("111 (elec)", "Electric", "2026/02/16", 200)
                       + row("222 (gas)", "Gas", "2026/02/16", 50))
        gas = billmail.BillNotice(amount=60.0, due=date(2026, 3, 16), fuel="gas")
        billmail.file_notices([gas], csv, usage)
        bills = analysis.build_bills(analysis.load_entries(csv))
        self.assertEqual([(b.account, b.amount) for b in bills if b.fuel == "Gas"],
                         [("222", 50.0), ("222", 60.0)])
        self.assertEqual([b.amount for b in bills if b.fuel == "Electric"], [200.0])

    def test_with_nothing_on_file_the_notice_is_still_filed(self):
        tmp = Path(tempfile.mkdtemp())
        csv, usage = tmp / "AccountHistory.csv", tmp / "usage"
        gas = billmail.BillNotice(amount=60.0, due=date(2026, 3, 16), fuel="gas")
        self.assertEqual(billmail.file_notices([gas], csv, usage).added, 1)
        self.assertIn("Gas (email)", csv.read_text())


class TestZipsFromOtherPortals(unittest.TestCase):
    def test_a_zip_holding_an_unknown_csv_is_read(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("readme.txt", "hello")
            z.writestr("Usage.csv", daily())
        r = importers.read(buffer.getvalue(), "download.zip")
        self.assertEqual((r.kind, r.periods), ("usage", 14))

    def test_a_zip_with_nothing_readable_says_so(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("photo.jpg", b"\xff\xd8")
        with self.assertRaisesRegex(importers.ImportFailed, "no CSV or XML"):
            importers.read(buffer.getvalue(), "x.zip")

    def test_credits_are_kept_and_said(self):
        text = bill_csv(rows=3).decode() + "\n2024-04-01,2024-04-21,-$12.00,Electric\n"
        r = importers.read(text.encode(), "b.csv")
        self.assertEqual(r.periods, 4)
        self.assertTrue(any("CREDIT" in n for n in r.notes))


class TestManualEntryIsAllOrNothing(WebCase):
    def test_a_bad_usage_half_writes_no_bill(self):
        self.client.post("/bills/add", data={"fuel": "Electric", "due": "2026-10-16", "amount": "150",
                                             "used": "5", "unit": "therms", "start": "2026-08-20",
                                             "end": "2026-09-18"})
        self.assertFalse(self.user.paths.csv.exists())

    def test_a_list_for_the_account_setting_cannot_break_the_dashboard(self):
        self.upload("export.csv", daily())
        self.client.post("/api/settings", json={"account": ["x"]})
        self.assertEqual(self.client.get("/api/report?fuel=Electric").status_code, 200)


if __name__ == "__main__":
    unittest.main()
