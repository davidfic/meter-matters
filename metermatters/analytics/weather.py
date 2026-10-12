"""Daily temperatures for a place: fetched once, then kept.

A gas bill is mostly a record of how cold it was. Comparing this January with
last January says little until the weather is taken out, and taking it out
needs the temperature of every day in both.

The source is Open-Meteo's historical archive. It needs no key, and what it
gives is licensed CC BY 4.0, so pages that use it must say so
(``ATTRIBUTION``). **Its free service is for non-commercial use**; a paid
product needs their paid plan or another source. Set ``ELEC_WEATHER=off`` to
switch weather off entirely - every comparison still works, it just can't say
how much of a change was the weather.

What leaves the server: a ZIP code, to find the town, and then the town's
position rounded to two decimal places (about a kilometre). Never an address.

Temperatures are shared, not per user - a town's weather is the same for
everyone in it - so they live in the database rather than in anyone's files.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from metermatters.store import db

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ATTRIBUTION = {"text": "Weather data by Open-Meteo.com", "href": "https://open-meteo.com/"}

TIMEOUT = 8
# Days the archive doesn't have yet are asked for again, but not on every page.
RETRY_AFTER = timedelta(hours=6)
ZIP = re.compile(r"^\d{5}$")


class Unavailable(Exception):
    """No weather, for a reason worth telling the user. Never fatal."""


@dataclass(frozen=True)
class Place:
    zip: str
    name: str
    lat: float
    lon: float
    tz: str

    @property
    def key(self) -> str:
        return f"{self.lat:.2f},{self.lon:.2f}"


def enabled() -> bool:
    return os.environ.get("ELEC_WEATHER", "open-meteo").strip().lower() not in (
        "", "0", "off", "no", "none", "false")


def zip_in(text: str | None) -> str | None:
    """The ZIP code at the end of an address line, if there is one."""
    found = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", text or "")
    return found.group(1) if found else None


def _get(url: str, params: dict) -> dict:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": "meter-matters"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unavailable(f"the weather service didn't answer ({type(exc).__name__})") from None


def known_place(zipcode: str | None) -> Place | None:
    """The place for a ZIP code if it was ever looked up. Never goes to the network."""
    zipcode = (zipcode or "").strip()
    if not ZIP.match(zipcode):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM places WHERE zip = ?", (zipcode,)).fetchone()
    return Place(row["zip"], row["name"], row["lat"], row["lon"], row["tz"]) if row else None


def held(place: Place, first: date, last: date) -> dict[date, float]:
    """Only the temperatures already on file. Never goes to the network."""
    return _cached(place, first, last)


def locate(zipcode: str) -> Place:
    """Where a ZIP code is. Looked up once, then remembered."""
    zipcode = (zipcode or "").strip()
    if not ZIP.match(zipcode):
        raise Unavailable("that isn't a five-digit ZIP code")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM places WHERE zip = ?", (zipcode,)).fetchone()
    if row:
        return Place(row["zip"], row["name"], row["lat"], row["lon"], row["tz"])

    found = _get(GEOCODE_URL, {"name": zipcode, "count": 1, "countryCode": "US",
                               "language": "en", "format": "json"})
    results = found.get("results") or []
    if not results:
        raise Unavailable(f"couldn't find ZIP code {zipcode}")
    hit = results[0]
    try:
        place = Place(zipcode,
                      ", ".join(x for x in (hit.get("name"), hit.get("admin1")) if x),
                      round(float(hit["latitude"]), 2), round(float(hit["longitude"]), 2),
                      hit.get("timezone") or "auto")
    except (KeyError, TypeError, ValueError):
        raise Unavailable("the weather service sent something unreadable") from None
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO places (zip, name, lat, lon, tz, found_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (zip) DO NOTHING",
            (place.zip, place.name, place.lat, place.lon, place.tz, db.now()))
    return place


def _days(first: date, last: date):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def _cached(place: Place, first: date, last: date) -> dict[date, float]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, temp FROM weather_days WHERE place = ? AND day >= ? AND day <= ?",
            (place.key, first.isoformat(), last.isoformat())).fetchall()
    return {date.fromisoformat(r["day"]): float(r["temp"]) for r in rows}


def _worth_asking(place: Place, first: date, last: date, now: datetime) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM weather_checks WHERE place = ?",
                           (place.key,)).fetchone()
    if row is None:
        return True
    # Days outside what was asked for last time have never been tried.
    if first.isoformat() < row["first_day"] or last.isoformat() > row["last_day"]:
        return True
    try:
        return now - datetime.fromisoformat(row["checked_at"]) >= RETRY_AFTER
    except ValueError:
        return True


def _fetch(place: Place, first: date, last: date) -> dict[date, float]:
    answer = _get(ARCHIVE_URL, {
        "latitude": place.lat, "longitude": place.lon,
        "start_date": first.isoformat(), "end_date": last.isoformat(),
        "daily": "temperature_2m_mean", "temperature_unit": "fahrenheit",
        "timezone": place.tz})
    try:
        days, temps = answer["daily"]["time"], answer["daily"]["temperature_2m_mean"]
        # The newest days come back empty until the archive catches up.
        return {date.fromisoformat(d): float(t) for d, t in zip(days, temps) if t is not None}
    except (KeyError, TypeError, ValueError):
        raise Unavailable("the weather service sent something unreadable") from None


def temperatures(place: Place, first: date, last: date,
                 now: datetime | None = None) -> dict[date, float]:
    """Mean temperature for each day from `first` to `last` that is known.

    Days already held are never asked for again. A failure to fetch what's
    missing is not an error: what is held is returned, and the caller works
    with the days it has.
    """
    now = now or datetime.now(timezone.utc)
    last = min(last, now.date() - timedelta(days=1))     # today isn't over yet
    if last < first:
        return {}
    held = _cached(place, first, last)
    missing = [d for d in _days(first, last) if d not in held]
    if not missing or not _worth_asking(place, missing[0], missing[-1], now):
        return held

    fetched, failure = {}, None
    try:
        # One request spanning every gap; anything re-sent is simply stored again.
        fetched = _fetch(place, missing[0], missing[-1])
    except Unavailable as exc:
        failure = exc
    with db.connect() as conn:
        if fetched:
            conn.executemany(
                "INSERT INTO weather_days (place, day, temp) VALUES (?, ?, ?) "
                "ON CONFLICT (place, day) DO UPDATE SET temp = excluded.temp",
                [(place.key, d.isoformat(), t) for d, t in fetched.items()])
        conn.execute(
            "INSERT INTO weather_checks (place, checked_at, first_day, last_day) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (place) DO UPDATE SET "
            "checked_at = excluded.checked_at, first_day = excluded.first_day, "
            "last_day = excluded.last_day",
            (place.key, now.isoformat(timespec="seconds"),
             missing[0].isoformat(), missing[-1].isoformat()))
    held.update(fetched)
    if failure is not None and not held:
        raise failure
    return held
