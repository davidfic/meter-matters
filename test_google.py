"""Tests for signing in with Google and for connecting Gmail.

Google itself is replaced by `FakeGoogle`; nothing here touches the network.
"""

import base64
import os
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet

import accounts
import db
import gmail_sync
import google_oauth
import jobs
from testing import PASSWORD, HostedCase

# An invented notice, so that these run in a clean checkout too. Nothing here
# depends on what the bill says - see fixtures/synthetic/generate.py.
SAMPLE = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "bill_ready.eml"
GMAIL = google_oauth.SCOPE_GMAIL


class FakeGoogle:
    """Stands in for google_oauth._call, answering as Google would."""

    def __init__(self, test):
        self.person = {"sub": "g-123", "email": "ann@gmail.com", "email_verified": True,
                       "name": "Ann"}
        self.scope = "openid email"
        self.refresh_token = "refresh-abc"
        self.messages = {"m1": SAMPLE.read_bytes(), "m2": b"Subject: outage\r\n\r\nnot a bill"}
        self.revoked, self.queries, self.fail = [], [], None
        patch = mock.patch.object(google_oauth, "_call", side_effect=self)
        patch.start()
        test.addCleanup(patch.stop)

    def __call__(self, url, *, data=None, token=None):
        if self.fail:
            raise self.fail
        if url == google_oauth.TOKEN_URL:
            if data["grant_type"] == "refresh_token":
                return {"access_token": "access-from-refresh"}
            assert data["code"] == "the-code" and data["client_secret"] == "shh"
            return {"access_token": "access-1", "refresh_token": self.refresh_token,
                    "scope": self.scope}
        if url == google_oauth.USERINFO_URL:
            return self.person
        if url == google_oauth.REVOKE_URL:
            self.revoked.append(data["token"])
            return {}
        if "/messages?" in url:
            self.queries.append(parse_qs(urlsplit(url).query)["q"][0])
            return {"messages": [{"id": k} for k in self.messages]}
        message_id = url.split("/messages/")[1].split("?")[0]
        return {"raw": base64.urlsafe_b64encode(self.messages[message_id]).decode()}


@unittest.skipIf(not SAMPLE.exists(), "no sample email")
class GoogleCase(HostedCase):
    env = {"ELEC_GOOGLE_CLIENT_ID": "cid", "ELEC_GOOGLE_CLIENT_SECRET": "shh",
           "ELEC_BASE_URL": "https://bills.example.com",
           "ELEC_TOKEN_KEY": Fernet.generate_key().decode()}

    def setUp(self):
        super().setUp()
        self.google = FakeGoogle(self)

    def start(self, purpose, client=None, **args):
        client = client or self.client
        res = client.get("/auth/google/start", query_string={"purpose": purpose, **args})
        self.assertEqual(res.status_code, 302)
        return parse_qs(urlsplit(res.headers["Location"]).query)

    def finish(self, asked, client=None, **override):
        client = client or self.client
        return client.get("/auth/google/callback",
                          query_string={"state": asked["state"][0], "code": "the-code", **override})

    def round_trip(self, purpose, client=None):
        return self.finish(self.start(purpose, client), client)

    def flashed(self, client=None):
        return (client or self.client).get("/connect").get_data(as_text=True)


