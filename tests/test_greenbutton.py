"""Tests for the Eversource usage-export parsers and the bill<->usage join."""

import unittest
from datetime import date
from pathlib import Path

from metermatters.analytics import analysis
from metermatters.intake import greenbutton

HERE = Path(__file__).resolve().parent
# Invented exports with the real files' shape - see fixtures/synthetic/generate.py
# for where every number in them comes from.
SYNTHETIC = HERE / "fixtures" / "synthetic"
SYNTHETIC_USAGE = SYNTHETIC / "usage"
SYNTHETIC_13 = SYNTHETIC / "account_history_13.csv"
SYNTHETIC_ELECTRIC = SYNTHETIC_USAGE / "demo-electric-BillingData.zip"
SYNTHETIC_GAS = SYNTHETIC_USAGE / "demo-gas-BillingData.zip"

# The "usage history" layout: a read date, a day count, and a temperature.
HISTORY_TEXT = """12 Placeholder Ln, Exampleton MA 01000
Data from 9/21/2023 to 10/19/2023
Account Nickname :
Requested By  on 9/23/2026
Meter No: ,

Account,Read Date,Usage,Number of Days,Usage per day,Charge,Read Type,Average Temperature
55500020002,10/19/2023,741.00,28,26.46,$234.96,Actual Read,58.0
55500020002,9/21/2023,642.00,31,20.71,$204.89,Estimated Read,71.0
"""

# The "billing data" layout: explicit start and end dates, no temperature.
BILLING_TEXT = """Name,A PERSON
Address,"12 PLACEHOLDER LN, EXAMPLETON MA 01000"
Account Number,55500010001
Service,555000100010000000001

TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES
Electric billing,2026-07-23,2026-08-20,808.00,$278.32
Electric billing,2026-08-21,2026-09-17,714.00,$248.26
"""


class TestBillingLayout(unittest.TestCase):
    def setUp(self):
        self.export = greenbutton.parse(BILLING_TEXT)

    def test_preamble_gives_account_and_address(self):
        self.assertEqual(self.export.accounts, {"55500010001"})
        self.assertIn("PLACEHOLDER", self.export.address)

    def test_periods_parse_with_inclusive_day_counts(self):
        p = self.export.periods[-1]
        self.assertEqual((p.start, p.end), (date(2026, 8, 21), date(2026, 9, 17)))
        self.assertEqual(p.days, 28)
        self.assertAlmostEqual(p.kwh, 714.0)
        self.assertAlmostEqual(p.cost, 248.26)

    def test_rate_and_per_day(self):
        p = self.export.periods[-1]
        self.assertAlmostEqual(p.rate, 248.26 / 714.0, places=6)
        self.assertAlmostEqual(p.kwh_per_day, 714.0 / 28, places=4)

    def test_no_temperature_means_no_degree_days(self):
        self.assertIsNone(self.export.periods[-1].hdd_per_day)
        self.assertIsNone(greenbutton.weather_fit(self.export.periods))


class TestUsageHistoryLayout(unittest.TestCase):
    def setUp(self):
        self.export = greenbutton.parse(HISTORY_TEXT)

    def test_read_date_closes_the_period(self):
        p = [x for x in self.export.periods if x.end == date(2023, 10, 19)][0]
        self.assertEqual(p.start, date(2023, 9, 22))   # 28 days inclusive
        self.assertEqual(p.days, 28)

    def test_estimated_reads_are_flagged(self):
        by_end = {p.end: p for p in self.export.periods}
        self.assertTrue(by_end[date(2023, 9, 21)].estimated)
        self.assertFalse(by_end[date(2023, 10, 19)].estimated)

    def test_degree_days_use_a_65f_balance_point(self):
        by_end = {p.end: p for p in self.export.periods}
        self.assertAlmostEqual(by_end[date(2023, 10, 19)].hdd_per_day, 7.0)   # 65-58
        self.assertAlmostEqual(by_end[date(2023, 9, 21)].hdd_per_day, 0.0)    # 71F
        self.assertAlmostEqual(by_end[date(2023, 9, 21)].cdd_per_day, 6.0)    # 71-65


