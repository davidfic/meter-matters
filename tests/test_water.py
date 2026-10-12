"""Tests for water as a third utility: units, pages, and what stays electric-only."""

import io
import unittest
from datetime import date

from metermatters import fuels
from metermatters.intake import greenbutton
from metermatters.intake import importers
from metermatters.analytics import insights
from tests.test_importers import WebCase


def water_csv(unit="gal", rows=8, service="Water"):
    out = [f"Service,Billing Start,Billing End,Usage ({unit}),Charges"]
    for i in range(rows):
        year, m = 2024 + i // 12, i % 12 + 1
        used = 4000 + i * 100 if unit == "gal" else 5 + i * 0.2
        out.append(f"{service},{date(year, m, 1)},{date(year, m, 28)},{used},${30 + i:.2f}")
    return "\n".join(out).encode()


class TestUnits(unittest.TestCase):
    def test_gallons_are_water(self):
        self.assertIs(fuels.of_unit("gal"), fuels.WATER)
        self.assertIs(fuels.of_unit("GALLONS"), fuels.WATER)
        self.assertIs(fuels.of_unit("ccf"), fuels.GAS)          # unless the file says water

    def test_a_water_file_in_ccf_comes_out_in_gallons(self):
        r = importers.read(water_csv(unit="CCF"), "w.csv")
        self.assertEqual((r.fuel, r.unit), ("Water", "gal"))
        first = greenbutton.parse(r.text).periods[0]
        self.assertAlmostEqual(first.kwh, 5.0 * fuels.GALLONS_PER_CCF, places=1)

    def test_ccf_with_no_word_of_water_is_still_gas(self):
        r = importers.read(water_csv(unit="CCF", service="Natural Gas"), "g.csv")
        self.assertEqual((r.fuel, r.unit), ("Gas", "ccf"))

    def test_water_has_no_carbon(self):
        self.assertEqual(insights.kg_per_unit("gal"), 0.0)
        self.assertGreater(insights.kg_per_unit("therms"), 0)


class TestWaterPages(WebCase):
    def test_a_water_upload_gets_its_own_page(self):
        res = self.upload("water.csv", water_csv())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])
        self.assertEqual(res.get_json()["fuel"], "Water")
        page = self.client.get("/water").get_data(as_text=True)
        self.assertIn("Tapped Out", page)
        report = self.client.get("/api/report?fuel=Water").get_json()
        self.assertEqual(len(report["series"]), 8)
        self.assertEqual(report["series"][-1]["unit"], "gal")
        self.assertEqual(report["scoreboard"]["level"]["title"], "Meter Reader")

    def test_the_overview_shows_a_water_card_and_the_energy_rows_carry_it(self):
        self.upload("water.csv", water_csv())
        d = self.client.get("/api/overview").get_json()
        self.assertEqual([c["fuel"] for c in d["fuels"]], ["Water"])
        self.assertTrue(all("water" in row for row in d["energy"]))
        self.assertGreater(d["energy"][-1]["water"], 0)

    def test_a_bill_by_hand_in_gallons(self):
        self.client.post("/bills/add", data={"fuel": "Water", "due": "2026-10-16", "amount": "48.10",
                                             "used": "3200", "unit": "gal",
                                             "start": "2026-07-01", "end": "2026-09-30"})
        row = self.client.get("/api/report?fuel=Water").get_json()["series"][-1]
        self.assertEqual((row["kwh"], row["unit"]), (3200.0, "gal"))

    def test_water_says_nothing_about_the_weather(self):
        self.upload("water.csv", water_csv(rows=14))
        out = self.client.get("/api/insights").get_json()
        block = out["fuels"]["Water"]
        self.assertIsNone(block.get("weather"))
        self.assertNotIn("Water", (out.get("carbon") or {}).get("fuels", {}))


if __name__ == "__main__":
    unittest.main()
