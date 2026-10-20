"""What a file loses on its way in: the name, the street, the account in full."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from metermatters.intake import greenbutton
from metermatters.intake import ingest
from metermatters.intake import redact
from metermatters.store import accounts
from metermatters import region
from metermatters.analytics import weather

BILLING = """Name,Pat Sample
Address,"12 Placeholder Ln, EXAMPLETON MA 01000"
Account Number,55500010001
Service,555000100010000000001

TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES
Electric billing,2026-08-21,2026-09-17,714.00,$248.26,
Electric billing,2026-07-22,2026-08-20,801.00,$271.03,
"""

HISTORY = """12 Placeholder Ln, Exampleton MA 01000
Data from 11/17/2023 to 7/18/2024

Account,Read Date,Usage,Number of Days,Usage per day,Charge,Read Type,Average Temperature
55500020002,10/19/2023,741.00,28,26.46,$234.96,Actual Read,58.0
55500020002,9/21/2023,642.00,31,20.71,$204.89,Estimated Read,71.0
"""

STATEMENTS = """Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status
55500010001&lt;br&gt;(electric10),Electric,2026/03/20,Current Bill,$300.00,-
******0003 (gas),Gas,2026/03/18,Current Bill,$52.74,-
"""


class Pieces(unittest.TestCase):
    def test_account_keeps_the_last_four(self):
        self.assertEqual(redact.account("55500010001"), "******0001")
        self.assertEqual(redact.account("55500010001&lt;br&gt;(demo42)"), "******0001&lt;br&gt;(demo42)")
        self.assertEqual(redact.account("555000100010000000001"), "******0001")

    def test_account_leaves_short_numbers_and_masks_alone(self):
        for text in ("******0001 (gas)", "Electric (email)", "2026/03/20", "$1,200.50", "12345"):
            self.assertEqual(redact.account(text), text)

    def test_address_becomes_state_and_zip(self):
        self.assertEqual(redact.address("12 Placeholder Ln, Exampleton MA 01000"), "MA 01000")
        self.assertEqual(redact.address("12 Placeholder Ln, Exampleton, ma 01000-1234"), "MA 01000")
        self.assertEqual(redact.address("Somewhere 01000"), "01000")
        self.assertIsNone(redact.address("12 Placeholder Ln"))
        self.assertIsNone(redact.address(None))

    def test_what_is_left_still_places_the_home(self):
        kept = redact.address("12 Placeholder Ln, Exampleton MA 01000")
        self.assertEqual(region.from_address(kept), "MA")
        self.assertEqual(weather.zip_in(kept), "01000")

    def test_subject_masks_long_numbers(self):
        self.assertEqual(redact.subject("Your bill for account 55500010001 is ready"),
                         "Your bill for account ******0001 is ready")
        self.assertEqual(redact.subject("Bill ready - 01000"), "Bill ready - ******1000")
        self.assertEqual(redact.subject("New Electric Bill from Eversource"),
                         "New Electric Bill from Eversource")


class UsageFiles(unittest.TestCase):
    def test_billing_preamble_is_reduced(self):
        text = redact.usage_csv(BILLING)
        self.assertNotIn("Pat Sample", text)
        self.assertNotIn("Placeholder", text)
        self.assertNotIn("55500010001", text)
        self.assertNotIn("555000100010000000001", text)
        self.assertIn("Name,\n", text)
        self.assertIn("Address,MA 01000\n", text)
        self.assertIn("Account Number,******0001\n", text)
        self.assertIn("Service,******0001\n", text)
        # The rows themselves are exactly as they were.
        self.assertIn("Electric billing,2026-08-21,2026-09-17,714.00,$248.26,\n", text)

    def test_history_preamble_and_account_column_are_reduced(self):
        text = redact.usage_csv(HISTORY)
        self.assertNotIn("Placeholder", text)
        self.assertNotIn("55500020002", text)
        self.assertTrue(text.startswith("Address,MA 01000\nData from 11/17/2023 to 7/18/2024\n"))
        self.assertIn("\n******0002,10/19/2023,741.00,28,26.46,$234.96,Actual Read,58.0\n", text)

    def test_the_parser_reads_the_reduced_file_the_same(self):
        before = greenbutton.parse(BILLING)
        after = greenbutton.parse(redact.usage_csv(BILLING))
        self.assertEqual([(p.start, p.end, p.kwh, p.cost) for p in before.periods],
                         [(p.start, p.end, p.kwh, p.cost) for p in after.periods])
        self.assertEqual(after.accounts, {"******0001"})
        self.assertEqual(after.address, "MA 01000")
        history = greenbutton.parse(redact.usage_csv(HISTORY))
        self.assertEqual(history.address, "MA 01000")
        self.assertEqual(history.accounts, {"******0002"})
        self.assertEqual(len(history.periods), 2)

    def test_reducing_twice_changes_nothing(self):
        once = redact.usage_csv(BILLING)
        self.assertEqual(redact.usage_csv(once), once)
        self.assertEqual(redact.usage_bytes(redact.usage_bytes(BILLING.encode())),
                         redact.usage_bytes(BILLING.encode()))

    def test_unknown_text_passes_through(self):
        self.assertEqual(redact.usage_csv("just,a,csv\n1,2,3\n"), "just,a,csv\n1,2,3\n")

    def test_zip_members_and_their_names_are_reduced_and_stable(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("demo_billing_data_555000100010000000001_1_1.csv", BILLING)
            archive.writestr("__MACOSX/._junk", b"xx")
        reduced = redact.usage_bytes(buffer.getvalue())
        with zipfile.ZipFile(io.BytesIO(reduced)) as archive:
            self.assertEqual(archive.namelist(), ["demo_billing_data_******0001_1_1.csv"])
            text = archive.read(archive.namelist()[0]).decode()
        self.assertNotIn("Placeholder", text)
        self.assertIn("Account Number,******0001", text)
        self.assertEqual(redact.usage_bytes(buffer.getvalue()), reduced, "same upload, same bytes")

    def test_store_usage_writes_only_the_reduced_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage = Path(tmp) / "usage"
            stored = ingest.store_usage(usage, "billing_555000100010000000001.csv", BILLING.encode())
            self.assertEqual(stored["accounts"], ["******0001"])
            self.assertEqual(stored["name"], "billing__0001.csv")
            on_disk = (usage / stored["name"]).read_text()
            self.assertNotIn("Pat Sample", on_disk)
            self.assertNotIn("Placeholder", on_disk)
            self.assertNotIn("55500010001", on_disk)
            again = ingest.store_usage(usage, "billing_555000100010000000001.csv", BILLING.encode())
            self.assertFalse(again["stored"], "the same upload is still recognised as a repeat")


class Statements(unittest.TestCase):
    def test_merge_masks_what_comes_in_and_what_was_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AccountHistory.csv"
            path.write_text(STATEMENTS, encoding="utf-8")
            row = ingest.bill_row("55500010001&lt;br&gt;(electric10)", "2026-04-17", 250.0)
            result = ingest.merge(path, [row], list(row))
            self.assertEqual(result.added, 1)
            text = path.read_text()
            self.assertNotIn("55500010001", text)
            self.assertEqual(text.count("******0001&lt;br&gt;(electric10)"), 2)
            self.assertIn("******0003 (gas)", text)

    def test_a_masked_row_is_the_same_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AccountHistory.csv"
            path.write_text(STATEMENTS, encoding="utf-8")
            row = ingest.bill_row("55500010001&lt;br&gt;(electric10)", "2026-03-20", 300.0)
            self.assertEqual(ingest.merge(path, [row], list(row)).added, 0)

    def test_default_account_is_the_reduced_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AccountHistory.csv"
            row = ingest.bill_row("55500010001&lt;br&gt;(electric10)", "2026-03-20", 300.0)
            ingest.merge(path, [row], list(row))
            self.assertEqual(ingest.default_account(path), "******0001&lt;br&gt;(electric10)")


class Scrub(unittest.TestCase):
    def test_files_already_on_disk_are_reduced_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = accounts.Paths.under(Path(tmp))
            paths.csv.parent.mkdir(parents=True, exist_ok=True)
            paths.csv.write_text(STATEMENTS, encoding="utf-8")
            paths.usage.mkdir(parents=True, exist_ok=True)
            (paths.usage / "billing_555000100010000000001.csv").write_text(BILLING)
            (paths.usage / "Green_Button.csv").write_text(HISTORY)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("data_555000100010000000001.csv", BILLING)
            (paths.usage / "export.zip").write_bytes(buffer.getvalue())

            changed = redact.scrub(paths)
            self.assertEqual(changed, {"bills": 1, "usage": 3})
            self.assertNotIn("55500010001", paths.csv.read_text())
            names = sorted(p.name for p in paths.usage.iterdir() if not p.name.startswith("."))
            self.assertEqual(names, ["Green_Button.csv", "billing__0001.csv", "export.zip"])
            for item in paths.usage.iterdir():
                if item.name.startswith("."):
                    continue
                blob = item.read_bytes()
                self.assertNotIn(b"Placeholder", blob)
                self.assertNotIn(b"Pat Sample", blob)
                self.assertNotIn(b"55500010001", blob)
            # Still an export the loader reads, with the ZIP for the weather.
            export = greenbutton.load_dir(paths.usage)
            self.assertEqual(export.address, "MA 01000")
            self.assertEqual(export.accounts, {"******0001", "******0002"})

            self.assertEqual(redact.scrub(paths), {"bills": 0, "usage": 0})

    def test_nothing_there_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(redact.scrub(accounts.Paths.under(Path(tmp) / "none")),
                             {"bills": 0, "usage": 0})


if __name__ == "__main__":
    unittest.main()
