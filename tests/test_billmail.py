"""Tests for the bill-ready email parser and the history backfill."""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from metermatters.analytics import analysis
from metermatters.intake import backfill
from metermatters.intake import billmail
from metermatters.intake import greenbutton
from metermatters.intake import ingest

HERE = Path(__file__).resolve().parent
# Invented fixtures with the real files' shape - see fixtures/synthetic/generate.py
# for where every number in them comes from.
SYNTHETIC = HERE / "fixtures" / "synthetic"
SYNTHETIC_SAMPLE = SYNTHETIC / "bill_ready.eml"
SYNTHETIC_13 = SYNTHETIC / "account_history_13.csv"
SYNTHETIC_USAGE = SYNTHETIC / "usage"

# The fields we depend on live only in the HTML part; plain text omits the date.
HTML_MAIL = """From: Example Utility <noreply@notices.example-utility.com>
Subject: New Electric Bill from Example Utility
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


# Invented, in the shape other utilities' notices take: different labels,
# spelled-out dates, the account number in full, the fuel said only in the body.
NATIONAL_GRID_STYLE = """From: National Grid <noreply@nationalgridus.com>
Subject: Your National Grid bill is ready
Date: Mon, 20 Oct 2026 14:02:11 +0000
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><body><p>Hello,</p><p>Your electric statement for account 1234567890 is ready to view.</p>
<table><tr><td>Amount Due</td><td>$187.43</td></tr>
<tr><td>Payment Due Date</td><td>November 12, 2026</td></tr></table>
<p><a href="https://example.com/pay?t=abc">View and pay your bill</a></p></body></html>
"""

WATER_DISTRICT_STYLE = """From: Acton Water District <billing@actonwater.example>
Subject: Your Acton Water District payment is due
Date: Sat, 1 Nov 2026 12:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Dear customer,

A payment of $85.00 is due on 11/15/2026 for account ending in 4321.
Pay online at https://example.com/water or by mail.
"""

RECEIPT = """From: National Grid <noreply@nationalgridus.com>
Subject: Thank you for your payment
Date: Mon, 20 Oct 2026 14:02:11 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

We received your payment of $187.43 on 10/20/2026. Thank you.
"""

PORTAL_ONLY = """From: Some Utility <noreply@utility.example>
Subject: Your electric bill is ready
Date: Mon, 20 Oct 2026 14:02:11 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Your new bill is ready. Sign in at https://utility.example/account to view it.
"""


class TestAnyUtility(unittest.TestCase):
    """The reader keeps no list of utilities. Issue #1."""

    def parse(self, mail: str, **kw):
        import email
        from email import policy
        return billmail.parse_message(email.message_from_string(mail, policy=policy.default), **kw)

    def test_another_utilitys_labels_and_dates_are_read(self):
        notice = self.parse(NATIONAL_GRID_STYLE)
        self.assertAlmostEqual(notice.amount, 187.43)
        self.assertEqual(notice.due, date(2026, 11, 12))
        self.assertEqual(notice.fuel, "electric")          # said in the body, not the subject
        self.assertEqual(notice.account_suffix, "7890")    # the last four of the full number

    def test_a_water_districts_reminder_is_a_water_bill(self):
        notice = self.parse(WATER_DISTRICT_STYLE, sender="billing@actonwater.example")
        self.assertAlmostEqual(notice.amount, 85.00)
        self.assertEqual(notice.due, date(2026, 11, 15))
        self.assertEqual(notice.fuel, "water")
        self.assertEqual(notice.account_suffix, "4321")

    def test_a_receipt_is_not_a_bill(self):
        with self.assertRaises(billmail.ParseError):
            self.parse(RECEIPT)

    def test_a_notice_that_only_links_to_a_website_is_not_readable(self):
        with self.assertRaises(billmail.ParseError):
            self.parse(PORTAL_ONLY)

    def test_the_fuel_comes_from_the_subject_first_then_the_text(self):
        self.assertEqual(billmail.fuel_of("Your water and sewer bill"), "water")
        self.assertEqual(billmail.fuel_in("Natural gas service. 42 therms used."), "gas")
        self.assertEqual(billmail.fuel_in("Your statement is ready."), "unknown")


