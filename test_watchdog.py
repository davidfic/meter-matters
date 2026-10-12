"""Tests for the missing-bill watchdog."""

import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import analysis
import ingest
import watchdog


def bills(*specs):
    """specs: (fuel, account, [due dates])"""
    return [analysis.Bill(period=f"{d.year:04d}-{d.month:02d}", due=d, amount=100.0,
                          account=account, nickname="", fuel=fuel)
            for fuel, account, dues in specs for d in dues]


def monthly(start: date, count: int, step: int = 30):
    return [start + timedelta(days=step * i) for i in range(count)]


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.dues = monthly(date(2026, 1, 16), 10)       # last due 2026-10-13
        self.last = self.dues[-1]
        self.expected = self.last + timedelta(days=30 - watchdog.LEAD_DAYS)

    def check(self, today, *extra):
        return watchdog.check(bills(("Electric", "111", self.dues), *extra), today)

    def test_quiet_while_the_next_bill_is_not_yet_expected(self):
        row, = self.check(self.last - timedelta(days=10))
        self.assertFalse(row["overdue"])
        self.assertEqual(row["expected_by"], self.expected)

    def test_quiet_inside_the_grace_period(self):
        row, = self.check(self.expected + timedelta(days=watchdog.GRACE_DAYS))
        self.assertFalse(row["overdue"])

    def test_overdue_once_grace_runs_out(self):
        row, = self.check(self.expected + timedelta(days=watchdog.GRACE_DAYS + 3))
        self.assertTrue(row["overdue"])
        self.assertEqual(row["days_late"], 3)

    def test_a_closed_account_is_not_a_missing_bill(self):
        self.assertEqual(self.check(self.last + timedelta(days=200)), [])

    def test_a_renumbered_account_is_not_chased(self):
        successor = monthly(self.last + timedelta(days=30), 3)
        rows = self.check(successor[-1], ("Electric", "222", successor))
        self.assertEqual([r["account"] for r in rows], ["222"])

    def test_fuels_are_judged_separately(self):
        gas = monthly(date(2026, 1, 16), 8)               # stops two months early
        rows = self.check(self.last, ("Gas", "333", gas))
        state = {r["fuel"]: r["overdue"] for r in rows}
        self.assertEqual(state, {"Electric": False, "Gas": True})

    def test_the_interval_is_the_accounts_own(self):
        self.dues = monthly(date(2026, 1, 16), 8, step=33)
        row, = self.check(self.dues[-1])
        self.assertEqual(row["interval"], 33)

    def test_a_hole_in_the_history_does_not_stretch_the_interval(self):
        self.dues = monthly(date(2025, 1, 16), 4) + monthly(date(2026, 1, 16), 4)
        row, = self.check(self.dues[-1])
        self.assertEqual(row["interval"], 30)

    def test_one_statement_falls_back_to_a_month(self):
        self.dues = [date(2026, 5, 16)]
        row, = self.check(date(2026, 5, 20))
        self.assertEqual(row["interval"], watchdog.DEFAULT_INTERVAL)

    def test_alerts_take_the_overview_shape(self):
        alert, = watchdog.overdue_alerts(bills(("Gas", "333", self.dues)),
                                         self.expected + timedelta(days=20))
        self.assertEqual(alert["href"], "/gas")
        self.assertEqual(alert["kind"], "missing")
        self.assertIn("15 days late", alert["title"])


class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.csv = self.dir / "AccountHistory.csv"
        self.state = self.dir / "watchdog.json"
        rows = [ingest.bill_row("111", d.isoformat(), 100.0)
                for d in monthly(date(2026, 1, 16), 10)]
        ingest.merge(self.csv, rows, list(rows[0]))

    def run_cli(self, today, *extra):
        with mock.patch("sys.stdout"):
            return watchdog.main(["--csv", str(self.csv), "--state", str(self.state),
                                  "--today", today, *extra])

    def test_exit_codes(self):
        self.assertEqual(self.run_cli("2026-10-01"), watchdog.EXIT_OK)
        self.assertEqual(self.run_cli("2026-11-05"), watchdog.EXIT_OVERDUE)
        self.assertEqual(watchdog.main(["--csv", str(self.dir / "nope.csv")]),
                         watchdog.EXIT_NO_DATA)

    def test_notifies_once_then_waits_a_week(self):
        with mock.patch("notify.send", return_value={"desktop": "sent"}) as send:
            self.run_cli("2026-11-05", "--notify")
            self.run_cli("2026-11-08", "--notify")
            self.assertEqual(send.call_count, 1)
            self.run_cli("2026-11-12", "--notify")
            self.assertEqual(send.call_count, 2)

    def test_an_undelivered_notice_is_retried(self):
        with mock.patch("notify.send", return_value={"desktop": "failed"}) as send:
            self.run_cli("2026-11-05", "--notify")
            self.run_cli("2026-11-06", "--notify")
            self.assertEqual(send.call_count, 2)

    def test_state_is_forgotten_once_the_bill_arrives(self):
        with mock.patch("notify.send", return_value={"desktop": "sent"}):
            self.run_cli("2026-11-05", "--notify")
        self.assertTrue(json.loads(self.state.read_text()))
        row = ingest.bill_row("111", "2026-11-12", 100.0)
        ingest.merge(self.csv, [row], list(row))
        with mock.patch("notify.send", return_value={"desktop": "sent"}):
            self.run_cli("2026-12-20", "--notify")
        self.assertEqual(list(json.loads(self.state.read_text())), ["111|2026-11-12"])


if __name__ == "__main__":
    unittest.main()
