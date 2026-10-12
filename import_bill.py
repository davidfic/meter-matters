#!/usr/bin/env python
"""Add a statement to AccountHistory.csv from the command line.

This is the seam every automated route plugs into - a scheduled job, an email
poller, or you at a terminal. It never replaces the file: rows are merged and
de-duplicated, so running it twice on the same bill is a no-op.

    # a whole export downloaded from the utility
    ./import_bill.py --file ~/Downloads/AccountHistory.csv

    # a single statement, e.g. parsed out of a bill-ready email
    ./import_bill.py --amount 248.26 --due 2026-10-16

    # see what would change without touching anything
    ./import_bill.py --file export.csv --dry-run

Exit codes: 0 added something, 1 an error, 2 nothing new (already up to date).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import analysis
import config
import ingest

DEFAULT_CSV = config.CSV_PATH

EXIT_OK, EXIT_ERROR, EXIT_NOTHING_NEW = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge a statement or a full export into AccountHistory.csv.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", "-f", metavar="PATH",
                        help="a CSV export to merge ('-' reads stdin)")
    source.add_argument("--amount", "-a", type=float, metavar="N",
                        help="a single statement amount, e.g. 248.26")

    parser.add_argument("--due", "-d", metavar="DATE",
                        help="due date for --amount (YYYY-MM-DD or YYYY/MM/DD)")
    parser.add_argument("--account", metavar="ID",
                        help="account to file it under (defaults to the one already in the CSV)")
    parser.add_argument("--account-type", default="Electric",
                        help="account type column (default: Electric)")
    parser.add_argument("--type", dest="kind", default="Current Bill",
                        choices=["Current Bill", "Payment"],
                        help="row type (default: Current Bill)")
    parser.add_argument("--status", default="-",
                        help="status column (default: '-')")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), metavar="PATH",
                        help=f"the history file to merge into (default: {DEFAULT_CSV})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    parser.add_argument("--quiet", "-q", action="store_true", help="only report errors")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.csv)

    def say(message: str) -> None:
        if not args.quiet:
            print(message)

    try:
        if args.file:
            text = sys.stdin.read() if args.file == "-" else \
                Path(args.file).read_text(encoding="utf-8-sig")
            result = ingest.merge_csv_text(target, text, dry_run=args.dry_run)
        else:
            if not args.due:
                print("error: --due is required with --amount", file=sys.stderr)
                return EXIT_ERROR
            account = args.account or ingest.default_account(target)
            if not account:
                print("error: no account in the CSV yet - pass --account", file=sys.stderr)
                return EXIT_ERROR
            row = ingest.bill_row(account, args.due, args.amount,
                                  account_type=args.account_type,
                                  kind=args.kind, status=args.status)
            result = ingest.merge(target, [row], list(row), dry_run=args.dry_run)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    prefix = "would add" if args.dry_run else "added"
    if not result.added:
        say(f"nothing new - all {result.skipped} incoming row(s) were already on file")
        return EXIT_NOTHING_NEW

    say(f"{prefix} {result.added} row(s); {result.total} on file"
        + (f" ({result.skipped} already present)" if result.skipped else ""))

    # Show the effect on the thing the user actually cares about.
    bills = analysis.build_bills(
        analysis.read_entries(
            "\n".join([",".join(result.fieldnames)] +
                      [",".join(f'"{r.get(k, "")}"' for k in result.fieldnames)
                       for r in result.rows])
        )
    )
    series = analysis.monthly_series(bills)
    if series and not args.quiet:
        latest = series[-1]
        change = "" if latest["delta"] is None else \
            f"  ({latest['delta']:+.2f} vs prior, {latest['pct']:+.1f}%)"
        print(f"latest statement: {latest['label']}  ${latest['amount']:,.2f}{change}")
        print(f"down-streak: {analysis.down_streak(series)} month(s)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
