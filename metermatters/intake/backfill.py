#!/usr/bin/env python
"""Extend the bill history backwards from the usage export.

`AccountHistory.csv` only goes back as far as the portal's payment history -
13 statements. The usage export carries **cost as well as kWh** for every
period Eversource has on file, which here is more than twice as far back. That
extra history is what makes a year-over-year comparison possible, and
year-over-year is the only seasonally fair scoreboard.

Synthesised rows are marked ``BACKFILL`` in the Status column so they are never
mistaken for statements scraped from the portal. The amounts are Eversource's
own; only the **due date is estimated**, from the median lag observed between a
service period ending and its statement falling due.

    ./backfill.py --dry-run
    ./backfill.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from statistics import median

from metermatters.analytics import analysis
from metermatters import config
from metermatters import fuels
from metermatters.intake import greenbutton
from metermatters.intake import ingest

DEFAULT_CSV = config.CSV_PATH
DEFAULT_USAGE = config.USAGE_DIR
BACKFILL_STATUS = "BACKFILL"
FALLBACK_LAG = 28  # days from service-period end to due date


def observed_lag(bills, periods) -> tuple[int, int]:
    """Median days between a period ending and its statement falling due."""
    analysis.attach_usage(bills, periods)
    lags = [(b.due - b.usage.end).days for b in bills if b.usage]
    return (int(median(lags)) if lags else FALLBACK_LAG), len(lags)


def already_billed(period, bills, lag: int) -> bool:
    """Is this service period already represented by a statement?

    Matching on the amount, with a generous date window, so a backfilled row
    and a later real export of the same bill can't both land.
    """
    for bill in bills:
        if bill.account != period.account:
            continue
        if abs(bill.amount - period.cost) < 0.01 and abs((bill.due - period.end).days - lag) <= 15:
            return True
    return False


def fill_missing(csv_path, usage_dir, today=None) -> int:  # -> how many statements were made
    """Make statements out of usage, for a fuel that has none at all.

    A utility that gives only a usage-with-cost file (no statement history)
    would otherwise leave the dashboard empty: it is built from statements.
    Each metered period becomes one, due ``FALLBACK_LAG`` days after it ends and
    marked ``BACKFILL``, so it can be told from a real one and is replaced
    in spirit - never duplicated - when real statements arrive. A fuel that
    already has statements is left alone: that is the ``main`` command's job.
    """
    from datetime import date as _date
    csv_path = Path(csv_path)
    today = today or _date.today()
    have = {b.fuel for b in analysis.build_bills(analysis.load_entries(csv_path))} \
        if csv_path.exists() else set()
    rows = []
    for period in greenbutton.load_dir(usage_dir).periods:
        fuel = fuels.of_unit(period.unit).name
        if fuel in have or not period.cost or period.cost <= 0:
            continue
        due = period.end + timedelta(days=FALLBACK_LAG)
        rows.append(ingest.bill_row(period.account, due.isoformat(), period.cost,
                                    account_type=fuel, status=BACKFILL_STATUS,
                                    source="usage-export"))
        if (today - due).days > 20:        # long gone: don't leave it reading as unpaid
            rows.append(ingest.bill_row(period.account, (due - timedelta(days=4)).isoformat(),
                                        period.cost, account_type=fuel, kind="Payment",
                                        status=BACKFILL_STATUS, source="usage-export"))
    if not rows or not ingest.merge(csv_path, rows, list(rows[0])).added:
        return 0
    return sum(1 for row in rows if row["Type"] == "Current Bill")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--usage-dir", default=str(DEFAULT_USAGE))
    parser.add_argument("--account", help="limit to one account (default: the CSV's own)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    target = Path(args.csv)
    bills = analysis.build_bills(analysis.load_entries(target))
    if not bills:
        print("error: no existing bills to anchor against", file=sys.stderr)
        return 1

    account_raw = ingest.default_account(target)
    # split_account strips the escaped <br> and the "(nickname)" suffix - doing
    # it by hand would pull the digits out of a nickname like "electric10".
    account_digits = args.account or analysis.split_account(account_raw or "")[0]
    periods = [p for p in greenbutton.load_dir(args.usage_dir).periods
               if p.account == account_digits]
    if not periods:
        print(f"error: no usage periods for account {account_digits}", file=sys.stderr)
        return 1

    lag, matched = observed_lag(bills, list(periods))
    print(f"account {account_digits}: {len(bills)} statements on file, "
          f"{len(periods)} usage periods available")
    print(f"median statement lag: {lag} days (from {matched} matched pairs)")

    missing = [p for p in periods if not already_billed(p, bills, lag)]
    if not missing:
        print("nothing to backfill - every usage period already has a statement")
        return 2

    rows = []
    for period in sorted(missing, key=lambda p: p.end):
        due = period.end + timedelta(days=lag)
        # The bill, plus a payment so the history doesn't read as unpaid. Both
        # carry BACKFILL so their provenance is never in doubt.
        # Mark the bill row too, not just the payment. Leaving it as "-" made
        # a synthesised statement indistinguishable from a real one, so a later
        # import with the true due date landed alongside it as a duplicate.
        rows.append(ingest.bill_row(account_raw, due.isoformat(), period.cost,
                                    status=BACKFILL_STATUS, source="usage-export"))
        rows.append(ingest.bill_row(account_raw, (due - timedelta(days=4)).isoformat(),
                                    period.cost, kind="Payment", status=BACKFILL_STATUS,
                                    source="usage-export"))

    print(f"\n{'would add' if args.dry_run else 'adding'} {len(missing)} statements:")
    for period in sorted(missing, key=lambda p: p.end):
        print(f"  {period.label}  {period.kwh:6.0f} kWh  ${period.cost:7.2f}  "
              f"-> due {(period.end + timedelta(days=lag)).isoformat()} (estimated)")

    result = ingest.merge(target, rows, list(rows[0]), dry_run=args.dry_run)
    print(f"\n{'would write' if args.dry_run else 'wrote'} {result.added} rows; "
          f"{result.total} on file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
