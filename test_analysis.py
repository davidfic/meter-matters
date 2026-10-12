"""Sanity tests for the parsing and analytics. Run: .venv/bin/python -m unittest -v"""

import unittest
from datetime import date
from pathlib import Path

import analysis
import gamify

# A frozen copy of the export as first downloaded. Regression fixtures must not
# move when the live file legitimately grows (backfill, a new statement), so
# these tests never read AccountHistory.csv.
FIXTURE_13 = Path(__file__).resolve().parent / "fixtures" / "account_history_13.csv"
# That export is personal, so it is never committed. This one has the same shape
# and invented contents, and is what a clean checkout is tested against - see
# fixtures/synthetic/generate.py for where every number in it comes from.
SYNTHETIC_13 = (Path(__file__).resolve().parent / "fixtures" / "synthetic"
                / "account_history_13.csv")

SAMPLE ="""Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,
55500010001&lt;br&gt;(electric10),Electric,2026/03/16,Payment,$300.00,APPROVED
55500010001&lt;br&gt;(electric10),Electric,2026/03/20,Current Bill,$300.00,-
55500010001&lt;br&gt;(electric10),Electric,2026/02/16,Current Bill,"$1,200.50",-
55500010001&lt;br&gt;(electric10),Electric,2026/01/16,Current Bill,$400.00,-
,,,,,
"""


class TestFields(unittest.TestCase):
    def test_clean_cell_unescapes_the_smuggled_br(self):
        self.assertEqual(
            analysis.clean_cell("55500010001&lt;br&gt;(electric10)"),
            "55500010001 (electric10)",
        )

    def test_split_account(self):
        self.assertEqual(
            analysis.split_account("55500010001&lt;br&gt;(electric10)"),
            ("55500010001", "electric10"),
        )

    def test_parse_money(self):
        self.assertEqual(analysis.parse_money("$1,200.50"), 1200.50)
        self.assertEqual(analysis.parse_money("$0.00"), 0.0)
        self.assertEqual(analysis.parse_money("($25.00)"), -25.0)
        self.assertIsNone(analysis.parse_money("-"))
        self.assertIsNone(analysis.parse_money(""))

    def test_parse_date(self):
        self.assertEqual(analysis.parse_date("2026/09/14"), date(2026, 9, 14))
        self.assertIsNone(analysis.parse_date("not a date"))


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.bills = analysis.build_bills(analysis.read_entries(SAMPLE))

    def test_blank_rows_are_dropped_and_bills_sorted_ascending(self):
        self.assertEqual([b.period for b in self.bills], ["2026-01", "2026-02", "2026-03"])

    def test_payment_matches_its_bill(self):
        march = self.bills[-1]
        self.assertEqual(march.paid, 300.00)
        self.assertEqual(march.status, "APPROVED")
        self.assertIsNone(self.bills[0].paid)

    def test_month_over_month_deltas(self):
        series = analysis.monthly_series(self.bills)
        self.assertIsNone(series[0]["delta"])
        self.assertAlmostEqual(series[1]["delta"], 800.50)
        self.assertAlmostEqual(series[2]["delta"], -900.50)
        self.assertTrue(series[2]["improved"])

    def test_streaks(self):
        series = analysis.monthly_series(self.bills)
        self.assertEqual(analysis.down_streak(series), 1)
        self.assertEqual(analysis.best_streak(series), 1)


