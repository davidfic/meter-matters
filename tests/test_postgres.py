"""The same app on Postgres - what Cloud Run and Supabase will actually run.

Skipped unless ELEC_TEST_DATABASE_URL points at a Postgres that may be wiped:

    docker run -d --name elec-pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 postgres:16
    ELEC_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres \\
        .venv/bin/python -m unittest test_postgres -v
"""

import io
import os
import threading
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from metermatters.store import accounts
from metermatters.store import db
from metermatters.intake import gmail_sync
from metermatters.intake import inbound
from metermatters.intake import ingest
from metermatters.tasks import jobs
from metermatters.store import homes
from metermatters.store import workspace
from tests.support import PASSWORD, HostedCase, throwaway_database_url

URL = throwaway_database_url()
# An invented notice, so that these run in a clean checkout too. Nothing here
# depends on what the bill says - see fixtures/synthetic/generate.py.
SAMPLE = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "bill_ready.eml"

HISTORY = ("Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,\n"
           "55500010001,Electric,2026/03/20,Current Bill,$311.11,-\n"
           "55500010001,Electric,2026/02/16,Current Bill,$400.00,-\n")
BILLING = """Name,SOMEONE
Address,"1 EXAMPLE RD, SOMEWHERE MA 01000"
Account Number,55500010001
Service,555000100010000000001

TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES
Electric billing,2026-01-21,2026-02-17,714.00,$311.11
Electric billing,2025-12-20,2026-01-20,800.00,$400.00
"""


def zipped(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BillingData.csv", text)
    return buffer.getvalue()


@unittest.skipUnless(URL, "set ELEC_TEST_DATABASE_URL to run")
class PostgresCase(HostedCase):
    env = {"ELEC_DATABASE_URL": URL, "ELEC_INBOUND_DOMAIN": "in.example.com",
           "ELEC_INBOUND_SECRET": "hook", "ELEC_INBOUND_DKIM": "off",
           "ELEC_TASK_TOKEN": "tok"}

    def setUp(self):
        super().setUp()
        with db.connect() as conn:
            for table in ("files", "meta", "inbound_log", "gmail_links", "login_attempts",
                          "users"):
                conn.execute(f"DELETE FROM {table}")

    def register(self, email):
        from metermatters.web import app as webapp
        client = webapp.app.test_client()
        self.sign_up(email, client=client)
        return client

    def upload(self, client, *files, prefix=""):
        return client.post(f"{prefix}/api/upload", content_type="multipart/form-data",
                           data={"file": [(io.BytesIO(body), name) for name, body in files]})

    def stored(self, user):
        with db.connect() as conn:
            return sorted(r["name"] for r in conn.execute(
                "SELECT name FROM files WHERE user_id = ?", (user.id,)).fetchall())


class TestNothingOnDisk(PostgresCase):
    def test_a_whole_visit_leaves_the_disk_empty(self):
        ann = self.sign_up()
        res = self.upload(self.client, ("h.csv", HISTORY.encode()), ("u.zip", zipped(BILLING)))
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        self.client.post("/api/settings", json={"goal": 200})
        for path in ("/", "/electric", "/raw", "/connect", "/api/report"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.stored(ann), ["AccountHistory.csv", "data/state.json", "usage/u.zip"])
        self.assertEqual([p for p in self.dir.rglob("*") if p.is_file()], [])
        self.assertFalse(db.path().exists())

    def test_what_was_uploaded_is_there_for_the_next_instance(self):
        self.sign_up()
        self.upload(self.client, ("h.csv", HISTORY.encode()), ("u.zip", zipped(BILLING)))
        db._pool.close()                         # as if a new container started
        db._pool = None
        report = self.client.get("/api/report").get_json()
        self.assertEqual(len(report["series"]), 2)
        self.assertTrue(report["has_usage"])
        self.assertEqual(report["settings"]["goal"], None)

    def test_temporary_folders_are_cleaned_up(self):
        import tempfile
        before = set(Path(tempfile.gettempdir()).glob("elec-ws-*"))
        self.sign_up()
        self.upload(self.client, ("h.csv", HISTORY.encode()))
        self.client.get("/api/report")
        self.client.post("/api/upload")          # a request that fails
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("elec-ws-*")), before)

    def test_a_failed_request_saves_nothing(self):
        ann = self.sign_up()
        with mock.patch("metermatters.web.app.build_report", side_effect=RuntimeError("boom")):
            res = self.upload(self.client, ("h.csv", HISTORY.encode()))
        self.assertEqual(res.status_code, 500)
        self.assertEqual(self.stored(ann), [])


