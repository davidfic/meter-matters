"""Tests for reading the database connection string."""

import os
import unittest
from unittest import mock

from psycopg.conninfo import conninfo_to_dict

import db

POOLER = "aws-0-us-east-2.pooler.supabase.com"


def read(url):
    return conninfo_to_dict(db.conninfo(url))


class TestConnectionString(unittest.TestCase):
    def test_the_string_supabase_hands_out(self):
        got = read(f"postgresql://postgres.abcdefgh:s3cretPASS@{POOLER}:6543/postgres")
        self.assertEqual((got["user"], got["password"], got["host"], got["port"], got["dbname"]),
                         ("postgres.abcdefgh", "s3cretPASS", POOLER, "6543", "postgres"))
        self.assertEqual(got["sslmode"], "require")

    def test_a_password_full_of_url_characters_is_still_a_password(self):
        for password in ("a@b", "a@b@c", "p:w", "p/w", "p#w", "p?w=1", "@lead", "trail@", "sp ace"):
            with self.subTest(password):
                got = read(f"postgresql://postgres.abcdefgh:{password}@{POOLER}:6543/postgres")
                self.assertEqual(got["password"], password)
                self.assertEqual(got["host"], POOLER)

    def test_a_properly_escaped_password_is_unescaped(self):
        got = read(f"postgresql://u:a%40b%2Fc@{POOLER}:6543/postgres")
        self.assertEqual(got["password"], "a@b/c")

    def test_options_after_the_database_are_kept(self):
        got = read("postgresql://u:p@localhost:5432/mydb?sslmode=disable")
        self.assertEqual((got["dbname"], got["sslmode"]), ("mydb", "disable"))

    def test_mistakes_are_named_without_repeating_the_string(self):
        secret = "hunter2hunter2"
        for url, expect in (
                (f"postgresql://postgres.abc:[YOUR-PASSWORD]@{POOLER}:6543/postgres", "placeholder"),
                (f"https://u:{secret}@{POOLER}/postgres", "postgresql://"),
                (f"postgresql://{POOLER}:6543/postgres", "no user"),
                (f"postgresql://postgres@{POOLER}:6543/postgres", "no password"),
                (f"postgresql://u:{secret}@{POOLER}:port/postgres", "host:port")):
            with self.subTest(expect):
                with self.assertRaises(db.BadUrl) as caught:
                    db.conninfo(url)
                self.assertIn(expect, str(caught.exception))
                self.assertNotIn(secret, str(caught.exception))

    def test_a_failed_connection_is_explained_without_the_password(self):
        import psycopg
        secret = "hunter2@hunter2"
        url = f"postgresql://postgres.abc:{secret}@{POOLER}:6543/postgres"
        for error, expect in (
                (f'password authentication failed for user "x" {secret}', "password was refused"),
                ("Tenant or user not found", "doesn't know that user"),
                (f"failed to resolve host '{secret}': Name or service not known", "find the server"),
                (f"something else entirely {secret}", "Details withheld")):
            with self.subTest(expect), mock.patch.dict(os.environ, {"ELEC_DATABASE_URL": url}), \
                    mock.patch("psycopg.connect", side_effect=psycopg.OperationalError(error)):
                ok, message = db.diagnose()
                self.assertFalse(ok)
                self.assertIn(expect, message)
                self.assertNotIn("hunter2", message)


if __name__ == "__main__":
    unittest.main()