class TestMeteredUnits(unittest.TestCase):
    """Electric is metered in kWh, gas in therms - read it, never assume."""

    GAS = """Name,A PERSON
Address,"12 PLACEHOLDER LN, EXAMPLETON MA 01000"
Account Number,99999991094

TYPE,START DATE,END DATE,USAGE (therms),COST,NOTES
Gas billing,2026-01-21,2026-02-20,142.00,$409.50
"""

    def test_electric_export_reports_kwh(self):
        self.assertEqual(greenbutton.parse(BILLING_TEXT).periods[0].unit, "kWh")

    def test_gas_export_reports_therms(self):
        p = greenbutton.parse(self.GAS).periods[0]
        self.assertEqual(p.unit, "therms")
        self.assertAlmostEqual(p.kwh, 142.0)
        # The rate is dollars per *therm* here - mislabelling it kWh would make
        # every derived figure silently wrong.
        self.assertAlmostEqual(p.rate, 409.50 / 142.0, places=4)

    def test_a_known_unit_is_spelled_one_way_whatever_the_header_did(self):
        for spelled, canonical in (("CCF", "ccf"), ("KWH", "kWh"), ("Therms", "therms")):
            with self.subTest(spelled):
                text = self.GAS.replace("USAGE (therms)", f"USAGE ({spelled})")
                self.assertEqual(greenbutton.parse(text).periods[0].unit, canonical)

    def test_an_unknown_unit_is_carried_through_verbatim(self):
        text = self.GAS.replace("USAGE (therms)", "USAGE (beans)")
        self.assertEqual(greenbutton.parse(text).periods[0].unit, "beans")

    def test_an_unlabelled_usage_column_is_refused(self):
        # "USAGE" with no unit means we cannot know what we are reading, and
        # guessing kWh would mislabel therms. Fail loudly, like everywhere else.
        text = self.GAS.replace("USAGE (therms)", "USAGE")
        with self.assertRaises(ValueError):
            greenbutton.parse(text)


class TestRejections(unittest.TestCase):
    def test_a_file_with_no_header_is_rejected(self):
        with self.assertRaises(ValueError):
            greenbutton.parse("just some text\nand more\n")

    def test_a_header_with_no_rows_is_rejected(self):
        with self.assertRaises(ValueError):
            greenbutton.parse("pre\n\nTYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES\n")

    def test_a_period_that_ends_before_it_starts_is_skipped(self):
        # Such a row would have zero or negative days, and the per-day figures
        # built on it take the whole dashboard down.
        reversed_row = "Electric billing,2026-10-15,2026-09-18,700.00,$240.00\n"
        export = greenbutton.parse(BILLING_TEXT + reversed_row)
        self.assertEqual(len(export.periods), 2)
        self.assertTrue(all(p.days > 0 for p in export.periods))
        with self.assertRaises(ValueError):
            greenbutton.parse(BILLING_TEXT.split("TYPE,")[0] + "TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES\n"
                              + reversed_row)

    def test_negative_usage_is_skipped_in_both_layouts(self):
        export = greenbutton.parse(BILLING_TEXT + "Electric billing,2026-09-18,2026-10-15,-700.00,$240.00\n")
        self.assertEqual(len(export.periods), 2)
        export = greenbutton.parse(HISTORY_TEXT + "55500020002,11/19/2023,-741.00,28,26.46,$234.96,Actual Read,58.0\n")
        self.assertEqual(len(export.periods), 2)
        self.assertTrue(all(p.kwh_per_day >= 0 for p in export.periods))


