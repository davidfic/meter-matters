"""Tests for bills that couldn't be read: kept 30 days to fix the reader, shown
to their owner, deletable by them, and triaged from the admin panel."""

import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from metermatters.intake import failed
from metermatters.intake import inbound
from metermatters.intake import scan
from metermatters.store import accounts
from metermatters.store import db
from tests.test_inbound import InboundCase, bill_from_utility, from_someone_else

UNREADABLE = "<p>Your payment is due. Log in to see your balance.</p>"


def rows_for(user_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM failed_bills WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()]


class FailedCase(InboundCase):
    env = {**InboundCase.env, "ELEC_ADMIN_EMAILS": "boss@example.com"}

    def unreadable(self, subject="Your Example Water District payment is due 12345678"):
        raw = bill_from_utility(self.address, subject=subject, body=UNREADABLE)
        self.assertEqual(self.deliver(raw).get_json()["outcome"], "rejected")
        return raw


class TestEmail(FailedCase):
    def test_an_unreadable_email_is_kept_as_it_came(self):
        raw = self.unreadable()
        row, = rows_for(self.ann.id)
        self.assertEqual(row["kind"], "email")
        self.assertEqual(bytes(row["content"]), raw)
        self.assertEqual(row["sender_domain"], "notices.example-utility.com")
        self.assertNotIn("12345678", row["label"])          # long numbers cut, as in the log
        self.assertEqual(row["status"], "open")
        received = datetime.fromisoformat(row["received_at"])
        self.assertEqual(datetime.fromisoformat(row["expires_at"]) - received, timedelta(days=30))
        self.assertIn("Kept until", self.log()[0]["detail"])

    def test_the_fuel_from_the_subject_is_the_prefill(self):
        self.unreadable()
        self.assertEqual(failed.open_for(self.ann)[0]["guess"].get("fuel"), "Water")

    def test_a_bill_of_no_known_fuel_is_kept_with_its_figures(self):
        self.deliver(bill_from_utility(self.address, subject="Your statement is ready",
                                       body="<p>Total Amount Due: $45.67</p><p>Due date: 10/16/2026</p>"))
        row, = failed.open_for(self.ann)
        self.assertEqual(row["guess"], {"amount": 45.67, "due": "2026-10-16"})

    def test_a_forward_by_someone_else_is_not_kept(self):
        raw = from_someone_else(self.address).replace(
            b"Subject: New Electric Bill", b"Subject: Fwd: New Electric Bill")
        self.deliver(raw.replace(b"Total Amount Due", b"---------- Forwarded message ---------\nFrom: x@y.com\nTotal"))
        self.assertEqual(rows_for(self.ann.id), [])

    def test_a_bill_that_reads_is_not_kept(self):
        self.deliver(bill_from_utility(self.address))
        self.assertEqual(rows_for(self.ann.id), [])

    def test_only_twenty_are_kept(self):
        for i in range(failed.KEEP_MAX + 2):
            self.unreadable(subject=f"Water payment due {i}")
        rows = rows_for(self.ann.id)
        self.assertEqual(len(rows), failed.KEEP_MAX)
        self.assertEqual(rows[-1]["label"], f"Water payment due {failed.KEEP_MAX + 1}")


class TestRefusedSignature(FailedCase):
    dkim = "fail"

    def test_a_refused_signature_is_not_kept(self):
        os.environ["ELEC_INBOUND_DKIM"] = "require"
        self.unreadable()
        self.assertEqual(rows_for(self.ann.id), [])


class TestTheOwnersPage(FailedCase):
    def test_the_page_and_the_banner_say_so(self):
        self.unreadable()
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertIn("1 bill we couldn't read", page)
        self.assertIn("only so we can fix the reader", page)
        self.assertIn("Type it in", page)
        self.assertIn("1 bill\n  couldn't be read", self.client.get("/rates").get_data(as_text=True))

    def test_typing_it_in_files_it_and_keeps_the_answer(self):
        self.unreadable()
        held = failed.open_for(self.ann)[0]
        res = self.client.post("/bills/add", data={"fuel": "Water", "due": "2026-11-20",
                                                   "amount": "61.20", "failed_id": held["id"]})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(failed.open_for(self.ann), [])
        row, = rows_for(self.ann.id)
        self.assertEqual(row["status"], "typed_in")
        self.assertIn("61.20", row["answer"])
        self.assertNotIn("couldn't be read", self.client.get("/rates").get_data(as_text=True))
        self.assertTrue(self.ann.paths.csv.exists())

    def test_delete_removes_it(self):
        self.unreadable()
        held = failed.open_for(self.ann)[0]
        self.client.post(f"/failed/{held['id']}/delete")
        self.assertEqual(rows_for(self.ann.id), [])

    def test_nobody_else_can_delete_or_see_it(self):
        self.unreadable()
        held = failed.open_for(self.ann)[0]
        self.client.post("/logout")
        self.sign_up("bob@example.com")
        self.client.post(f"/failed/{held['id']}/delete")
        self.assertEqual(len(rows_for(self.ann.id)), 1)
        self.assertNotIn("we couldn't read", self.client.get("/connect").get_data(as_text=True))

    def test_deleting_the_account_deletes_it(self):
        self.unreadable()
        self.client.post("/account/delete", data={"confirm": "ann@example.com"})
        self.assertEqual(rows_for(self.ann.id), [])


