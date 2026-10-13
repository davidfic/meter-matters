"""Tests for bills arriving by forwarded email."""

import base64
import hashlib
import hmac
import os
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from metermatters.store import accounts
from metermatters.intake import inbound
from tests.support import HostedCase

# An invented notice, so that these run in a clean checkout too. Nothing here
# depends on what the bill says - see fixtures/synthetic/generate.py.
SAMPLE = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "bill_ready.eml"
DOMAIN = "in.example.com"
REAL_DKIM_RESULT = inbound.dkim_result


def confirmation(to: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Gmail Team <forwarding-noreply@google.com>"
    msg["To"] = to
    msg["Subject"] = "(#123456789) Gmail Forwarding Confirmation"
    msg.set_content(
        "ann@gmail.com has requested to automatically forward mail to your address.\n"
        "Confirmation code: 123456789\n\n"
        "To allow this, click the link below:\n"
        "https://mail-settings.google.com/mail/vf-%5BANGjdJ8abc%5D-XyZ123\n\n"
        "If you click https://evil.example/mail/confirm instead, bad things happen.\n")
    return msg.as_bytes()


def from_someone_else(to: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Eversource <billing@eversource-payments.example>"
    msg["To"] = to
    msg["Subject"] = "New Electric Bill from Eversource"
    msg.set_content("Total Amount Due: $9,999.00  Due date: 10/16/2026  Account Number: ******0001")
    return msg.as_bytes()


def bill_from_utility(to: str, subject="New Electric Bill from Eversource",
                      body="<p>Total Amount Due: $45.67</p><p>Due date: 10/16/2026</p>"
                           "<p>Account Number: ******0001</p>",
                      sender=None, charset="utf-8") -> bytes:
    """A bill-ready notice built to order: the subject, body and charset vary."""
    msg = EmailMessage()
    msg["From"] = f"Eversource Do Not Reply <{sender or inbound.billmail.DEFAULT_SENDER}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Mon, 12 Oct 2026 09:00:00 -0400"
    msg.set_content(body, subtype="html")
    # Declared after the fact: Python won't encode to a charset it hasn't heard of.
    return msg.as_bytes().replace(b'charset="utf-8"', f'charset="{charset}"'.encode())


def forwarded_by_hand(to: str, sender="ann@example.com") -> bytes:
    """What Gmail sends when a person opens the bill and presses Forward -
    from their own address, the one the account is under."""
    msg = EmailMessage()
    msg["From"] = f"Ann Example <{sender}>"
    msg["To"] = to
    msg["Subject"] = "Fwd: New Electric Bill from Eversource"
    msg["Date"] = "Mon, 12 Oct 2026 22:43:10 -0400"
    msg.set_content("---------- Forwarded message ---------\n"
                    "From: Eversource Do Not Reply <noreply@notifications.eversource.com>\n"
                    "Date: Mon, 12 Oct 2026 09:00:00 -0400\n"
                    "Subject: New Electric Bill from Eversource\n"
                    "To: <ann@gmail.com>\n\n"
                    "Total Amount Due: $123.45  Due date: 10/16/2026  Account Number: ******0001")
    msg.add_alternative(
        '<div dir="ltr"><br><div class="gmail_quote"><div dir="ltr" class="gmail_attr">'
        '---------- Forwarded message ---------<br>From: <strong>Eversource Do Not Reply</strong> '
        '<span dir="auto">&lt;noreply@notifications.eversource.com&gt;</span><br>'
        'Date: Mon, 12 Oct 2026 at 09:00<br>Subject: New Electric Bill from Eversource<br>'
        'To: &lt;ann@gmail.com&gt;<br></div><br><br>'
        '<p>Total Amount Due: $123.45</p><p>Due date: 10/16/2026</p>'
        '<p>Account Number: ******0001</p></div></div>', subtype="html")
    return msg.as_bytes()


@unittest.skipIf(not SAMPLE.exists(), "no sample email")
class InboundCase(HostedCase):
    env = {"ELEC_INBOUND_DOMAIN": DOMAIN, "ELEC_INBOUND_SECRET": "s3cret-hook"}
    dkim = "pass"

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(inbound, "dkim_result", return_value=self.dkim)
        patch.start()
        self.addCleanup(patch.stop)
        self.ann = self.sign_up("ann@example.com")
        self.address = inbound.address_for(self.ann)
        self.bill = SAMPLE.read_bytes()
        self.hook = self.client.application.test_client()      # the service has no session

    def deliver(self, raw=None, to=None, secret="s3cret-hook"):
        return self.hook.post("/api/inbound/email", data=raw or self.bill,
                              content_type="message/rfc822",
                              headers={"Authorization": f"Bearer {secret}",
                                       "X-Envelope-To": to or self.address})

    def log(self):
        return inbound.log_for(accounts.get(self.ann.id))


class TestDelivery(InboundCase):
    def test_a_subject_naming_both_fuels_files_as_electric(self):
        # The tie-break, pinned: "electric" is looked for first.
        self.deliver(bill_from_utility(self.address, subject="Your Gas and Electric bills"))
        self.assertTrue(self.log()[0]["detail"].startswith("Electric $45.67"))

    def test_a_water_bill_is_not_filed_and_the_log_says_why(self):
        # Only electric and gas are read from email today. A water notice must
        # not be guessed into either fuel, and the person must be told.
        res = self.deliver(bill_from_utility(self.address, subject="New Water Bill from Eversource"))
        self.assertEqual(res.get_json()["outcome"], "rejected")
        row = self.log()[0]
        self.assertEqual(row["outcome"], "rejected")
        self.assertIn("neither electric nor gas", row["detail"])
        self.assertFalse(self.ann.paths.csv.exists())

    def test_a_zero_bill_is_a_bill(self):
        self.deliver(bill_from_utility(
            self.address, body="<p>Total Amount Due: $0.00</p><p>Due date: 10/16/2026</p>"))
        self.assertEqual(self.log()[0]["outcome"], "imported")
        self.assertTrue(self.log()[0]["detail"].startswith("Electric $0.00"))

    def test_the_log_keeps_fifty_rows_and_the_page_asks_for_eight(self):
        for _ in range(55):
            self.deliver(from_someone_else(self.address))
        self.assertEqual(len(inbound.log_for(self.ann, limit=1000)), inbound.KEEP_LOG)
        self.assertEqual(len(self.client.get("/connect/arrivals").get_json()["rows"]), 8)

    def test_an_oversize_message_is_refused_before_it_is_read(self):
        res = self.deliver(self.bill + b"x" * (5 * 1024 * 1024 + 1 - len(self.bill)))
        self.assertEqual(res.status_code, 413)
        self.assertEqual(self.log(), [])
        self.assertFalse(self.ann.paths.csv.exists())


class TestHandForwards(InboundCase):
    def test_a_bill_forwarded_by_hand_is_filed_from_the_original_inside_it(self):
        res = self.deliver(forwarded_by_hand(self.address))
        self.assertEqual(res.get_json(), {"ok": True, "outcome": "imported"})
        row = self.log()[0]
        self.assertEqual(row["outcome"], "imported")
        self.assertIn("Electric $123.45, due 2026-10-16.", row["detail"])
        self.assertIn("Forwarded by ann@example.com.", row["detail"])

    def test_a_hand_forward_of_something_else_is_still_refused(self):
        raw = forwarded_by_hand(self.address).replace(b"noreply@notifications.eversource.com",
                                                      b"billing@eversource-payments.example")
        res = self.deliver(raw)
        self.assertEqual(res.get_json()["outcome"], "rejected")
        self.assertIn("Not from the utility's address", self.log()[0]["detail"])

    def test_a_hand_forward_by_anyone_else_is_refused(self):
        # A forward carries no signature of the utility's, so the only thing
        # vouching for it is who sent it. Anyone who knows the address could
        # otherwise plant a bill, with signature checking required or not.
        for mode in ("warn", "require"):
            with self.subTest(mode):
                os.environ["ELEC_INBOUND_DKIM"] = mode
                res = self.deliver(forwarded_by_hand(self.address, sender="mallory@evil.example"))
                self.assertEqual(res.get_json()["outcome"], "rejected")
                self.assertIn("isn't you", self.log()[0]["detail"])
                self.assertIn("mallory@evil.example", self.log()[0]["detail"])
        self.assertFalse(self.ann.paths.csv.exists())

    def test_the_forwarder_is_matched_whatever_the_case(self):
        res = self.deliver(forwarded_by_hand(self.address, sender="Ann@Example.COM"))
        self.assertEqual(res.get_json()["outcome"], "imported")

    def test_the_forwarded_subject_stops_at_the_next_header_line(self):
        # Apple Mail and Outlook put Subject before Date or To, and HTML comes
        # to us on one line - so the subject must not run on into the body,
        # where "electric" would turn a gas bill into an electric one.
        import email as email_mod
        from email import policy as email_policy
        msg = EmailMessage()
        msg["From"] = "Ann Example <ann@example.com>"
        msg["Subject"] = "Fwd: New Gas Bill from Eversource"
        msg.set_content(
            "<div>Begin forwarded message:<br>---------- Forwarded message ---------<br>"
            "From: Eversource Do Not Reply &lt;noreply@notifications.eversource.com&gt;<br>"
            "Subject: New Gas Bill from Eversource<br>"
            "Date: Mon, 12 Oct 2026 at 09:00<br>"
            "To: ann@example.com<br></div>"
            "<p>Your electric account is separate. Total Amount Due: $45.67</p>", subtype="html")
        msg = email_mod.message_from_bytes(msg.as_bytes(), policy=email_policy.default)
        sender, subject = inbound.forwarded_original(msg)
        self.assertEqual(sender, inbound.billmail.DEFAULT_SENDER)
        self.assertEqual(subject, "New Gas Bill from Eversource")
        self.assertEqual(inbound.billmail.fuel_of(subject), "gas")

    def test_the_log_says_how_long_the_message_took_to_arrive(self):
        import email as email_mod
        from email import policy as email_policy
        msg = email_mod.message_from_bytes(forwarded_by_hand(self.address), policy=email_policy.default)
        sent = email_mod.utils.parsedate_to_datetime(msg["Date"]).timestamp()
        self.assertEqual(inbound.transit_note(msg, now=sent + 42), " Took 42 s to arrive.")
        self.assertEqual(inbound.transit_note(msg, now=sent + 250), " Took 4 min to arrive.")
        self.assertEqual(inbound.transit_note(msg, now=sent - 5), "")      # a clock ahead of ours
        del msg["Date"]
        self.assertEqual(inbound.transit_note(msg), "")

    def test_the_page_can_ask_what_has_arrived(self):
        self.assertEqual(self.client.get("/connect/arrivals").get_json(), {"rows": []})
        self.deliver()
        rows = self.client.get("/connect/arrivals").get_json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "imported")
        self.assertEqual(set(rows[0]), {"id", "received_at", "outcome", "detail"})
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertIn(f'data-newest="{rows[0]["id"]}"', page)

    def test_a_forwarded_bill_is_filed_for_its_owner(self):
        res = self.deliver()
        self.assertEqual(res.get_json(), {"ok": True, "outcome": "imported"})
        report = self.client.get("/api/report").get_json()
        self.assertEqual(len(report["series"]), 1)
        self.assertEqual(self.log()[0]["outcome"], "imported")

    def test_the_same_bill_twice_counts_once(self):
        self.deliver()
        self.assertEqual(self.deliver().get_json()["outcome"], "duplicate")
        self.assertEqual(len(self.client.get("/api/report").get_json()["series"]), 1)

    def test_it_lands_with_nobody_else(self):
        bob = self.sign_up("bob@example.com", self.client.application.test_client())
        self.deliver()
        self.assertFalse(bob.paths.csv.exists())

    def test_the_address_is_found_in_the_headers_when_the_envelope_is_lost(self):
        raw = f"X-Forwarded-To: {self.address}\r\n".encode() + self.bill
        res = self.deliver(raw, to="someone@elsewhere.example")
        self.assertEqual(res.get_json()["outcome"], "imported")

    def test_the_address_is_found_wherever_it_appears(self):
        # Deliberately: a header naming us wins over an envelope naming a stranger.
        for header in ("To", "Cc", "Delivered-To"):
            with self.subTest(header):
                raw = f"{header}: {self.address}\r\n".encode() + self.bill
                res = self.deliver(raw, to="someone@elsewhere.example")
                self.assertEqual(res.get_json()["outcome"], "imported" if header == "To" else "duplicate")
        local, _, host = self.address.partition("@")
        for to in (self.address.upper(), f"{local}+tag@{host}"):
            with self.subTest(to):
                self.assertEqual(self.deliver(to=to).get_json()["outcome"], "duplicate")

    def test_an_unknown_charset_is_unreadable_not_a_crash(self):
        # The mail service treats a 500 as a bounce and nothing would be logged.
        from_stranger = bill_from_utility(
            self.address, sender="ann@example.com", charset="x-bogus",
            body="---------- Forwarded message --------- From: noreply@notifications.eversource.com")
        for name, raw in (("utility", bill_from_utility(self.address, charset="x-bogus")),
                          ("forwarded", from_stranger)):
            with self.subTest(name):
                res = self.deliver(raw)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.get_json()["outcome"], "rejected")
                self.assertTrue(self.log()[0]["detail"].startswith("Couldn't read a bill out of it"),
                                self.log()[0]["detail"])
        google = confirmation(self.address).replace(b'charset="utf-8"', b'charset="x-bogus"')
        self.assertIn(b"x-bogus", google)
        res = self.deliver(google)
        self.assertEqual((res.status_code, res.get_json()["outcome"]), (200, "confirm"))
        self.assertIsNone(self.log()[0]["confirm_url"])

    def test_an_unknown_address_is_indistinguishable_from_a_known_one(self):
        res = self.deliver(to=f"bills-{'z' * 24}@{DOMAIN}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"ok": True, "outcome": "ok"})
        self.assertEqual(self.log(), [])

    def test_the_wrong_domain_is_not_our_address(self):
        self.deliver(to=f"bills-{self.ann.inbound_token}@evil.example")
        self.assertFalse(self.ann.paths.csv.exists())

    def test_a_new_address_retires_the_old_one(self):
        self.client.post("/connect/forwarding/rotate")
        self.deliver()
        self.assertFalse(self.ann.paths.csv.exists())
        self.deliver(to=inbound.address_for(accounts.get(self.ann.id)))
        self.assertTrue(self.ann.paths.csv.exists())


class TestWhoMaySend(InboundCase):
    def test_without_the_secret_nothing_is_read(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic !!!"}):
            with self.subTest(headers):
                res = self.hook.post("/api/inbound/email", data=self.bill,
                                     content_type="message/rfc822",
                                     headers={**headers, "X-Envelope-To": self.address})
                self.assertEqual(res.status_code, 401)
        self.assertFalse(self.ann.paths.csv.exists())

    def test_a_signed_in_user_is_not_the_email_service(self):
        res = self.client.post("/api/inbound/email", data=self.bill,
                               content_type="message/rfc822",
                               headers={"X-Envelope-To": self.address})
        self.assertEqual(res.status_code, 401)

    def test_mail_from_anyone_but_the_utility_is_not_filed(self):
        res = self.deliver(from_someone_else(self.address))
        self.assertEqual(res.get_json()["outcome"], "rejected")
        self.assertFalse(self.ann.paths.csv.exists())
        self.assertIn("eversource-payments.example", self.log()[0]["detail"])

    def test_a_hostile_sender_address_is_escaped_on_the_page(self):
        raw = from_someone_else(self.address).replace(
            b"Eversource <billing@eversource-payments.example>",
            b'"<img src=x onerror=alert(1)>"@evil.example')
        self.assertEqual(self.deliver(raw).get_json()["outcome"], "rejected")
        self.assertIn("<img src=x", self.log()[0]["detail"])
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x", page)

    def test_the_endpoint_is_absent_until_configured(self):
        with mock.patch.dict(os.environ, {"ELEC_INBOUND_DOMAIN": ""}):
            self.assertEqual(self.deliver().status_code, 404)


class TestSignature(InboundCase):
    dkim = "fail"

    def test_by_default_an_unverified_bill_is_filed_and_flagged(self):
        self.assertEqual(self.deliver().get_json()["outcome"], "imported")
        self.assertIn("not verified", self.log()[0]["detail"])

    def test_switched_off_it_is_filed_without_comment(self):
        os.environ["ELEC_INBOUND_DKIM"] = "off"
        self.assertEqual(self.deliver().get_json()["outcome"], "imported")
        self.assertNotIn("not verified", self.log()[0]["detail"])

    def test_when_required_it_is_refused(self):
        os.environ["ELEC_INBOUND_DKIM"] = "require"
        self.assertEqual(self.deliver().get_json()["outcome"], "rejected")
        self.assertFalse(self.ann.paths.csv.exists())

    def test_only_the_utilitys_own_signature_counts(self):
        import dkim
        real = REAL_DKIM_RESULT
        vendor_only = b"DKIM-Signature: v=1; d=sendgrid.info; s=x; b=abc\r\nFrom: a@b.c\r\n\r\nhi"
        ours = vendor_only.replace(b"sendgrid.info", b"eversource.com")
        with mock.patch.object(dkim, "DKIM") as checker:
            checker.return_value.verify.return_value = True
            self.assertEqual(real(vendor_only), "unknown")
            self.assertEqual(real(ours), "pass")
            checker.return_value.verify.return_value = False
            self.assertEqual(real(ours), "fail")
            checker.return_value.verify.side_effect = OSError("dns down")
            self.assertEqual(real(ours), "unknown")


class TestGmailConfirmation(InboundCase):
    def test_the_confirmation_is_shown_to_its_owner(self):
        res = self.deliver(confirmation(self.address))
        self.assertEqual(res.get_json()["outcome"], "confirm")
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertIn("https://mail-settings.google.com/mail/vf-%5BANGjdJ8abc%5D-XyZ123", page)
        self.assertIn("123456789", page)

    def test_only_googles_own_link_is_ever_offered(self):
        self.deliver(confirmation(self.address))
        self.assertNotIn("evil.example", self.client.get("/connect").get_data(as_text=True))

    def test_lookalike_hosts_are_never_offered(self):
        raw = confirmation(self.address).replace(
            b"https://mail-settings.google.com/mail/vf-%5BANGjdJ8abc%5D-XyZ123",
            b"https://mail.google.com.evil.example/x https://evil.example/mail.google.com/x "
            b"https://mail.google.com@evil.example/x")
        self.assertEqual(self.deliver(raw).get_json()["outcome"], "confirm")
        self.assertIsNone(self.log()[0]["confirm_url"])
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertNotIn("evil.example", page)

    def test_saying_you_confirmed_it_clears_the_box(self):
        self.deliver(confirmation(self.address))
        self.assertIn("Gmail's confirmation has arrived", self.client.get("/connect").get_data(as_text=True))
        self.client.post("/connect/forwarding/confirmed")
        page = self.client.get("/connect").get_data(as_text=True)
        self.assertNotIn("Gmail's confirmation has arrived", page)
        self.assertIn("Forwarding confirmed in Gmail.", page)

    def test_it_stops_being_offered_once_mail_is_flowing(self):
        self.deliver(confirmation(self.address))
        self.deliver()
        self.assertNotIn("Gmail's confirmation has arrived", self.client.get("/connect").get_data(as_text=True))

    def test_a_forged_confirmation_cannot_inject_markup(self):
        raw = confirmation(self.address).replace(
            b"(#123456789) Gmail Forwarding Confirmation", b"<script>alert(1)</script>")
        self.deliver(raw)
        self.assertNotIn("<script>alert(1)", self.client.get("/connect").get_data(as_text=True))


class TestOtherServices(InboundCase):
    env = {**InboundCase.env, "ELEC_MAILGUN_SIGNING_KEY": "mg-key"}

    def mailgun(self, key="mg-key", stamp=None, **extra):
        stamp = str(int(time.time()) if stamp is None else stamp)
        signature = hmac.new(key.encode(), (stamp + "tok").encode(), hashlib.sha256).hexdigest()
        return self.hook.post("/api/inbound/email", data={
            "recipient": self.address, "body-mime": self.bill.decode("utf-8"),
            "timestamp": stamp, "token": "tok", "signature": signature, **extra})

    def test_mailgun_is_recognised_by_its_signature(self):
        self.assertEqual(self.mailgun().get_json()["outcome"], "imported")

    def test_a_bad_or_replayed_mailgun_signature_is_refused(self):
        self.assertEqual(self.mailgun(key="other").status_code, 403)
        self.assertEqual(self.mailgun(stamp=int(time.time()) - 3600).status_code, 403)
        self.assertFalse(self.ann.paths.csv.exists())

    def test_postmark_sends_json_and_a_password_in_the_url(self):
        basic = base64.b64encode(b"postmark:s3cret-hook").decode()
        res = self.hook.post("/api/inbound/email", headers={"Authorization": f"Basic {basic}"},
                             json={"OriginalRecipient": self.address,
                                   "RawEmail": self.bill.decode("utf-8")})
        self.assertEqual(res.get_json()["outcome"], "imported")

    def test_postmark_payloads_of_the_wrong_shape_are_refused_not_crashed(self):
        basic = base64.b64encode(b"postmark:s3cret-hook").decode()
        for payload in ([1, 2], "x", {"RawEmail": 5}, {"RawEmail": ["a"]}):
            with self.subTest(payload):
                res = self.hook.post("/api/inbound/email",
                                     headers={"Authorization": f"Basic {basic}"}, json=payload)
                self.assertEqual(res.status_code, 422)

    def test_a_postmark_recipient_of_the_wrong_type_is_ignored_not_fatal(self):
        basic = base64.b64encode(b"postmark:s3cret-hook").decode()
        raw = f"Delivered-To: {self.address}\r\n" + self.bill.decode("utf-8")
        for payload in ({"OriginalRecipient": 5, "RawEmail": raw},
                        {"OriginalRecipient": None, "To": ["x"], "RawEmail": raw}):
            with self.subTest(payload):
                res = self.hook.post("/api/inbound/email",
                                     headers={"Authorization": f"Basic {basic}"}, json=payload)
                self.assertEqual(res.status_code, 200)
                self.assertIn(res.get_json()["outcome"], ("imported", "duplicate"))

    def test_postmark_without_the_raw_message_says_how_to_fix_it(self):
        res = self.hook.post("/api/inbound/email",
                             headers={"Authorization": "Bearer s3cret-hook"},
                             json={"OriginalRecipient": self.address, "TextBody": "hi"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("raw email", res.get_json()["error"])


class TestHeldBills(InboundCase):
    """With two homes and an account neither has seen, the bill waits to be placed."""

    def setUp(self):
        super().setUp()
        from metermatters.store import homes
        self.homes = homes
        homes.add(self.ann.paths, "Cabin")

    def test_a_held_bill_arriving_again_is_still_waiting_not_on_file(self):
        self.assertEqual(self.deliver().get_json()["outcome"], "held")
        self.assertIn("none of your homes yet", self.log()[0]["detail"])
        self.assertEqual(self.deliver().get_json()["outcome"], "held")
        self.assertIn("still waiting for a home", self.log()[0]["detail"])
        self.assertNotIn("Already on file", self.log()[0]["detail"])
        self.assertEqual(len(self.homes.pending(self.ann.paths)), 1)

    def test_when_too_many_are_waiting_the_log_says_so(self):
        with mock.patch.object(self.homes, "MAX_PENDING", 1):
            self.deliver()
            self.deliver()
            res = self.deliver(self.bill.replace(b"******0001", b"******0003"))
        self.assertEqual(res.get_json()["outcome"], "rejected")
        self.assertIn("too many", self.log()[0]["detail"].lower())
        self.assertNotIn("Already on file", self.log()[0]["detail"])
        self.assertEqual(len(self.homes.pending(self.ann.paths)), 1)


if __name__ == "__main__":
    unittest.main()
