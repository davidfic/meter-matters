#!/usr/bin/env bash
# Pipeline step: the whole test suite, on the Python the image runs.
#
# Runs in a clean checkout, where none of your own bills exist - the tests
# written against those skip, and the ones written against the invented
# fixtures in tests/fixtures/synthetic/ stand in for them.
#
# Exits non-zero, which fails the build, if any test fails or errors, if no
# tests were found at all, or if a database was provided and a Postgres test
# was skipped anyway. What was skipped, and why, is printed at the end.
set -euo pipefail
cd "$(dirname "$0")/../.."

python --version
python -m pip install --quiet --no-cache-dir --disable-pip-version-check \
    --root-user-action=ignore -r requirements.txt

exec python - <<'EOF'
import collections
import os
import sys
import unittest

suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py", top_level_dir=".")
result = unittest.TextTestRunner(verbosity=1).run(suite)

skipped = [(getattr(test, "id", lambda: str(test))(), why) for test, why in result.skipped]
ran = result.testsRun - len(skipped)
print(f"\n{ran} tests ran, {len(skipped)} were skipped", flush=True)
for why, count in sorted(collections.Counter(why for _, why in skipped).items()):
    print(f"  {count:4d}  {why}")

problems = []
if not result.wasSuccessful():
    problems.append(f"{len(result.failures)} failed, {len(result.errors)} had errors")
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
