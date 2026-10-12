"""Tests for what changes when the app is hosted: tasks, the job, start-up."""

import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import accounts
import ingest
import jobs
from testing import CLEAN, SECRET, HostedCase

HERE = Path(__file__).resolve().parent


def give_bills(paths, until: date, count=6):
    rows = [ingest.bill_row("111", (until - timedelta(days=30 * i)).isoformat(), 100.0 + i)
            for i in range(count)]
    ingest.merge(paths.csv, rows, list(rows[0]))


class JobCase(HostedCase):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(jobs, "LAST_RUN_PATH", self.dir / "data" / "jobs.json"),
                      mock.patch("notify.send", return_value={"ntfy": "sent"}),
                      mock.patch("notify.send_user", return_value={"email": "sent"})):
            started = patch.start()
            self.addCleanup(patch.stop)
        self.send_user = started
        self.send = jobs.notify.send


class TestTaskEndpoint(JobCase):
    env = {"ELEC_TASK_TOKEN": "tok-123"}

    def test_the_token_runs_the_check(self):
        give_bills(self.sign_up().paths, date.today())
        res = self.client.post("/api/tasks/check", headers={"Authorization": "Bearer tok-123"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(set(body["steps"]), {"import", "staleness", "watchdog", "benchmark"})
        self.assertEqual(body["people"], 1)

    def test_the_answer_names_nobody(self):
        give_bills(self.sign_up("ann@example.com").paths, date.today())
        res = self.client.post("/api/tasks/check", headers={"Authorization": "Bearer tok-123"})
        text = json.dumps(res.get_json()) + (self.dir / "data" / "jobs.json").read_text()
        self.assertNotIn("ann@", text)
        self.assertNotIn("111", text)

    def test_anything_else_does_not(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "tok-123"}):
            with self.subTest(headers):
                res = self.client.post("/api/tasks/check", headers=headers)
                self.assertEqual(res.status_code, 403)
        self.assertIsNone(jobs.last_run())

    def test_the_token_opens_nothing_else(self):
        res = self.client.get("/api/report", headers={"Authorization": "Bearer tok-123"})
        self.assertEqual(res.status_code, 401)

    def test_absent_without_a_token_configured(self):
        with mock.patch.dict(os.environ, {"ELEC_TASK_TOKEN": ""}):
            res = self.client.post("/api/tasks/check", headers={"Authorization": "Bearer "})
        self.assertEqual(res.status_code, 404)


class TestJobPerUser(JobCase):
    def test_each_user_is_checked_against_their_own_bills(self):
        ann, bob = self.sign_up("ann@example.com"), self.sign_up("bob@example.com",
                                                                 self.client.application.test_client())
        give_bills(ann.paths, date.today())                          # current
        give_bills(bob.paths, date.today() - timedelta(days=60))     # a bill has gone missing
        result = jobs.run_check()
        self.assertEqual(result["steps"]["watchdog"], {"ok": 1, "overdue": 1})
        told = [call[0][0] for call in self.send_user.call_args_list]
        self.assertEqual(told, ["bob@example.com"])

    def test_nothing_about_a_user_reaches_the_operator(self):
        give_bills(self.sign_up().paths, date.today() - timedelta(days=60))
        jobs.run_check()
        self.send.assert_not_called()

    def test_a_user_with_no_bills_is_not_a_failure(self):
        self.sign_up()
        result = jobs.run_check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"]["watchdog"], {"off": 1})

    def test_one_users_broken_file_stops_nobody_else(self):
        ann, bob = self.sign_up("ann@example.com"), self.sign_up("bob@example.com",
                                                                 self.client.application.test_client())
        give_bills(bob.paths, date.today())
        with mock.patch.object(jobs, "step_staleness",
                               side_effect=[RuntimeError("bad zip"), {"status": "ok", "detail": ""}]), \
                mock.patch.dict(jobs.STEPS, {"staleness": lambda t: jobs.step_staleness(t)}):
            result = jobs.run_check()
        self.assertEqual(result["steps"]["staleness"], {"failed": 1, "ok": 1})
        self.assertEqual(result["steps"]["watchdog"], {"off": 1, "ok": 1})

    def test_no_connected_mailbox_means_no_polling(self):
        self.sign_up()
        self.assertEqual(jobs.run_check()["steps"]["import"], {"off": 1})

    def test_runs_once_a_day_however_often_it_is_asked(self):
        self.assertIsNotNone(jobs.run_if_due())
        self.assertIsNone(jobs.run_if_due())
        self.assertTrue(jobs.due(datetime.now(timezone.utc) + timedelta(hours=25)))

    def test_only_one_worker_runs_it(self):
        with jobs.storage.try_locked(jobs.LAST_RUN_PATH) as mine:
            self.assertTrue(mine)
            self.assertIsNone(jobs.run_if_due())
        self.assertIsNone(jobs.last_run())


class TestJobOnOnesOwnMachine(JobCase):
    multi_user = False

    def test_the_one_person_is_told_on_every_channel(self):
        give_bills(accounts.local_paths(), date.today() - timedelta(days=60))
        result = jobs.run_check()
        self.assertEqual(result["steps"]["watchdog"], {"overdue": 1})
        self.send.assert_called_once()
        self.send_user.assert_not_called()

    def test_a_failed_mailbox_import_is_critical_and_the_rest_still_runs(self):
        os.environ["ELEC_IMAP_USER"] = "me@example.com"
        with mock.patch.object(jobs.billmail, "main", side_effect=RuntimeError("boom")):
            result = jobs.run_check()
        self.assertEqual(result["steps"]["import"], {"failed": 1})
        self.assertEqual(self.send.call_args[0][2], "critical")
        self.assertIn("staleness", result["detail"][0]["steps"])
        self.assertEqual(jobs.exit_code(result), jobs.EXIT_ERROR)


class TestLocalRun(HostedCase):
    multi_user = False

    def test_there_is_no_sign_in(self):
        give_bills(accounts.local_paths(), date.today())
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 302)

    def test_another_site_cannot_write(self):
        res = self.client.post("/api/settings", json={"goal": 5},
                               headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)
        self.assertFalse((self.dir / "data" / "state.json").exists())

    def test_our_own_page_can(self):
        res = self.client.post("/api/settings", json={"goal": 5},
                               headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(res.status_code, 200)

    def test_health_check_says_nothing(self):
        self.assertEqual(self.client.get("/healthz").get_json(), {"ok": True})


class TestProductionEntry(unittest.TestCase):
    def start(self, **env):
        return subprocess.run([sys.executable, "-c", "import wsgi"], cwd=HERE,
                              env={**CLEAN, **env}, capture_output=True, text=True)

    def test_refuses_to_start_without_a_session_key(self):
        done = self.start()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ELEC_SECRET_KEY", done.stderr)

    def test_refuses_a_key_too_short_to_be_random(self):
        self.assertNotEqual(self.start(ELEC_SECRET_KEY="hunter2").returncode, 0)

    def test_on_cloud_run_it_will_not_start_without_a_database(self):
        done = self.start(ELEC_SECRET_KEY=SECRET, K_SERVICE="elec-bill")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ELEC_DATABASE_URL", done.stderr)

    def test_starts_with_one(self):
        done = self.start(ELEC_SECRET_KEY=SECRET, ELEC_DATA_DIR="/tmp")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_data_dir_moves_everything(self):
        done = subprocess.run(
            [sys.executable, "-c",
             "import config, accounts, db, jobs; u = accounts.User(7,'a','','t',True,False);"
             "print(config.CSV_PATH, config.USAGE_DIR, db.path(), jobs.LAST_RUN_PATH, "
             "u.paths.csv, u.paths.usage, u.paths.state, u.paths.watchdog)"],
            cwd=HERE, env={**CLEAN, "ELEC_DATA_DIR": "/srv/x"}, capture_output=True, text=True)
        found = done.stdout.split()
        self.assertEqual(len(found), 8, done.stderr)
        self.assertTrue(all(p.startswith("/srv/x/") for p in found), found)
        self.assertTrue(all("/users/7/" in p for p in found[4:]), found)


if __name__ == "__main__":
    unittest.main()
