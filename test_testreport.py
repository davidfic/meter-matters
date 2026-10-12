"""Tests for writing the test report and showing it. Nothing here runs the real suite."""

import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import testreport
from testing import HostedCase


SAMPLE_TESTS = textwrap.dedent('''
    import unittest

    class Things(unittest.TestCase):
        def test_ok(self):
            """It adds."""
            self.assertEqual(1 + 1, 2)

        def test_wrong(self):
            self.assertEqual(1, 2)

        def test_blows_up(self):
            raise RuntimeError("boom")

        @unittest.skip("not today")
        def test_skipped(self):
            pass

        @unittest.expectedFailure
        def test_known_bad(self):
            self.fail("known")

        def test_parts(self):
            for n in (1, 2, 3):
                with self.subTest(n=n):
                    self.assertNotEqual(n, 2)
''')


def report(**changes):
    base = {"version": 1, "label": "rev-1", "started": "2026-10-10T12:00:00+00:00", "seconds": 1.5,
            "python": "3.13.0", "system": "Linux",
            "tests": [{"id": "test_a.A.test_x", "module": "test_a", "case": "A", "name": "test_x",
                       "doc": "does x", "status": "passed", "seconds": 0.01, "message": ""},
                      {"id": "test_a.A.test_y", "module": "test_a", "case": "A", "name": "test_y",
                       "doc": "", "status": "failed", "seconds": 0.02, "message": "AssertionError: 1 != 2"},
                      {"id": "test_b.B.test_z", "module": "test_b", "case": "B", "name": "test_z",
                       "doc": "", "status": "skipped", "seconds": 0.0, "message": "needs a database"}]}
    base.update(changes)
    return base