class TestAskingTheModel(unittest.TestCase):
    """When the patterns can't read a notice, Claude is asked - if the server
    has a key. The model is faked here; what is sent and what is believed are real."""

    def parse(self, mail: str, **kw):
        import email
        from email import policy
        return billmail.parse_message(email.message_from_string(mail, policy=policy.default), **kw)

    def setUp(self):
        import os
        from unittest import mock
        patch = mock.patch.dict(os.environ, {"ELEC_ANTHROPIC_KEY": "k"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_without_a_key_the_model_is_never_asked(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"ELEC_ANTHROPIC_KEY": ""}), \
                mock.patch.object(billmail, "ask_model") as ask:
            with self.assertRaises(billmail.ParseError):
                self.parse(PORTAL_ONLY)
        ask.assert_not_called()

    def test_what_the_patterns_read_is_not_sent(self):
        from unittest import mock
        with mock.patch.object(billmail, "ask_model") as ask:
            self.parse(NATIONAL_GRID_STYLE)
        ask.assert_not_called()

    def test_the_models_reading_fills_in_what_the_patterns_missed(self):
        from unittest import mock
        answer = {"is_bill": True, "fuel": "gas", "amount_due": 61.2, "due_date": "2026-11-10",
                  "bill_date": "2026-10-20", "account_last4": "0042", "provider": "Some Utility"}
        unusual = PORTAL_ONLY.replace("Your new bill is ready.",
                                      "New charges this month come to sixty-one dollars and twenty cents, "
                                      "payable by the tenth of November.")
        with mock.patch.object(billmail, "ask_model", return_value=answer) as ask:
            notice = self.parse(unusual.replace("electric bill", "statement"))
        self.assertEqual((notice.amount, notice.due, notice.fuel, notice.account_suffix),
                         (61.2, date(2026, 11, 10), "gas", "0042"))
        sent = ask.call_args[0][0]
        self.assertIn("Subject: Your statement is ready", sent)
        self.assertNotIn("https://", sent)                 # links are cut before sending

    def test_the_model_is_asked_the_fuel_when_the_text_does_not_say(self):
        from unittest import mock
        answer = {"is_bill": True, "fuel": "water", "amount_due": 187.43, "due_date": "2026-11-12",
                  "bill_date": None, "account_last4": None, "provider": "x"}
        mail = NATIONAL_GRID_STYLE.replace("electric statement", "statement")
        with mock.patch.object(billmail, "ask_model", return_value=answer):
            self.assertEqual(self.parse(mail).fuel, "water")

    def test_what_the_model_calls_not_a_bill_is_not_filed(self):
        from unittest import mock
        answer = {"is_bill": False, "fuel": "unknown", "amount_due": None, "due_date": None,
                  "bill_date": None, "account_last4": None, "provider": None}
        with mock.patch.object(billmail, "ask_model", return_value=answer):
            with self.assertRaises(billmail.ParseError):
                self.parse(PORTAL_ONLY)

    def test_a_model_answer_without_both_figures_is_not_believed(self):
        from unittest import mock
        for partial in ({"amount_due": None, "due_date": "2026-11-10"},
                        {"amount_due": 10.0, "due_date": None},
                        {"amount_due": 10.0, "due_date": "soon"},
                        {"amount_due": -5.0, "due_date": "2026-11-10"}):
            answer = {"is_bill": True, "fuel": "gas", "bill_date": None, "account_last4": None,
                      "provider": None, **partial}
            with self.subTest(partial), mock.patch.object(billmail, "ask_model", return_value=answer):
                with self.assertRaises(billmail.ParseError):
                    self.parse(PORTAL_ONLY)

    def test_a_model_that_is_down_leaves_the_bill_unread_not_the_intake_broken(self):
        from unittest import mock
        with mock.patch.object(billmail, "ask_model", return_value=None):
            with self.assertRaises(billmail.ParseError):
                self.parse(PORTAL_ONLY)

    def test_what_is_sent_has_no_links_addresses_or_long_numbers(self):
        text = ("Account 1234567890 at https://x.example/a?b=c. Write to me@example.com. "
                "Due 10/16/2026, bill 2026-10-01, $1,234.56. Phone 617-555-0100.")
        sent = billmail.for_model(text)
        self.assertNotIn("1234567890", sent)
        self.assertIn("...7890", sent)
        self.assertNotIn("x.example", sent)
        self.assertNotIn("me@example.com", sent)
        self.assertIn("10/16/2026", sent)
        self.assertIn("$1,234.56", sent)
        self.assertNotIn("617-555-0100", sent)

    def test_the_request_to_the_model_is_well_formed(self):
        # The SDK is faked at the client: the request it would send is checked.
        import json
        from types import SimpleNamespace
        from unittest import mock
        import anthropic
        reply = SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="text", text=json.dumps({"is_bill": True, "fuel": "gas"}))])
        with mock.patch.object(anthropic, "Anthropic") as client:
            client.return_value.messages.create.return_value = reply
            self.assertEqual(billmail.ask_model("hello"), {"is_bill": True, "fuel": "gas"})
        client.assert_called_once_with(api_key="k", timeout=billmail.MODEL_TIMEOUT, max_retries=1)
        kw = client.return_value.messages.create.call_args.kwargs
        self.assertEqual(kw["model"], billmail.DEFAULT_MODEL)
        self.assertEqual(kw["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(kw["output_config"]["format"]["schema"], billmail.MODEL_SCHEMA)
        # A refusal, a bad answer or an API error is None, never an exception.
        with mock.patch.object(anthropic, "Anthropic") as client:
            client.return_value.messages.create.return_value = SimpleNamespace(
                stop_reason="refusal", content=[])
            self.assertIsNone(billmail.ask_model("hello"))
            client.return_value.messages.create.side_effect = anthropic.APIConnectionError(
                request=mock.Mock())
            self.assertIsNone(billmail.ask_model("hello"))


class TestHostileMail(unittest.TestCase):
    """What a parser fed by strangers' email has to shrug off."""

    def parse(self, mail: str):
        import email
        from email import policy
        return billmail.parse_message(email.message_from_string(mail, policy=policy.default))

    def test_amounts_are_dollars_and_cents_or_nothing(self):
        self.assertEqual(self.parse(HTML_MAIL.replace("$1,248.26", "$0.00")).amount, 0.0)
        for bad in ("$1,248", "$-12.34", "$1.234.56"):
            with self.subTest(bad), self.assertRaises(billmail.ParseError):
                self.parse(HTML_MAIL.replace("$1,248.26", bad))

    def test_impossible_due_dates_are_refused(self):
        for bad in ("02/30/2026", "13/01/2026"):
            with self.subTest(bad), self.assertRaises(billmail.ParseError):
                self.parse(HTML_MAIL.replace("10/16/2026", bad))
        # Another utility's way of writing the same day is still that day.
        self.assertEqual(self.parse(HTML_MAIL.replace("10/16/2026", "10-16-2026")).due,
                         date(2026, 10, 16))

    def test_a_due_date_years_from_the_message_is_refused(self):
        # A typo'd or forged year would file the bill far outside every trend.
        for bad in ("10/16/0026", "10/16/2062", "10/16/2023"):
            with self.subTest(bad), self.assertRaisesRegex(billmail.ParseError, "years"):
                self.parse(HTML_MAIL.replace("10/16/2026", bad))
        self.assertEqual(self.parse(HTML_MAIL.replace("10/16/2026", "10/16/2027")).due,
                         date(2027, 10, 16))

    def test_without_a_date_header_today_is_the_yardstick(self):
        from datetime import timedelta
        undated = HTML_MAIL.replace("Date: Tue, 22 Sep 2026 19:55:26 +0000\n", "")
        self.assertNotIn("Date:", undated.split("\n\n")[0])
        soon = (date.today() + timedelta(days=30)).strftime("%m/%d/%Y")
        self.assertEqual(self.parse(undated.replace("10/16/2026", soon)).due,
                         date.today() + timedelta(days=30))
        far = (date.today() + timedelta(days=3 * 365)).strftime("%m/%d/%Y")
        with self.assertRaisesRegex(billmail.ParseError, "years"):
            self.parse(undated.replace("10/16/2026", far))

    def test_a_subject_naming_both_fuels_is_electric(self):
        # The tie-break, pinned so that it cannot drift unnoticed.
        self.assertEqual(billmail.fuel_of("Your Gas and Electric bills"), "electric")
        self.assertEqual(billmail.fuel_of("Electric and gas"), "electric")

    def test_a_message_with_nothing_readable_says_so(self):
        from email.message import EmailMessage
        only_attachment = EmailMessage()
        only_attachment["Subject"] = "New Electric Bill from Example Utility"
        only_attachment.add_attachment(b"Total Amount Due: $1.00 Due date: 10/16/2026",
                                       maintype="application", subtype="pdf", filename="bill.pdf")
        no_boundary = ("Subject: New Electric Bill from Example Utility\n"
                       "Content-Type: multipart/alternative\n\n"
                       "Total Amount Due: $1.00 Due date: 10/16/2026\n")
        with self.assertRaisesRegex(billmail.ParseError, "no readable body"):
            billmail.parse_message(only_attachment)
        with self.assertRaisesRegex(billmail.ParseError, "no readable body"):
            self.parse(no_boundary)

    def test_an_unknown_charset_is_unreadable_not_a_crash(self):
        # Python raises LookupError for it, which is not a ValueError and
        # would otherwise come out of the inbound endpoint as a 500.
        with self.assertRaisesRegex(billmail.ParseError, "no readable body"):
            self.parse(HTML_MAIL.replace('charset="utf-8"', 'charset="x-bogus"'))


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


if __name__ == "__main__":
    unittest.main()