class TestLoadAllSynthetic(unittest.TestCase):
    """Loading a folder of exports: zips, loose files, and duplicates."""

    def test_a_corrupt_zip_is_skipped_not_fatal(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
            fh.write(b"not a zip at all")
        self.assertEqual(greenbutton.load_zip(fh.name), [])

    def test_a_zip_is_read_without_unpacking_it(self):
        exports = greenbutton.load_zip(SYNTHETIC_ELECTRIC)
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(exports[0].periods), 26)
        # Archive and member, so a row can be traced to the file inside.
        self.assertEqual(
            exports[0].periods[0].source,
            "demo-electric-BillingData.zip:"
            "demo_electric_billing_billing_data_555000100010000000001_1_1.csv")

    def test_a_zip_and_its_extracted_csv_do_not_double_count(self):
        import shutil
        import tempfile
        import zipfile
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        shutil.copy(SYNTHETIC_ELECTRIC, folder)
        with zipfile.ZipFile(SYNTHETIC_ELECTRIC) as archive:
            archive.extractall(folder)
        self.assertEqual(len(list(folder.iterdir())), 2)
        self.assertEqual(len(greenbutton.load_dir(folder).periods), 26)

    def test_every_account_loads_and_stays_separate(self):
        export = greenbutton.load_dir(SYNTHETIC_USAGE)
        # Two electric accounts (renumbered) plus the gas one.
        self.assertEqual(export.accounts, {"55500010001", "55500020002", "55500030003"})
        units = {p.account: p.unit for p in export.periods}
        self.assertEqual(units, {"55500010001": "kWh", "55500020002": "kWh",
                                 "55500030003": "therms"})
        counts = {a: sum(p.account == a for p in export.periods) for a in export.accounts}
        self.assertEqual(counts, {"55500010001": 26, "55500020002": 8, "55500030003": 24})

    def test_periods_are_deduplicated_across_files(self):
        once = greenbutton.load_all([SYNTHETIC_ELECTRIC])
        twice = greenbutton.load_all([SYNTHETIC_ELECTRIC, SYNTHETIC_ELECTRIC])
        self.assertEqual(len(once.periods), 26)
        self.assertEqual(len(twice.periods), 26)

    def test_a_period_the_utility_filed_twice_counts_once(self):
        # The gas export lists 2025-04-22 onwards twice, ending a day apart.
        listed = greenbutton.load_zip(SYNTHETIC_GAS)[0].periods
        self.assertEqual(len(listed), 25)
        self.assertEqual(sum(p.start == date(2025, 4, 22) for p in listed), 2)
        kept = [p for p in greenbutton.load_all([SYNTHETIC_GAS]).periods
                if p.start == date(2025, 4, 22)]
        self.assertEqual(len(kept), 1)
        # The later end is the one that keeps the series contiguous.
        self.assertEqual(kept[0].end, date(2025, 5, 20))
        self.assertAlmostEqual(kept[0].kwh, 33.0)
        self.assertAlmostEqual(kept[0].cost, 68.76)                    # 12.00 + 33 x 1.72

    def test_the_address_comes_from_whichever_layout_has_one(self):
        self.assertIn("PLACEHOLDER", greenbutton.load_zip(SYNTHETIC_ELECTRIC)[0].address)
        history = greenbutton.load(SYNTHETIC_USAGE / "Green_Button.csv")
        self.assertEqual(history.address, "12 Placeholder Ln, Exampleton MA 01000")


class TestSyntheticExports(unittest.TestCase):
    """Invented exports in the utility's two layouts, as format fixtures."""

    @classmethod
    def setUpClass(cls):
        periods = greenbutton.load_dir(SYNTHETIC_USAGE).periods
        cls.current = [p for p in periods if p.account == "55500010001"]
        cls.old = [p for p in periods if p.account == "55500020002"]
        cls.gas = [p for p in periods if p.account == "55500030003"]

    def test_current_account_span(self):
        self.assertEqual(len(self.current), 26)
        self.assertEqual(self.current[0].start, date(2024, 7, 19))
        self.assertEqual(self.current[-1].end, date(2026, 9, 18))

    def test_the_two_accounts_are_contiguous_not_overlapping(self):
        # The old account closes the day before the current one opens.
        self.assertEqual(self.old[-1].end, date(2024, 7, 18))
        self.assertEqual(self.current[0].start, date(2024, 7, 19))

    def test_periods_within_an_account_leave_no_gaps(self):
        for periods in (self.current, self.old, self.gas):
            for before, after in zip(periods, periods[1:]):
                self.assertEqual((after.start - before.end).days, 1, after.label)

    def test_a_period_is_what_the_file_says_it_is(self):
        latest = self.current[-1]
        self.assertEqual((latest.start, latest.days), (date(2026, 8, 20), 30))
        self.assertAlmostEqual(latest.kwh, 590.0)
        self.assertAlmostEqual(latest.cost, 211.19)                    # 10.00 + 590 x 0.341
        self.assertAlmostEqual(latest.rate, 211.19 / 590.0, places=6)
        self.assertIsNone(latest.avg_temp)

    def test_old_account_has_temperatures_and_a_weak_weather_fit(self):
        fit = greenbutton.weather_fit(self.old)
        self.assertIsNotNone(fit)
        self.assertEqual(fit["n"], 8)
        self.assertLess(fit["r2"], 0.2)
        # 65F less 36 29 31 41 50 60, and nothing for the two summer months.
        self.assertAlmostEqual(fit["mean_hdd"], (29 + 36 + 34 + 24 + 15 + 5) / 8)

    def test_the_one_estimated_read_is_flagged(self):
        self.assertEqual([p.end for p in self.old if p.estimated], [date(2024, 2, 16)])

    def test_gas_has_no_temperatures_so_no_fit(self):
        self.assertEqual(len(self.gas), 24)
        self.assertIsNone(greenbutton.weather_fit(self.gas))


