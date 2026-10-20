"""The admin panel: who can reach it, what it shows and - more to the point -
what it never shows, and each thing it can do, logged."""

import json
import os
import unittest
from unittest import mock

from metermatters.intake import ingest
from metermatters.store import accounts
from metermatters.store import db
from metermatters.store import site
from metermatters.store import workspace
from metermatters.tasks import jobs
from tests.support import HostedCase, PASSWORD

ADMIN = "boss@example.com"
# Things that belong to a user and must never reach an admin page.
ZIP, ACCOUNT, SUBJECT, AMOUNT = "02139", "987654321", "Your Eversource bill is ready", "123.45"


class AdminCase(HostedCase):
    env = {"ELEC_ADMIN_EMAILS": " Boss@Example.com , other@example.com "}

    def setUp(self):
        super().setUp()
        self.ann = self.sign_up("ann@example.com")
        self.give_ann_some_data()
        self.client.post("/logout")
        self.boss = self.sign_up(ADMIN)

    def give_ann_some_data(self):
        """A bill with an account number, a ZIP in the settings, a message in the log."""
        with workspace.open_(self.ann, write=True) as paths:
            rows = [ingest.bill_row(ACCOUNT, "2026-09-01", float(AMOUNT))]
            ingest.merge(paths.csv, rows, list(rows[0]))
            paths.usage.mkdir(parents=True, exist_ok=True)
            (paths.usage / f"Usage_{ACCOUNT}.csv").write_text("Date,kWh\n2026-09-01,10\n")
            paths.state.parent.mkdir(parents=True, exist_ok=True)
            paths.state.write_text(json.dumps({"zip": ZIP}))
        with db.connect() as conn:
            conn.execute("INSERT INTO inbound_log (user_id, received_at, outcome, subject, detail) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (self.ann.id, db.now(), "imported", SUBJECT, f"Electric ${AMOUNT}, due 2026-09-20."))
            conn.execute("INSERT INTO inbound_log (user_id, received_at, outcome, subject, detail) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (self.ann.id, db.now(), "rejected", SUBJECT, "Not from the utility."))

    def page(self, path="/admin"):
        res = self.client.get(path)
        self.assertEqual(res.status_code, 200, path)
        return res.get_data(as_text=True)


class TestWhoGetsIn(HostedCase):
    env = {"ELEC_ADMIN_EMAILS": "boss@example.com"}

    def test_signed_out_it_asks_you_to_sign_in(self):
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["Location"].startswith("/login"))

    def test_a_plain_user_finds_no_such_page(self):
        self.sign_up("ann@example.com")
        for path in ("/admin", "/admin/accounts/1"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/admin/settings", data={"signups": "closed"}).status_code, 404)
        self.assertEqual(self.client.post("/admin/check").status_code, 404)
        self.assertTrue(site.signups_open())

    def test_a_plain_user_sees_no_admin_button(self):
        self.sign_up("ann@example.com")
        self.assertNotIn('href="/admin"', self.client.get("/").get_data(as_text=True))

    def test_an_admin_gets_in_and_has_the_button(self):
        self.sign_up("boss@example.com")
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Robots-Tag"], "noindex, nofollow")
        for path in ("/connect", "/docs/start"):       # each kind of header, with no bills yet
            self.assertIn('href="/admin"', self.client.get(path).get_data(as_text=True), path)

    def test_the_list_is_read_with_care(self):
        with mock.patch.dict(os.environ, {"ELEC_ADMIN_EMAILS": " Boss@Example.COM ,, x@example.com "}):
            self.assertEqual(site.admins(), {"boss@example.com", "x@example.com"})
        with mock.patch.dict(os.environ, {"ELEC_ADMIN_EMAILS": ""}):
            self.assertEqual(site.admins(), set())
            self.assertFalse(site.is_admin(accounts.by_email("boss@example.com")))
        self.assertFalse(site.is_admin(None))
        self.assertFalse(site.is_admin(accounts.LOCAL))


class TestNoPanelWithoutAccounts(HostedCase):
    multi_user = False
    env = {"ELEC_ADMIN_EMAILS": "boss@example.com"}

    def test_the_page_does_not_exist(self):
        self.assertEqual(self.client.get("/admin").status_code, 404)


class TestWhatItShows(AdminCase):
    def test_every_account_with_counts_and_sizes(self):
        html = self.page()
        self.assertIn("ann@example.com", html)
        self.assertIn(ADMIN, html)
        self.assertIn("1 filed, 1 not", html)
        self.assertIn("(you)", html)

    def test_one_account_in_detail(self):
        html = self.page(f"/admin/accounts/{self.ann.id}")
        self.assertIn("<td>password</td>", html)
        self.assertRegex(html, r"Statement files</th><td>1 <span")
        self.assertRegex(html, r"Usage exports</th><td>1</td>")
        self.assertRegex(html, r"Imported</th><td>1</td>")
        self.assertRegex(html, r"Rejected</th><td>1</td>")
        self.assertIn("Delete the account", html)

    def test_nothing_of_the_user_s_own_is_on_either_page(self):
        for path in ("/admin", f"/admin/accounts/{self.ann.id}"):
            html = self.page(path)
            for secret in (ZIP, ACCOUNT, SUBJECT, AMOUNT, self.ann.inbound_token,
                           self.boss.inbound_token, "Usage_"):
                self.assertNotIn(secret, html, f"{secret!r} on {path}")

    def test_the_pages_open_nobody_s_files(self):
        with mock.patch.object(workspace.Workspace, "enter",
                               side_effect=AssertionError("a workspace was opened")):
            self.page()
            self.page(f"/admin/accounts/{self.ann.id}")

    def test_an_account_that_is_not_there(self):
        self.assertEqual(self.client.get("/admin/accounts/999").status_code, 404)
        self.assertEqual(self.client.get("/admin/accounts/x").status_code, 404)

    def test_the_server_card_says_whether_not_what(self):
        with mock.patch.dict(os.environ, {"ELEC_VISION_KEY": "AIza-very-secret",
                                          "ELEC_INBOUND_DOMAIN": "in.example.com",
                                          "ELEC_INBOUND_SECRET": "hook-secret"}):
            html = self.page()
        self.assertNotIn("AIza-very-secret", html)
        self.assertNotIn("hook-secret", html)
        self.assertNotIn("in.example.com", html)
        self.assertRegex(html, r"Scans</th><td><span class=\"tag good\">on")
        self.assertRegex(html, r"Forwarding</th><td><span class=\"tag good\">on")


class TestDeleting(AdminCase):
    def test_typing_the_email_deletes_the_account_and_its_files(self):
        root = self.ann.root
        self.assertTrue(root.exists())
        res = self.client.post(f"/admin/accounts/{self.ann.id}/delete",
                               data={"confirm": " Ann@Example.com "}, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(accounts.by_email("ann@example.com"))
        self.assertFalse(root.exists())
        with db.connect() as conn:
            left = conn.execute("SELECT COUNT(*) AS n FROM inbound_log").fetchone()["n"]
        self.assertEqual(left, 0)
        log = site.recent()
        self.assertEqual((log[0]["admin_email"], log[0]["action"], log[0]["target"]),
                         (ADMIN, "deleted account", "ann@example.com"))

    def test_anything_else_typed_does_not(self):
        for typed in ("", "ann", "boss@example.com"):
            res = self.client.post(f"/admin/accounts/{self.ann.id}/delete", data={"confirm": typed})
            self.assertEqual(res.status_code, 302)
            self.assertIsNotNone(accounts.by_email("ann@example.com"), typed)
        self.assertEqual(site.recent(), [])

    def test_not_yourself(self):
        res = self.client.post(f"/admin/accounts/{self.boss.id}/delete", data={"confirm": ADMIN},
                               follow_redirects=True)
        self.assertIn("your own account", res.get_data(as_text=True))
        self.assertIsNotNone(accounts.by_email(ADMIN))

    def test_only_our_own_pages_may_post(self):
        res = self.client.post(f"/admin/accounts/{self.ann.id}/delete",
                               data={"confirm": "ann@example.com"},
                               headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)
        self.assertIsNotNone(accounts.by_email("ann@example.com"))


class TestSettings(AdminCase):
    def test_closing_sign_ups_from_the_panel_needs_no_redeploy(self):
        self.assertEqual(self.client.get("/register").status_code, 302)   # signed in: sent home
        anyone = self.other()
        self.assertEqual(anyone.get("/register").status_code, 200)
        res = self.client.post("/admin/settings", data={"signups": "closed", "dkim": ""},
                               follow_redirects=True)
        self.assertIn("signups: closed (panel)", res.get_data(as_text=True))
        self.assertEqual(anyone.get("/register").status_code, 403)
        self.assertEqual(anyone.post("/register", data={"email": "new@example.com",
                                                        "password": PASSWORD}).status_code, 403)
        self.assertEqual(site.recent()[0]["action"], "changed settings")

    def test_the_panel_wins_over_the_variable_until_handed_back(self):
        with mock.patch.dict(os.environ, {"ELEC_ALLOW_SIGNUP": "0", "ELEC_INBOUND_DKIM": "require"}):
            self.assertEqual(site.setting("signups"), ("closed", "environment"))
            self.assertEqual(site.setting("dkim"), ("require", "environment"))
            self.client.post("/admin/settings", data={"signups": "open", "dkim": "off"})
            self.assertEqual(site.setting("signups"), ("open", "panel"))
            self.assertEqual(site.dkim_mode(), "off")
            self.client.post("/admin/settings", data={"signups": "", "dkim": ""})
            self.assertEqual(site.setting("signups"), ("closed", "environment"))
            self.assertEqual(site.dkim_mode(), "require")

    def test_a_choice_that_is_not_one(self):
        res = self.client.post("/admin/settings", data={"signups": "maybe", "dkim": "warn"},
                               follow_redirects=True)
        self.assertIn("not a choice", res.get_data(as_text=True))
        self.assertEqual(site.stored(), {})
        with self.assertRaises(ValueError):
            site.set_("dkim", "loudly")

    def test_a_stored_value_that_is_no_longer_a_choice_is_ignored(self):
        site.set_("notice", "x")                       # not a choice setting; harmless
        with db.connect() as conn:
            conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'setting:notice'")
            conn.execute("INSERT INTO meta (key, value) VALUES ('setting:dkim', 'bogus')")
        self.assertEqual(site.setting("dkim"), ("warn", "environment"))

    def test_the_page_says_which_is_in_force(self):
        html = self.page()
        self.assertIn("now open, from the environment", html)
        self.client.post("/admin/settings", data={"signups": "closed", "dkim": "require"})
        html = self.page()
        self.assertIn("now closed, from the panel", html)
        self.assertIn("now require, from the panel", html)

    def other(self):
        from metermatters.web import app as webapp
        return webapp.app.test_client()


class TestTheNotice(AdminCase):
    def test_set_it_and_everyone_signed_in_sees_it(self):
        self.client.post("/admin/notice", data={"text": "  Down   tonight, 9pm  "})
        self.assertEqual(site.notice(), "Down tonight, 9pm")
        ann = self.other_signed_in_as_ann()
        for path in ("/", "/connect", "/docs/start", "/rates", "/raw"):
            self.assertIn("Down tonight, 9pm", ann.get(path).get_data(as_text=True), path)
        self.assertEqual(site.recent()[0]["action"], "set the notice")

    def test_signed_out_pages_do_not_carry_it(self):
        self.client.post("/admin/notice", data={"text": "Down tonight"})
        from metermatters.web import app as webapp
        anyone = webapp.app.test_client()
        for path in ("/login", "/docs/start"):
            self.assertNotIn("Down tonight", anyone.get(path).get_data(as_text=True), path)

    def test_it_is_text_not_html(self):
        self.client.post("/admin/notice", data={"text": "<script>alert(1)</script> & co"})
        html = self.other_signed_in_as_ann().get("/").get_data(as_text=True)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; co", html)

    def test_too_long_is_cut(self):
        self.client.post("/admin/notice", data={"text": "x" * 1000})
        self.assertEqual(len(site.notice()), site.NOTICE_MAX)

    def test_clearing(self):
        self.client.post("/admin/notice", data={"text": "Down tonight"})
        self.client.post("/admin/notice", data={"text": "Down tonight", "clear": "1"})
        self.assertEqual(site.notice(), "")
        self.assertNotIn("Down tonight", self.other_signed_in_as_ann().get("/").get_data(as_text=True))
        self.client.post("/admin/notice", data={"text": "Back"})
        self.client.post("/admin/notice", data={"text": "   "})
        self.assertEqual(site.notice(), "")
        self.assertEqual([r["action"] for r in site.recent()],
                         ["cleared the notice", "set the notice", "cleared the notice", "set the notice"])

    def other_signed_in_as_ann(self):
        from metermatters.web import app as webapp
        ann = webapp.app.test_client()
        ann.post("/login", data={"email": "ann@example.com", "password": PASSWORD})
        return ann


class TestTheDailyCheck(AdminCase):
    def test_run_it_now(self):
        result = {"ok": True, "ran_at": "2026-10-16T10:00:00+00:00", "seconds": 1.5, "people": 2,
                  "steps": {"import": {"nothing": 2}, "watchdog": {"ok": 2}}}
        with mock.patch.object(jobs, "run_check", return_value=result) as ran:
            res = self.client.post("/admin/check", follow_redirects=True)
        ran.assert_called_once_with()
        self.assertIn("The check ran: 2 account(s) in 1.5s.", res.get_data(as_text=True))
        self.assertEqual(site.recent()[0]["action"], "ran the daily check")

    def test_failures_are_said(self):
        result = {"ok": False, "ran_at": "2026-10-16T10:00:00+00:00", "seconds": 1.0, "people": 1,
                  "steps": {"import": {"failed": 1}}}
        with mock.patch.object(jobs, "run_check", return_value=result):
            res = self.client.post("/admin/check", follow_redirects=True)
        self.assertIn("with failures: 1 account(s) in 1.0s, 1 step(s) failed.", res.get_data(as_text=True))

    def test_the_last_run_is_shown_as_counts(self):
        last = {"ok": True, "ran_at": "2026-10-16T10:00:00+00:00", "seconds": 2.0, "people": 3,
                "steps": {"import": {"nothing": 3}, "staleness": {"ok": 2, "stale": 1}}}
        with mock.patch.object(jobs, "last_run", return_value=last):
            html = self.page()
        self.assertIn("Oct 16, 2026", html)
        self.assertIn("3 accounts", html)
        self.assertIn("2 ok, 1 stale", html)


class TestTheLog(AdminCase):
    def test_it_is_kept_short(self):
        for i in range(site.LOG_KEEP + 20):
            site.record(ADMIN, "test", detail=str(i))
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM admin_log").fetchone()["n"]
        self.assertEqual(n, site.LOG_KEEP)
        self.assertEqual(site.recent(1)[0]["detail"], str(site.LOG_KEEP + 19))

    def test_the_page_shows_it_newest_first(self):
        site.record(ADMIN, "first thing")
        site.record(ADMIN, "second thing", "ann@example.com", "why")
        html = self.page()
        self.assertLess(html.index("second thing"), html.index("first thing"))


if __name__ == "__main__":
    unittest.main()
