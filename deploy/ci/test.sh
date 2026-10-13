#!/usr/bin/env bash
# Pipeline step: the whole test suite, on the Python the image runs.
#
# Runs in a clean checkout, where none of your own bills exist - the tests
# written against those skip, and the ones written against the invented
# fixtures in tests/fixtures/synthetic/ stand in for them.
#
# Exits non-zero, which fails the build, if any test fails or errors, if no
# tests were found at all, or if a database was provided and a Postgres test
# was skipped anyway. What was skipped, and why, is printed at the end. The
# results are also written to test-report.json, which ships in the image and
# is what /test-suite shows.
set -euo pipefail
cd "$(dirname "$0")/../.."

python --version
python -m pip install --quiet --no-cache-dir --disable-pip-version-check \
    --root-user-action=ignore -r requirements.txt

# The suite runs through metermatters.testreport, which records how each test
# went and writes test-report.json beside the code. The image build that
# follows copies that file in, and /test-suite shows it. The checks below are
# this step's own: the report is written whatever happened, but the build
# goes on only if everything passed.
exec python - <<'EOF'
import collections
import json
import os
import sys

from metermatters import testreport

label = (os.environ.get("COMMIT_SHA") or os.environ.get("TAG") or "")[:12] or "pipeline"
report = testreport.run(".", label=label, start="tests")
json.dump(report, open("test-report.json", "w", encoding="utf-8"))
counts = testreport.totals(report)
rows = report["tests"]

skipped = [(row["id"], row["message"]) for row in rows if row["status"] == "skipped"]
ran = counts["total"] - counts["skipped"]
print(f"{counts['total']} tests in {report['seconds']}s: {counts['passed']} passed, "
      f"{counts['failed']} failed, {counts['error']} errors, {counts['skipped']} skipped", flush=True)
for row in rows:
    if row["status"] in ("failed", "error"):
        print(f"  {row['status'].upper()}: {row['id']}")
        print("    " + row["message"].strip().replace("\n", "\n    ")[-1500:])
print(f"\n{ran} tests ran, {len(skipped)} were skipped; report labelled {label!r} written to test-report.json")
for why, count in sorted(collections.Counter(why for _, why in skipped).items()):
    print(f"  {count:4d}  {why}")

problems = []
if counts["failed"] or counts["error"]:
    problems.append(f"{counts['failed']} failed, {counts['error']} had errors")
if ran == 0:
    problems.append("no tests ran - is this the top of the checkout?")
if os.environ.get("ELEC_TEST_DATABASE_URL"):
    missed = [name for name, _ in skipped if "test_postgres" in name]
    if missed:
        problems.append(f"{len(missed)} Postgres tests were skipped although a database "
                        f"was provided, e.g. {missed[0]}")
else:
    print("  note: ELEC_TEST_DATABASE_URL is not set, so nothing was tested on Postgres")

for problem in problems:
    print(f"FAILED: {problem}", file=sys.stderr)
sys.exit(1 if problems else 0)
EOF
