"""Tests for the year page, share links, and the similar-homes comparison."""

import unittest
from unittest import mock

import accounts
import benchmarks
import yearly
from test_importers import WebCase, daily


class TestYearPage(WebCase):
    def setUp(self):
        super().setUp()
        self.upload("export.csv", daily())                 # Jan 2024 - Mar 2025, electric, California

    def test_the_latest_year_is_summed_up(self):
        html = self.client.get("/year").get_data(as_text=True)
        self.assertIn("2025", html)
        self.assertIn("Share this year", html)
        self.assertIn("Watt&#39;s the Damage", html)

    def test_an_earlier_year_can_be_picked_and_compares_with_nothing_before_it(self):
        html = self.client.get("/year/2024").get_data(as_text=True)
        self.assertIn("No full year before it", html)
        self.assertEqual(self.client.get("/year/1999").status_code, 200)   # falls back to the latest

    def test_the_summary_adds_up(self):
        reports = {"Electric": self.client.get("/api/report?fuel=Electric").get_json()}
        s = yearly.build(reports, 2024, "CA")
        rows = [r for r in reports["Electric"]["series"] if r["year"] == 2024]
        self.assertAlmostEqual(s["total"], sum(r["amount"] for r in rows), places=2)
        self.assertEqual(s["fuels"][0]["statements"], len(rows))
        self.assertGreaterEqual(len(s["months"]), 11)      # due dates spill into the next year
        self.assertGreater(s["kg"], 0)
        self.assertIn("California", s["grid"])

    def test_without_bills_it_says_so(self):
        import app as webapp
        other = webapp.app.test_client()
        self.sign_up("bea@example.com", client=other)
        self.assertIn("Nothing to sum up yet", other.get("/year").get_data(as_text=True))


class TestSharing(WebCase):
    def setUp(self):
        super().setUp()
        self.upload("export.csv", daily())

    def token(self):
        res = self.client.post("/api/share/year/2024", json={})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()["token"]

    def test_a_link_opens_the_year_without_signing_in_and_names_nobody(self):
        import app as webapp
        token = self.token()
        stranger = webapp.app.test_client()
        res = stranger.get(f"/s/{token}")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("2024", html)
        for private in ("ann@example.com", "9876543210", "Example St", "Sign out", "Add bills", "home-switch"):
            self.assertNotIn(private, html, private)
        self.assertIn("noindex", res.headers.get("X-Robots-Tag", ""))

    def test_the_picture_is_an_svg_of_the_same_year(self):
        import app as webapp
        token = self.token()
        res = webapp.app.test_client().get(f"/s/{token}.svg")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Content-Type"], "image/svg+xml")
        self.assertIn(b"2024", res.data)
        self.assertNotIn(b"9876543210", res.data)

    def test_asking_twice_gives_the_same_link_and_stopping_closes_it(self):
        import app as webapp
        first = self.token()
        self.assertEqual(self.token(), first)
        self.client.post("/api/share/year/2024?stop=1", json={})
        self.assertEqual(webapp.app.test_client().get(f"/s/{first}").status_code, 404)

    def test_a_made_up_token_is_nothing(self):
        import app as webapp
        self.assertEqual(webapp.app.test_client().get("/s/nope").status_code, 404)
        self.assertEqual(webapp.app.test_client().get("/s/" + "x" * 80).status_code, 404)

    def test_a_year_with_no_bills_cannot_be_shared(self):
        self.assertEqual(self.client.post("/api/share/year/1999", json={}).status_code, 404)

    def test_deleting_the_account_takes_the_link_with_it(self):
        import app as webapp
        token = self.token()
        accounts.delete(self.user)
        self.assertEqual(webapp.app.test_client().get(f"/s/{token}").status_code, 404)


class TestBenchmarks(WebCase):
    def homes_in(self, state, n, per_day=20.0):
        for i in range(n):
            benchmarks.publish(1000 + i, "main", state, [], allowed=True)
        with benchmarks.db.connect() as conn:
            for i in range(n):
                conn.execute("INSERT INTO benchmarks (home_key, fuel, state, per_day, unit, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (f"other-{state}-{i}", "Electric", state, per_day + i, "kWh", "2026-01-01"))

    def test_too_few_homes_says_how_many_are_needed(self):
        self.upload("export.csv", daily())
        self.homes_in("CA", 3)
        sim = self.client.get("/api/insights").get_json()["fuels"]["Electric"]["similar"]
        self.assertEqual((sim["status"], sim["count"], sim["needed"]), ("too-few", 3, 5))

    def test_enough_homes_gives_a_comparison_that_leaves_this_home_out(self):
        self.upload("export.csv", daily())                    # ~14 kWh/day
        self.homes_in("CA", 6, per_day=20.0)                  # 20..25
        sim = self.client.get("/api/insights").get_json()["fuels"]["Electric"]["similar"]
        self.assertEqual(sim["status"], "ok")
        self.assertEqual(sim["count"], 6)
        self.assertLess(sim["vs_median_pct"], 0)
        self.assertEqual(sim["percentile"], 0)

    def test_another_states_homes_do_not_count(self):
        self.upload("export.csv", daily())
        self.homes_in("TX", 8)
        sim = self.client.get("/api/insights").get_json()["fuels"]["Electric"]["similar"]
        self.assertEqual(sim["status"], "too-few")

    def test_an_upload_publishes_one_row_with_nothing_personal_in_it(self):
        self.upload("export.csv", daily())
        with benchmarks.db.connect() as conn:
            rows = conn.execute("SELECT * FROM benchmarks").fetchall()
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertEqual((row["state"], row["fuel"], row["unit"]), ("CA", "Electric", "kWh"))
        self.assertEqual(len(row["home_key"]), 32)
        self.assertNotEqual(row["home_key"], benchmarks._key(self.user.id + 1, "main"))

    def test_opting_out_takes_the_row_away(self):
        self.upload("export.csv", daily())
        self.client.post("/place", data={"state": "CA", "zip": "93701"})   # checkbox absent = off
        with benchmarks.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM benchmarks").fetchone()["n"], 0)
        self.assertFalse(self.client.get("/api/insights").get_json()["benchmark_shared"])

    def test_no_state_means_no_row_and_no_comparison(self):
        self.upload("export.csv", daily(preamble=False))      # no address, no state
        with benchmarks.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM benchmarks").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