class TestSyntheticExport(unittest.TestCase):
    """The invented 13-statement export - these are its known values.

    Each can be worked out by hand from the file: the amounts, oldest first, are
    250.48 213.74 237.12 267.18 291.60 259.92 | 235.28 217.68 242.32 266.96
    264.05 245.29 211.19, and the bar marks where the six-month baseline ends.
    """

    @classmethod
    def setUpClass(cls):
        bills = analysis.build_bills(analysis.load_entries(SYNTHETIC_13))
        cls.report = analysis.analyse(bills, {"baseline_months": 6, "target_pct": 5, "goal": None})

    def test_thirteen_statements_parsed(self):
        self.assertEqual(len(self.report["series"]), 13)
        self.assertEqual(self.report["series"][0]["period"], "2025-10")
        self.assertEqual(self.report["series"][-1]["period"], "2026-10")

    def test_the_escaped_account_cell_is_split(self):
        bills = analysis.build_bills(analysis.load_entries(SYNTHETIC_13))
        self.assertEqual({(b.account, b.nickname) for b in bills}, {("55500010001", "demo42")})

    def test_latest_statement_is_open_and_a_record_low(self):
        latest = self.report["summary"]["latest"]
        self.assertAlmostEqual(latest["amount"], 211.19)
        self.assertFalse(latest["paid"])
        self.assertTrue(self.report["summary"]["is_record_low"])

    def test_a_scheduled_payment_of_nothing_settles_nothing(self):
        # The portal lists "$0.00 NOTREADY" against the open statement.
        self.assertEqual([r["period"] for r in self.report["summary"]["unpaid"]], ["2026-10"])
        self.assertEqual(self.report["series"][-2]["paid_on"], "2026-09-12")
        self.assertEqual(self.report["series"][-2]["status"], "APPROVED")

    def test_three_month_down_streak(self):
        self.assertEqual(self.report["summary"]["streak"], 3)
        self.assertEqual(self.report["summary"]["best_streak"], 3)

    def test_baseline_and_banked_savings(self):
        # 1520.04 / 6, then seven statements totalling 1682.77 against it.
        self.assertAlmostEqual(self.report["baseline"], 253.34, places=2)
        self.assertAlmostEqual(self.report["banked"], 90.61, places=2)

    def test_rolling_comparison_suppressed_until_a_full_prior_year(self):
        self.assertIsNone(self.report["summary"]["avg_prior_12"])
        self.assertIsNone(self.report["summary"]["rolling_delta"])

    def test_year_over_year_pairs_only_october(self):
        yoy = self.report["yoy"]
        self.assertEqual(len(yoy), 1)
        self.assertEqual(yoy[0]["period"], "2026-10")
        self.assertAlmostEqual(yoy[0]["delta"], -39.29, places=2)      # 211.19 - 250.48

    def test_scoreboard_is_consistent(self):
        board = gamify.scoreboard(self.report)
        self.assertEqual(board["level"]["level"], 2)                   # $50 banked, not yet $150
        self.assertEqual(board["total_count"], len(board["badges"]))
        self.assertTrue(any(b["id"] == "record-low" and b["earned"] for b in board["badges"]))
        self.assertTrue(any(b["id"] == "streak-3" and b["earned"] for b in board["badges"]))
        self.assertFalse(any(b["id"] == "streak-5" and b["earned"] for b in board["badges"]))


@unittest.skipUnless(FIXTURE_13.exists(), "the personal export is not in this checkout")
class TestRealExport(unittest.TestCase):
    """The original 13-statement export, frozen - these are its known values."""

    @classmethod
    def setUpClass(cls):
        bills = analysis.build_bills(analysis.load_entries(FIXTURE_13))
        cls.report = analysis.analyse(bills, {"baseline_months": 6, "target_pct": 5, "goal": None})

    def test_thirteen_statements_parsed(self):
        self.assertEqual(len(self.report["series"]), 13)
        self.assertEqual(self.report["series"][0]["period"], "2025-10")
        self.assertEqual(self.report["series"][-1]["period"], "2026-10")

    def test_latest_statement_is_open_and_a_record_low(self):
        latest = self.report["summary"]["latest"]
        self.assertAlmostEqual(latest["amount"], 248.26)
        self.assertFalse(latest["paid"])
        self.assertTrue(self.report["summary"]["is_record_low"])

    def test_four_month_down_streak(self):
        self.assertEqual(self.report["summary"]["streak"], 4)

    def test_baseline_and_banked_savings(self):
        self.assertAlmostEqual(self.report["baseline"], 324.07, places=2)
        self.assertAlmostEqual(self.report["banked"], 231.63, places=2)

    def test_rolling_comparison_suppressed_until_a_full_prior_year(self):
        # 13 statements is not enough to compare against a prior twelve.
        self.assertIsNone(self.report["summary"]["avg_prior_12"])
        self.assertIsNone(self.report["summary"]["rolling_delta"])

    def test_year_over_year_pairs_only_october(self):
        yoy = self.report["yoy"]
        self.assertEqual(len(yoy), 1)
        self.assertEqual(yoy[0]["period"], "2026-10")
        self.assertAlmostEqual(yoy[0]["delta"], -12.21, places=2)

    def test_scoreboard_is_consistent(self):
        board = gamify.scoreboard(self.report)
        self.assertEqual(board["level"]["level"], 3)
        self.assertEqual(board["total_count"], len(board["badges"]))
        self.assertTrue(any(b["id"] == "record-low" and b["earned"] for b in board["badges"]))
        self.assertTrue(any(b["id"] == "streak-3" and b["earned"] for b in board["badges"]))
        self.assertFalse(any(b["id"] == "streak-5" and b["earned"] for b in board["badges"]))


