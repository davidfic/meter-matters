"""Tests for sessions ending when they must, and for saves that say what happened."""

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
