"""Tests for the insights endpoints: what the pages ask for, and per user."""

import io
import os
import unittest
import zipfile
from datetime import date, timedelta
from unittest import mock

from metermatters.store import accounts
from metermatters.analytics import weather
from tests.test_insights import climate
from tests.support import HostedCase, throwaway_database_url


def export(account="74000011111", unit="kWh", per_day=25.0, step=None, months=30, address=True):
    """A usage export as the utility writes it, for an invented home."""
    lines = ["Name,SOMEONE INVENTED"]
    if address:
        lines.append('Address,"1 EXAMPLE RD, ANYTOWN MA 01000"')
    lines += [f"Account Number,{account}", f"Service,{account}0071079989", "",
              f"TYPE,START DATE,END DATE,USAGE ({unit}),COST,NOTES"]
    start = date(2024, 1, 1)
    for _ in range(months):
        end = start + timedelta(days=29)
        used = (per_day + (step(start) if step else 0)) * 30
        lines.append(f"Electric billing,{start},{end},{used:.2f},${10 + 0.3 * used:.2f}")
        start = end + timedelta(days=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BillingData.csv", "\n".join(lines) + "\n")
    return buffer.getvalue()


def fake_temperatures(place, first, last, now=None):
    days = [first + timedelta(n) for n in range((last - first).days + 1)]
    return {d: climate(d) for d in days}


class InsightsCase(HostedCase):
    def setUp(self):
        super().setUp()
        self.locate = mock.patch.object(
            weather, "locate",
            side_effect=lambda z: weather.Place(z, "Anytown, Massachusetts", 42.12, -72.57, "auto"))
        self.temps = mock.patch.object(weather, "temperatures", side_effect=fake_temperatures)
        self.located, self.fetched = self.locate.start(), self.temps.start()
        self.addCleanup(self.locate.stop)
        self.addCleanup(self.temps.stop)
        self.user = self.sign_up()

    def upload(self, body, client=None, name="export.zip"):
        res = (client or self.client).post(
            "/api/upload", content_type="multipart/form-data",
            data={"file": [(io.BytesIO(body), name)]})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])

    def insights(self, client=None):
        res = (client or self.client).get("/api/insights")
        self.assertEqual(res.status_code, 200)
        return res.get_json()


def stepped(start):
    return 8.0 if date(2025, 3, 1) <= start < date(2025, 10, 1) else 0.0