class TestSyntheticJoin(unittest.TestCase):
    """Bills must line up with the service periods they charge for."""

    @classmethod
    def setUpClass(cls):
        bills = analysis.build_bills(analysis.load_entries(SYNTHETIC_13))
        periods = greenbutton.load_dir(SYNTHETIC_USAGE).periods
        cls.matched = analysis.attach_usage(bills, periods)
        cls.bills = bills
        cls.report = analysis.analyse(
            analysis.build_bills(analysis.load_entries(SYNTHETIC_13)),
            {"baseline_months": 6, "target_pct": 5}, periods)

    def test_every_bill_finds_its_service_period(self):
        self.assertEqual(self.matched, 13)
        self.assertEqual(self.report["usage_matched"], 13)

    def test_no_bill_is_matched_to_the_other_fuel(self):
        self.assertEqual({b.usage.unit for b in self.bills}, {"kWh"})
        self.assertEqual({b.usage.account for b in self.bills}, {"55500010001"})

    def test_bills_are_a_month_in_arrears(self):
        # The statement due 2026-10-16 charges for usage metered in Aug/Sep.
        latest = self.report["series"][-1]
        self.assertEqual(latest["due"], "2026-10-16")
        self.assertEqual(latest["service_label"], "2026-08-20..2026-09-18")
        self.assertAlmostEqual(latest["kwh"], 590.0)

    def test_the_split_reconciles(self):
        # usage effect + rate effect must reconstruct the bill change. Each
        # part is rounded to the cent on its own, so allow the sum one cent.
        split = [r for r in self.report["series"] if r["usage_effect"] is not None]
        self.assertEqual(len(split), 12)
        for row in split:
            self.assertAlmostEqual(
                row["usage_effect"] + row["rate_effect"], row["delta"], delta=0.011,
                msg=f"split does not reconcile for {row['label']}")

    def test_a_rate_cut_is_not_mistaken_for_using_less(self):
        # Due 2026-08: 745 kWh after 730, yet $264.05 after $266.96.
        row = self.report["series"][-3]
        self.assertEqual(row["period"], "2026-08")
        self.assertAlmostEqual(row["delta"], -2.91, places=2)
        self.assertAlmostEqual(row["kwh_delta"], 15.0)
        self.assertAlmostEqual(row["usage_effect"], 266.96 / 730 * 15, places=2)
        self.assertLess(row["rate_effect"], 0)

    def test_usage_streak_is_reported(self):
        # Three statements in a row cost less; only the last two used less. The
        # streak the page shows is the one that can't be flattered by a rate cut.
        self.assertEqual(self.report["summary"]["dollar_streak"], 3)
        self.assertEqual(self.report["summary"]["usage_streak"], 2)
        self.assertEqual((self.report["summary"]["streak"], self.report["summary"]["streak_basis"]),
                         (2, "usage"))

    def test_no_usage_means_no_crash(self):
        bills = analysis.build_bills(analysis.load_entries(SYNTHETIC_13))
        report = analysis.analyse(bills, {"baseline_months": 6, "target_pct": 5}, [])
        self.assertEqual(report["usage_matched"], 0)
        self.assertIsNone(report["series"][-1]["kwh"])
        self.assertEqual(report["summary"]["usage_streak"], 0)


class TestFitMaths(unittest.TestCase):
    def test_perfect_line_gives_r2_of_one(self):
        slope, intercept, r2 = greenbutton._fit([1, 2, 3, 4], [3, 5, 7, 9])
        self.assertAlmostEqual(slope, 2.0)
        self.assertAlmostEqual(intercept, 1.0)
        self.assertAlmostEqual(r2, 1.0)

    def test_too_few_points_returns_none(self):
        self.assertIsNone(greenbutton._fit([1, 2], [1, 2]))

    def test_no_variation_in_x_returns_none(self):
        self.assertIsNone(greenbutton._fit([5, 5, 5], [1, 2, 3]))


