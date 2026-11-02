#!/usr/bin/env python
"""Read the amount and due date out of a utility's "bill ready" email.

Any utility's. The app keeps no list of them and no template per provider:
there are thousands of utilities in the country, and a reader written by hand
for each would never be finished. One set of rules reads every notice, and
nothing in it names a provider. The email is read two ways, the second only
when the first could not decide:

    by looking   the general reader in scan.py, built for bills photographed
                 off paper: it knows the many ways an amount due, a due date
                 and a fuel are written, whoever wrote them.
    by asking    Claude, when the server has a key (ELEC_ANTHROPIC_KEY). The
                 message's text, with account numbers, links and addresses
                 cut out, goes to the model with a fixed question: is this a
                 bill, for which fuel, how much, due when. Without a key this
                 step is skipped, and a notice the patterns could not read is
                 logged as not filed.

A figure is never guessed at. No amount or no due date means no bill, since a
bill filed in the wrong month corrupts every trend on the dashboard.

The useful fields often live only in the HTML part; a plain-text alternative
can omit the due date. Both are read.

    ./billmail.py bill-ready.eml                            # parse one file
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

DEFAULT_HOST = "imap.gmail.com"
# The model that reads what the patterns could not. Overridable for cost.
DEFAULT_MODEL = "claude-opus-5-5"
MODEL_TIMEOUT = 45.0
# What is sent to the model is capped: a bill notice is a page, not a book.
MODEL_MAX_CHARS = 12000

# The ways an account is named: "Account #: 1234567890", "Account number
# ending in 4321", "Acct No. ****4321". The last four digits are all that is
# kept, whichever way it was written.
RE_ACCOUNT = re.compile(
    r"\bacc(?:oun)?t\.?\s*(?:number|no\.?|num\.?|#|id)?\s*:?\s*"
    r"(?:ending\s+(?:in|with)\s*|[*xX•#]+\s*|-\s*)?(\d[\d -]{2,}\d)\b", re.I)
# A due date further than this from the message itself is a typo or a
# forgery, and filing it would put the bill outside every trend.
MAX_DUE_DISTANCE = timedelta(days=2 * 365)


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
    """The fuel a subject line names: electric, gas, water or unknown.

    Many utilities send every fuel from one address, and filing a gas bill in
    the electric history would corrupt every trend. So the subject is read
    first - it is the one line the utility wrote to say what the mail is -
    and "electric" wins a subject naming both, pinned by test.
    """
    lowered = (subject or "").lower()
    if "electric" in lowered:
        return "electric"
    if "gas" in lowered:
        return "gas"
    if "water" in lowered or "sewer" in lowered:
        return "water"
    return "unknown"


def fuel_in(text: str) -> str:
    """The fuel the text as a whole is about, by which words appear most -
    the scan reader's judgement, in this module's spelling."""
    from metermatters.intake import scan
    found = scan._fuel(text or "")
    return found.lower() if found else "unknown"


def visible_text(markup: str) -> str:
    """Strip HTML to the text a reader would actually see."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip()


def _text_of(msg) -> str:
    """Everything a reader would see, HTML part first. Raises ParseError."""
    chunks = []
    for part in ("html", "plain"):
        try:
            body = msg.get_body(preferencelist=(part,))
        except Exception:
            body = None
        if body is None:
            continue
        try:
            content = body.get_content()
        except (LookupError, UnicodeError):     # a charset Python has never heard of
            continue
        chunks.append(visible_text(content) if part == "html" else content)
    blob = "\n".join(chunks)
    if not blob.strip():
        raise ParseError("the message had no readable body")
    return blob


def _account_suffix(blob: str) -> str | None:
    for m in RE_ACCOUNT.finditer(blob):
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) >= 4:
            return digits[-4:]
    return None


def _bill_date(blob: str):
    """The statement's own date, when the notice gives one under any of its names."""
    from metermatters.intake import scan
    m = re.search(rf"(?:bill|billing|statement|invoice)\s+date\s*:?\s*({scan.DATE})", blob, re.I)
    return scan._date(m.group(1)) if m else None


def _by_looking(blob: str, subject: str) -> dict | None:
    """The scan reader's patterns: the many ways any utility writes an amount
    due and a due date. Taken only when both were found by a label - a
    figure it had to look around for is a guess, and a guess is never filed."""
    from metermatters.intake import scan
    found = scan.guess(f"{subject}\n{blob}")
    if "amount" not in found["sure"] or "due" not in found["sure"]:
        return None
    return {"amount": found["amount"], "due": date.fromisoformat(found["due"]),
            "bill_date": _bill_date(blob)}


def model_enabled() -> bool:
    return bool(os.environ.get("ELEC_ANTHROPIC_KEY", "").strip())


RE_LINK = re.compile(r"https?://\S+|www\.\S+", re.I)
RE_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
RE_LONG_NUMBER = re.compile(r"\d[\d -]{4,}\d")


def for_model(text: str) -> str:
    """The text as it is sent to the model: links, addresses and long numbers
    cut, since none of them is needed to read a bill and all of them say
    who the customer is. Dates and amounts survive. Capped in length."""
    text = RE_LINK.sub("[link]", text)
    text = RE_EMAIL.sub("[email]", text)

    def cut(m):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) <= 4 or re.fullmatch(r"\d{1,2}[ -]\d{1,2}[ -]\d{2,4}", m.group()):
            return m.group()                    # a date written with dashes
        return "..." + digits[-4:]
    text = RE_LONG_NUMBER.sub(cut, text)
    text = re.sub(r"[ \t]+", " ", text)
    return text[:MODEL_MAX_CHARS]


