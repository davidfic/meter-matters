"""Tests for uploading exports through the web app."""

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from metermatters.web import app as webapp
from metermatters.intake import greenbutton
from metermatters.intake import ingest
from metermatters.intake import scan

HISTORY = """Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,
55500010001&lt;br&gt;(electric10),Electric,2026/03/20,Current Bill,$300.00,-
55500010001&lt;br&gt;(electric10),Electric,2026/02/16,Current Bill,$400.00,-
"""

BILLING = """Name,SOMEONE
Address,"1 EXAMPLE RD, SOMEWHERE MA 01000"
Account Number,55500010001
Service,555000100010000000001

TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES
Electric billing,2026-01-21,2026-02-17,714.00,$300.00
Electric billing,2025-12-20,2026-01-20,800.00,$400.00
"""

GAS_BILLING = BILLING.replace("55500010001", "55500030003").replace("kWh", "therms")

# A usage file in no layout the loader knows by heart: the general importer reads it.
GENERIC = "Start,End,Usage (kWh),Cost\n" + "\n".join(
    f"2024-{m:02d}-01,2024-{m:02d}-28,{600 + m},${(600 + m) * 0.2:.2f}" for m in range(1, 8)) + "\n"


def zipped(text: str | bytes, name: str = "BillingData.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


class UploadCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.csv = self.dir / "AccountHistory.csv"
        self.usage = self.dir / "usage"
        self.csv.write_text(HISTORY, encoding="utf-8")
        for name, value in (("CSV_PATH", self.csv), ("USAGE_DIR", self.usage),
                            ("STATE_PATH", self.dir / "data" / "state.json")):
            patch = mock.patch.object(webapp, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.client = webapp.app.test_client()

    def post(self, *files):
        data = {"file": [(io.BytesIO(body), name) for name, body in files]}
        return self.client.post("/api/upload", data=data, content_type="multipart/form-data")

    def stored(self):
        return sorted(p.name for p in self.usage.iterdir() if not p.name.startswith("."))


class TestUsageUpload(UploadCase):
    def test_the_portal_zip_is_filed_as_is(self):
        res = self.post(("ever-abc-BillingData.zip", zipped(BILLING)))
        self.assertEqual(res.status_code, 200)
        upload, = res.get_json()["uploads"]
        self.assertEqual(upload["kind"], "usage")
        self.assertTrue(upload["stored"])
        self.assertEqual(upload["periods"], 2)
        self.assertEqual(upload["accounts"], ["55500010001"])
        self.assertEqual(self.stored(), ["ever-abc-BillingData.zip"])

    def test_the_dashboard_gains_usage_from_it(self):
        report = self.post(("export.zip", zipped(BILLING))).get_json()
        self.assertTrue(report["has_usage"])

    def test_a_loose_csv_works_too(self):
        res = self.post(("BillingData.csv", BILLING.encode()))
        self.assertEqual(res.get_json()["uploads"][0]["kind"], "usage")
        self.assertEqual(self.stored(), ["BillingData.csv"])

    def test_the_same_file_twice_is_stored_once(self):
        self.post(("a.zip", zipped(BILLING)))
        upload, = self.post(("renamed.zip", zipped(BILLING))).get_json()["uploads"]
        self.assertFalse(upload["stored"])
        self.assertEqual(self.stored(), ["a.zip"])

    def test_a_later_export_never_overwrites_an_earlier_one(self):
        self.post(("export.zip", zipped(BILLING)))
        self.post(("export.zip", zipped(BILLING + "Electric billing,2026-02-18,2026-03-19,650.00,$250.00\n")))
        self.assertEqual(self.stored(), ["export-2.zip", "export.zip"])

    def test_the_filename_cannot_escape_the_folder(self):
        self.post(("../../evil.zip", zipped(BILLING)))
        self.assertEqual(self.stored(), ["evil.zip"])
        self.assertFalse((self.dir.parent / "evil.zip").exists())

    def test_junk_is_refused_and_leaves_nothing_behind(self):
        for name, body in (("notes.csv", b"just,some,junk\n1,2,3\n"),
                           ("photo.zip", zipped("hello", "readme.txt")),
                           ("broken.zip", b"PK\x03\x04 not really"),
                           ("binary.csv", b"\xff\xfe\x00\x01")):
            with self.subTest(name):
                res = self.post((name, body))
                self.assertEqual(res.status_code, 400)
                self.assertIn("error", res.get_json())
                self.assertEqual(list(self.usage.iterdir()) if self.usage.exists() else [], [])

    def test_a_zip_bomb_is_refused(self):
        # Both ways in: the layout the loader knows by heart, and the general
        # importer the app falls back to when that one says no.
        for layout, text in (("eversource", BILLING), ("generic", GENERIC)):
            with self.subTest(layout), mock.patch.object(ingest, "MAX_UNPACKED_BYTES", 10):
                res = self.post(("big.zip", zipped(text)))
                self.assertEqual(res.status_code, 400)
                self.assertIn("unpacks to far more", res.get_json()["error"])
                self.assertEqual(self.stored() if self.usage.exists() else [], [])

    def test_a_zip_holding_only_another_zip_says_so(self):
        res = self.post(("outer.zip", zipped(zipped(BILLING), "inner.zip")))
        self.assertEqual(res.status_code, 400)
        self.assertIn("holds no csv or xml", res.get_json()["error"].lower())
        self.assertEqual(self.stored() if self.usage.exists() else [], [])

    def test_a_re_export_with_changed_figures_keeps_the_first_and_says_so(self):
        # The loader keeps the first figure it meets for a period, reading files
        # in name order - so here the first upload is also the first name.
        self.post(("a-export.zip", zipped(BILLING)))
        changed = BILLING.replace("$300.00", "$333.00").replace("$400.00", "$444.00")
        upload, = self.post(("b-export.zip", zipped(changed))).get_json()["uploads"]
        self.assertTrue(upload["stored"])
        self.assertIn("2 periods", " ".join(upload.get("notes", [])))
        self.assertEqual(sorted(p.cost for p in greenbutton.load_dir(self.usage).periods), [300.0, 400.0])

    def test_an_error_keeps_its_own_capitals(self):
        # str.capitalize() would make this "That zip holds no csv or xml file."
        res = self.post(("outer.zip", zipped(zipped(BILLING), "inner.zip")))
        self.assertEqual(res.get_json()["error"], "That zip holds no CSV or XML file.")

    def test_a_generic_re_export_with_changed_figures_says_so_too(self):
        # The same note as above, on the path through the general importer.
        generic = "Start,End,Usage (kWh),Cost\n" + "\n".join(
            f"2024-{m:02d}-01,2024-{m:02d}-28,{600 + m},${100 + m}.00" for m in range(1, 9))
        self.post(("a-export.csv", generic.encode()))
        upload, = self.post(("b-export.csv", generic.replace("$101.00", "$999.00").encode())).get_json()["uploads"]
        self.assertTrue(upload["stored"])
        self.assertIn("already on file", " ".join(upload.get("notes", [])))

    def test_masked_accounts_learn_their_real_number(self):
        row = ingest.bill_row("******0003 (gas)", "2026-03-20", 52.74, account_type="Gas")
        ingest.merge(self.csv, [row], list(row))
        upload, = self.post(("gas.zip", zipped(GAS_BILLING))).get_json()["uploads"]
        self.assertEqual(upload["renamed"], 1)
        self.assertIn("55500030003 (gas)", self.csv.read_text())


class TestMixedUpload(UploadCase):
    NEWER = HISTORY.replace("2026/02/16", "2026/04/17").replace("$400.00", "$250.00")

    def test_statement_history_still_merges(self):
        report = self.post(("AccountHistory.csv", self.NEWER.encode())).get_json()
        self.assertEqual(report["merge"], {"added": 1, "total": 3})
        self.assertFalse(self.usage.exists() and self.stored())

    def test_several_files_are_sorted_by_content(self):
        res = self.post(("b.csv", self.NEWER.encode()), ("a.zip", zipped(BILLING)),
                        ("junk.csv", b"nope\n"))
        self.assertEqual(res.status_code, 200)
        kinds = [u["kind"] for u in res.get_json()["uploads"]]
        self.assertEqual(kinds, ["bills", "usage", "error"])

    def test_no_file_is_an_error(self):
        self.assertEqual(self.client.post("/api/upload").status_code, 400)

    def test_an_oversized_upload_is_refused(self):
        res = self.post(("huge.csv", b"x" * (6 * 1024 * 1024)))
        self.assertEqual(res.status_code, 413)
        self.assertIn("export", res.get_json()["error"])

    def test_an_oversized_scan_is_told_it_is_an_image(self):
        with mock.patch.object(scan, "enabled", return_value=True):
            res = self.client.post("/api/scan", content_type="multipart/form-data",
                                   data={"file": (io.BytesIO(b"x" * (6 * 1024 * 1024)), "bill.png")})
        self.assertEqual(res.status_code, 413)
        self.assertIn("image", res.get_json()["error"])

    def test_a_column_choice_goes_with_one_file_only(self):
        data = {"file": [(io.BytesIO(GENERIC.encode()), "a.csv"), (io.BytesIO(GENERIC.encode()), "b.csv")],
                "mapping": json.dumps({"kind": "usage", "fuel": "Electric", "unit": "kWh", "columns": {}})}
        res = self.client.post("/api/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.usage.exists() and self.stored())


class TestPages(UploadCase):
    def test_a_missing_bill_reaches_the_overview(self):
        # The fixture's last statement is long past; make it recent instead.
        with mock.patch.object(webapp.watchdog, "overdue_alerts",
                               return_value=[{"fuel": "Electric", "kind": "missing",
                                              "href": "/electric", "title": "t", "detail": "d"}]):
            data = self.client.get("/api/overview").get_json()
        self.assertEqual(data["alerts"][0]["kind"], "missing")

    def test_every_page_still_renders(self):
        for path in ("/", "/electric", "/raw", "/api/report", "/api/raw"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