class TestSeparation(PostgresCase):
    def test_users_cannot_see_each_others_bills(self):
        self.sign_up("ann@example.com")
        bob_client = self.client.application.test_client()
        bob = self.sign_up("bob@example.com", bob_client)
        self.upload(self.client, ("h.csv", HISTORY.encode()))
        for path in ("/api/report", "/api/raw", "/api/overview"):
            self.assertIn(b"311.11", self.client.get(path).data)
            self.assertNotIn(b"311.11", bob_client.get(path).data)
        self.assertEqual(self.stored(bob), [])

    def test_deleting_an_account_takes_its_files(self):
        ann = self.sign_up()
        self.upload(self.client, ("h.csv", HISTORY.encode()))
        self.client.post("/account/delete", data={"confirm": "ann@example.com"})
        with db.connect() as conn:
            left = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        self.assertEqual(left, 0)
        self.assertIsNone(accounts.get(ann.id))

    def test_accounts_behave_as_they_do_on_sqlite(self):
        self.sign_up("Ann@Example.com")
        other = self.client.application.test_client()
        res = other.post("/register", data={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual(res.status_code, 400)
        for _ in range(accounts.MAX_FAILURES):
            other.post("/login", data={"email": "ann@example.com", "password": "guess"})
        res = other.post("/login", data={"email": "ann@example.com", "password": PASSWORD})
        self.assertIn(b"Too many attempts", res.data)


class TestConcurrency(PostgresCase):
    def test_simultaneous_writes_for_one_user_lose_nothing(self):
        # Twelve instances each file a different bill at the same moment.
        ann = self.sign_up()
        rows = [ingest.bill_row("acct", f"2026-{m:02d}-16", 100.0 + m) for m in range(1, 13)]
        errors = []

        def file_one(row):
            try:
                with workspace.open_(ann, write=True) as paths:
                    ingest.merge(paths.csv, [row], list(row))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=file_one, args=(row,)) for row in rows]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with workspace.open_(ann) as paths:
            written, _ = ingest.read_rows(paths.csv)
        self.assertEqual(len(written), 12)

    def test_reading_never_waits_for_a_writer(self):
        ann = self.sign_up()
        with workspace.open_(ann, write=True) as paths:
            paths.csv.write_text(HISTORY)
            done = []
            reader = threading.Thread(
                target=lambda: done.append(self.client.get("/api/report").status_code))
            reader.start()
            reader.join(timeout=10)
            self.assertEqual(done, [200])


