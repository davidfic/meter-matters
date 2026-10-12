#!/usr/bin/env python
"""Read the amount and due date out of Eversource's "bill ready" email.

This is the monthly automation. Eversource has no usable API for an individual
- Green Button Connect requires registering as a third-party company - but the
notification email carries everything the dashboard needs, and reading your own
mailbox needs no utility credentials at all.

The useful fields live only in the HTML part; the plain-text alternative omits
the due date. Both are checked, HTML first.

    ./billmail.py "New Electric Bill from Eversource.eml"   # parse one file
    ./billmail.py --imap                                    # poll the mailbox
    ./billmail.py --imap --import                           # ...and file them

Credentials come from the environment - on a host, from its secret store:

    ELEC_IMAP_HOST (default imap.gmail.com)  ELEC_IMAP_USER  ELEC_IMAP_PASS

On a desktop with no ELEC_IMAP_PASS set, the login keyring is tried instead:

    secret-tool store --label='elec-bill' service elec-bill account <you>

Gmail needs an app password, not your account password.
"""

from __future__ import annotations

import argparse
import email
import html as html_mod
import imaplib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email import policy
from pathlib import Path

DEFAULT_SENDER = "noreply@notifications.eversource.com"
DEFAULT_HOST = "imap.gmail.com"

# Labelled fields in the HTML body. Matching the labels rather than the markup
# keeps this working when the template's styling changes.
RE_DUE = re.compile(r"Due date:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)
RE_BILL_DATE = re.compile(r"Bill date:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)
# 2018-era mail says "Amount Due:"; later mail says "Total Amount Due:".
RE_TOTAL = re.compile(r"(?:Total\s+)?Amount Due:?\s*\$\s*([\d,]+\.\d{2})", re.I)
RE_ACCOUNT = re.compile(r"Account Number:?\s*\*+\s*(\d{3,4})", re.I)
RE_ANY_AMOUNT = re.compile(r"total amount due of \$\s*([\d,]+\.\d{2})", re.I)


class ParseError(ValueError):
    """The mail didn't contain what we need - loudly, not silently."""


@dataclass
class BillNotice:
    amount: float
    due: date
    bill_date: date | None = None
    account_suffix: str | None = None
    subject: str = ""
    received: date | None = None
    fuel: str = "unknown"        # electric | gas | unknown
    message_id: str = ""

    def __str__(self) -> str:
        acct = f" acct ...{self.account_suffix}" if self.account_suffix else ""
        return f"{self.fuel:8} ${self.amount:>8,.2f} due {self.due}{acct}"


def fuel_of(subject: str) -> str:
    """Eversource sends electric and gas from the same address.

    Filing a gas bill in the electric history would corrupt every trend, so an
    unrecognised subject is "unknown" and gets skipped rather than guessed at.
    """
    lowered = (subject or "").lower()
    if "electric" in lowered:
        return "electric"
    if "gas" in lowered:
        return "gas"
    return "unknown"


def visible_text(markup: str) -> str:
    """Strip HTML to the text a reader would actually see."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip()


def _date(value: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_message(msg) -> BillNotice:
    """Pull the bill details out of a parsed email message."""
    chunks = []
    for part in ("html", "plain"):
        try:
            body = msg.get_body(preferencelist=(part,))
        except Exception:
            body = None
        if body is None:
            continue
        content = body.get_content()
        chunks.append(visible_text(content) if part == "html" else content)
    blob = "\n".join(chunks)
    if not blob.strip():
        raise ParseError("the message had no readable body")

    total = RE_TOTAL.search(blob) or RE_ANY_AMOUNT.search(blob)
    if not total:
        raise ParseError("no 'Total Amount Due' found")
    amount = float(total.group(1).replace(",", ""))

    due_match = RE_DUE.search(blob)
    due = _date(due_match.group(1)) if due_match else None
    if due is None:
        # Refuse to guess. A wrong due date files the bill in the wrong month,
        # which silently corrupts every trend on the dashboard.
        raise ParseError("no 'Due date' found - the HTML part is required")

    bill_match = RE_BILL_DATE.search(blob)
    acct = RE_ACCOUNT.search(blob)
    received = None
    if msg.get("Date"):
        try:
            received = email.utils.parsedate_to_datetime(msg["Date"]).date()
        except (TypeError, ValueError):
            pass

    subject = str(msg.get("Subject") or "")
    return BillNotice(
        amount=amount,
        due=due,
        bill_date=_date(bill_match.group(1)) if bill_match else None,
        account_suffix=acct.group(1) if acct else None,
        subject=subject,
        received=received,
        fuel=fuel_of(subject),
        message_id=str(msg.get("Message-ID") or ""),
    )


def parse_file(path) -> BillNotice:
    raw = Path(path).read_bytes()
    return parse_message(email.message_from_bytes(raw, policy=policy.default))


# --------------------------------------------------------------------------
# Mailbox polling
# --------------------------------------------------------------------------

def keyring_password(user: str) -> str | None:
    """Ask libsecret, so the password never sits in a file or the shell history."""
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", "service", "elec-bill", "account", user],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def fetch_imap(*, host: str, user: str, password: str, folder: str = "INBOX",
               sender: str = DEFAULT_SENDER, days: int | None = 45,
               fuel: str = "electric", account: str | None = None) -> list[BillNotice]:
    """Return matching bill notices, oldest first.

    ``days=None`` searches the whole mailbox - use that to backfill, and
    ``[Gmail]/All Mail`` as the folder so archived mail is included (it
    excludes Spam and Trash). Messages are de-duplicated on Message-ID,
    because Gmail's All Mail overlaps every other label.
    """
    criteria: list[str] = ["FROM", f'"{sender}"']
    if days is not None:
        criteria += ["SINCE", (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")]

    notices: list[BillNotice] = []
    seen: set[str] = set()
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(folder, readonly=True)
        status, data = imap.search(None, *criteria)
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        for uid in (data[0].split() if data and data[0] else []):
            status, payload = imap.fetch(uid, "(RFC822)")
            if status != "OK" or not payload or not payload[0]:
                continue
            msg = email.message_from_bytes(payload[0][1], policy=policy.default)
            key = str(msg.get("Message-ID") or uid)
            if key in seen:
                continue
            seen.add(key)
            try:
                notice = parse_message(msg)
            except ParseError:
                continue  # not a bill-ready mail, or a template we don't know
            if fuel != "any" and notice.fuel != fuel:
                continue
            if account and notice.account_suffix and not account.endswith(notice.account_suffix):
                continue
            notices.append(notice)
    notices.sort(key=lambda n: n.due)
    return notices


def known_accounts(csv_path: Path, usage_dir: Path) -> list[str]:
    """Account strings we can file against, in the CSV's own formatting."""
    from metermatters.analytics import analysis
    from metermatters.intake import ingest

    accounts: list[str] = []
    rows, _ = ingest.read_rows(csv_path)
    for row in rows:
        value = (row.get("Account# or Nickname") or "").strip()
        if value and value not in accounts:
            accounts.append(value)

    # Usage exports name accounts the bill history may not have seen yet.
    try:
        from metermatters.intake import greenbutton
        template = accounts[0] if accounts else None
        for account in sorted(greenbutton.load_dir(usage_dir).accounts):
            if any(account in a for a in accounts):
                continue
            # Reuse the CSV's formatting so the column stays consistent.
            accounts.append(
                re.sub(r"\d{6,}", account, template, count=1) if template else account)
    except Exception:
        pass
    return accounts


@dataclass
class Filed:
    added: int = 0
    skipped: int = 0              # of unknown fuel - never guessed at
    new_accounts: list = None
    held: int = 0                 # for no home in particular; waiting to be placed


def file_notices(notices: list[BillNotice], csv_path, usage_dir) -> Filed:
    """File bill notices into a statement history. Idempotent.

    However a notice reached us - the mailbox, a forwarded message, a saved
    file - it is filed the same way, so the routes can never disagree.
    """
    from metermatters.analytics import analysis
    from metermatters.intake import ingest  # local import: parsing shouldn't require the importer

    csv_path = Path(csv_path)
    # Bills arrive for whichever account was live at the time. File each under
    # its real account rather than forcing them all under the current one -
    # the CSV stays truthful and the dashboard can still show them together.
    known = known_accounts(csv_path, Path(usage_dir))

    rows, skipped, invented = [], 0, []
    for notice in notices:
        if notice.fuel == "unknown":
            skipped += 1
            continue
        target_account = None
        if notice.account_suffix:
            for full in known:
                if re.sub(r"\D", "", analysis.split_account(full)[0]).endswith(notice.account_suffix):
                    target_account = full
                    break
            if target_account is None:
                # An account we've never seen. All we have is the masked
                # number, so that becomes the identifier until a usage export
                # names it in full.
                target_account = f"******{notice.account_suffix} ({notice.fuel})"
                known.append(target_account)
                invented.append(target_account)
        else:
            # No account on the notice: the account already on file for that
            # fuel - never the other fuel's - or a name that says what it is.
            target_account = ingest.default_account(csv_path, fuel=notice.fuel.capitalize()) \
                or f"{notice.fuel.capitalize()} (email)"
        rows.append(ingest.bill_row(
            target_account, notice.due.isoformat(), notice.amount,
            account_type=notice.fuel.capitalize(), source="email"))

    added = ingest.merge(csv_path, rows, list(rows[0])).added if rows else 0
    return Filed(added=added, skipped=skipped, new_accounts=sorted(invented))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read Eversource bill-ready emails.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("files", nargs="*", help=".eml files to parse")
    parser.add_argument("--imap", action="store_true", help="poll the mailbox instead")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="file what's found into AccountHistory.csv")
    parser.add_argument("--days", type=int, default=45, help="how far back to search")
    parser.add_argument("--all", dest="all_time", action="store_true",
                        help="search the whole mailbox, not just --days")
    parser.add_argument("--folder", default=None,
                        help="mailbox (default: INBOX, or All Mail with --all)")
    parser.add_argument("--fuel", default="electric", choices=["electric", "gas", "any"],
                        help="which utility (default: electric - gas must never mix in)")
    parser.add_argument("--account", help="only bills whose masked account matches this")
    parser.add_argument("--sender", default=DEFAULT_SENDER)
    args = parser.parse_args(argv)

    notices: list[BillNotice] = []
    if args.imap:
        user = os.environ.get("ELEC_IMAP_USER")
        if not user:
            print("error: set ELEC_IMAP_USER", file=sys.stderr)
            return 1
        password = os.environ.get("ELEC_IMAP_PASS") or keyring_password(user)
        if not password:
            print("error: no mailbox password - set ELEC_IMAP_PASS", file=sys.stderr)
            return 1
        folder = args.folder or ("[Gmail]/All Mail" if args.all_time else "INBOX")
        try:
            notices = fetch_imap(host=os.environ.get("ELEC_IMAP_HOST", DEFAULT_HOST),
                                 user=user, password=password, folder=folder,
                                 sender=args.sender,
                                 days=None if args.all_time else args.days,
                                 fuel=args.fuel, account=args.account)
        except (imaplib.IMAP4.error, OSError, RuntimeError) as exc:
            print(f"error: mailbox: {exc}", file=sys.stderr)
            return 1
    elif args.files:
        for path in args.files:
            try:
                notices.append(parse_file(path))
            except (ParseError, OSError) as exc:
                print(f"error: {path}: {exc}", file=sys.stderr)
                return 1
    else:
        parser.print_help()
        return 1

    if not notices:
        print("no bill notices found")
        return 2

    for notice in notices:
        print(notice)

    if not args.do_import:
        return 0

    from metermatters import config
    try:
        result = file_notices(notices, config.CSV_PATH, config.USAGE_DIR)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"imported {result.added} new statement(s)"
          + (f"; skipped {result.skipped} of unknown fuel" if result.skipped else ""))
    for name in result.new_accounts:
        print(f"  new account: {name}")
    return 0 if result.added else 2


if __name__ == "__main__":
    sys.exit(main())