class TestInsights(InsightsCase):
    def test_the_location_comes_off_the_export(self):
        self.upload(export())
        out = self.insights()
        self.assertEqual(out["weather_status"], "ok")
        self.assertEqual(out["place"], "Anytown, Massachusetts")
        self.located.assert_called_with("01000")
        self.assertEqual(out["attribution"]["text"], "Weather data by Open-Meteo.com")
        self.assertEqual(out["fuels"]["Electric"]["status"], "ok")

    def test_nothing_uploaded_yet(self):
        out = self.insights()
        self.assertEqual((out["weather_status"], out["fuels"], out["carbon"]),
                         ("no-usage", {}, None))
        self.located.assert_not_called()

    def test_an_export_with_no_address_asks_for_a_zip_code(self):
        self.upload(export(address=False))
        out = self.insights()
        self.assertEqual(out["weather_status"], "no-location")
        self.assertIsNone(out["attribution"])
        # The comparison is still made; only the weather's share is missing.
        self.assertIsNotNone(out["fuels"]["Electric"]["totals"])

        res = self.client.post("/api/settings", json={"zip": "01000"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.insights()["weather_status"], "ok")

    def test_a_zip_code_that_is_not_one_is_not_kept(self):
        self.upload(export(address=False))
        for value in ("0100", "abcde", "01000-1234", "<b>"):
            self.client.post("/api/settings", json={"zip": value})
            self.assertEqual(self.insights()["weather_status"], "no-location")

    def test_weather_switched_off_fetches_nothing(self):
        self.upload(export())
        with mock.patch.dict(os.environ, {"ELEC_WEATHER": "off"}):
            out = self.insights()
        self.assertEqual(out["weather_status"], "off")
        self.located.assert_not_called()
        self.assertIsNotNone(out["fuels"]["Electric"]["totals"])

    def test_a_weather_service_that_is_down_degrades_and_does_not_fail(self):
        self.upload(export())
        self.fetched.side_effect = weather.Unavailable("down")
        out = self.insights()
        self.assertEqual(out["weather_status"], "unavailable")
        self.assertIsNone(out["fuels"]["Electric"]["weather"])

    def test_the_bills_never_wait_for_the_weather(self):
        self.upload(export())
        self.located.reset_mock()
        for path in ("/", "/electric", "/api/report", "/api/overview", "/raw"):
            self.client.get(path)
        self.located.assert_not_called()
        self.fetched.assert_not_called()

    def test_signed_out_it_says_so(self):
        self.client.post("/logout")
        self.assertEqual(self.client.get("/api/insights").status_code, 401)
        self.assertEqual(self.client.post("/api/notes", json={}).status_code, 401)


class TestNotes(InsightsCase):
    def setUp(self):
        super().setUp()
        self.upload(export(step=stepped))
        self.key = self.insights()["fuels"]["Electric"]["shift"]["key"]

    def note(self, text, key=None, client=None):
        return (client or self.client).post("/api/notes", json={"key": key or self.key, "text": text})

    def test_a_step_is_flagged_until_it_is_named(self):
        self.assertEqual(len(self.insights()["alerts"]), 1)
        out = self.note("  space heater   in the office ").get_json()
        self.assertEqual(out["fuels"]["Electric"]["shift"]["note"], "space heater in the office")
        self.assertEqual(out["alerts"], [])
        self.assertEqual(self.insights()["alerts"], [])        # and it is remembered

    def test_taking_the_note_back_flags_it_again(self):
        self.note("space heater")
        out = self.note("").get_json()
        self.assertEqual(out["fuels"]["Electric"]["shift"]["note"], "")
        self.assertEqual(len(out["alerts"]), 1)

    def test_a_note_is_text_and_stays_text(self):
        out = self.note("<img src=x onerror=alert(1)>" + "x" * 500).get_json()
        kept = out["fuels"]["Electric"]["shift"]["note"]
        self.assertLessEqual(len(kept), 200)
        # Kept as typed: it is escaped where it is shown, not mangled where it is stored.
        self.assertTrue(kept.startswith("<img"))

    def test_notes_only_attach_to_what_can_have_one(self):
        for key in ("", "Electric", "Oil:2025-03-01", "Electric:yesterday", "../../etc/passwd",
                    "Electric:2025-03-01; DROP TABLE"):
            with self.subTest(key):
                self.assertEqual(self.client.post("/api/notes", json={"key": key, "text": "x"})
                                 .status_code, 400)

    def test_there_is_a_limit_to_how_many(self):
        for n in range(50):
            self.assertEqual(self.note("x", f"Gas:2020-01-{n % 28 + 1:02d}" if n < 28
                                       else f"Gas:2021-01-{n % 28 + 1:02d}").status_code, 200)
        self.assertEqual(self.note("one too many", "Gas:2022-01-01").status_code, 400)

    def test_one_users_notes_and_usage_are_not_anothers(self):
        self.note("space heater")
        other = self.client.application.test_client()
        bob = self.sign_up("bob@example.com", other)
        self.assertEqual(self.insights(other)["fuels"], {})
        self.upload(export(account="74000022222", step=stepped), other)
        theirs = self.insights(other)
        self.assertEqual(theirs["fuels"]["Electric"]["shift"]["note"], "")
        self.assertEqual(len(theirs["alerts"]), 1)
        self.assertFalse(bob.paths.state.exists())
        self.assertIn("space heater", accounts.get(self.user.id).paths.state.read_text())

    def test_settings_saved_afterwards_do_not_lose_the_note(self):
        self.note("space heater")
        self.client.post("/api/settings", json={"goal": 200})
        self.assertEqual(self.insights()["fuels"]["Electric"]["shift"]["note"], "space heater")

    def test_a_water_change_can_be_named_too(self):
        # Step-change alerts are raised for water as well; a note must be able to answer one.
        self.assertEqual(self.note("sprinkler", "Water:2025-03-01").status_code, 200)

    def test_a_body_that_is_not_an_object_is_refused_not_a_crash(self):
        for body in ([1], "abc", True):
            with self.subTest(body=body):
                res = self.client.post("/api/notes", json=body)
                self.assertEqual(res.status_code, 400)
                self.assertIn("error", res.get_json())




class TestSettingsInput(InsightsCase):
    """What the settings endpoint does with input that is wrong, or hostile."""

    def stored(self):
        import json
        return json.loads(self.user.paths.state.read_text())

    def test_a_body_that_is_not_an_object_is_refused_not_a_crash(self):
        for body in ([1], "abc", True):
            with self.subTest(body=body):
                res = self.client.post("/api/settings", json=body)
                self.assertEqual(res.status_code, 400)
                self.assertIn("error", res.get_json())

    def test_an_infinite_goal_is_no_goal(self):
        # float("inf") > 0, and json.dumps writes Infinity, which no browser can parse.
        for goal in ("inf", "nan", "-inf"):
            with self.subTest(goal=goal):
                res = self.client.post("/api/settings", json={"goal": goal})
                self.assertEqual(res.status_code, 200)
                self.assertNotIn(b"Infinity", res.data)
                self.assertNotIn(b"NaN", res.data)
                self.assertIsNone(self.stored()["goal"])

    def test_a_fuel_that_is_not_one_is_left_as_it_was(self):
        for fuel in ({"a": 1}, 123, "<script>", "Oil", [], ""):
            with self.subTest(fuel=fuel):
                res = self.client.post("/api/settings", json={"fuel": fuel})
                self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])
                self.assertEqual(self.stored()["fuel"], "Electric")
        self.assertEqual(self.client.get("/api/report").status_code, 200)

    def test_a_fuel_is_stored_under_its_own_name_whatever_the_case(self):
        self.assertEqual(self.client.post("/api/settings", json={"fuel": "water"}).status_code, 200)
        self.assertEqual(self.stored()["fuel"], "Water")
        self.assertEqual(self.client.post("/api/settings", json={"fuel": "GAS"}).status_code, 200)
        self.assertEqual(self.stored()["fuel"], "Gas")

    def test_a_hand_edited_state_file_with_the_wrong_types_does_not_take_the_page_down(self):
        import json
        row = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
               "74000011111,Electric,2026/03/20,Current Bill,$300.00,-\n").encode()
        self.upload(row, name="AccountHistory.csv")
        path = self.user.paths.state
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"goal": "200", "baseline_months": "abc", "game_window_months": None,
                                    "target_pct": [], "fuel": 7, "notes": "none", "supply_cents": "x"}))
        for url in ("/api/report", "/api/insights", "/api/rates", "/api/overview"):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get("/api/report").get_json()["settings"]["goal"], 200)

