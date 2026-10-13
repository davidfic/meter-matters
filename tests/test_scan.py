"""Tests for reading a scanned bill: the text reader is faked, the finding is real."""

import os
import unittest
from unittest import mock

from metermatters.intake import scan
from tests.support import HostedCase

# Invented, in the shape a city water department's bill comes back from the
# text reader: table headings on their own lines, values on the lines after,
# the unit only in a column heading, and a conversion note that is not a reading.
CITY_WATER = """Water and Sewer Department Bill
Bill Number
880011
Bill Date
09/24/2026
Current Billing Due Date
11/09/2026
Previous
Current
Read Date Read Date Meter Reading
4455667 06/01/2026 09/15/2026
Previous
210
Current
Meter Reading Code
219 A
Usage
(100 cu. ft.)
9
Charge
72.89
100 cu. ft. equals 748 gallons
Total Current Billing
254.10
Outstanding Balance
Total Amount Due
.00
$254.10
Terms: charges will be considered delinquent if
not paid by 11/09/2026.
"""

ELECTRIC = """Your electric statement
Service period: Aug 14, 2026 to Sep 13, 2026
Total kWh used: 612 kWh
Previous balance $0.00
Total Amount Due $148.22
Payment due by October 5, 2026
"""


class TestFindingTheBill(unittest.TestCase):
    def test_a_city_water_bill_with_the_unit_in_a_column_heading(self):
        g = scan.guess(CITY_WATER)
        self.assertEqual(g["fuel"], "Water")
        self.assertEqual(g["amount"], 254.10)
        self.assertEqual(g["due"], "2026-11-09")
        self.assertEqual((g["start"], g["end"]), ("2026-06-01", "2026-09-15"))
        self.assertAlmostEqual(g["used"], 9 * 748.05, places=1)
        self.assertEqual(g["unit"], "gal")
        self.assertEqual(set(g["sure"]), {"fuel", "amount", "due", "period", "used"})

    def test_an_electric_bill_with_spelled_out_dates(self):
        g = scan.guess(ELECTRIC)
        self.assertEqual(g["fuel"], "Electric")
        self.assertEqual(g["amount"], 148.22)
        self.assertEqual(g["due"], "2026-10-05")
        self.assertEqual((g["start"], g["end"]), ("2026-08-14", "2026-09-13"))
        self.assertEqual((g["used"], g["unit"]), (612.0, "kWh"))

    def test_a_conversion_note_is_not_mistaken_for_a_reading(self):
        text = "Water bill\n100 cu. ft. equals 748 gallons\nTotal Amount Due $40.00\n"
        self.assertIsNone(scan.guess(text)["used"])

    def test_meter_readings_vouch_for_an_unlabelled_figure(self):
        text = "Water usage\nPrevious reading 1200\nCurrent reading 1207\n7 HCF this period\nAmount due $31.00\n"
        g = scan.guess(text)
        self.assertAlmostEqual(g["used"], 7 * 748.05, places=1)
        self.assertIn("used", g["sure"])

    def test_nothing_recognised_is_all_none(self):
        g = scan.guess("A letter about your account.\nThank you.")
        self.assertEqual([g[k] for k in ("fuel", "amount", "due", "start", "end", "used")], [None] * 6)

    def test_a_scan_is_known_by_its_name(self):
        self.assertEqual(scan.mime_of("bill.PNG"), "image/png")
        self.assertEqual(scan.mime_of("bill.pdf"), "application/pdf")
        self.assertIsNone(scan.mime_of("export.csv"))


class TestTheRoute(HostedCase):
    env = {"ELEC_VISION_KEY": "k"}

    def setUp(self):
        super().setUp()
        self.sign_up()

    def post(self, name="bill.png", body=b"\x89PNG..."):
        import io
        return self.client.post("/api/scan", data={"file": (io.BytesIO(body), name)},
                                content_type="multipart/form-data")

    def test_what_is_read_comes_back_for_checking_and_nothing_is_filed(self):
        with mock.patch.object(scan, "read_text", return_value=CITY_WATER):
            res = self.post()
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        g = res.get_json()
        self.assertEqual((g["file"], g["fuel"], g["amount"], g["used"]), ("bill.png", "Water", 254.10, 6732))
        self.assertEqual(self.client.get("/api/report?fuel=Water").get_json()["series"], [])

    def test_the_image_goes_to_the_reader_and_nowhere_else(self):
        with mock.patch.object(scan, "read_text", return_value=CITY_WATER) as read:
            self.post(body=b"the picture")
        read.assert_called_once_with(b"the picture", "image/png")

    def test_a_file_that_is_not_a_scan_is_refused(self):
        self.assertEqual(self.post(name="export.csv").status_code, 400)

    def test_an_unreadable_scan_says_why(self):
        with mock.patch.object(scan, "read_text", side_effect=scan.Unreadable("no text was found on it")):
            res = self.post()
        self.assertEqual(res.status_code, 400)
        self.assertIn("no text was found", res.get_json()["error"])

    def test_not_offered_until_a_key_is_set(self):
        with mock.patch.dict(os.environ, {"ELEC_VISION_KEY": ""}):
            self.assertEqual(self.post().status_code, 404)
            self.assertNotIn("scans or photos", self.client.get("/connect").get_data(as_text=True))
        self.assertIn("scans or photos", self.client.get("/connect").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
