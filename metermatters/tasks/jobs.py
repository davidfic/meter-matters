#!/usr/bin/env python
"""The recurring check: import new bills, and say so when something is wrong.

This is what keeps the dashboard current without anyone touching it. It needs
no particular scheduler - anything that can do one of these on a timer will do:

    python jobs.py                      a platform's cron / scheduled job
    POST /api/tasks/check               an HTTP scheduler, with the task token
    ELEC_SCHEDULER=1                    the web app runs it itself, daily

Running it more often than bills arrive costs nothing. The import is
idempotent, and the watchdog rations its own notifications.

Each run, for every user:

1. imports new statements from their Gmail, if they chose to connect it
   (forwarded bills need no polling - they arrive on their own);
2. warns if their newest usage export is about to age out of the portal's window;
3. checks that no expected bill has gone missing.

Users hear about their own bills by email. Nothing about anyone's bill goes to
the operator's channels.

Exit codes: 0 imported something, 1 a step failed, 2 nothing new.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from metermatters.store import accounts
from metermatters.analytics import analysis
from metermatters.intake import billmail
from metermatters import config
from metermatters.store import db
from metermatters.intake import greenbutton
from metermatters.store import homes
from metermatters.tasks import notify
from metermatters.store import storage
from metermatters.tasks import watchdog
from metermatters.store import workspace

EXIT_OK, EXIT_ERROR, EXIT_NOTHING_NEW = 0, 1, 2

LAST_RUN_PATH = config.STATE_DIR / "jobs.json"
RUN_EVERY = timedelta(hours=24)
JOB_LOCK = 0x6A6F6273
# How often the in-process scheduler looks at the clock.
WAKE_SECONDS = 15 * 60


def _captured(func, argv) -> tuple[int, str]:
    """Run one of the CLIs in-process, keeping what it would have printed."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            code = func(argv)
        except Exception as exc:  # a step must never take the others down
            print(f"error: {type(exc).__name__}: {exc}")
            code = EXIT_ERROR
    return code, buffer.getvalue().strip()


def _last_line(text: str) -> str:
    return text.splitlines()[-1] if text else ""


@dataclass
class Target:
    """One person's data, and how to reach them about it."""
    label: str
    paths: accounts.Paths
    tell: object                  # (title, body, urgency) -> channel outcomes
    user: accounts.User | None = None


def targets() -> list[Target]:
    """Everyone to check. `paths` is filled in when their files are opened."""
    if config.multi_user():
        return [Target(u.email, None,
                       lambda t, b, urgency="normal", to=u.email: notify.send_user(to, t, b, urgency),
                       u)
                for u in accounts.everyone()]
    # One person, their own machine: every channel is theirs.
    return [Target("local", None, notify.send)]


def step_import(target: Target) -> dict:
    if target.user is not None:
        # Hosted: bills arrive at the forwarding address on their own.
        return {"status": "off", "detail": "bills arrive by forwarding"}
    if not os.environ.get("ELEC_IMAP_USER"):
        return {"status": "off", "detail": "ELEC_IMAP_USER is not set"}
    code, output = _captured(billmail.main, ["--imap", "--import", "--fuel", "any"])
    if code == EXIT_OK:
        target.tell("New bill imported", _last_line(output))
        return {"status": "imported", "detail": output}
    if code == EXIT_NOTHING_NEW:
        return {"status": "nothing-new", "detail": output}
    target.tell("Bill import failed", _last_line(output), "critical")
    return {"status": "failed", "detail": output}


def step_staleness(target: Target) -> dict:
    try:
        export = greenbutton.load_dir(target.paths.usage)
        last_billed: dict = {}
        if target.paths.csv.exists():
            for bill in analysis.build_bills(analysis.load_entries(target.paths.csv)):
                digits = "".join(c for c in bill.account if c.isdigit())
                if digits and (digits not in last_billed or bill.due > last_billed[digits]):
                    last_billed[digits] = bill.due
        lines, urgent = greenbutton.staleness_report(export, last_billed)
    except (OSError, ValueError) as exc:
        return {"status": "failed", "detail": str(exc)}
    if urgent:
        target.tell("Re-export your usage data", "\n".join(lines), "critical")
    return {"status": "urgent" if urgent else "ok",
            "detail": "\n".join(lines) or "usage exports are current"}


