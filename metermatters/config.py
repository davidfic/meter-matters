"""Where things live, and how the app is told.

Everything is configured from the environment, because that is the one
mechanism every host has - a container platform, a VPS, a laptop. Nothing here
reads a keyring, a desktop session or a unit file.

All state sits under one directory, so hosting the app means mounting one
volume. ``ELEC_DATA_DIR`` names it; unset, it is the checkout itself, which is
the layout a local run has always used.

    ELEC_DATA_DIR/AccountHistory.csv    statements
    ELEC_DATA_DIR/usage/                usage exports
    ELEC_DATA_DIR/data/                 settings and job state
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # the checkout, above the package
DATA_DIR = Path(os.environ.get("ELEC_DATA_DIR") or BASE_DIR)

CSV_PATH = Path(os.environ.get("ELEC_CSV") or DATA_DIR / "AccountHistory.csv")
USAGE_DIR = Path(os.environ.get("ELEC_USAGE_DIR") or DATA_DIR / "usage")
STATE_DIR = DATA_DIR / "data"


def flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def multi_user() -> bool:
    """Accounts are on when there is a key to sign sessions with.

    Without one this is a single person running the app on their own machine,
    on localhost, with their files where they have always been.
    """
    return bool(os.environ.get("ELEC_SECRET_KEY"))
