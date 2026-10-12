"""Tests for getting usage and bills in from any utility, and for places that aren't Massachusetts."""

import io
import json
import unittest
import unittest.mock
from datetime import date, datetime, timedelta, timezone

from metermatters.intake import greenbutton
from metermatters.intake import importers
from metermatters.analytics import insights
from metermatters.analytics import rates
from metermatters import region
from metermatters.analytics import weather
from tests.support import HostedCase


def daily(start=date(2024, 1, 1), end=date(2025, 3, 15), preamble=True, cost=True):
    lines = (['Name,Jane Example', 'Address,"1 Example St, Fresno CA 93701"',
              'Account Number,9876543210', 'Service,Electric', ''] if preamble else [])
    lines.append("TYPE,DATE,START TIME,END TIME,USAGE (kWh)" + (",COST,NOTES" if cost else ""))
    d = start
    while d <= end:
        used = 12 + d.day % 5
        lines.append(f"Electric usage,{d},00:00,23:59,{used}" + (f",${used * 0.31:.2f}," if cost else ""))
        d += timedelta(1)
    return "\n".join(lines).encode()


def monthly(unit="kWh", rows=9, head="Billing Start,Billing End,Usage ({u}),Charges"):
    out = [head.format(u=unit)]
    for m in range(1, rows + 1):
        out.append(f"{date(2024, m, 1)},{date(2024, m, 28)},{600 + m * 10},${(600 + m * 10) * 0.2:.2f}")
    return "\n".join(out).encode()


def bills(rows=7, due="Due Date", fuel="Electric"):
    out = [f"Statement Date,{due},Amount Due,Service"]
    for m in range(1, rows + 1):
        out.append(f"2024-{m:02d}-01,2024-{m:02d}-21,${100 + m * 3:.2f},{fuel}")
    return "\n".join(out).encode()


def espi(days=70, uom=72, multiplier=0):
    start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    readings = "".join(
        f"<espi:IntervalReading><espi:cost>{3100000}</espi:cost><espi:timePeriod>"
        f"<espi:duration>86400</espi:duration><espi:start>{start + i * 86400}</espi:start>"
        f"</espi:timePeriod><espi:value>{13000 + i}</espi:value></espi:IntervalReading>"
        for i in range(days))
    return (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:espi="http://naesb.org/espi">'
            f"<entry><content><espi:ReadingType><espi:powerOfTenMultiplier>{multiplier}</espi:powerOfTenMultiplier>"
            f"<espi:uom>{uom}</espi:uom></espi:ReadingType></content></entry>"
            f"<entry><content><espi:IntervalBlock>{readings}</espi:IntervalBlock></content></entry></feed>").encode()