def step_watchdog(target: Target) -> dict:
    if not target.paths.csv.exists():
        return {"status": "off", "detail": "no statements on file yet"}
    try:
        rows = watchdog.check(analysis.build_bills(analysis.load_entries(target.paths.csv)))
        watchdog.notify_overdue(rows, target.paths.watchdog,
                                lambda title, body: target.tell(title, body, "critical"))
    except (OSError, ValueError) as exc:
        return {"status": "failed", "detail": str(exc)}
    late = [r for r in rows if r["overdue"]]
    return {"status": "overdue" if late else "ok",
            "detail": "; ".join(f"{r['fuel']} {r['days_late']}d late" for r in late)
                      or "nothing overdue"}


STEPS = {"import": step_import, "staleness": step_staleness, "watchdog": step_watchdog}


# Worst first: when one home is late and another fine, the person is told about the late one.
_SEVERITY = ("failed", "urgent", "overdue", "imported", "ok", "nothing-new", "off")


def _worst(results: list[tuple[str, dict]], several: bool) -> dict:
    """One outcome for a step that ran once per home."""
    if not several:
        return results[0][1]
    pick = min((r for _, r in results),
               key=lambda r: _SEVERITY.index(r["status"]) if r["status"] in _SEVERITY else 0)
    return {"status": pick["status"],
            "detail": "; ".join(f"{name}: {r['detail']}" for name, r in results if r["detail"])}


REDUCED_KEY = "files_reduced"              # set once a pass has reduced everyone's files


def _reduce(base) -> bool:
    """Files kept before names, streets and full account numbers were left out
    of them are reduced here. Returns whether every home was done; never a
    reason for the check to fail."""
    from metermatters.intake import redact
    done = True
    for home in homes.everything(base):
        try:
            redact.scrub(homes.paths_for(base, home))
        except Exception:                      # noqa: BLE001 - best effort, every run
            done = False
    return done


def _reduce_log() -> bool:
    """Subject lines logged before long numbers were cut from them, cut now.
    Returns whether it got through; never a reason for the check to fail."""
    from metermatters.intake import redact
    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT id, subject FROM inbound_log WHERE subject <> ''").fetchall()
            for row in rows:
                reduced = redact.subject(row["subject"])
                if reduced != row["subject"]:
                    conn.execute("UPDATE inbound_log SET subject = ? WHERE id = ?", (reduced, row["id"]))
        return True
    except Exception:                          # noqa: BLE001 - best effort, every run
        return False


def _reduced_already() -> bool:
    """Has one check reduced everything that was on disk and in the log before
    the intake started reducing it? Everything since arrives reduced, so after
    that pass the work is a no-op and is skipped - rereading and rebuilding
    every zip on every run was the whole of the check's cost for some."""
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (REDUCED_KEY,)).fetchone()
        return row is not None
    except Exception:                          # noqa: BLE001 - no record means do it again
        return False


def _mark_reduced(when: datetime) -> None:
    try:
        with db.connect() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                         (REDUCED_KEY, when.isoformat(timespec="seconds")))
    except Exception:                          # noqa: BLE001 - next run does the pass again
        pass


def _run_steps(target: Target, base, reduce: bool = True) -> dict:
    """Every step for one person: the mailbox once, everything else once per home."""
    # Import first: a bill that just arrived must not be reported missing.
    steps = {}
    target.paths = base
    try:
        steps["import"] = STEPS["import"](target)
    except Exception as exc:                   # one bad step stops nobody else
        steps["import"] = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}

    homes.purge(base)
    if reduce:
        steps["_reduced"] = _reduce(base)
    each = homes.active(base)
    several = len(each) > 1
    for name in ("staleness", "watchdog"):
        results = []
        for home in each:
            own = Target(target.label, homes.paths_for(base, home),
                         (lambda t, b, urgency="normal", n=home.name:
                          target.tell(f"{n}: {t}", b, urgency)) if several else target.tell,
                         target.user)
            try:
                results.append((home.name, STEPS[name](own)))
            except Exception as exc:
                results.append((home.name, {"status": "failed",
                                            "detail": f"{type(exc).__name__}: {exc}"}))
        steps[name] = _worst(results, several)
    if target.user is not None:
        steps["benchmark"] = _step_benchmark(target.user, base, each)
    return steps


