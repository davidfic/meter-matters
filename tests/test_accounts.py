"""Tests for accounts: signing up and in, and keeping users' bills apart."""

import io
import os
import sqlite3
import unittest
from datetime import date
from unittest import mock

from metermatters.store import accounts
from metermatters.store import db
from metermatters.intake import ingest
from tests.test_hosting import give_bills
from tests.support import PASSWORD, HostedCase


class TestRegister(HostedCase):
    def test_signing_up_signs_you_in_and_offers_the_ways_in(self):
        res = self.client.post("/register", data={"email": "Ann@Example.com", "password": PASSWORD})
        self.assertEqual(res.headers["Location"], "/connect")
        self.assertEqual(self.client.get("/connect").status_code, 200)

    def test_the_password_is_never_stored(self):
        self.sign_up()
        raw = db.path().read_bytes()
        self.assertNotIn(PASSWORD.encode(), raw)
        with db.connect() as conn:
            stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertTrue(stored.startswith("$argon2id$"))

    def test_the_database_is_private_to_the_app(self):
        self.sign_up()
        self.assertEqual(db.path().stat().st_mode & 0o077, 0)

    def test_one_account_per_address_whatever_the_case(self):
        self.sign_up("ann@example.com")
        other = self.client.application.test_client()
        res = other.post("/register", data={"email": "ANN@example.com", "password": PASSWORD})
        self.assertEqual(res.status_code, 400)
        self.assertIn(b"already an account", res.data)

    def test_weak_or_malformed_details_are_refused(self):
        for email, password in (("ann@example.com", "short"), ("not-an-email", PASSWORD),
                                ("", PASSWORD), ("ann@example.com", "x" * 300)):
            with self.subTest(email=email, password=password[:12]):
                res = self.client.post("/register", data={"email": email, "password": password})
                self.assertEqual(res.status_code, 400)
        self.assertEqual(accounts.everyone(), [])

    def test_sign_ups_can_be_closed(self):
        os.environ["ELEC_ALLOW_SIGNUP"] = "0"
        res = self.client.post("/register", data={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(accounts.everyone(), [])

    def test_everyone_gets_their_own_unguessable_address(self):
        ann = self.sign_up("ann@example.com")
        bob = self.sign_up("bob@example.com", self.client.application.test_client())
        self.assertNotEqual(ann.inbound_token, bob.inbound_token)
        self.assertGreaterEqual(len(ann.inbound_token), 24)
        self.assertEqual(ann.inbound_token, ann.inbound_token.lower())


class TestSignIn(HostedCase):
    def setUp(self):
        super().setUp()
        self.sign_up()
        self.client.post("/logout")

    def sign_in(self, email="ann@example.com", password=PASSWORD, **extra):
        return self.client.post("/login", data={"email": email, "password": password, **extra})

    def test_signed_out_pages_send_you_to_sign_in_and_back(self):
        res = self.client.get("/electric")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["Location"], "/login?next=/electric")
        res = self.sign_in(next="/electric")
        self.assertEqual(res.headers["Location"], "/electric")

    def test_signed_out_apis_say_so_plainly(self):
        for method, path in (("get", "/api/report"), ("get", "/api/raw"), ("get", "/api/overview"),
                             ("post", "/api/settings"), ("post", "/api/upload")):
            with self.subTest(path):
                res = getattr(self.client, method)(path)
                self.assertEqual(res.status_code, 401)
                self.assertIn("error", res.get_json())

    def test_the_right_password_gets_in(self):
        self.assertEqual(self.sign_in().status_code, 302)
        self.assertEqual(self.client.get("/connect").status_code, 200)

    def test_a_wrong_password_and_an_unknown_address_look_the_same(self):
        wrong = self.sign_in(password="not the password")
        nobody = self.sign_in(email="nobody@example.com")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.data.replace(b"ann@example.com", b"X"),
                         nobody.data.replace(b"nobody@example.com", b"X"))

    def test_guessing_is_cut_off(self):
        for _ in range(accounts.MAX_FAILURES):
            self.sign_in(password="guess")
        res = self.sign_in()                    # even the right one, now
        self.assertEqual(res.status_code, 401)
        self.assertIn(b"Too many attempts", res.data)

    def test_one_machine_cannot_work_through_many_accounts(self):
        for i in range(accounts.MAX_FAILURES):
            self.sign_in(email=f"user{i}@example.com", password="guess")
        self.assertIn(b"Too many attempts", self.sign_in().data)

    def test_sign_in_never_redirects_off_site(self):
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:x"):
            with self.subTest(target):
                self.client.post("/logout")
                self.assertEqual(self.sign_in(next=target).headers["Location"], "/")

    def test_signing_out_ends_the_session(self):
        self.sign_in()
        self.client.post("/logout")
        self.assertEqual(self.client.get("/api/report").status_code, 401)

    def test_a_deleted_accounts_session_is_worth_nothing(self):
        self.sign_in()
        accounts.delete(accounts.by_email("ann@example.com"))
        self.assertEqual(self.client.get("/api/report").status_code, 401)

    def test_the_session_cookie_is_kept_from_scripts_and_other_sites(self):
        cookie = self.sign_in().headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_another_site_cannot_act_as_you(self):
        self.sign_in()
        res = self.client.post("/api/settings", json={"goal": 1},
                               headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)


