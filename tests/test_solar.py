"""Tests for a meter that runs both ways, and for credits on the bill."""

import unittest
from datetime import datetime, timezone

from metermatters.intake import greenbutton
from metermatters.intake import importers
from tests.test_importers import WebCase, bills as bill_csv, daily, espi


def two_way(days=70, second_uom=72, second_flow=19):
    start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())

    def block(value, mr):
        readings = "".join(
            f"<espi:IntervalReading><espi:cost>310000</espi:cost><espi:timePeriod><espi:duration>86400</espi:duration>"
            f"<espi:start>{start + i * 86400}</espi:start></espi:timePeriod>"
            f"<espi:value>{value}</espi:value></espi:IntervalReading>" for i in range(days))
        return (f'<entry><link rel="up" href="/UsagePoint/1/MeterReading/{mr}/IntervalBlock"/>'
                f"<content><espi:IntervalBlock>{readings}</espi:IntervalBlock></content></entry>")

    def kind(mr, flow, uom):
        return (f'<entry><link rel="self" href="/UsagePoint/1/MeterReading/{mr}/ReadingType"/>'
                f"<content><espi:ReadingType><espi:flowDirection>{flow}</espi:flowDirection>"
                f"<espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier><espi:uom>{uom}</espi:uom>"
                f"</espi:ReadingType></content></entry>")

    return (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:espi="http://naesb.org/espi">'
            f"{kind(1, 1, 72)}{kind(2, second_flow, second_uom)}{block(20000, 1)}{block(5000, 2)}</feed>").encode()


class TestTwoWayMeter(unittest.TestCase):
    def test_delivered_and_received_are_kept_apart(self):
        r = importers.read(two_way(), "gb.xml")
        january = greenbutton.parse(r.text).periods[0]
        self.assertAlmostEqual(january.kwh, 20 * 31)          # what was used, not the net
        self.assertAlmostEqual(january.exported, 5 * 31)
        self.assertTrue(any("both ways" in n for n in r.notes))

    def test_a_feed_with_one_reading_type_and_no_links_still_reads(self):
        r = importers.read(espi(), "gb.xml")
        self.assertEqual(r.periods, 2)
        self.assertEqual(greenbutton.parse(r.text).periods[0].exported, 0.0)

    def test_two_fuels_in_one_feed_are_refused_with_a_reason(self):
        with self.assertRaisesRegex(importers.ImportFailed, "more than one fuel"):
            importers.read(two_way(second_uom=169, second_flow=1), "gb.xml")


class TestCredits(unittest.TestCase):
    def test_a_credit_month_is_kept_and_marked(self):
        text = bill_csv(rows=3).decode() + "\n2024-04-01,2024-04-21,-$12.00,Electric\n"
        r = importers.read(text.encode(), "b.csv")
        self.assertEqual(r.periods, 4)
        credit = [b for b in r.bills if b["Status"] == "CREDIT"]
        self.assertEqual(len(credit), 1)


class TestOnThePage(WebCase):
    def test_exported_energy_reaches_the_report(self):
        res = self.upload("gb.xml", two_way())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])
        report = self.client.get("/api/report?fuel=Electric").get_json()
        self.assertGreater(report["summary"]["exported_12"], 0)
        self.assertGreater(report["series"][0]["exported"], 0)

    def test_the_calculator_has_a_price_and_a_factor_to_work_from(self):
        self.upload("export.csv", daily())
        report = self.client.get("/api/report?fuel=Electric").get_json()
        self.assertGreater(report["carbon_factor"], 0)
        self.assertGreater(report["summary"]["rate_latest"], 0)

    def test_the_walkthroughs_are_on_the_add_bills_page(self):
        self.assertIn("How to find the file", self.client.get("/connect").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