class TestEdges(unittest.TestCase):
    def test_empty_csv_does_not_explode(self):
        report = analysis.analyse([], {"baseline_months": 6, "target_pct": 5})
        self.assertEqual(report["series"], [])
        self.assertEqual(report["summary"], {})

    def test_single_statement(self):
        text = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                "A&lt;br&gt;(n),Electric,2026/01/16,Current Bill,$100.00,-\n")
        bills = analysis.build_bills(analysis.read_entries(text))
        report = analysis.analyse(bills, {"baseline_months": 6, "target_pct": 5})
        self.assertEqual(len(report["series"]), 1)
        self.assertEqual(report["summary"]["streak"], 0)
        self.assertEqual(report["savings"], [])
        self.assertEqual(gamify.level_for(0)["level"], 1)


if __name__ == "__main__":
    unittest.main()


class TestNegativeSavingsAreRepresentable(unittest.TestCase):
    """A running total that goes negative must stay in range.

    The line chart used to floor its y-axis at zero, which is right for bill
    amounts and wrong for cumulative savings: with every value negative, every
    point landed outside the plot and the area fill painted over the page.
    This guards the data side of that - the series really can be all-negative.
    """

    def test_savings_can_be_entirely_negative(self):
        series = [
            {"period": "2026-01", "label": "Jan 2026", "short": "Jan '26", "amount": 400.0},
            {"period": "2026-02", "label": "Feb 2026", "short": "Feb '26", "amount": 420.0},
            {"period": "2026-03", "label": "Mar 2026", "short": "Mar '26", "amount": 430.0},
        ]
        savings, base = analysis.savings_series(series, 1)
        self.assertAlmostEqual(base, 400.0)
        self.assertEqual(len(savings), 2)
        self.assertTrue(all(s["cumulative"] < 0 for s in savings),
                        "spending above baseline must produce negative totals")
        self.assertAlmostEqual(savings[-1]["cumulative"], -50.0)


class TestDataQualityDetectors(unittest.TestCase):
    """Guards for the two things that quietly corrupt a bill history."""

    def _bills(self, amounts, fuel="Electric"):
        from datetime import date as _d
        return [analysis.Bill(period=f"2020-{i+1:02d}", due=_d(2020, i + 1, 15),
                              amount=a, account="A", nickname="n", fuel=fuel)
                for i, a in enumerate(amounts)]

    def test_a_climb_then_collapse_is_flagged_as_a_carried_balance(self):
        # What an accruing balance looks like: four rises, then a payment.
        runs = analysis.suspect_balances(self._bills([90, 180, 270, 360, 80, 95]))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["months"], 4)
        self.assertAlmostEqual(runs[0]["peak"], 360)

    def test_ordinary_seasonal_variation_is_not_flagged(self):
        # Rises and falls, but never a sustained climb followed by a collapse.
        self.assertEqual(
            analysis.suspect_balances(self._bills([90, 120, 100, 140, 110, 130])), [])

    def test_a_steady_climb_without_a_collapse_is_not_flagged(self):
        # Genuine winter heating ramps up and eases off; it doesn't crater.
        self.assertEqual(
            analysis.suspect_balances(self._bills([50, 120, 200, 280, 260, 180])), [])

    def test_anomalies_ignore_distant_years(self):
        # Prices tripled over the history, so an all-time median would flag the
        # trend rather than anything anomalous.
        series = [{"period": f"{y}-01", "label": f"Jan {y}", "short": f"Jan '{str(y)[2:]}",
                   "month": 1, "year": y, "amount": amt, "kwh_per_day": None}
                  for y, amt in [(2018, 100), (2019, 105), (2020, 110),
                                 (2025, 300), (2026, 310)]]
        flagged = {r["period"] for r in analysis.anomalies(series, {"anomaly_pct": 25})}
        self.assertNotIn("2026-01", flagged)
        self.assertNotIn("2018-01", flagged)