def _step_benchmark(user, base, each) -> dict:
    """Each home's average into its state's bucket, or out of it. Never a notification."""
    import json as _json
    from metermatters.analytics import benchmarks
    from metermatters import region
    written = 0
    try:
        for home in each:
            paths = homes.paths_for(base, home)
            try:
                state = _json.loads(paths.state.read_text(encoding="utf-8")) if paths.state.exists() else {}
            except ValueError:
                state = {}
            export = greenbutton.load_dir(paths.usage) if paths.usage.exists() else None
            where = region.resolve(state.get("state"), export.address if export else None)
            written += benchmarks.publish(user.id, home.id, where, export.periods if export else [],
                                          allowed=bool(state.get("share_benchmark", True)))
    except Exception as exc:                                    # noqa: BLE001
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", "detail": f"{written} row{'s' if written != 1 else ''} published"}


def run_check() -> dict:
    """Run every step for everyone and record the outcome. Safe at any time."""
    started = datetime.now(timezone.utc)
    reducing = not _reduced_already()
    reduced = _reduce_log() if reducing else False
    people = []
    for target in targets():
        steps = {}
        try:
            with workspace.open_(target.user, write=True) as base:
                steps = _run_steps(target, base, reduce=reducing)
        except Exception as exc:               # couldn't open, or couldn't save
            for name in STEPS:
                steps.setdefault(name, {"status": "failed",
                                        "detail": f"{type(exc).__name__}: {exc}"})
        reduced = reduced and steps.pop("_reduced", False)
        people.append({"who": target.label, "steps": steps})
    if reducing and reduced:
        _mark_reduced(started)

    tally: dict[str, dict[str, int]] = {name: {} for name in list(STEPS) + ["benchmark"]}
    for person in people:
        for name, step in person["steps"].items():
            tally.setdefault(name, {})[step["status"]] = tally.get(name, {}).get(step["status"], 0) + 1
    result = {
        "ran_at": started.isoformat(timespec="seconds"),
        "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "ok": not any(t.get("failed") for t in tally.values()),
        "people": len(people),
        # Counts only. This file is the operator's; whose bill did what is not.
        "steps": tally,
    }
    _record(result)
    result["detail"] = people
    return result


def _record(result: dict) -> None:
    text = json.dumps(result, indent=2) + "\n"
    if db.is_postgres():
        # No disk to speak of, and every instance needs to see it.
        with db.connect() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('last_run', ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (text,))
    else:
        storage.atomic_write_text(LAST_RUN_PATH, text)


def last_run() -> dict | None:
    try:
        if db.is_postgres():
            with db.connect() as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'last_run'").fetchone()
            return json.loads(row["value"]) if row else None
        return json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def exit_code(result: dict) -> int:
    if not result["ok"]:
        return EXIT_ERROR
    return EXIT_OK if result["steps"]["import"].get("imported") else EXIT_NOTHING_NEW


# --------------------------------------------------------------------------
# Running it from inside the web app
# --------------------------------------------------------------------------

def due(now: datetime | None = None) -> bool:
    previous = last_run()
    if not previous:
        return True
    try:
        ran = datetime.fromisoformat(previous["ran_at"])
    except (KeyError, ValueError):
        return True
    return (now or datetime.now(timezone.utc)) - ran >= RUN_EVERY


def run_if_due() -> dict | None:
    """Run the check if a day has passed - and if no other worker already is."""
    if db.is_postgres():
        # A file lock reaches one machine. This one reaches every instance.
        with db.connect() as conn:
            conn.lock(JOB_LOCK)
            return run_check() if due() else None
    with storage.try_locked(LAST_RUN_PATH) as mine:
        # Checked inside the lock: the worker that lost the race must see the
        # winner's result, not run the job a second time.
        if not mine or not due():
            return None
        return run_check()


def start_scheduler() -> threading.Thread:
    """A daemon thread that keeps the check running for as long as the app is up.

    For a host that keeps one instance alive. Where instances sleep when idle,
    point the platform's scheduler at POST /api/tasks/check instead.
    """
    def loop():
        while True:
            try:
                run_if_due()
            except Exception as exc:
                print(f"scheduler: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            time.sleep(WAKE_SECONDS)

    thread = threading.Thread(target=loop, name="elec-scheduler", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return EXIT_OK
    result = run_check()
    for person in result["detail"]:
        print(person["who"])
        for name, step in person["steps"].items():
            print(f"  [{name}] {step['status']}")
            for line in step["detail"].splitlines():
                print(f"      {line}")
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
