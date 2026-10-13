"""Tests for fetching and keeping temperatures. Nothing here reaches the network."""

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from metermatters.store import db
from metermatters.analytics import weather
from tests.support import HostedCase

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
PLACE = weather.Place("01000", "Exampleton, Massachusetts", 42.12, -72.57, "America/New_York")


class FakeService:
    """Stands in for weather._get. Knows every day up to `through`."""

    def __init__(self, through=date(2026, 9, 27)):
        self.through, self.calls, self.fail = through, [], None

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if self.fail:
            raise self.fail
        if url == weather.GEOCODE_URL:
            if params["name"] == "00000":
                return {"generationtime_ms": 1}
            return {"results": [{"name": "Exampleton", "admin1": "Massachusetts",
                                 "latitude": 42.1234, "longitude": -72.5678,
                                 "timezone": "America/New_York"}]}
        first = date.fromisoformat(params["start_date"])
        last = date.fromisoformat(params["end_date"])
        days = [first + timedelta(n) for n in range((last - first).days + 1)]
        known = lambda value: [value(d) if d <= self.through else None for d in days]  # noqa: E731
        return {"daily": {"time": [d.isoformat() for d in days],
                          "temperature_2m_mean": known(lambda d: 50.0 + d.day),
                          # Millimetres, as the service sends them: 2.54 mm is a tenth of an inch.
                          "precipitation_sum": known(lambda d: 2.54 * (d.day % 3)),
                          "et0_fao_evapotranspiration": known(lambda d: 2.54 * 1.5)}}

    def asked_for(self):
        return [(c[1]["start_date"], c[1]["end_date"]) for c in self.calls
                if c[0] == weather.ARCHIVE_URL]


class WeatherCase(HostedCase):
    def setUp(self):
        super().setUp()
        self.service = FakeService()
        patch = mock.patch.object(weather, "_get", side_effect=self.service)
        patch.start()
        self.addCleanup(patch.stop)


class TestPlaces(WeatherCase):
    def test_a_zip_code_is_looked_up_once(self):
        first = weather.locate("01000")
        again = weather.locate("01000")
        self.assertEqual(first, again)
        self.assertEqual(first.name, "Exampleton, Massachusetts")
        self.assertEqual(len(self.service.calls), 1)

    def test_only_the_town_is_sent_never_more(self):
        place = weather.locate("01000")
        weather.temperatures(place, date(2026, 1, 1), date(2026, 1, 31), NOW)
        sent = self.service.calls[-1][1]
        self.assertEqual((sent["latitude"], sent["longitude"]), (42.12, -72.57))
        self.assertEqual(set(sent), {"latitude", "longitude", "start_date", "end_date", "daily",
                                     "temperature_unit", "precipitation_unit", "timezone"})

    def test_what_is_not_a_zip_code_is_never_sent(self):
        for value in ("", "0100", "010005", "abcde", "01000; DROP", None):
            with self.subTest(value):
                with self.assertRaises(weather.Unavailable):
                    weather.locate(value)
        self.assertEqual(self.service.calls, [])

    def test_an_unknown_zip_code_is_said_to_be(self):
        with self.assertRaisesRegex(weather.Unavailable, "couldn't find"):
            weather.locate("00000")

    def test_the_zip_is_read_off_the_end_of_an_address(self):
        self.assertEqual(weather.zip_in("1 EXAMPLE RD, SOMEWHERE MA 01000"), "01000")
        self.assertEqual(weather.zip_in("1 EXAMPLE RD, SOMEWHERE MA 01000-1234"), "01000")
        self.assertIsNone(weather.zip_in("12345 EXAMPLE RD, SOMEWHERE MA"))
        self.assertIsNone(weather.zip_in(None))