URL = throwaway_database_url()


@unittest.skipUnless(URL, "set ELEC_TEST_DATABASE_URL to run")
class TestOnPostgres(InsightsCase):
    """As it runs on Cloud Run: every file and every note in the database."""
    env = {"ELEC_DATABASE_URL": URL}

    def setUp(self):
        from metermatters.store import db
        HostedCase.setUp(self)
        with db.connect() as conn:
            for table in ("files", "meta", "inbound_log", "login_attempts", "users"):
                conn.execute(f"DELETE FROM {table}")
        super().setUp()

    def test_insights_and_notes_with_no_disk(self):
        self.upload(export(step=stepped))
        out = self.insights()
        self.assertEqual(out["weather_status"], "ok")
        key = out["fuels"]["Electric"]["shift"]["key"]
        named = self.client.post("/api/notes", json={"key": key, "text": "space heater"}).get_json()
        self.assertEqual(named["alerts"], [])
        self.assertEqual(self.insights()["fuels"]["Electric"]["shift"]["note"], "space heater")
        self.assertEqual([p for p in self.dir.rglob("*") if p.is_file()], [])


class TestPages(InsightsCase):
    def test_the_health_check_answers_on_a_path_cloud_run_lets_through(self):
        stranger = self.client.application.test_client()
        for path in ("/health", "/healthz"):
            with self.subTest(path):
                res = stranger.get(path)
                self.assertEqual((res.status_code, res.get_json()), (200, {"ok": True}))
        # Nothing else has become reachable by sharing the prefix.
        self.assertEqual(stranger.get("/healthy-looking-path").status_code, 404)

    def test_a_nickname_cannot_end_the_script_it_is_written_into(self):
        hostile = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                   "74000011111&lt;br&gt;(</script><script>alert(1)</script>),Electric,"
                   "2026/03/20,Current Bill,$300.00,-\n").encode()
        self.upload(hostile, name="AccountHistory.csv")
        for path in ("/", "/electric", "/raw"):
            with self.subTest(path):
                page = self.client.get(path).get_data(as_text=True)
                self.assertNotIn("<script>alert(1)", page)

    def test_the_pages_carry_what_the_scripts_fill_in(self):
        self.upload(export())
        row = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
               "74000011111,Electric,2026/03/20,Current Bill,$300.00,-\n").encode()
        self.upload(row, name="AccountHistory.csv")
        fuel = self.client.get("/electric").get_data(as_text=True)
        for needed in ('id="compare-box"', 'id="insights"', 'id="chart-compare"', 'id="split3"',
                       'id="changed"', 'id="note-form"', 'id="chart-scatter"', 'id="tips"',
                       'id="streak-count"', 'id="zip-form"'):
            self.assertIn(needed, fuel)
        # The bills sit beside the hero; the comparison leads the panes; the game follows.
        self.assertLess(fuel.index('id="tiles"'), fuel.index('id="insights"'))
        self.assertLess(fuel.index('id="insights"'), fuel.index('id="streak-count"'))
        overview = self.client.get("/").get_data(as_text=True)
        for needed in ('id="carbon-section"', 'id="chart-carbon"', 'id="carbon-tiles"'):
            self.assertIn(needed, overview)