MODEL_QUESTION = """You read emails that utilities send to their customers, for an app that keeps a household's bill history.

Decide whether this email is a new bill, or a notice that a new bill is ready, for electricity, natural gas, or water (sewer counts as water). Then pull out the figures.

Not a bill: a payment receipt or confirmation, an autopay or "payment scheduled" notice, a reminder with no amount, an outage or service notice, a marketing message, or an email that only links to a website and states no amount owed.

amount_due is the total the customer must now pay. due_date is the day it must be paid by. bill_date is the statement date if the email gives one. account_last4 is the last four digits of the account number if one appears. provider is the utility's name as the email gives it.

Use only what the text says. A figure that is not in the text is null. If you cannot tell which fuel the bill is for, say unknown."""

MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "is_bill": {"type": "boolean"},
        "fuel": {"type": "string", "enum": ["electric", "gas", "water", "unknown"]},
        "amount_due": {"type": ["number", "null"]},
        "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "bill_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "account_last4": {"type": ["string", "null"]},
        "provider": {"type": ["string", "null"]},
    },
    "required": ["is_bill", "fuel", "amount_due", "due_date", "bill_date",
                 "account_last4", "provider"],
    "additionalProperties": False,
}


def ask_model(text: str) -> dict | None:
    """Claude's reading of the text, as a dict in MODEL_SCHEMA's shape, or
    None when it couldn't be had. Nothing here raises: a model that is down
    or declines leaves the bill unread, never the intake broken."""
    import json
    import logging
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=os.environ["ELEC_ANTHROPIC_KEY"].strip(),
                                 timeout=MODEL_TIMEOUT, max_retries=1)
    try:
        response = client.messages.create(
            model=os.environ.get("ELEC_ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
            max_tokens=1024,
            system=MODEL_QUESTION,
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": MODEL_SCHEMA}},
        )
    except anthropic.APIError as exc:
        logging.getLogger(__name__).warning("bill reader: %s", type(exc).__name__)
        return None
    if response.stop_reason != "end_turn":
        return None
    try:
        answer = next(b.text for b in response.content if b.type == "text")
        return json.loads(answer)
    except (StopIteration, ValueError):
        return None


def _by_asking(blob: str, subject: str, sender: str | None) -> dict | None:
    """The model's reading, held to the same bar as the patterns: a bill,
    with an amount and a due date, or nothing."""
    text = f"Subject: {subject}\n"
    if sender:
        text += f"From: {sender.rpartition('@')[2] or sender}\n"
    answer = ask_model(for_model(text + "\n" + blob))
    if not answer or not answer.get("is_bill"):
        return None
    amount, due = answer.get("amount_due"), answer.get("due_date")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
        return None
    try:
        due = date.fromisoformat(due) if due else None
        bill_date = date.fromisoformat(answer["bill_date"]) if answer.get("bill_date") else None
    except (TypeError, ValueError):
        return None
    if due is None:
        return None
    last4 = answer.get("account_last4")
    return {"amount": float(amount), "due": due, "bill_date": bill_date,
            "fuel": answer.get("fuel") if answer.get("fuel") in ("electric", "gas", "water") else None,
            "account_suffix": last4 if isinstance(last4, str) and re.fullmatch(r"\d{4}", last4) else None}


def parse_message(msg, subject: str | None = None, sender: str | None = None) -> BillNotice:
    """Pull the bill details out of a parsed email message.

    `subject` replaces the message's own when the bill was forwarded by hand
    and the real subject - the one that says which fuel - is in the body.
    `sender` is the utility's address, a hint to the fuel (a water district
    says so in its name) and to the model.
    """
    blob = _text_of(msg)
    subject = subject or str(msg.get("Subject") or "")

    received = None
    if msg.get("Date"):
        try:
            received = email.utils.parsedate_to_datetime(msg["Date"]).date()
        except (TypeError, ValueError):
            pass

    found = _by_looking(blob, subject)
    fuel = fuel_of(subject)
    if fuel == "unknown":
        fuel = fuel_in(f"{subject}\n{sender or ''}\n{blob}")
    if (found is None or fuel == "unknown") and model_enabled():
        asked = _by_asking(blob, subject, sender)
        if asked:
            found = found or asked
            if fuel == "unknown" and asked["fuel"]:
                fuel = asked["fuel"]
            found.setdefault("account_suffix", asked["account_suffix"])
    if found is None:
        raise ParseError("no amount due and due date found")
    due = found["due"]
    if abs(due - (received or date.today())) > MAX_DUE_DISTANCE:
        raise ParseError(f"the due date {due} is years away from when the message was sent")

    return BillNotice(
        amount=found["amount"],
        due=due,
        bill_date=found.get("bill_date"),
        account_suffix=found.get("account_suffix") or _account_suffix(blob),
        subject=subject,
        received=received,
        fuel=fuel,
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
               sender: str | None = None, days: int | None = 45,
               fuel: str = "electric", account: str | None = None) -> list[BillNotice]:
    """Return matching bill notices, oldest first.

    ``days=None`` searches the whole mailbox - use that to backfill, and
    ``[Gmail]/All Mail`` as the folder so archived mail is included (it
    excludes Spam and Trash). Messages are de-duplicated on Message-ID,
    because Gmail's All Mail overlaps every other label.
    """
    criteria: list[str] = ["FROM", f'"{sender}"'] if sender else ["ALL"]
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
    waiting: int = 0              # held before and still waiting - not "already on file"
    dropped: int = 0              # not kept: too many were already waiting


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
        description="Read utilities' bill-ready emails.",
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
    parser.add_argument("--fuel", default="electric", choices=["electric", "gas", "water", "any"],
                        help="which utility (default: electric - gas must never mix in)")
    parser.add_argument("--account", help="only bills whose masked account matches this")
    parser.add_argument("--sender", default=None,
                        help="only mail from this address (default: every message in the folder)")
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
