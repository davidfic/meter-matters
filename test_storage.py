"""Tests for atomic writes and the merge lock."""

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import ingest
import storage


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_writes_and_replaces(self):
        target = self.dir / "a.txt"
        storage.atomic_write_text(target, "one")
        storage.atomic_write_text(target, "two")
        self.assertEqual(target.read_text(), "two")

    def test_a_failed_write_leaves_the_old_file_whole(self):
        target = self.dir / "a.txt"
        storage.atomic_write_text(target, "precious")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                storage.atomic_write_text(target, "half")
        self.assertEqual(target.read_text(), "precious")

    def test_a_failed_write_leaves_no_debris(self):
        target = self.dir / "a.txt"
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                storage.atomic_write_text(target, "half")
        self.assertEqual([p.name for p in self.dir.iterdir()], [])

    def test_the_file_mode_survives_a_rewrite(self):
        target = self.dir / "a.txt"
        storage.atomic_write_text(target, "one")
        target.chmod(0o600)
        storage.atomic_write_text(target, "two")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_creates_missing_directories(self):
        target = self.dir / "deep" / "er" / "a.txt"
        storage.atomic_write_text(target, "x")
        self.assertTrue(target.exists())


class TestConcurrentMerge(unittest.TestCase):
    def test_simultaneous_merges_lose_nothing(self):
        # Without the lock both writers read the same rows and the second
        # write erases the first one's bill.
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        csv = folder / "AccountHistory.csv"
        rows = [ingest.bill_row("acct", f"2026-{m:02d}-16", 100.0 + m) for m in range(1, 13)]
        threads = [threading.Thread(target=ingest.merge, args=(csv, [row], list(row)))
                   for row in rows]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        written, _ = ingest.read_rows(csv)
        self.assertEqual(len(written), 12)


if __name__ == "__main__":
    unittest.main()