class TestSignInWithGoogle(GoogleCase):
    def test_it_asks_google_for_identity_and_nothing_more(self):
        asked = self.start("signin")
        self.assertEqual(asked["scope"], ["openid email profile"])
        self.assertNotIn("access_type", asked)
        self.assertEqual(asked["redirect_uri"], ["https://bills.example.com/auth/google/callback"])

    def test_a_new_person_gets_an_account_with_no_password(self):
        res = self.round_trip("signin")
        self.assertEqual(res.headers["Location"], "/connect")
        user = accounts.by_email("ann@gmail.com")
        self.assertTrue(user.has_google)
        self.assertFalse(user.has_password)
        self.assertEqual(self.client.get("/connect").status_code, 200)

    def test_nothing_of_googles_is_kept(self):
        self.round_trip("signin")
        self.assertIsNone(gmail_sync.status(accounts.by_email("ann@gmail.com")))
        raw = db.path().read_bytes()
        self.assertNotIn(b"access-1", raw)
        self.assertNotIn(b"refresh-abc", raw)

    def test_coming_back_finds_the_same_account_even_if_the_address_changed(self):
        self.round_trip("signin")
        self.client.post("/logout")
        self.google.person["email"] = "ann.new@gmail.com"
        self.round_trip("signin")
        self.assertEqual(len(accounts.everyone()), 1)

    def test_an_unverified_address_is_refused(self):
        self.google.person["email_verified"] = False
        self.round_trip("signin")
        self.assertEqual(accounts.everyone(), [])

    def test_a_reply_nobody_asked_for_is_refused(self):
        self.start("signin")
        for state in ("forged", ""):
            res = self.client.get("/auth/google/callback",
                                  query_string={"state": state, "code": "the-code"})
            self.assertEqual(res.headers["Location"], "/login")
        self.assertEqual(accounts.everyone(), [])

    def test_a_reply_cannot_be_used_twice(self):
        asked = self.start("signin")
        self.finish(asked)
        self.client.post("/logout")
        self.finish(asked)
        self.assertEqual(self.client.get("/api/report").status_code, 401)

    def test_declining_at_google_changes_nothing(self):
        res = self.finish(self.start("signin"), error="access_denied", code="")
        self.assertEqual(res.headers["Location"], "/login")
        self.assertEqual(accounts.everyone(), [])

    def test_closed_sign_ups_still_let_existing_people_in(self):
        self.round_trip("signin")
        self.client.post("/logout")
        os.environ["ELEC_ALLOW_SIGNUP"] = "0"
        self.round_trip("signin")
        self.assertEqual(self.client.get("/connect").status_code, 200)
        self.google.person.update(sub="g-999", email="new@gmail.com")
        other = self.client.application.test_client()
        self.round_trip("signin", other)
        self.assertIsNone(accounts.by_email("new@gmail.com"))

    def test_a_password_set_by_whoever_got_there_first_stops_working(self):
        # Someone signs up as ann@gmail.com without owning it, and waits.
        squatter = self.client.application.test_client()
        self.sign_up("ann@gmail.com", squatter)
        squatter.post("/logout")
        # The real Ann arrives, proven by Google.
        self.round_trip("signin")
        res = squatter.post("/login", data={"email": "ann@gmail.com", "password": PASSWORD})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(len(accounts.everyone()), 1)

    def test_google_is_not_offered_until_configured(self):
        with mock.patch.dict(os.environ, {"ELEC_GOOGLE_CLIENT_ID": ""}):
            self.assertNotIn(b"Continue with Google", self.client.get("/login").data)
            self.assertEqual(self.client.get("/auth/google/start").headers["Location"], "/")
        self.assertIn(b"Continue with Google", self.client.get("/login").data)


class GmailCase(GoogleCase):
    def setUp(self):
        super().setUp()
        self.ann = self.sign_up("ann@example.com")
        self.google.scope = f"openid email {GMAIL}"

    def bills(self):
        return self.client.get("/api/report").get_json().get("series", [])


class TestImportOnce(GmailCase):
    def test_it_says_plainly_what_it_is_asking_for(self):
        asked = self.start("gmail-once")
        self.assertIn(GMAIL, asked["scope"][0].split())
        self.assertEqual(asked["login_hint"], ["ann@example.com"])

    def test_bills_come_in_and_the_key_goes_straight_back(self):
        self.round_trip("gmail-once")
        self.assertEqual(len(self.bills()), 1)
        self.assertEqual(self.google.revoked, ["refresh-abc"])
        self.assertIsNone(gmail_sync.status(self.ann))
        self.assertNotIn(b"refresh-abc", db.path().read_bytes())
        self.assertIn("Nothing was kept", self.flashed())

    def test_only_the_utilitys_mail_is_ever_asked_for(self):
        self.round_trip("gmail-once")
        self.assertEqual(self.google.queries, ["from:noreply@notifications.eversource.com"])

    def test_importing_again_adds_nothing(self):
        self.round_trip("gmail-once")
        self.round_trip("gmail-once")
        self.assertEqual(len(self.bills()), 1)

    def test_leaving_the_mail_box_unticked_imports_nothing(self):
        self.google.scope = "openid email"
        self.round_trip("gmail-once")
        self.assertEqual(self.bills(), [])
        self.assertEqual(self.google.revoked, ["refresh-abc"])

    def test_it_needs_someone_signed_in(self):
        stranger = self.client.application.test_client()
        res = stranger.get("/auth/google/start", query_string={"purpose": "gmail-once"})
        self.assertTrue(res.headers["Location"].startswith("/login"))

    def test_a_mailbox_with_no_bills_is_said_so(self):
        self.google.messages = {}
        self.round_trip("gmail-once")
        self.assertIn("No bill emails", self.flashed())