class TestPurge(FailedCase):
    def test_a_month_on_it_is_gone(self):
        self.unreadable()
        later = datetime.now(timezone.utc) + timedelta(days=30, seconds=1)
        self.assertEqual(failed.purge(later), 1)
        self.assertEqual(rows_for(self.ann.id), [])

    def test_before_then_it_stays(self):
        self.unreadable()
        self.assertEqual(failed.purge(datetime.now(timezone.utc) + timedelta(days=29)), 0)


class TestUploadAndScan(FailedCase):
    def test_an_unreadable_upload_is_kept(self):
        res = self.client.post("/api/upload", data={"file": [(io.BytesIO(b"\x00\x01garbage\xff"), "bill.bin")]},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Kept until", res.get_json()["error"])
        row, = rows_for(self.ann.id)
        self.assertEqual((row["kind"], row["label"]), ("upload", "bill.bin"))

    def test_an_unreadable_scan_is_kept(self):
        with mock.patch.object(scan, "enabled", return_value=True), \
             mock.patch.object(scan, "read_text", side_effect=scan.Unreadable("blank page")):
            res = self.client.post("/api/scan", data={"file": [(io.BytesIO(b"\x89PNG..."), "bill.png")]},
                                   content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        row, = rows_for(self.ann.id)
        self.assertEqual((row["kind"], row["content_type"]), ("scan", "image/png"))

    def test_a_scan_with_nothing_sure_is_kept_and_linked_to_the_form(self):
        with mock.patch.object(scan, "enabled", return_value=True), \
             mock.patch.object(scan, "read_text", return_value="hello there"):
            res = self.client.post("/api/scan", data={"file": [(io.BytesIO(b"\x89PNG..."), "bill.png")]},
                                   content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["failed_id"], rows_for(self.ann.id)[0]["id"])


class TestAdmin(FailedCase):
    def as_boss(self):
        self.client.post("/logout")
        return self.sign_up("boss@example.com")

    def test_only_admins_see_it(self):
        self.unreadable()
        self.assertEqual(self.client.get("/admin/failed").status_code, 404)
        held = rows_for(self.ann.id)[0]
        self.assertEqual(self.client.get(f"/admin/failed/{held['id']}/raw").status_code, 404)

    def test_the_list_and_a_logged_download(self):
        raw = self.unreadable()
        self.as_boss()
        held = rows_for(self.ann.id)[0]
        page = self.client.get("/admin/failed").get_data(as_text=True)
        self.assertIn("notices.example-utility.com", page)
        res = self.client.get(f"/admin/failed/{held['id']}/raw")
        self.assertEqual(res.data, raw)
        self.assertIn(".eml", res.headers["Content-Disposition"])
        from metermatters.store import site
        self.assertEqual(site.recent()[0]["action"], "failed-download")

    def test_once_the_reader_is_fixed_it_is_filed_for_them(self):
        self.unreadable()
        self.as_boss()
        held = rows_for(self.ann.id)[0]
        # Today's reader still can't.
        self.client.post(f"/admin/failed/{held['id']}/file")
        self.assertEqual(len(rows_for(self.ann.id)), 1)
        # "Fix" the reader.
        fixed = {"amount": 61.2, "due": datetime(2026, 11, 20).date(), "bill_date": None}
        with mock.patch("metermatters.intake.billmail._by_looking", return_value=fixed):
            self.client.post(f"/admin/failed/{held['id']}/file")
        self.assertEqual(rows_for(self.ann.id), [])
        self.assertTrue(self.log()[0]["detail"].startswith("Filed after a fix"))
        self.assertTrue(accounts.get(self.ann.id).paths.csv.exists())

    def test_admin_delete(self):
        self.unreadable()
        self.as_boss()
        held = rows_for(self.ann.id)[0]
        self.client.post(f"/admin/failed/{held['id']}/delete")
        self.assertEqual(rows_for(self.ann.id), [])


if __name__ == "__main__":
    unittest.main()