class TestRunning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Discovery imports the sample as a module and puts its folder on the path;
        # one run for the whole class, and everything put back afterwards.
        cls.tmp = tempfile.TemporaryDirectory()
        Path(cls.tmp.name, "test_sample.py").write_text(SAMPLE_TESTS)
        before = list(sys.path)
        cls.run_ = testreport.run(cls.tmp.name, label="x")
        sys.path[:] = before
        sys.modules.pop("test_sample", None)
        cls.by = {t["name"]: t for t in cls.run_["tests"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_outcome_is_told_apart(self):
        self.assertEqual({n: t["status"] for n, t in self.by.items()},
                         {"test_ok": "passed", "test_wrong": "failed", "test_blows_up": "error",
                          "test_skipped": "skipped", "test_known_bad": "passed", "test_parts": "failed"})

    def test_a_failure_carries_what_went_wrong(self):
        self.assertIn("AssertionError", self.by["test_wrong"]["message"])
        self.assertIn("boom", self.by["test_blows_up"]["message"])
        self.assertEqual(self.by["test_skipped"]["message"], "not today")

    def test_a_failing_subtest_fails_its_test_and_says_which_part(self):
        self.assertIn("n=2", self.by["test_parts"]["message"])

    def test_the_docstring_and_where_it_lives_are_kept(self):
        t = self.by["test_ok"]
        self.assertEqual((t["doc"], t["module"], t["case"]), ("It adds.", "test_sample", "Things"))

    def test_the_run_describes_itself(self):
        self.assertEqual(self.run_["label"], "x")
        self.assertTrue(self.run_["python"])
        self.assertEqual(testreport.totals(self.run_), {"total": 6, "passed": 2, "failed": 2,
                                                         "error": 1, "skipped": 1})

    def test_what_a_run_produces_is_what_the_server_accepts(self):
        self.assertEqual(testreport.clean(self.run_)["tests"], self.run_["tests"])


class TestAccepting(unittest.TestCase):
    def test_a_good_report_comes_back_unchanged(self):
        self.assertEqual(testreport.clean(report())["tests"], report()["tests"])

    def test_nonsense_is_refused(self):
        for bad in (None, [], {}, {"tests": "x"}, report(tests=[{"status": "great"}]),
                    report(started="yesterday-ish"), report(tests=[{"status": "passed", "seconds": "slow"}])):
            with self.subTest(bad), self.assertRaises(ValueError):
                testreport.clean(bad)

    def test_it_is_trimmed_not_trusted(self):
        long = report()
        long["tests"][1]["message"] = "x" * 100_000
        self.assertEqual(len(testreport.clean(long)["tests"][1]["message"]), testreport.MAX_MESSAGE)

    def test_too_many_tests_is_refused(self):
        with self.assertRaises(ValueError):
            testreport.clean(report(tests=report()["tests"] * 3000))

    def test_the_tests_are_grouped_with_the_failing_files_first(self):
        names = [m["module"] for m in testreport.modules(report())]
        self.assertEqual(names, ["test_a", "test_b"])
        self.assertEqual(testreport.modules(report())[0]["counts"]["failed"], 1)


class TestTheFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "test-report.json"

    def test_what_is_written_is_read_back(self):
        self.path.write_text(json.dumps(report()))
        self.assertEqual(testreport.load(self.path)["tests"], report()["tests"])

    def test_no_file_means_no_report(self):
        self.assertIsNone(testreport.load(self.path))

    def test_a_damaged_file_means_no_report_not_a_crash(self):
        for body in ("", "{", "[]", json.dumps({"tests": "x"})):
            with self.subTest(body):
                self.path.write_text(body)
                self.assertIsNone(testreport.load(self.path))

    def test_the_command_writes_the_report_and_says_how_it_went(self):
        with mock.patch.object(testreport, "run", return_value=report()), redirect_stdout(io.StringIO()):
            code = testreport.main(["--out", str(self.path), "--label", "rev-9"])
        self.assertEqual(code, 1)                      # one test fails in the sample
        self.assertEqual(testreport.load(self.path)["tests"], report()["tests"])

    def test_the_command_succeeds_when_nothing_failed(self):
        ok = report()
        ok["tests"] = [t for t in ok["tests"] if t["status"] != "failed"]
        with mock.patch.object(testreport, "run", return_value=ok), redirect_stdout(io.StringIO()):
            self.assertEqual(testreport.main(["--out", str(self.path)]), 0)


class Case(HostedCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "test-report.json"
        patch = mock.patch.object(testreport, "REPORT_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def push(self, body):
        self.path.write_text(json.dumps(body))


class TestThePage(Case):
    def setUp(self):
        super().setUp()
        self.push(report())

    def signed_out(self):
        import app as webapp
        return webapp.app.test_client()

    def test_anyone_can_see_the_results_without_signing_in(self):
        html = self.signed_out().get("/test-suite").get_data(as_text=True)
        self.assertIn("Failing", html)
        self.assertIn("1 of 3 tests fails", html)
        self.assertIn("AssertionError: 1 != 2", html)
        self.assertIn("needs a database", html)
        self.assertIn("rev-1", html)
        self.assertIn("Not run on this server", html)

    def test_signed_in_it_is_the_same_page(self):
        self.sign_up()
        self.assertIn("Failing", self.client.get("/test-suite").get_data(as_text=True))

    def test_it_asks_not_to_be_indexed(self):
        res = self.signed_out().get("/test-suite")
        self.assertIn("noindex", res.headers["X-Robots-Tag"])
        self.assertIn('name="robots"', res.get_data(as_text=True))

    def test_nothing_on_the_site_links_to_it(self):
        self.sign_up()
        for page in ("/", "/electric", "/gas", "/rates", "/raw", "/connect", "/login"):
            html = self.client.get(page, follow_redirects=True).get_data(as_text=True)
            self.assertNotIn("test-suite", html, page)

    def test_visiting_never_runs_the_tests(self):
        with mock.patch.object(testreport, "run", side_effect=AssertionError("ran the tests")), \
                mock.patch("unittest.TestSuite.run", side_effect=AssertionError("ran the tests")):
            self.assertEqual(self.signed_out().get("/test-suite").status_code, 200)

    def test_it_cannot_be_told_to_do_anything(self):
        client = self.signed_out()
        for method in (client.post, client.put, client.delete):
            self.assertEqual(method("/test-suite").status_code, 405)

    def test_before_anything_is_there_it_says_so(self):
        self.path.unlink()
        html = self.signed_out().get("/test-suite").get_data(as_text=True)
        self.assertIn("No results yet", html)
        self.assertIn("deploy", html)

    def test_an_old_report_says_so(self):
        self.push(report(started="2026-01-01T00:00:00+00:00"))
        self.assertIn("This report is old", self.signed_out().get("/test-suite").get_data(as_text=True))

    def test_what_a_test_prints_cannot_break_the_page(self):
        bad = report()
        bad["tests"][1]["message"] = "</pre><script>alert(1)</script>"
        self.push(bad)
        html = self.signed_out().get("/test-suite").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;alert(1)", html)


if __name__ == "__main__":
    unittest.main()