class TestSeparation(HostedCase):
    def setUp(self):
        super().setUp()
        self.ann = self.sign_up("ann@example.com")
        self.bob_client = self.client.application.test_client()
        self.bob = self.sign_up("bob@example.com", self.bob_client)
        row = ingest.bill_row("74000011111", "2026-03-20", 311.11)
        ingest.merge(self.ann.paths.csv, [row], list(row))

    def test_one_users_bills_are_invisible_to_another(self):
        for path in ("/api/report", "/api/raw", "/api/overview"):
            with self.subTest(path):
                self.assertIn(b"311.11", self.client.get(path).data)
                theirs = self.bob_client.get(path).data
                self.assertNotIn(b"311.11", theirs)
                self.assertNotIn(b"74000011111", theirs)

    def test_uploads_land_in_the_uploaders_folder_only(self):
        body = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
                "222,Electric,2026/04/17,Current Bill,$250.00,-\n").encode()
        res = self.bob_client.post("/api/upload", data={"file": [(io.BytesIO(body), "h.csv")]},
                                   content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)
        self.assertIn("222", self.bob.paths.csv.read_text())
        self.assertNotIn("222", self.ann.paths.csv.read_text())

    def test_settings_are_per_user(self):
        self.client.post("/api/settings", json={"goal": 123})
        self.assertEqual(self.client.get("/api/report").get_json()["settings"]["goal"], 123)
        self.assertTrue(self.ann.paths.state.exists())
        self.assertFalse(self.bob.paths.state.exists())

    def test_a_new_user_starts_at_the_ways_in_not_an_empty_dashboard(self):
        self.assertEqual(self.bob_client.get("/").headers["Location"], "/connect")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_nothing_shows_where_the_files_are(self):
        for path in ("/api/report", "/api/raw"):
            self.assertNotIn(str(self.dir).encode(), self.bob_client.get(path).data)

    def test_deleting_an_account_takes_everything_and_only_that(self):
        res = self.client.post("/account/delete", data={"confirm": "ann@example.com"})
        self.assertEqual(res.status_code, 302)
        self.assertIsNone(accounts.by_email("ann@example.com"))
        self.assertFalse(self.ann.root.exists())
        self.assertIsNotNone(accounts.by_email("bob@example.com"))

    def test_deleting_needs_the_address_typed(self):
        self.client.post("/account/delete", data={"confirm": "yes"})
        self.assertIsNotNone(accounts.by_email("ann@example.com"))
        self.assertTrue(self.ann.paths.csv.exists())


class TestAdopt(HostedCase):
    def test_a_single_user_installs_data_is_copied_not_moved(self):
        give_bills(accounts.local_paths(), date(2026, 6, 16))
        (self.dir / "usage").mkdir()
        (self.dir / "usage" / "export.zip").write_bytes(b"PK")
        ann = self.sign_up()
        self.assertEqual(accounts.main(["adopt", "ann@example.com"]), 0)
        self.assertEqual(ann.paths.csv.read_text(), (self.dir / "AccountHistory.csv").read_text())
        self.assertTrue((ann.paths.usage / "export.zip").exists())
        self.assertTrue((self.dir / "usage" / "export.zip").exists())

    def test_it_never_writes_over_a_users_own_data(self):
        give_bills(accounts.local_paths(), date(2026, 6, 16))
        ann = self.sign_up()
        give_bills(ann.paths, date(2026, 1, 16), count=1)
        before = ann.paths.csv.read_text()
        with mock.patch("sys.stderr"):
            self.assertEqual(accounts.main(["adopt", "ann@example.com"]), 1)
        self.assertEqual(ann.paths.csv.read_text(), before)


if __name__ == "__main__":
    unittest.main()