def billing_csv(rows, account="74000011111"):
    """A BillingData.csv as the portal writes it, with the rows given as (start, end, kwh)."""
    lines = ["Name,SOMEONE INVENTED", 'Address,"1 EXAMPLE RD, ANYTOWN MA 01000"',
             f"Account Number,{account}", f"Service,{account}0071079989", "",
             "TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES"]
    for start, end, used in rows:
        lines.append(f"Electric billing,{start},{end},{used:.2f},${10 + 0.3 * used:.2f}")
    return "\n".join(lines) + "\n"


class TestRowsOnFile(InsightsCase):
    """Files already on disk, whatever the importer would say about them now."""

    def good_rows(self, n=14):
        rows, start = [], date(2024, 1, 1)
        for _ in range(n):
            end = start + timedelta(days=29)
            rows.append((start, end, 750.0))
            start = end + timedelta(days=1)
        return rows

    def store(self, text):
        folder = self.user.paths.usage
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "BillingData.csv").write_text(text, encoding="utf-8")

    def test_a_reversed_period_on_file_does_not_take_the_pages_down(self):
        rows = self.good_rows()
        self.store(billing_csv(rows + [(date(2026, 3, 2), date(2026, 3, 1), 10.0)]))
        bills = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                 + "".join(f"74000011111,Electric,{end + timedelta(days=20)},Current Bill,"
                           f"${10 + 0.3 * used:.2f},-\n" for _, end, used in rows)).encode()
        self.upload(bills, name="AccountHistory.csv")
        for path in ("/", "/electric", "/api/report", "/api/overview", "/api/insights", "/year"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 200)
        block = self.insights()["fuels"]["Electric"]
        self.assertEqual(block["periods"], len(rows))

    def test_a_long_period_on_file_is_kept(self):
        rows = self.good_rows()
        last = rows[-1][1]
        rows.append((last + timedelta(days=1), last + timedelta(days=400), 9000.0))
        self.store(billing_csv(rows))
        block = self.insights()["fuels"]["Electric"]
        self.assertEqual(block["periods"], len(rows))
        self.assertEqual(block["latest_weather"]["days"], 400)


if __name__ == "__main__":
    unittest.main()