class TestReading(unittest.TestCase):
    def test_daily_readings_become_whole_calendar_months(self):
        r = importers.read(daily(), "x.csv")
        self.assertEqual((r.kind, r.fuel, r.unit, r.periods), ("usage", "Electric", "kWh", 14))
        self.assertEqual(r.accounts, ["9876543210"])
        self.assertTrue(any("part-month" in n for n in r.notes))

    def test_the_result_is_a_file_the_existing_loader_reads(self):
        export = greenbutton.parse(importers.read(daily(), "x.csv").text)
        self.assertEqual(len(export.periods), 14)
        self.assertEqual(export.address, "1 Example St, Fresno CA 93701")
        first = export.periods[0]
        self.assertEqual((first.start, first.end, first.unit), (date(2024, 1, 1), date(2024, 1, 31), "kWh"))
        self.assertGreater(first.cost, 100)

    def test_a_month_short_of_readings_is_left_out_not_read_as_low_use(self):
        r = importers.read(daily(end=date(2024, 3, 10)), "x.csv")
        self.assertEqual(r.periods, 2)

    def test_start_and_end_columns_are_taken_as_the_period(self):
        r = importers.read(monthly(), "m.csv")
        self.assertEqual((r.periods, r.fuel), (9, "Electric"))
        export = greenbutton.parse(r.text)
        self.assertEqual((export.periods[0].start, export.periods[0].end),
                         (date(2024, 1, 1), date(2024, 1, 28)))

    def test_gas_and_its_unit_come_from_the_column_name(self):
        r = importers.read(b"Read Date,Usage (CCF),Cost\n" + "\n".join(
            f"2024-{m:02d}-15,{20 + m},${(20 + m) * 1.5:.2f}" for m in range(1, 10)).encode(), "g.csv")
        self.assertEqual((r.fuel, r.unit), ("Gas", "ccf"))

    def test_units_are_converted_into_the_ones_the_app_uses(self):
        r = importers.read(monthly(unit="MWh"), "m.csv")
        self.assertEqual(r.unit, "kWh")
        self.assertAlmostEqual(greenbutton.parse(r.text).periods[0].kwh, 610 * 1000)
        r = importers.read(monthly(unit="MMBtu"), "m.csv")
        self.assertEqual((r.unit, r.fuel), ("therms", "Gas"))
        self.assertAlmostEqual(greenbutton.parse(r.text).periods[0].kwh, 610 * 10)

    def test_no_cost_in_the_file_is_said_not_hidden(self):
        r = importers.read(daily(cost=False), "x.csv")
        self.assertTrue(any("no cost" in n for n in r.notes))

    def test_a_bill_list_becomes_statements(self):
        r = importers.read(bills(), "b.csv")
        self.assertEqual((r.kind, r.fuel, r.periods), ("bills", "Electric", 7))
        self.assertEqual(r.bills[0]["Pay/Due Date"], "2024/01/21")
        self.assertEqual(r.bills[0]["Amount"], "$103.00")
        self.assertEqual(r.bills[0]["Status"], "IMPORTED")

    def test_a_statement_date_is_not_passed_off_as_a_due_date(self):
        r = importers.read(bills(due="Notes"), "b.csv")      # no due-date column: dates come from the statement
        self.assertEqual(r.bills[0]["Status"], "BACKFILL")
        self.assertEqual(r.bills[0]["Pay/Due Date"], "2024/01/22")

    def test_the_fuel_of_a_bill_list_comes_from_its_service_column(self):
        self.assertEqual(importers.read(bills(fuel="Natural Gas"), "b.csv").fuel, "Gas")

    def test_semicolons_and_tabs_are_fine(self):
        for sep in (";", "\t"):
            with self.subTest(sep):
                text = monthly().decode().replace(",", sep)
                self.assertEqual(importers.read(text.replace("$", "").encode(), "m.csv").periods, 9)

    def test_a_file_that_is_unclear_is_put_to_the_person_with_a_sample(self):
        with self.assertRaises(importers.Unsure) as caught:
            importers.read(b"Period,Reading,Value\n" + "\n".join(
                f"2024-{m:02d}-15,{500 + m},{100 + m * 2}.50" for m in range(1, 8)).encode(), "e.csv")
        self.assertEqual(caught.exception.headers, ["Period", "Reading", "Value"])
        self.assertEqual(len(caught.exception.sample), 5)

    def test_a_missing_unit_is_asked_for_never_assumed(self):
        text = b"Start,End,Consumption,Cost\n" + "\n".join(
            f"2024-{m:02d}-01,2024-{m:02d}-28,{600 + m},${(600 + m) * 0.2:.2f}" for m in range(1, 8)).encode()
        with self.assertRaises(importers.Unsure) as caught:
            importers.read(text, "m.csv")
        self.assertIn("unit", caught.exception.reason)

    def test_the_answer_is_used_when_it_comes_back(self):
        text = b"Start,End,Consumption,Cost\n" + "\n".join(
            f"2024-{m:02d}-01,2024-{m:02d}-28,{600 + m},${(600 + m) * 0.2:.2f}" for m in range(1, 8)).encode()
        r = importers.read(text, "m.csv", mapping={
            "kind": "usage", "fuel": "Electric", "unit": "kWh",
            "columns": {"start": "Start", "end": "End", "usage": "Consumption", "money": "Cost"}})
        self.assertEqual((r.periods, r.unit), (7, "kWh"))

    def test_a_mapping_naming_a_column_that_does_not_exist_is_ignored(self):
        with self.assertRaises(importers.Unsure):
            importers.read(b"Period,Reading,Value\n2024-01-15,5,10.5\n2024-02-15,6,11.5\n", "e.csv",
                           mapping={"kind": "usage", "columns": {"usage": "Nope"}, "unit": "kWh",
                                    "fuel": "Electric"})

    def test_junk_is_refused_not_turned_into_a_form(self):
        for body in (b"just,some,junk\n1,2,3\n", b"", b"\n\n"):
            with self.subTest(body), self.assertRaises(importers.ImportFailed):
                importers.read(body, "j.csv")


