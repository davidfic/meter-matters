"""Tests for the notification channels. Nothing here leaves the machine."""

import os
import unittest
from unittest import mock

import notify

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith("ELEC_")}


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestSend(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, CLEAN, clear=True)
        env.start()
        self.addCleanup(env.stop)
        which = mock.patch("shutil.which", return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def test_unconfigured_channels_are_off_not_failed(self):
        outcome = notify.send("t", "b")
        self.assertEqual(outcome, {"desktop": "off", "ntfy": "off", "email": "off"})
        self.assertFalse(notify.delivered(outcome))

    def test_ntfy_posts_the_body_with_title_and_priority(self):
        os.environ["ELEC_NTFY_URL"] = "https://ntfy.example/topic"
        os.environ["ELEC_NTFY_TOKEN"] = "tk_123"
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            outcome = notify.send("Bill — late", "body text", "critical")
        self.assertEqual(outcome["ntfy"], "sent")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://ntfy.example/topic")
        self.assertEqual(request.data, b"body text")
        self.assertEqual(request.get_header("Priority"), "5")
        self.assertEqual(request.get_header("Title"), "Bill ? late")
        self.assertEqual(request.get_header("Authorization"), "Bearer tk_123")

    def test_one_dead_channel_does_not_stop_the_others(self):
        os.environ["ELEC_NTFY_URL"] = "https://ntfy.example/topic"
        os.environ["ELEC_NOTIFY_EMAIL"] = "me@example.com"
        os.environ["ELEC_SMTP_USER"] = "me@example.com"
        os.environ["ELEC_SMTP_PASS"] = "app-password"
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")), \
                mock.patch("smtplib.SMTP_SSL") as smtp:
            outcome = notify.send("t", "b")
        self.assertTrue(outcome["ntfy"].startswith("failed"))
        self.assertEqual(outcome["email"], "sent")
        self.assertTrue(notify.delivered(outcome))
        sent = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        self.assertEqual(sent["To"], "me@example.com")
        self.assertIn("t", sent["Subject"])

    def test_email_without_a_password_fails_loudly(self):
        os.environ["ELEC_NOTIFY_EMAIL"] = "me@example.com"
        os.environ["ELEC_SMTP_USER"] = "me@example.com"
        with mock.patch("billmail.keyring_password", return_value=None):
            outcome = notify.send("t", "b")
        self.assertIn("no SMTP password", outcome["email"])

    def test_desktop_uses_notify_send_when_present(self):
        with mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            outcome = notify.send("t", "b", "critical")
        self.assertEqual(outcome["desktop"], "sent")
        self.assertIn("--urgency=critical", run.call_args[0][0])

    def test_cli_exit_code_reflects_delivery(self):
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(notify.main(["t", "b"]), 1)
            self.assertEqual(notify.main([]), 1)


if __name__ == "__main__":
    unittest.main()
