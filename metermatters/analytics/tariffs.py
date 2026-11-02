"""Published residential electricity prices, by state and month.

Used only to estimate usage for statements older than the usage export:
usage = bill / price per kWh. The estimate is offered only where the metered
statements show the account's own price really does sit near the published
one (see longview.published_matches), and the page says it is an estimate.

The source is the U.S. Energy Information Administration's monthly retail
sales survey (form EIA-861M): the average price of electricity sold to
residential customers, every state, every month, back to 2001. It is revenue
per kWh, so the customer charge is already inside it and the customer charge
here is zero. It is a state average, not any one utility's tariff; that is
what the check on the metered statements is for.

Reading it needs a free EIA API key, ``ELEC_EIA_KEY``. Without one, nothing
is estimated: a year without usage shows its dollars and nothing more.

What leaves the server: the home's state, two letters. Prices are shared,
not per user, so they are kept in the database per state and asked for
again only when a month has passed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from metermatters import region
from metermatters.store import db

API_URL = "https://api.eia.gov/v2/electricity/retail-sales/data/"
SOURCE_HREF = "https://www.eia.gov/electricity/monthly/"
TIMEOUT = 15
FRESH_FOR = timedelta(days=30)
# Asked for again after a failure no sooner than this, so an outage isn't hammered.
RETRY_AFTER = timedelta(hours=1)
# EIA publishes a month about two months after it ends. A day with no month
# of its own takes the newest month before it, this far back at most.
LAG_MONTHS = 4
FIRST_MONTH = "2001-01"


class Unavailable(Exception):
    """No prices, for a reason worth logging. Never fatal."""


def enabled() -> bool:
    return bool(os.environ.get("ELEC_EIA_KEY", "").strip())


def source_for(state: str) -> dict:
    return {"text": f"EIA's average residential price for {region.name(state) or state}",
            "href": SOURCE_HREF}


def _get(params: dict) -> object:
    url = API_URL + "?" + urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(url, headers={"User-Agent": "meter-matters"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unavailable(f"EIA didn't answer ({type(exc).__name__})") from None


def parse(payload: object) -> dict[str, float]:
    """``{"2024-01": 29.5, ...}`` cents per kWh, out of the API's answer."""
    rows = payload.get("response", {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        error = payload.get("error") if isinstance(payload, dict) else None
        raise Unavailable(f"EIA sent something unreadable ({error or 'no data'})")
    months = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            cents = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        period = str(row.get("period") or "")
        if len(period) == 7 and cents > 0:
            months[period] = cents
    if not months:
        raise Unavailable("EIA listed no prices")
    return months


def _fetch(state: str) -> dict[str, float]:
    return parse(_get({
        "api_key": os.environ.get("ELEC_EIA_KEY", "").strip(),
        "frequency": "monthly", "data[0]": "price",
        "facets[stateid][]": state, "facets[sectorid][]": "RES",
        "start": FIRST_MONTH, "sort[0][column]": "period", "sort[0][direction]": "asc",
        "length": 5000,
    }))


def prices_for(state: str, now: datetime | None = None) -> dict[str, float] | None:
    """Every month's price for a state, or None when there is none to be had.

    A month-old answer is used without asking. If asking fails, whatever is
    held is used, however old; only with nothing held is there nothing.
    """
    state = (state or "").strip().upper()
    if state not in region.STATES or not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM state_prices WHERE state = ?", (state,)).fetchone()
    held = None
    if row:
        try:
            held = json.loads(row["payload"]) or None
            age = now - datetime.fromisoformat(row["fetched_at"])
            backing_off = bool(row["failed_at"]) and \
                now - datetime.fromisoformat(row["failed_at"]) < RETRY_AFTER
            if held and (age < FRESH_FOR or backing_off):
                return held
            if backing_off:
                return held
        except (ValueError, TypeError):
            held = None
    try:
        months = _fetch(state)
    except Unavailable:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO state_prices (state, payload, fetched_at, failed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (state) DO UPDATE SET failed_at = excluded.failed_at",
                (state, "{}", "1970-01-01T00:00:00+00:00", now.isoformat(timespec="seconds")))
        return held
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO state_prices (state, payload, fetched_at, failed_at) VALUES (?, ?, ?, NULL) "
            "ON CONFLICT (state) DO UPDATE SET payload = excluded.payload, "
            "fetched_at = excluded.fetched_at, failed_at = NULL",
            (state, json.dumps(months), now.isoformat(timespec="seconds")))
    return months


def lookup(months: dict[str, float], day: date) -> dict | None:
    """The price in force on a day: its own month, else the newest month
    before it within the publishing lag."""
    wanted = day.strftime("%Y-%m")
    key = wanted if wanted in months else max((m for m in months if m < wanted), default=None)
    if key is None:
        return None
    year, month = int(key[:4]), int(key[5:])
    behind = (day.year - year) * 12 + (day.month - month)
    if behind > LAG_MONTHS:
        return None
    return {"per_kwh": round(months[key] / 100, 5), "customer": 0.0, "month": key}


def for_area(state: str | None, fuel: str):
    """A lookup function day -> price for the area, or None when there is none to offer."""
    if fuel != "Electric" or not state:
        return None, None
    try:
        months = prices_for(state)
    except (sqlite3.Error, OSError):
        return None, None
    if not months:
        return None, None
    return (lambda day: lookup(months, day)), source_for(state)
