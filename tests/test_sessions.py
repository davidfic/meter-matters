"""Tests for sessions ending when they must, and for saves that say what happened."""

import os
import unittest
from unittest import mock

from metermatters.store import accounts
from metermatters.store import workspace
from tests.support import HostedCase, PASSWORD


class TestSessionsEnd(HostedCase):
    def other(self):
        from metermatters.web import app as webapp
        return webapp.app.test_client()

    def test_a_google_sign_in_ends_the_sessions_a_password_opened(self):
        squatter = self.client
        self.sign_up("victim@example.com", client=squatter)          # registered first, by someone else
        self.assertEqual(squatter.get("/connect").status_code, 200)
        accounts.from_google("sub-1", "victim@example.com", "The Owner")
        res = squatter.get("/connect")
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["Location"].startswith("/login"))

    def test_sign_out_everywhere(self):
        user = self.sign_up("ann@example.com")
        here, there = self.client, self.other()
        there.post("/login", data={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual(there.get("/connect").status_code, 200)
        accounts.sign_out_everywhere(accounts.by_email("ann@example.com"))
        self.assertEqual(here.get("/connect").status_code, 302)
        self.assertEqual(there.get("/connect").status_code, 302)

    def test_signing_in_again_works_after(self):
        self.sign_up("ann@example.com")
        accounts.sign_out_everywhere(accounts.by_email("ann@example.com"))
        res = self.client.post("/login", data={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.client.get("/connect").status_code, 200)


class TestTheCookieAndWhoMaySend(HostedCase):
    def setUp(self):
        super().setUp()
        self.sign_up()
        self.client.post("/logout")

    def sign_in(self):
        return self.client.post("/login", data={"email": "ann@example.com", "password": PASSWORD})

    def test_the_cookie_is_secure_when_the_site_is(self):
        # The flag is computed from ELEC_BASE_URL when the app is imported, so
        # the env is too late to set here: patch the config the flag ended up in.
        from metermatters.web import app as webapp
        with mock.patch.dict(webapp.app.config, {"SESSION_COOKIE_SECURE": True}):
            cookie = self.sign_in().headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertNotIn("Secure", self.sign_in().headers["Set-Cookie"])

    def test_only_our_own_pages_may_post(self):
        self.sign_in()
        for site, status in (("same-site", 403), ("cross-site", 403), ("none", 200), (None, 200)):
            with self.subTest(site=site):
                headers = {"Sec-Fetch-Site": site} if site else {}
                res = self.client.post("/api/settings", json={"goal": 1}, headers=headers)
                self.assertEqual(res.status_code, status)

    def test_a_browser_session_is_not_a_task_token(self):
        self.sign_in()
        with mock.patch.dict(os.environ, {"ELEC_TASK_TOKEN": "tok-123"}):
            self.assertEqual(self.client.post("/api/tasks/check").status_code, 403)
            self.assertEqual(self.client.post("/api/tasks/check",
                                              headers={"Authorization": "Bearer wrong"}).status_code, 403)

    def test_signed_in_the_login_page_never_sends_you_off_site(self):
        self.sign_in()
        res = self.client.get("/login?next=https://evil.example")
        self.assertEqual((res.status_code, res.headers["Location"]), (302, "/"))


class TestSavesAreHonest(HostedCase):
    def setUp(self):
        super().setUp()
        self.sign_up()

    def test_a_save_that_fails_is_reported_as_a_failure(self):
        with mock.patch.object(workspace.Workspace, "commit", side_effect=RuntimeError("disk gone")):
            res = self.client.post("/api/settings", json={"zip": "01000"})
        self.assertEqual(res.status_code, 500)
        self.assertIn("couldn't be saved", res.get_json()["error"])

    def test_a_save_that_works_is_a_200(self):
        self.assertEqual(self.client.post("/api/settings", json={"zip": "01000"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
