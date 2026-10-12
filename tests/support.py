"""Shared scaffolding for tests that need the hosted app: a throwaway data
directory, accounts switched on, and nothing allowed to leave the machine."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from metermatters.web import app as webapp
from metermatters import config

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith("ELEC_")}


def throwaway_database_url() -> str:
    """The Postgres the database tests may wipe. Never the real one.

    They DELETE every row in every table they touch, in the schema production
    uses. A URL that looks like a hosted database is refused outright rather
    than trusted to be a copy.
    """
    url = os.environ.get("ELEC_TEST_DATABASE_URL", "")
    host = url.split("@")[-1].split("/")[0].split(":")[0].lower()
    if host and host not in ("localhost", "127.0.0.1", "::1") and not host.startswith("elec-"):
        raise SystemExit(f"refusing to run the database tests against {host}: they empty every "
                         "table. Point ELEC_TEST_DATABASE_URL at a throwaway Postgres on localhost.")
    return url
SECRET = "test-secret-key-that-is-long-enough-0123456789"
PASSWORD = "correct horse battery"


class HostedCase(unittest.TestCase):
    """Accounts on. Subclasses add to `env`."""
    env: dict = {}
    multi_user = True

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        env = {**CLEAN, **({"ELEC_SECRET_KEY": SECRET} if self.multi_user else {}), **self.env}
        for patch in (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(config, "DATA_DIR", self.dir),
            mock.patch.object(config, "CSV_PATH", self.dir / "AccountHistory.csv"),
            mock.patch.object(config, "USAGE_DIR", self.dir / "usage"),
            mock.patch.object(config, "STATE_DIR", self.dir / "data"),
            mock.patch.object(webapp, "CSV_PATH", self.dir / "AccountHistory.csv"),
            mock.patch.object(webapp, "USAGE_DIR", self.dir / "usage"),
            mock.patch.object(webapp, "STATE_PATH", self.dir / "data" / "state.json"),
            mock.patch.dict(webapp.app.config,
                            {"SECRET_KEY": SECRET if self.multi_user else None}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        # Anything that would reach the network fails the test instead.
        for target in ("urllib.request.urlopen", "smtplib.SMTP_SSL", "subprocess.run"):
            patch = mock.patch(target, side_effect=AssertionError(f"{target} was called"))
            patch.start()
            self.addCleanup(patch.stop)
        self.client = webapp.app.test_client()

    def sign_up(self, email="ann@example.com", client=None):
        client = client or self.client
        res = client.post("/register", data={"email": email, "password": PASSWORD})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:300])
        from metermatters.store import accounts
        return accounts.by_email(email)