class TestGreenButtonXml(unittest.TestCase):
    def test_interval_readings_are_added_up_by_month(self):
        r = importers.read(espi(), "gb.xml")
        self.assertEqual((r.kind, r.unit, r.periods), ("usage", "kWh", 2))
        january = greenbutton.parse(r.text).periods[0]
        self.assertAlmostEqual(january.kwh, sum(13000 + i for i in range(31)) / 1000)
        self.assertAlmostEqual(january.cost, 31 * 31.0)       # 3,100,000 x 1e-5 dollars a day

    def test_the_unit_code_decides_the_fuel(self):
        r = importers.read(espi(uom=169), "gb.xml")
        self.assertEqual((r.fuel, r.unit), ("Gas", "therms"))

    def test_the_power_of_ten_multiplier_applies(self):
        a = greenbutton.parse(importers.read(espi(multiplier=0), "g.xml").text).periods[0].kwh
        b = greenbutton.parse(importers.read(espi(multiplier=3), "g.xml").text).periods[0].kwh
        self.assertAlmostEqual(b, a * 1000)

    def test_an_unknown_unit_is_refused_rather_than_mislabelled(self):
        with self.assertRaisesRegex(importers.ImportFailed, "unrecognised unit"):
            importers.read(espi(uom=999), "gb.xml")

    def test_entity_tricks_are_not_accepted(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed/>'
        with self.assertRaisesRegex(importers.ImportFailed, "features that aren't accepted"):
            importers.read(bomb, "gb.xml")

    def test_garbage_xml_is_refused(self):
        with self.assertRaises(importers.ImportFailed):
            importers.read(b"<feed><oops>", "gb.xml")


class TestByHand(unittest.TestCase):
    def test_a_typed_bill_is_a_statement_row(self):
        row = importers.manual("Gas", "2026-10-16", "$87.50")
        self.assertEqual((row["Account Type"], row["Amount"], row["Pay/Due Date"], row["Source"]),
                         ("Gas", "$87.50", "2026/10/16", "manual"))

    def test_bad_entries_say_what_is_wrong(self):
        for args, word in ((("Steam", "2026-10-16", "5"), "electric"),
                           (("Gas", "soon", "5"), "due"), (("Gas", "2026-10-16", "free"), "amount"),
                           (("Gas", "2026-10-16", "-4"), "amount")):
            with self.subTest(args), self.assertRaisesRegex(importers.ImportFailed, word):
                importers.manual(*args)


class TestRegion(unittest.TestCase):
    def test_the_state_is_read_off_an_address_or_a_geocoder_name(self):
        self.assertEqual(region.from_address('1 Example St, Fresno CA 93701'), "CA")
        self.assertEqual(region.from_address("3 Oak Ave, Austin tx 78701-1234"), "TX")
        self.assertIsNone(region.from_address("12345 EXAMPLE RD"))
        self.assertIsNone(region.from_address("no state ZZ 12345"))
        self.assertEqual(region.from_place("Fresno, California"), "CA")

    def test_what_a_person_chose_wins(self):
        self.assertEqual(region.resolve("tx", "1 Main, Fresno CA 93701", "Fresno, California"), "TX")
        self.assertEqual(region.resolve("", "1 Main, Fresno CA 93701"), "CA")
        self.assertEqual(region.resolve("nowhere", None, "Fresno, California"), "CA")
        self.assertIsNone(region.resolve(None, None, None))

    def test_every_state_has_a_published_figure(self):
        self.assertEqual(set(region.STATES) - set(region.EGRID_STATE_LB_PER_MWH), set())

    def test_new_england_uses_its_one_grid_and_is_unchanged(self):
        for state in region.NEW_ENGLAND:
            self.assertAlmostEqual(region.grid(state).kg_per_kwh, insights.KG_PER_KWH)
        self.assertIn("New England", region.grid("MA").label)

    def test_other_states_use_their_own_and_unknown_uses_the_national_figure(self):
        self.assertAlmostEqual(region.grid("TX").kg_per_kwh, 771.2 * 0.45359237 / 1000)
        self.assertAlmostEqual(region.grid("CA").kg_per_kwh, 394.8 * 0.45359237 / 1000)
        self.assertAlmostEqual(region.grid(None).kg_per_kwh, 770.9 * 0.45359237 / 1000)
        self.assertIn("U.S. average", region.grid(None).label)

    def test_carbon_follows_the_grid_for_the_length_of_the_block_only(self):
        before = insights.kg_per_unit("kWh")
        with insights.grid(0.5):
            self.assertEqual(insights.kg_per_unit("kWh"), 0.5)
            self.assertEqual(insights.kg_per_unit("therms"), 5.3)     # gas doesn't depend on the grid
        self.assertEqual(insights.kg_per_unit("kWh"), before)

    def test_supplier_comparison_exists_only_where_a_reader_does(self):
        self.assertTrue(rates.covers("MA"))
        self.assertFalse(rates.covers(None))         # not known where: nothing is sent anywhere
        self.assertFalse(rates.covers("TX"))


class WebCase(HostedCase):
    def setUp(self):
        super().setUp()
        # The weather is somebody else's service; the insights ask it for a place and its days.
        for name, fake in (("locate", lambda z: weather.Place(z, "Fresno, California", 36.7, -119.8, "auto")),
                           ("temperatures", lambda place, first, last, now=None: {})):
            patch = unittest.mock.patch.object(weather, name, side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)
        self.user = self.sign_up()

    def upload(self, name, body, mapping=None, prefix=""):
        data = {"file": [(io.BytesIO(body), name)]}
        if mapping:
            data["mapping"] = json.dumps(mapping)
        return self.client.post(f"{prefix}/api/upload", data=data, content_type="multipart/form-data")


class TestUploadingFromAnotherUtility(WebCase):
    def test_a_daily_usage_file_gives_a_working_dashboard_with_no_other_file(self):
        res = self.upload("export.csv", daily())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        report = res.get_json()
        upload, = report["uploads"]
        self.assertEqual((upload["kind"], upload["periods"], upload["bills_made"]), ("usage", 14, 14))
        self.assertEqual(len(report["series"]), 14)
        self.assertTrue(report["has_usage"])
        self.assertEqual(report["series"][-1]["unit"], "kWh")
        self.assertGreater(report["series"][-1]["rate"], 0.2)

    def test_derived_bills_are_marked_as_estimates(self):
        self.upload("export.csv", daily())
        self.assertIn("BACKFILL", self.user.paths.csv.read_text())

    def test_uploading_the_same_file_twice_adds_nothing(self):
        self.upload("export.csv", daily())
        again = self.upload("export.csv", daily()).get_json()
        self.assertEqual(len(again["series"]), 14)

    def test_real_bills_arriving_later_replace_the_derived_ones(self):
        self.upload("export.csv", daily())
        first = self.client.get("/api/report?fuel=Electric").get_json()["series"]
        self.assertEqual(len(first), 14)
        # The real statement for each of the first three months, due a few days off the estimate.
        real = "Due Date,Amount Due,Service\n" + "\n".join(
            f"2024-{m + 1:02d}-24,${140 + m:.2f},Electric" for m in (1, 2, 3))
        self.upload("bills.csv", real.encode())
        again = self.client.get("/api/report?fuel=Electric").get_json()["series"]
        self.assertEqual(len(again), 14)                                # replaced, not added
        self.assertEqual([r["amount"] for r in again[:3]], [141.0, 142.0, 143.0])

    def test_a_bill_list_alone_works_and_says_it_has_no_usage(self):
        report = self.upload("bills.csv", bills(rows=7)).get_json()
        self.assertEqual(report["uploads"][0]["kind"], "bills")
        self.assertEqual(len(report["series"]), 7)
        self.assertFalse(report["has_usage"])

    def test_a_green_button_xml_file_is_accepted(self):
        report = self.upload("gb.xml", espi()).get_json()
        self.assertEqual(report["uploads"][0]["kind"], "usage")
        self.assertEqual(len(report["series"]), 2)

    def test_an_unclear_file_comes_back_as_a_question_not_an_error(self):
        res = self.upload("e.csv", b"Period,Reading,Value\n" + "\n".join(
            f"2024-{m:02d}-15,{500 + m},{100 + m * 2}.50" for m in range(1, 8)).encode())
        self.assertEqual(res.status_code, 200)
        q, = res.get_json()["uploads"]
        self.assertEqual((q["kind"], q["headers"]), ("unsure", ["Period", "Reading", "Value"]))
        self.assertFalse(self.user.paths.csv.exists())          # nothing filed until they answer

    def test_the_answer_files_it(self):
        body = b"Period,Reading,Value\n" + "\n".join(
            f"2024-{m:02d}-15,{500 + m},{100 + m * 2}.50" for m in range(1, 8)).encode()
        res = self.upload("e.csv", body, mapping={
            "kind": "usage", "fuel": "Gas", "unit": "therms",
            "columns": {"date": "Period", "usage": "Reading", "money": "Value"}})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        self.assertEqual(res.get_json()["uploads"][0]["kind"], "usage")
        self.assertEqual(res.get_json()["fuel"], "Gas")

    def test_a_mangled_answer_is_refused(self):
        res = self.client.post("/api/upload", content_type="multipart/form-data",
                               data={"file": [(io.BytesIO(b"a,b,c\n1,2,3\n"), "x.csv")], "mapping": "{nope"})
        self.assertEqual(res.status_code, 400)

    def test_junk_is_still_refused_outright(self):
        self.assertEqual(self.upload("n.csv", b"just,some,junk\n1,2,3\n").status_code, 400)

    def test_the_address_in_the_file_gives_the_state_and_a_figure_for_its_grid(self):
        self.upload("export.csv", daily())
        out = self.client.get("/api/insights").get_json()
        self.assertEqual(out["state"], "CA")
        self.assertIn("California", out["grid"])

    def test_a_home_in_another_state_is_kept_apart_from_one_in_the_first(self):
        self.client.post("/homes/add", data={"name": "Cabin"})
        self.upload("export.csv", daily(), prefix="/home/cabin")
        self.assertEqual(self.client.get("/api/report?fuel=Electric").get_json().get("series"), [])
        self.assertEqual(len(self.client.get("/home/cabin/api/report?fuel=Electric").get_json()["series"]), 14)


class TestWhereAHomeIs(WebCase):
    def setUp(self):
        super().setUp()
        self.upload("export.csv", daily())

    def test_state_provider_and_zip_are_kept_per_home(self):
        self.client.post("/place", data={"state": "tx", "zip": "78701", "provider": "  Austin   Energy "})
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertIn('value="Austin Energy"', page)
        self.assertIn('value="78701"', page)
        self.assertIn('<option value="TX" selected>', page)
        self.assertEqual(self.client.get("/api/insights").get_json()["state"], "TX")

    def test_a_bad_zip_is_refused_and_a_bad_state_is_forgotten(self):
        self.client.post("/place", data={"state": "TX", "zip": "12"})
        self.assertIn("five digits", self.client.get("/connect").get_data(as_text=True))
        self.client.post("/place", data={"state": "ZZ", "zip": "78701"})
        self.assertEqual(self.client.get("/api/insights").get_json()["state"], "CA")   # back to the address

    def test_the_state_the_file_names_is_shown_when_none_was_chosen(self):
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertIn("We read <strong>CA</strong>", page)

    def test_the_email_reader_is_labelled_for_what_it_reads(self):
        self.client.post("/place", data={"provider": "National Grid"})
        self.assertIn("Eversource bills only", self.client.get("/connect").get_data(as_text=True))
        self.client.post("/place", data={"provider": "Eversource"})
        self.assertNotIn("Eversource bills only", self.client.get("/connect").get_data(as_text=True))
        self.client.post("/place", data={"provider": ""})
        self.assertNotIn("Eversource bills only", self.client.get("/connect").get_data(as_text=True))

    def test_the_pages_say_nothing_about_a_utility_they_were_not_told_of(self):
        for page in ("/", "/electric", "/rates", "/raw"):
            html = self.client.get(page).get_data(as_text=True)
            self.assertNotIn("Eversource", html, page)
            self.assertNotIn("Massachusetts", html, page)


class TestSupplierComparisonByPlace(WebCase):
    def setUp(self):
        super().setUp()
        self.upload("export.csv", daily())
        self.calls = []
        patch = unittest.mock.patch.object(rates, "offers_for",
                                           side_effect=lambda z: self.calls.append(z) or {"offers": [], "fetched_at": ""})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_state_with_no_comparison_is_told_so_and_nothing_is_sent_anywhere(self):
        out = self.client.get("/api/rates").get_json()
        self.assertEqual((out["status"], out["state"], out["state_name"]), ("not-covered", "CA", "California"))
        self.assertEqual(self.calls, [])

    def test_a_zip_the_massachusetts_site_does_not_know_is_not_covered_not_down(self):
        self.client.post("/place", data={"state": "MA", "zip": "93701"})
        with unittest.mock.patch.object(rates, "offers_for", side_effect=rates.NotCovered("no")):
            out = self.client.get("/api/rates").get_json()
        self.assertEqual(out["status"], "not-covered")


class TestManualBill(WebCase):
    def post(self, **fields):
        return self.client.post("/bills/add", data=fields)

    def test_a_bill_by_hand_appears_on_the_dashboard(self):
        self.post(fuel="Gas", due="2026-10-16", amount="87.50")
        series = self.client.get("/api/report?fuel=Gas").get_json()["series"]
        self.assertEqual([r["amount"] for r in series], [87.5])

    def test_with_usage_it_gets_a_price_per_unit(self):
        self.post(fuel="Electric", due="2026-10-16", amount="150", used="500", unit="kWh",
                  start="2026-08-20", end="2026-09-18")
        row = self.client.get("/api/report?fuel=Electric").get_json()["series"][-1]
        self.assertEqual((row["kwh"], row["rate"]), (500.0, 0.3))

    def test_a_unit_that_does_not_match_the_fuel_is_refused(self):
        self.post(fuel="Electric", due="2026-10-16", amount="150", used="5", unit="therms",
                  start="2026-08-20", end="2026-09-18")
        self.assertIn("doesn&#39;t match", self.client.get("/connect").get_data(as_text=True))

    def test_bad_input_is_said_not_swallowed(self):
        self.post(fuel="Electric", due="", amount="150")
        self.assertIn("due", self.client.get("/connect").get_data(as_text=True))
        self.assertFalse(self.user.paths.csv.exists())


if __name__ == "__main__":
    unittest.main()
