#!/usr/bin/env bash
# Start the dashboard. Creates the venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi

exec .venv/bin/python app.py "$@"