class TestTemperatures(WeatherCase):
    def get(self, first, last, now=NOW):
        return weather.temperatures(PLACE, first, last, now)

    def test_days_are_fetched_once_and_then_kept(self):
        first = self.get(date(2026, 1, 1), date(2026, 3, 31))
        again = self.get(date(2026, 1, 1), date(2026, 3, 31))
        self.assertEqual(len(first), 90)
        self.assertEqual(first, again)
        self.assertEqual(first[date(2026, 1, 15)], 65.0)
        self.assertEqual(self.service.asked_for(), [("2026-01-01", "2026-03-31")])

    def test_only_what_is_missing_is_asked_for(self):
        self.get(date(2026, 1, 1), date(2026, 3, 31))
        self.get(date(2025, 11, 1), date(2026, 4, 30))
        # One request spanning the gaps either side; never the whole range again
        # without need.
        self.assertEqual(self.service.asked_for()[-1], ("2025-11-01", "2026-04-30"))
        self.get(date(2026, 2, 1), date(2026, 2, 28))
        self.assertEqual(len(self.service.asked_for()), 2)

    def test_days_the_archive_does_not_have_yet_are_left_out(self):
        got = self.get(date(2026, 9, 20), date(2026, 9, 28))
        self.assertEqual(max(got), date(2026, 9, 27))
        self.assertEqual(len(got), 8)

    def test_today_is_never_asked_for(self):
        self.get(date(2026, 9, 20), date(2026, 10, 15))
        self.assertEqual(self.service.asked_for(), [("2026-09-20", "2026-09-28")])

    def test_a_gap_it_cannot_fill_is_not_asked_about_on_every_page(self):
        for _ in range(3):
            self.get(date(2026, 9, 20), date(2026, 9, 28))
        self.assertEqual(len(self.service.asked_for()), 1)

    def test_but_it_is_asked_about_again_later(self):
        self.get(date(2026, 9, 20), date(2026, 9, 28))
        self.service.through = date(2026, 9, 30)
        later = NOW + weather.RETRY_AFTER + timedelta(minutes=1)
        got = self.get(date(2026, 9, 20), date(2026, 9, 28), later)
        self.assertEqual(max(got), date(2026, 9, 28))

    def test_older_history_is_fetched_at_once_even_after_a_recent_check(self):
        self.get(date(2026, 9, 20), date(2026, 9, 28))
        got = self.get(date(2024, 1, 1), date(2024, 1, 31))
        self.assertEqual(len(got), 31)

    def test_a_service_that_is_down_costs_nothing_already_held(self):
        held = self.get(date(2026, 1, 1), date(2026, 1, 31))
        self.service.fail = weather.Unavailable("down")
        got = self.get(date(2026, 1, 1), date(2026, 2, 28))
        self.assertEqual(got, held)

    def test_with_nothing_held_it_says_the_service_is_down(self):
        self.service.fail = weather.Unavailable("down")
        with self.assertRaises(weather.Unavailable):
            self.get(date(2026, 1, 1), date(2026, 1, 31))

    def test_weather_is_shared_not_per_user(self):
        self.get(date(2026, 1, 1), date(2026, 1, 31))
        with db.connect() as conn:
            columns = [r["name"] for r in conn.execute("PRAGMA table_info(weather_days)").fetchall()]
        self.assertEqual(columns, ["place", "day", "temp", "rain", "et0"])

    def test_rain_and_what_the_garden_needed_are_kept_in_inches(self):
        place = weather.locate("01000")
        self.get(date(2026, 1, 1), date(2026, 1, 31))
        temps, rain, need = weather.held_all(place, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(len(temps), 31)
        self.assertAlmostEqual(rain[date(2026, 1, 2)], 0.2)        # day 2: 2 × 2.54 mm
        self.assertAlmostEqual(need[date(2026, 1, 2)], 0.0)        # rain beat evaporation
        self.assertAlmostEqual(need[date(2026, 1, 3)], 0.15)       # day 3: no rain, 0.15 in lost

    def test_days_held_from_before_rain_was_kept_are_filled_in(self):
        place = weather.locate("01000")
        with db.connect() as conn:
            conn.execute("INSERT INTO weather_days (place, day, temp) VALUES (?, ?, ?)",
                         (place.key, "2026-01-10", 40.0))
        self.get(date(2026, 1, 10), date(2026, 1, 10))
        _, rain, _ = weather.held_all(place, date(2026, 1, 10), date(2026, 1, 10))
        self.assertIn(date(2026, 1, 10), rain)


URL = os.environ.get("ELEC_TEST_DATABASE_URL", "")


@unittest.skipUnless(URL, "set ELEC_TEST_DATABASE_URL to run")
class TestOnPostgres(WeatherCase):
    """The same keeping of temperatures, where there is no disk to keep them on."""
    env = {"ELEC_DATABASE_URL": URL}

    def setUp(self):
        super().setUp()
        with db.connect() as conn:
            for table in ("weather_days", "weather_checks", "places"):
                conn.execute(f"DELETE FROM {table}")

    def test_places_and_days_are_kept_in_the_database(self):
        place = weather.locate("01000")
        self.assertEqual(weather.locate("01000"), place)
        first = weather.temperatures(place, date(2026, 1, 1), date(2026, 3, 31), NOW)
        again = weather.temperatures(place, date(2026, 1, 1), date(2026, 3, 31), NOW)
        self.assertEqual(len(first), 90)
        self.assertEqual(first, again)
        self.assertEqual(len(self.service.calls), 2)           # one lookup, one fetch
        self.assertEqual([p for p in self.dir.rglob("*") if p.is_file()], [])

    def test_a_gap_is_not_asked_about_on_every_page(self):
        for _ in range(3):
            weather.temperatures(PLACE, date(2026, 9, 20), date(2026, 9, 28), NOW)
        self.assertEqual(len(self.service.asked_for()), 1)

    def test_the_tables_are_locked_down_like_the_rest(self):
        weather.locate("01000")
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT c.relname, c.relrowsecurity, n.nspname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname IN ('places', 'weather_days', 'weather_checks')").fetchall()
        self.assertEqual({r["nspname"] for r in rows}, {"elec"})
        self.assertTrue(all(r["relrowsecurity"] for r in rows))
        self.assertEqual(len(rows), 3)


class TestSwitch(unittest.TestCase):
    def test_on_unless_switched_off(self):
        for value, expected in ((None, True), ("open-meteo", True), ("off", False),
                                ("0", False), ("OFF", False), ("none", False)):
            env = {} if value is None else {"ELEC_WEATHER": value}
            with self.subTest(value), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(weather.enabled(), expected)


class TestTheWire(HostedCase):
    """The one test of the real request code, with the socket replaced."""

    def test_a_service_that_does_not_answer_is_unavailable_not_a_crash(self):
        for failure in (OSError("no route"), TimeoutError("slow"), ValueError("not json")):
            with self.subTest(failure), \
                    mock.patch("urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(weather.Unavailable):
                    weather._get(weather.ARCHIVE_URL, {"latitude": 1})


if __name__ == "__main__":
    unittest.main()
