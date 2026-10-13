"""Tests for signing in with Google.

Google itself is replaced by `FakeGoogle`; nothing here touches the network.
"""

import os
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from metermatters.store import accounts
from metermatters.store import db
from metermatters.web import google_oauth
from tests.support import PASSWORD, HostedCase



class FakeGoogle:
    """Stands in for google_oauth._call, answering as Google would."""

    def __init__(self, test):
        self.person = {"sub": "g-123", "email": "ann@gmail.com", "email_verified": True,
                       "name": "Ann"}
        self.fail = None
        patch = mock.patch.object(google_oauth, "_call", side_effect=self)
        patch.start()
        test.addCleanup(patch.stop)

    def __call__(self, url, *, data=None, token=None):
        if self.fail:
            raise self.fail
        if url == google_oauth.TOKEN_URL:
            assert data["code"] == "the-code" and data["client_secret"] == "shh"
            return {"access_token": "access-1", "scope": "openid email profile"}
        if url == google_oauth.USERINFO_URL:
            return self.person
        raise AssertionError(f"unexpected call to {url}")


class GoogleCase(HostedCase):
    env = {"ELEC_GOOGLE_CLIENT_ID": "cid", "ELEC_GOOGLE_CLIENT_SECRET": "shh",
           "ELEC_BASE_URL": "https://bills.example.com"}

    def setUp(self):
        super().setUp()
        self.google = FakeGoogle(self)

    def start(self, client=None, **args):
        client = client or self.client
        res = client.get("/auth/google/start", query_string=args)
        self.assertEqual(res.status_code, 302)
        return parse_qs(urlsplit(res.headers["Location"]).query)

    def finish(self, asked, client=None, **override):
        client = client or self.client
        return client.get("/auth/google/callback",
                          query_string={"state": asked["state"][0], "code": "the-code", **override})

    def round_trip(self, client=None):
        return self.finish(self.start(client), client)

    def flashed(self, client=None):
        return (client or self.client).get("/connect").get_data(as_text=True)


class TestSignInWithGoogle(GoogleCase):
    def test_it_asks_google_for_identity_and_nothing_more(self):
        asked = self.start()
        self.assertEqual(asked["scope"], ["openid email profile"])
        self.assertNotIn("access_type", asked)
        self.assertEqual(asked["redirect_uri"], ["https://bills.example.com/auth/google/callback"])

    def test_a_new_person_gets_an_account_with_no_password(self):
        res = self.round_trip()
        self.assertEqual(res.headers["Location"], "/connect")
        user = accounts.by_email("ann@gmail.com")
        self.assertTrue(user.has_google)
        self.assertFalse(user.has_password)
        self.assertEqual(self.client.get("/connect").status_code, 200)

    def test_nothing_of_googles_is_kept(self):
        self.round_trip()
        raw = db.path().read_bytes()
        self.assertNotIn(b"access-1", raw)
        self.assertNotIn(b"refresh-abc", raw)

    def test_coming_back_finds_the_same_account_even_if_the_address_changed(self):
        self.round_trip()
        self.client.post("/logout")
        self.google.person["email"] = "ann.new@gmail.com"
        self.round_trip()
        self.assertEqual(len(accounts.everyone()), 1)

    def test_an_unverified_address_is_refused(self):
        self.google.person["email_verified"] = False
        self.round_trip()
        self.assertEqual(accounts.everyone(), [])

    def test_a_reply_nobody_asked_for_is_refused(self):
        self.start()
        for state in ("forged", ""):
            res = self.client.get("/auth/google/callback",
                                  query_string={"state": state, "code": "the-code"})
            self.assertEqual(res.headers["Location"], "/login")
        self.assertEqual(accounts.everyone(), [])

    def test_a_reply_cannot_be_used_twice(self):
        asked = self.start()
        self.finish(asked)
        self.client.post("/logout")
        self.finish(asked)
        self.assertEqual(self.client.get("/api/report").status_code, 401)

    def test_declining_at_google_changes_nothing(self):
        res = self.finish(self.start(), error="access_denied", code="")
        self.assertEqual(res.headers["Location"], "/login")
        self.assertEqual(accounts.everyone(), [])

    def test_closed_sign_ups_still_let_existing_people_in(self):
        self.round_trip()
        self.client.post("/logout")
        os.environ["ELEC_ALLOW_SIGNUP"] = "0"
        self.round_trip()
        self.assertEqual(self.client.get("/connect").status_code, 200)
        self.google.person.update(sub="g-999", email="new@gmail.com")
        other = self.client.application.test_client()
        self.round_trip(other)
        self.assertIsNone(accounts.by_email("new@gmail.com"))

    def test_a_password_set_by_whoever_got_there_first_stops_working(self):
        # Someone signs up as ann@gmail.com without owning it, and waits.
        squatter = self.client.application.test_client()
        self.sign_up("ann@gmail.com", squatter)
        squatter.post("/logout")
        # The real Ann arrives, proven by Google.
        self.round_trip()
        res = squatter.post("/login", data={"email": "ann@gmail.com", "password": PASSWORD})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(len(accounts.everyone()), 1)

    def test_google_is_not_offered_until_configured(self):
        with mock.patch.dict(os.environ, {"ELEC_GOOGLE_CLIENT_ID": ""}):
            self.assertNotIn(b"Continue with Google", self.client.get("/login").data)
            self.assertEqual(self.client.get("/auth/google/start").headers["Location"], "/")
        self.assertIn(b"Continue with Google", self.client.get("/login").data)


class TestThePage(GoogleCase):
    def test_every_way_is_offered_when_configured(self):
        os.environ.update(ELEC_INBOUND_DOMAIN="in.example.com", ELEC_INBOUND_SECRET="x")
        user = self.sign_up()
        page = self.client.get("/connect").get_data(as_text=True)
        for text in ("Upload them yourself", "Your private bills address",
                     f"bills-{user.inbound_token}@in.example.com"):
            with self.subTest(text):
                self.assertIn(text, page)

    def test_uploading_is_always_there(self):
        with mock.patch.dict(os.environ, {"ELEC_GOOGLE_CLIENT_ID": ""}):
            self.sign_up()
            page = self.client.get("/connect").get_data(as_text=True)
        self.assertIn('id="dropzone"', page)
        self.assertNotIn("Connect Gmail", page)
        self.assertNotIn("bills-", page)
        self.assertEqual(page.count("Not available on this server"), 1)


if __name__ == "__main__":
    unittest.main()