@unittest.skipUnless(SAMPLE.exists(), "no sample email")
class TestWaysIn(PostgresCase):
    def test_a_forwarded_bill_is_filed_and_logged(self):
        ann = self.sign_up()
        hook = self.client.application.test_client()
        for expected in ("imported", "duplicate"):
            res = hook.post("/api/inbound/email", data=SAMPLE.read_bytes(),
                            content_type="message/rfc822",
                            headers={"Authorization": "Bearer hook",
                                     "X-Envelope-To": inbound.address_for(ann)})
            self.assertEqual(res.get_json()["outcome"], expected)
        self.assertEqual(self.stored(ann), ["AccountHistory.csv"])
        self.assertEqual(len(self.client.get("/api/report").get_json()["series"]), 1)
        self.assertIn("Filed", self.client.get("/connect").get_data(as_text=True))

    def test_a_kept_gmail_key_round_trips_through_bytea(self):
        from cryptography.fernet import Fernet
        os.environ["ELEC_TOKEN_KEY"] = Fernet.generate_key().decode()
        ann = self.sign_up()
        gmail_sync.link(ann, "ann@gmail.com", "refresh-abc")
        self.assertEqual(gmail_sync.status(ann)["email"], "ann@gmail.com")
        with db.connect() as conn:
            sealed = conn.execute("SELECT refresh_token FROM gmail_links").fetchone()
        self.assertNotIn(b"refresh-abc", bytes(sealed["refresh_token"]))
        from metermatters.store import secrets_box
        self.assertEqual(secrets_box.open_(sealed["refresh_token"]), "refresh-abc")

    def test_the_scheduled_check_runs_for_everyone(self):
        ann = self.sign_up("ann@example.com")
        self.sign_up("bob@example.com", self.client.application.test_client())
        late = date.today() - timedelta(days=60)
        with workspace.open_(ann, write=True) as paths:
            rows = [ingest.bill_row("111", (late - timedelta(days=30 * i)).isoformat(), 100.0)
                    for i in range(6)]
            ingest.merge(paths.csv, rows, list(rows[0]))
        with mock.patch("metermatters.tasks.notify.send_user", return_value={"email": "sent"}) as told:
            res = self.client.post("/api/tasks/check", headers={"Authorization": "Bearer tok"})
        self.assertEqual(jobs.last_run()["people"], 2)
        self.assertIsNone(jobs.run_if_due())            # it has run today, on any instance
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["steps"]["watchdog"], {"overdue": 1, "off": 1})
        self.assertEqual([c[0][0] for c in told.call_args_list], ["ann@example.com"])
        # The "already told them" note must outlive the container.
        self.assertIn("data/watchdog.json", self.stored(ann))


class TestSupabaseSafety(PostgresCase):
    def test_tables_are_kept_out_of_the_public_schema(self):
        self.sign_up()
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_name IN ('users', 'files', 'gmail_links')").fetchall()
        self.assertEqual({r["table_schema"] for r in rows}, {"elec"})

    def test_row_level_security_is_on_everywhere(self):
        self.sign_up()
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname = 'elec' AND c.relkind = 'r'").fetchall()
        self.assertEqual(len(rows), len(db.TABLES))
        self.assertTrue(all(r["relrowsecurity"] for r in rows))


class TestHomesOnPostgres(PostgresCase):
    """Several homes are a folder each in the user's files - which here are rows."""

    def test_a_second_homes_files_round_trip_through_the_database(self):
        client = self.register("ann@example.com")
        res = client.post("/homes/add", data={"name": "Cabin"})
        self.assertEqual(res.headers["Location"], "/home/cabin/connect")
        self.upload(client, ("AccountHistory.csv", HISTORY.encode()), prefix="/home/cabin")
        self.assertEqual(len(client.get("/home/cabin/api/report").get_json()["series"]), 2)
        self.assertEqual(client.get("/api/report").get_json().get("series"), [])
        with db.connect() as conn:
            names = sorted(r["name"] for r in conn.execute("SELECT name FROM files").fetchall())
        self.assertTrue(any(n.startswith("homes/") and n.endswith("AccountHistory.csv") for n in names))
        self.assertIn("data/homes.json", names)

    def test_a_purged_home_is_gone_from_the_database(self):
        from datetime import datetime, timedelta, timezone
        client = self.register("ann@example.com")
        client.post("/homes/add", data={"name": "Cabin"})
        self.upload(client, ("AccountHistory.csv", HISTORY.encode()), prefix="/home/cabin")
        user = accounts.by_email("ann@example.com")
        with workspace.open_(user, write=True) as paths:
            cabin = homes.find(paths, "cabin")
            homes.delete(paths, cabin.id, "Cabin", datetime.now(timezone.utc) - timedelta(days=31))
        client.post("/api/settings", json={"zip": "01000"})           # any write purges
        with db.connect() as conn:
            names = [r["name"] for r in conn.execute("SELECT name FROM files").fetchall()]
        self.assertFalse(any(n.startswith("homes/") for n in names))

    def test_reads_do_not_hold_a_connection(self):
        client = self.register("ann@example.com")
        self.upload(client, ("AccountHistory.csv", HISTORY.encode()))
        with mock.patch.dict(os.environ, {"ELEC_DB_POOL": "1"}):
            db._pool = None                       # a pool of one: a held connection would wedge the second
            for _ in range(3):
                self.assertEqual(client.get("/api/report").status_code, 200)
            db._pool = None


if __name__ == "__main__":
    unittest.main()