class TestStayConnected(GmailCase):
    def test_the_key_is_kept_but_never_in_the_clear(self):
        self.round_trip("gmail-keep")
        self.assertEqual(gmail_sync.status(self.ann)["email"], "ann@gmail.com")
        self.assertEqual(self.google.revoked, [])
        for name in ("app.db", "app.db-wal"):
            path = self.dir / name
            if path.exists():
                self.assertNotIn(b"refresh-abc", path.read_bytes())

    def test_the_database_alone_opens_nothing(self):
        self.round_trip("gmail-keep")
        os.environ["ELEC_TOKEN_KEY"] = Fernet.generate_key().decode()
        with self.assertRaises(Exception):
            gmail_sync.sync(self.ann, self.ann.paths)
        self.assertIn("decrypt", gmail_sync.status(self.ann)["last_error"])

    def test_a_rotated_key_still_opens_what_the_old_one_sealed(self):
        self.round_trip("gmail-keep")
        os.environ["ELEC_TOKEN_KEY"] = (Fernet.generate_key().decode() + ","
                                        + self.env["ELEC_TOKEN_KEY"])
        self.assertIsNotNone(gmail_sync.sync(self.ann, self.ann.paths))

    def test_without_a_key_it_is_not_offered_and_falls_back_to_once(self):
        with mock.patch.dict(os.environ, {"ELEC_TOKEN_KEY": ""}):
            self.assertNotIn("Stay connected", self.flashed())
            res = self.client.get("/auth/google/start", query_string={"purpose": "gmail-keep"})
            self.assertEqual(res.headers["Location"], "/connect")
        self.assertIsNone(gmail_sync.status(self.ann))

    def test_the_daily_check_picks_up_new_bills_and_tells_their_owner(self):
        self.google.messages = {}
        self.round_trip("gmail-keep")
        self.google.messages = {"m1": SAMPLE.read_bytes()}
        with mock.patch.object(jobs, "LAST_RUN_PATH", self.dir / "data" / "jobs.json"), \
                mock.patch("notify.send_user", return_value={"email": "sent"}) as told, \
                mock.patch("notify.send") as operator:
            result = jobs.run_check()
        self.assertEqual(result["steps"]["import"], {"imported": 1})
        self.assertEqual(len(self.bills()), 1)
        self.assertEqual(told.call_args[0][0], "ann@example.com")
        operator.assert_not_called()
        self.assertEqual(self.google.queries[-1],
                         "from:noreply@notifications.eversource.com newer_than:45d")

    def test_withdrawing_access_at_google_disconnects_and_says_so(self):
        self.round_trip("gmail-keep")
        self.google.fail = google_oauth.Revoked("gone")
        with mock.patch.object(jobs, "LAST_RUN_PATH", self.dir / "data" / "jobs.json"), \
                mock.patch("notify.send_user", return_value={"email": "sent"}) as told:
            result = jobs.run_check()
        self.assertEqual(result["steps"]["import"], {"revoked": 1})
        self.assertTrue(result["ok"])
        self.assertIsNone(gmail_sync.status(self.ann))
        self.assertIn("disconnected", told.call_args[0][1])

    def test_google_being_down_is_not_a_disconnection(self):
        self.round_trip("gmail-keep")
        self.google.fail = google_oauth.GoogleError("503")
        with self.assertRaises(google_oauth.GoogleError):
            gmail_sync.sync(self.ann, self.ann.paths)
        self.assertIsNotNone(gmail_sync.status(self.ann))

    def test_disconnecting_forgets_the_key_and_hands_it_back(self):
        self.round_trip("gmail-keep")
        self.client.post("/connect/gmail/disconnect")
        self.assertIsNone(gmail_sync.status(self.ann))
        self.assertEqual(self.google.revoked, ["refresh-abc"])
        self.assertEqual(len(self.bills()), 1)       # the bills stay

    def test_deleting_the_account_hands_it_back_too(self):
        self.round_trip("gmail-keep")
        self.client.post("/account/delete", data={"confirm": "ann@example.com"})
        self.assertEqual(self.google.revoked, ["refresh-abc"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM gmail_links").fetchone()[0], 0)

    def test_one_users_mailbox_feeds_only_their_bills(self):
        bob_client = self.client.application.test_client()
        bob = self.sign_up("bob@example.com", bob_client)
        self.round_trip("gmail-keep")
        self.assertFalse(bob.paths.csv.exists())
        self.assertIsNone(gmail_sync.status(bob))


class TestThePage(GoogleCase):
    def test_every_way_is_offered_when_configured(self):
        os.environ.update(ELEC_INBOUND_DOMAIN="in.example.com", ELEC_INBOUND_SECRET="x")
        user = self.sign_up()
        page = self.client.get("/connect").get_data(as_text=True)
        for text in ("Upload them yourself", "Forward them from your email", "Connect Gmail",
                     "Import once, keep nothing", "Stay connected",
                     f"bills-{user.inbound_token}@in.example.com"):
            with self.subTest(text):
                self.assertIn(text, page)

    def test_uploading_is_always_there(self):
        with mock.patch.dict(os.environ, {"ELEC_GOOGLE_CLIENT_ID": ""}):
            self.sign_up()
            page = self.client.get("/connect").get_data(as_text=True)
        self.assertIn('id="dropzone"', page)
        self.assertNotIn("Import once", page)
        self.assertNotIn("bills-", page)
        self.assertEqual(page.count("Not available on this server"), 2)


if __name__ == "__main__":
    unittest.main()
