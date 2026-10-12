#!/usr/bin/env python
"""Tell a person that something needs them - wherever they happen to be.

Sends the same message down every channel that is configured; a channel that
isn't configured is skipped silently:

    ntfy      ELEC_NTFY_URL    e.g. https://ntfy.sh/<a-long-random-topic>
              ELEC_NTFY_TOKEN  optional, for a protected topic
    email     ELEC_NOTIFY_EMAIL  where the operator's copy goes
              ELEC_SMTP_HOST     default smtp.gmail.com
              ELEC_SMTP_USER     the account to send as
              ELEC_SMTP_PASS     its password
              ELEC_SMTP_FROM     the From address, default ELEC_SMTP_USER

Users are told about their own bills by email alone (`send_user`), which needs
only the SMTP settings.
    desktop   notify-send, where there is a desktop to show it

    ./notify.py "Bill import failed" "the mailbox refused the login"
    ./notify.py --urgency critical "Title" "Body"
    ./notify.py --test          # send a test message, report each channel

ntfy and email leave the machine, and the message may name a bill amount.
Both are off until you set them. An ntfy.sh topic is a shared secret - anyone
who knows the name can read it - so make it long and random, or self-host.

Exit codes: 0 at least one channel delivered, 1 none did.
"""

from __future__ import annotations

import argparse
import os
import shutil
import smtplib
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage

APP_NAME = "Meter Matters"
URGENCIES = ("low", "normal", "critical")
# ntfy's scale runs 1-5.
NTFY_PRIORITY = {"low": "2", "normal": "3", "critical": "5"}
TIMEOUT = 15


def _desktop(title: str, body: str, urgency: str) -> bool | None:
    tool = shutil.which("notify-send")
    if not tool:
        return None
    done = subprocess.run(
        [tool, f"--app-name={APP_NAME}", f"--urgency={urgency}", title, body],
        capture_output=True, timeout=TIMEOUT)
    return done.returncode == 0


def _ntfy(title: str, body: str, urgency: str) -> bool | None:
    url = os.environ.get("ELEC_NTFY_URL")
    if not url:
        return None
    # Headers are latin-1 on the wire; a title with an em dash would raise.
    headers = {"Title": title.encode("ascii", "replace").decode("ascii"),
               "Priority": NTFY_PRIORITY[urgency]}
    token = os.environ.get("ELEC_NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body.encode("utf-8"),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return 200 <= response.status < 300


def _email(title: str, body: str, urgency: str, to: str | None = None) -> bool | None:
    to = to or os.environ.get("ELEC_NOTIFY_EMAIL")
    if not to or not os.environ.get("ELEC_SMTP_USER", os.environ.get("ELEC_IMAP_USER")):
        return None
    user = os.environ.get("ELEC_SMTP_USER") or os.environ.get("ELEC_IMAP_USER")
    password = os.environ.get("ELEC_SMTP_PASS") or os.environ.get("ELEC_IMAP_PASS")
    if not password:
        import billmail  # a desktop run may keep it in the login keyring
        password = billmail.keyring_password(user)
    if not password:
        raise RuntimeError("no SMTP password in the environment or the keyring")

    message = EmailMessage()
    message["Subject"] = f"[{APP_NAME}] {title}"
    message["From"] = os.environ.get("ELEC_SMTP_FROM", user)
    message["To"] = to
    if urgency == "critical":
        message["X-Priority"] = "1"
    message.set_content(body)
    host = os.environ.get("ELEC_SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, timeout=TIMEOUT) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)
    return True


CHANNELS = {"desktop": _desktop, "ntfy": _ntfy, "email": _email}


def send(title: str, body: str, urgency: str = "normal") -> dict[str, str]:
    """Send down every configured channel. Returns channel -> outcome.

    Outcomes are "sent", "off" (not configured) or "failed: <why>". One channel
    failing never stops the others - a dead ntfy server must not swallow the
    email that says the bill import broke.
    """
    if urgency not in URGENCIES:
        urgency = "normal"
    outcome = {}
    for name, channel in CHANNELS.items():
        try:
            result = channel(title, body, urgency)
        except (OSError, RuntimeError, subprocess.SubprocessError,
                smtplib.SMTPException, urllib.error.URLError, ValueError) as exc:
            outcome[name] = f"failed: {exc}"
            continue
        outcome[name] = "off" if result is None else ("sent" if result else "failed")
    return outcome


def send_user(to: str, title: str, body: str, urgency: str = "normal") -> dict[str, str]:
    """Tell one user something about their own bills - by email, and only email.

    The other channels belong to whoever runs the server. A user's bill has no
    business on the operator's phone.
    """
    if urgency not in URGENCIES:
        urgency = "normal"
    try:
        result = _email(title, body, urgency, to=to)
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError) as exc:
        return {"email": f"failed: {exc}"}
    return {"email": "off" if result is None else "sent"}


def delivered(outcome: dict[str, str]) -> bool:
    return any(v == "sent" for v in outcome.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a notification down every configured channel.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("title", nargs="?")
    parser.add_argument("body", nargs="?", default="")
    parser.add_argument("--urgency", default="normal", choices=URGENCIES)
    parser.add_argument("--test", action="store_true",
                        help="send a test message and report each channel")
    args = parser.parse_args(argv)

    if args.test:
        args.title = args.title or "Test notification"
        args.body = args.body or "If you can read this, this channel works."
    elif not args.title:
        parser.print_help()
        return 1

    outcome = send(args.title, args.body, args.urgency)
    # The journal is a channel too, and the only one that is always there.
    print(f"{args.title} - {args.body}")
    for name, result in outcome.items():
        if args.test or result.startswith("failed"):
            print(f"  {name}: {result}", file=sys.stderr if result.startswith("failed") else sys.stdout)
    return 0 if delivered(outcome) else 1


if __name__ == "__main__":
    sys.exit(main())
