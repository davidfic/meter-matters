"""Daily weather for a place: fetched once, then kept.

A gas bill is mostly a record of how cold it was. Comparing this January with
last January says little until the weather is taken out, and taking it out
needs the temperature of every day in both. A water bill in summer is partly
a record of how dry it was: each day's rain and reference evapotranspiration
(what a lawn loses to sun and air) are kept too, and their difference is what
the garden needed and didn't get.

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


MM_PER_INCH = 25.4


def _rows(place: Place, first: date, last: date) -> dict[date, dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, temp, rain, et0 FROM weather_days WHERE place = ? AND day >= ? AND day <= ?",
            (place.key, first.isoformat(), last.isoformat())).fetchall()
    return {date.fromisoformat(r["day"]): dict(r) for r in rows}


def _cached(place: Place, first: date, last: date) -> dict[date, float]:
    return {d: float(r["temp"]) for d, r in _rows(place, first, last).items()}


def _complete(r: dict) -> bool:
    return r.get("rain") is not None and r.get("et0") is not None


def held_all(place: Place, first: date, last: date) -> tuple[dict, dict, dict]:
    """(temperatures, rain, watering need) already on file, by day. Rain and
    need are in inches; need is evapotranspiration less rain, never below
    zero. Never goes to the network."""
    rows = _rows(place, first, last)
    temps = {d: float(r["temp"]) for d, r in rows.items()}
    rain = {d: float(r["rain"]) for d, r in rows.items() if _complete(r)}
    need = {d: max(0.0, float(r["et0"]) - float(r["rain"])) for d, r in rows.items() if _complete(r)}
    return temps, rain, need


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


def _fetch(place: Place, first: date, last: date) -> dict[date, tuple]:
    """Each day's (temperature °F, rain in, reference evapotranspiration in)."""
    answer = _get(ARCHIVE_URL, {
        "latitude": place.lat, "longitude": place.lon,
        "start_date": first.isoformat(), "end_date": last.isoformat(),
        "daily": "temperature_2m_mean,precipitation_sum,et0_fao_evapotranspiration",
        "temperature_unit": "fahrenheit", "precipitation_unit": "mm",
        "timezone": place.tz})
    try:
        daily = answer["daily"]
        days, temps = daily["time"], daily["temperature_2m_mean"]
        rains = daily.get("precipitation_sum") or [None] * len(days)
        et0s = daily.get("et0_fao_evapotranspiration") or [None] * len(days)
        # The newest days come back empty until the archive catches up.
        return {date.fromisoformat(d): (float(t),
                                        None if r is None else float(r) / MM_PER_INCH,
                                        None if e is None else float(e) / MM_PER_INCH)
                for d, t, r, e in zip(days, temps, rains, et0s) if t is not None}
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
    rows = _rows(place, first, last)
    held = {d: float(r["temp"]) for d, r in rows.items()}
    # A day held from before rain was kept counts as missing, so it is filled in.
    missing = [d for d in _days(first, last) if d not in rows or not _complete(rows[d])]
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
                "INSERT INTO weather_days (place, day, temp, rain, et0) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (place, day) DO UPDATE SET temp = excluded.temp, "
                "rain = excluded.rain, et0 = excluded.et0",
                [(place.key, d.isoformat(), t, r, e) for d, (t, r, e) in fetched.items()])
        conn.execute(
            "INSERT INTO weather_checks (place, checked_at, first_day, last_day) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (place) DO UPDATE SET "
            "checked_at = excluded.checked_at, first_day = excluded.first_day, "
            "last_day = excluded.last_day",
            (place.key, now.isoformat(timespec="seconds"),
             missing[0].isoformat(), missing[-1].isoformat()))
    held.update({d: t for d, (t, _r, _e) in fetched.items()})
    if failure is not None and not held:
        raise failure
    return held