if __name__ == "__main__":
    unittest.main()


class TestTariffSplit(unittest.TestCase):
    """A bill is a standing charge plus usage x rate; don't conflate them."""

    def _series(self, pairs, unit="therms"):
        return [{"period": f"2025-{i+1:02d}", "label": f"M{i}", "short": f"M{i}",
                 "kwh": q, "amount": c, "unit": unit, "rate": (c / q if q else None)}
                for i, (q, c) in enumerate(pairs)]

    def test_it_recovers_a_known_standing_charge_and_rate(self):
        # cost = 10 + 2*therms, exactly.
        rows = self._series([(q, 10 + 2 * q) for q in (0, 5, 20, 60, 100, 150)])
        t = analysis.tariff(rows)
        self.assertAlmostEqual(t["standing_charge"], 10.0, places=2)
        self.assertAlmostEqual(t["unit_rate"], 2.0, places=3)
        self.assertGreater(t["r2"], 0.99)
        self.assertEqual(t["unit"], "therms")

    def test_a_near_zero_usage_month_is_not_quoted_as_a_rate(self):
        # 1 therm against a $10 standing charge is "$11/therm" if you divide.
        rows = self._series([(1, 11), (60, 130), (80, 170), (100, 210), (120, 250)])
        rates = analysis.rate_history(rows, {})
        self.assertIsNotNone(rates)
        self.assertEqual(rates["excluded"], 1)
        self.assertLess(rates["max"], 5.0, "the 1-therm month should be excluded")

    def test_too_few_points_gives_no_tariff(self):
        self.assertIsNone(analysis.tariff(self._series([(10, 30), (20, 50)])))

    def test_a_nonsense_fit_is_rejected(self):
        # Cost falling as usage rises can't be a tariff.
        self.assertIsNone(analysis.tariff(self._series([(10, 90), (40, 60), (80, 40), (120, 20)])))


class TestRetentionWarnings(unittest.TestCase):
    """Freshness and loss are different; only one of them is urgent."""

    TODAY = date(2026, 9, 23)

    def _export(self, start, end, account="A"):
        return greenbutton.Export(periods=[greenbutton.Period(
            account=account, start=start, end=end, kwh=100.0, cost=30.0)])

    def test_recent_export_is_quiet(self):
        export = self._export(date(2023, 9, 25), date(2026, 8, 20))
        lines, urgent = greenbutton.staleness_report(
            export, {"A": date(2026, 9, 20)}, self.TODAY)
        self.assertFalse(urgent)
        self.assertEqual(lines, [])

    def test_old_data_at_the_window_start_is_NOT_urgent(self):
        # The oldest saved period sitting at the window edge only means you
        # can't fetch anything older - which you already have on disk.
        export = self._export(date(2023, 9, 24), date(2026, 9, 1))
        lines, urgent = greenbutton.staleness_report(
            export, {"A": date(2026, 9, 20)}, self.TODAY)
        self.assertFalse(urgent, "having old data is not a reason to warn")

    def test_urgent_only_when_the_NEWEST_saved_period_is_ageing_out(self):
        # Last exported over three years ago: re-exporting now cannot reach
        # back far enough to meet what we have, so a gap would open.
        export = self._export(date(2020, 6, 1), date(2023, 10, 1))
        lines, urgent = greenbutton.staleness_report(
            export, {"A": date(2026, 9, 20)}, self.TODAY)
        self.assertTrue(urgent)
        self.assertIn("permanent gap", lines[0])

    def test_months_behind_is_reported_without_alarm(self):
        export = self._export(date(2024, 1, 1), date(2026, 3, 1))
        lines, urgent = greenbutton.staleness_report(
            export, {"A": date(2026, 9, 20)}, self.TODAY)
        self.assertFalse(urgent)
        self.assertTrue(any("nothing is at risk" in l for l in lines))

    def test_a_closed_account_is_never_nagged_about(self):
        export = self._export(date(2023, 8, 22), date(2024, 5, 20))
        lines, urgent = greenbutton.staleness_report(
            export, {"A": date(2024, 6, 15)}, self.TODAY)
        self.assertFalse(urgent)
        self.assertEqual(lines, [])
