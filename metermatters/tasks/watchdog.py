#!/usr/bin/env python
"""Notice when a bill that should have arrived, hasn't.

The monthly import exits 2 - "nothing new" - both when there is no bill yet and
when the bill came and we failed to see it: the email template changed, the
sender changed, the app password was revoked. Those look identical from the
inside, and the dashboard just goes quietly stale.

The statements themselves say when the next one is due. Each account bills on
its own rhythm, so the expected date is the last statement plus that account's
own median gap - not a fixed day of the month.

    ./watchdog.py             # report; exit 1 if a bill is overdue
    ./watchdog.py --notify    # ...and send a notification, once a week at most

Exit codes: 0 nothing overdue, 1 something is, 2 no statements to judge by.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from metermatters.analytics import analysis
from metermatters import config
from metermatters.store import storage

DEFAULT_CSV = config.CSV_PATH
STATE_PATH = config.STATE_DIR / "watchdog.json"

EXIT_OK, EXIT_OVERDUE, EXIT_NO_DATA = 0, 1, 2

# Statements are dated by their due date, but the notice arrives well before
# it - about three weeks here. So the *next* bill is expected that much sooner
# than its own due date.
LEAD_DAYS = 21
# Billing dates wander by a few days; don't cry wolf over a weekend.
GRACE_DAYS = 5
DEFAULT_INTERVAL = 30
# Past this an account has closed, not missed a bill. Matches the usage
# staleness check, so the two never disagree about which accounts are live.
CLOSED_AFTER_DAYS = 120
# The check runs daily; one nag a week is plenty.
RENOTIFY_DAYS = 7


def billing_interval(dues: list[date]) -> int:
    """Median days between statements, ignoring gaps where months are missing."""
    gaps = [(b - a).days for a, b in zip(dues, dues[1:])]
    # Recent rhythm only, and only plausible monthly gaps: a hole in the
    # history is a 60-day gap, which says nothing about the billing cycle.
    gaps = [g for g in gaps[-12:] if 20 <= g <= 45]
    return int(median(gaps)) if gaps else DEFAULT_INTERVAL


def check(bills, today: date | None = None) -> list[dict]:
    """One row per live account, newest statement first within each fuel."""
    today = today or date.today()
    by_account: dict[tuple[str, str], list[date]] = {}
    for bill in bills:
        by_account.setdefault((bill.fuel, bill.account), []).append(bill.due)

    newest_in_fuel: dict[str, date] = {}
    for (fuel, _), dues in by_account.items():
        newest_in_fuel[fuel] = max(max(dues), newest_in_fuel.get(fuel, date.min))

    rows = []
    for (fuel, account), dues in sorted(by_account.items()):
        dues.sort()
        last = dues[-1]
        # A renumbered account stops billing the day its successor starts.
        # Only the newest account of each fuel can be missing a bill.
        if last < newest_in_fuel[fuel]:
            continue
        interval = billing_interval(dues)
        expected = last + timedelta(days=interval - LEAD_DAYS)
        late = (today - expected).days - GRACE_DAYS
        if (today - last).days > CLOSED_AFTER_DAYS:
            continue
        rows.append({
            "fuel": fuel,
            "account": account,
            "last_due": last,
            "interval": interval,
            "expected_by": expected,
            "next_due": last + timedelta(days=interval),
            "days_late": max(0, late),
            "overdue": late > 0,
        })
    return rows


def describe(row: dict) -> tuple[str, str]:
    """Title and detail, shared by the notification and the dashboard."""
    title = f"{row['fuel']} bill is {row['days_late']} days late"
    detail = (f"The last statement on file was due {row['last_due']:%b %-d}. The next, "
              f"due around {row['next_due']:%b %-d}, was expected by "
              f"{row['expected_by']:%b %-d} and hasn't been imported. Check the "
              f"mailbox import, or add it by hand.")
    return title, detail


def overdue_alerts(bills, today: date | None = None) -> list[dict]:
    """Overdue rows in the shape the overview's alert list takes."""
    alerts = []
    for row in check(bills, today):
        if not row["overdue"]:
            continue
        title, detail = describe(row)
        alerts.append({"fuel": row["fuel"], "kind": "missing",
                       "href": f"/{row['fuel'].lower()}",
                       "title": title, "detail": detail})
    return alerts


def _due_for_notice(row: dict, state: dict, today: date) -> bool:
    sent = state.get(f"{row['account']}|{row['last_due'].isoformat()}")
    if not sent:
        return True
    try:
        return (today - date.fromisoformat(sent)).days >= RENOTIFY_DAYS
    except ValueError:
        return True


def notify_overdue(rows: list[dict], state_path: Path, send, today: date | None = None) -> int:
    """Send a notice for each overdue row, at most once a week each.

    `send(title, body)` returns the channel outcomes, as notify.send does.
    Returns how many notices went out.
    """
    from metermatters.tasks import notify
    today = today or date.today()
    overdue = [r for r in rows if r["overdue"]]
    if not overdue and not Path(state_path).exists():
        return 0
    sent = 0
    with storage.locked(state_path):
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        # Forget accounts that have since caught up.
        live = {f"{r['account']}|{r['last_due'].isoformat()}" for r in overdue}
        state = {k: v for k, v in state.items() if k in live}
        for row in overdue:
            if not _due_for_notice(row, state, today):
                continue
            title, detail = describe(row)
            if notify.delivered(send(title, detail)):
                state[f"{row['account']}|{row['last_due'].isoformat()}"] = today.isoformat()
                sent += 1
        storage.atomic_write_text(state_path, json.dumps(state, indent=2) + "\n")
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Warn when an expected statement hasn't been imported.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--state", default=str(STATE_PATH), help="where --notify remembers")
    parser.add_argument("--notify", action="store_true", help="send a notification")
    parser.add_argument("--today", help="pretend it is this date (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    try:
        today = date.fromisoformat(args.today) if args.today else date.today()
    except ValueError:
        print(f"error: unparseable date: {args.today!r}", file=sys.stderr)
        return EXIT_NO_DATA

    try:
        bills = analysis.build_bills(analysis.load_entries(args.csv))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NO_DATA
    if not bills:
        print("no statements on file - nothing to judge by")
        return EXIT_NO_DATA

    rows = check(bills, today)
    overdue = [r for r in rows if r["overdue"]]
    for row in rows:
        if row["overdue"]:
            print(f"{row['fuel']:8} OVERDUE {row['days_late']}d - expected by "
                  f"{row['expected_by']}, last statement due {row['last_due']}")
        else:
            print(f"{row['fuel']:8} ok - next expected by {row['expected_by']} "
                  f"(every ~{row['interval']}d, last due {row['last_due']})")
    if not rows:
        print("no live accounts - every one has been quiet for over "
              f"{CLOSED_AFTER_DAYS} days")

    if args.notify:
        from metermatters.tasks import notify
        notify_overdue(rows, Path(args.state),
                       lambda title, body: notify.send(title, body, "critical"), today)

    return EXIT_OVERDUE if overdue else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
