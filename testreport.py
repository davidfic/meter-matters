#!/usr/bin/env python
"""Run the test suite here and write what happened to a file that ships with the site.

The suite never runs on the server: it patches the environment and the network
while it runs, and the image doesn't carry the tests. It runs where the code is
written, as the last step of ``./deploy/cloudrun.sh deploy``, and the results
travel with the code they describe, in ``test-report.json``. The ``/test-suite``
page only reads that file.

    python testreport.py                  run, write test-report.json, print a summary
    python testreport.py --out other.json
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import sys
import time
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("passed", "failed", "error", "skipped")
MAX_TESTS = 5000
MAX_MESSAGE = 4000
MAX_BYTES = 2 * 1024 * 1024
REPORT_PATH = Path(__file__).resolve().parent / "test-report.json"


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

class Collector(unittest.TestResult):
    """Remembers how each test went, in a shape that can be shown."""

    def __init__(self):
        super().__init__()
        self.rows: dict[str, dict] = {}
        self._began: dict[str, float] = {}

    def _row(self, test) -> dict:
        key = test.id()
        if key not in self.rows:
            case, _, name = key.rpartition(".")
            module = case.split(".")[0] if case else key
            doc = (getattr(test, "_testMethodDoc", None) or "").strip().splitlines()
            self.rows[key] = {"id": key, "module": module, "case": case.rpartition(".")[2] or module,
                              "name": name, "doc": doc[0].strip() if doc else "",
                              "status": None, "seconds": 0.0, "message": ""}
        return self.rows[key]

    def startTest(self, test):
        super().startTest(test)
        self._row(test)
        self._began[test.id()] = time.perf_counter()

    def stopTest(self, test):
        super().stopTest(test)
        row = self._row(test)
        row["seconds"] = round(time.perf_counter() - self._began.pop(test.id(), time.perf_counter()), 4)
        if row["status"] is None:
            row["status"] = "passed"

    def _fail(self, test, err, status):
        row = self._row(test)
        text = "".join(traceback.format_exception(*err)) if isinstance(err, tuple) else str(err)
        row["message"] = (row["message"] + ("\n" if row["message"] else "") + text)[-MAX_MESSAGE:]
        if row["status"] != "error":
            row["status"] = status

    def addSuccess(self, test):
        super().addSuccess(test)
        self._row(test)["status"] = self._row(test)["status"] or "passed"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._fail(test, err, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self._fail(test, err, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        row = self._row(test)
        row["status"], row["message"] = "skipped", str(reason)[:MAX_MESSAGE]

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._row(test)["status"] = "passed"

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._fail(test, "expected to fail, but passed", "failed")

    def addSubTest(self, test, subtest, err):
        # A passing sub-test says nothing; a failing one fails the test it is in.
        super().addSubTest(test, subtest, err)
        if err is not None:
            label = subtest._subDescription() if hasattr(subtest, "_subDescription") else "subtest"
            self._fail(test, err, "failed" if issubclass(err[0], AssertionError) else "error")
            row = self._row(test)
            row["message"] = f"[{label}]\n{row['message']}"[-MAX_MESSAGE:]


def run(top: str = ".", pattern: str = "test_*.py", label: str = "") -> dict:
    """Run every test under `top` and return the report."""
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()
    # Its own loader: the shared one remembers the last directory it was pointed at.
    suite = unittest.TestLoader().discover(top, pattern=pattern, top_level_dir=top)
    result = Collector()
    # Tests print as they go (importers, backfills). That is noise here.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        suite.run(result)
    tests = sorted(result.rows.values(), key=lambda r: (r["module"], r["case"], r["name"]))
    return {
        "version": 1,
        "label": label,
        "started": started.isoformat(timespec="seconds"),
        "seconds": round(time.perf_counter() - clock, 2),
        "python": platform.python_version(),
        "system": platform.system(),
        "tests": tests,
    }


# --------------------------------------------------------------------------
# What the server will accept, keep and show
# --------------------------------------------------------------------------

def _text(value, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def clean(payload) -> dict:
    """A report as posted, checked and trimmed. Raises ValueError for anything else."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tests"), list):
        raise ValueError("that isn't a test report")
    if len(payload["tests"]) > MAX_TESTS:
        raise ValueError(f"more than {MAX_TESTS} tests")
    tests = []
    for row in payload["tests"]:
        if not isinstance(row, dict) or row.get("status") not in STATUSES:
            raise ValueError("a test has no valid status")
        try:
            seconds = round(max(0.0, float(row.get("seconds") or 0)), 4)
        except (TypeError, ValueError):
            raise ValueError("a test has no valid time") from None
        tests.append({"id": _text(row.get("id"), 300), "module": _text(row.get("module"), 100),
                      "case": _text(row.get("case"), 100), "name": _text(row.get("name"), 200),
                      "doc": _text(row.get("doc"), 300), "status": row["status"],
                      "seconds": seconds, "message": _text(row.get("message"), MAX_MESSAGE)})
    try:
        started = datetime.fromisoformat(str(payload.get("started")))
    except ValueError:
        raise ValueError("the report has no start time") from None
    report = {"version": 1, "label": _text(payload.get("label"), 100),
              "started": started.isoformat(timespec="seconds"),
              "seconds": round(max(0.0, float(payload.get("seconds") or 0)), 2),
              "python": _text(payload.get("python"), 30), "system": _text(payload.get("system"), 30),
              "tests": tests}
    if len(json.dumps(report)) > MAX_BYTES:
        raise ValueError("that report is too large")
    return report


def totals(report: dict) -> dict:
    counts = {s: 0 for s in STATUSES}
    for row in report["tests"]:
        counts[row["status"]] += 1
    return {"total": len(report["tests"]), **counts}


def modules(report: dict) -> list[dict]:
    """The tests grouped by file and then by class, worst files first."""
    grouped: dict[str, dict[str, list]] = {}
    for row in report["tests"]:
        grouped.setdefault(row["module"], {}).setdefault(row["case"], []).append(row)
    out = []
    for module, cases in grouped.items():
        every = [t for rows in cases.values() for t in rows]
        out.append({"module": module, "counts": totals({"tests": every}),
                    "seconds": round(sum(t["seconds"] for t in every), 2),
                    "cases": [{"case": c, "tests": rows} for c, rows in sorted(cases.items())]})
    out.sort(key=lambda m: (-(m["counts"]["failed"] + m["counts"]["error"]), m["module"]))
    return out


def load(path=None) -> dict | None:
    """The report that shipped with this code, or None if there isn't one."""
    try:
        return clean(json.loads(Path(path or REPORT_PATH).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="", help="what was tested, e.g. the revision being deployed")
    parser.add_argument("--out", default=str(REPORT_PATH), help="where to write the report")
    args = parser.parse_args(argv)

    report = run(str(Path(__file__).resolve().parent), label=args.label)
    counts = totals(report)
    print(f"{counts['total']} tests in {report['seconds']}s: {counts['passed']} passed, "
          f"{counts['failed']} failed, {counts['error']} errors, {counts['skipped']} skipped")
    for row in report["tests"]:
        if row["status"] in ("failed", "error"):
            print(f"  {row['status'].upper()}: {row['id']}")
    Path(args.out).write_text(json.dumps(report), encoding="utf-8")
    return 0 if not (counts["failed"] or counts["error"]) else 1


if __name__ == "__main__":
    sys.exit(main())
