"""Bills that arrive by forwarded email.

Each user has a private address - ``bills-<token>@<ELEC_INBOUND_DOMAIN>`` - and
a filter in their own mailbox that forwards the utility's notices to it. An
inbound email service turns each message into an HTTPS request to us. The app
never holds a mailbox credential and never sees any other mail.

Three services are understood, told apart by what they send:

    raw        the message itself as the body (message/rfc822), the recipient
               in X-Envelope-To. For a Cloudflare Email Worker, or anything
               that can POST. Authenticated with  Authorization: Bearer <secret>
    mailgun    a route forwarding to a URL, so the form carries `body-mime`.
               Authenticated by Mailgun's own signature (ELEC_MAILGUN_SIGNING_KEY)
    postmark   JSON with `RawEmail` (switch on "include raw email content").
               Authenticated by the basic-auth password in the webhook URL.

ELEC_INBOUND_SECRET is the shared secret for `raw` and `postmark`.
"""

from __future__ import annotations

import base64
import email
import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass
from email import policy
from email.utils import getaddresses, parseaddr

from metermatters.store import accounts
from metermatters.intake import billmail
from metermatters.store import db
from metermatters.store import homes
from metermatters.store import workspace

ADDRESS_PREFIX = "bills-"
# Mailgun signs a timestamp; an old one is a replay.
MAX_SIGNATURE_AGE = 15 * 60
GOOGLE_FORWARDING = "forwarding-noreply@google.com"
# The only hosts a Gmail confirmation link may point at. It gets shown to the
# user as something to click, so nothing else is ever surfaced.
CONFIRM_HOSTS = ("mail.google.com", "mail-settings.google.com")
RE_URL = re.compile(r"https://[^\s<>\"')\]]+")
RE_CODE = re.compile(r"confirmation code:?\s*(\d{6,12})", re.I)
KEEP_LOG = 50


class Refused(Exception):
    """The request isn't from our email service. Answered with a 4xx."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


@dataclass
class Delivery:
    recipient: str
    raw: bytes


def enabled() -> bool:
    return bool(os.environ.get("ELEC_INBOUND_DOMAIN")) and bool(
        os.environ.get("ELEC_INBOUND_SECRET") or os.environ.get("ELEC_MAILGUN_SIGNING_KEY"))


def address_for(user) -> str:
    return f"{ADDRESS_PREFIX}{user.inbound_token}@{os.environ.get('ELEC_INBOUND_DOMAIN', '')}"


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _shared_secret(request) -> bool:
    secret = os.environ.get("ELEC_INBOUND_SECRET")
    if not secret:
        return False
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return _same(header[7:], secret)
    if header.startswith("Basic "):
        try:
            _, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return _same(password, secret)
    return False


def read(request) -> Delivery:
    """Authenticate the request and pull the message out of it."""
    kind = (request.content_type or "").split(";")[0].strip().lower()

    if "body-mime" in request.form:                                   # mailgun
        key = os.environ.get("ELEC_MAILGUN_SIGNING_KEY")
        stamp = request.form.get("timestamp", "")
        expected = hmac.new((key or "").encode(), (stamp + request.form.get("token", "")).encode(),
                            hashlib.sha256).hexdigest()
        if not key or not _same(request.form.get("signature", ""), expected):
            raise Refused("bad signature")
        if not stamp.isdigit() or abs(time.time() - int(stamp)) > MAX_SIGNATURE_AGE:
            raise Refused("stale signature")
        return Delivery(request.form.get("recipient", ""),
                        request.form["body-mime"].encode("utf-8", "surrogateescape"))

    if not _shared_secret(request):
        raise Refused("bad credentials", 401)

    if kind == "application/json":                                     # postmark
        payload = request.get_json(silent=True) or {}
        raw = payload.get("RawEmail")
        if not raw:
            raise Refused("no RawEmail in the payload - switch on "
                          "'include raw email content' for the inbound stream", 422)
        return Delivery(payload.get("OriginalRecipient") or payload.get("To", ""),
                        raw.encode("utf-8", "surrogateescape"))

    body = request.get_data()                                          # raw
    if not body:
        raise Refused("empty message", 422)
    return Delivery(request.headers.get("X-Envelope-To", ""), body)


def token_from(recipient: str, msg) -> str | None:
    """Find our address - in the envelope recipient, else in the headers."""
    domain = os.environ.get("ELEC_INBOUND_DOMAIN", "").lower()
    candidates = [recipient or ""]
    for header in ("Delivered-To", "X-Forwarded-To", "X-Original-To", "To", "Cc"):
        candidates += [addr for _, addr in getaddresses(msg.get_all(header) or [])]
    for candidate in candidates:
        local, _, host = parseaddr(candidate)[1].lower().partition("@")
        if host == domain and local.startswith(ADDRESS_PREFIX):
            # Strip any +tag a mail system appended.
            return local[len(ADDRESS_PREFIX):].split("+")[0]
    return None


def dkim_result(raw: bytes) -> str:
    """"pass", "fail" or "unknown" - for a signature by the utility's own domain.

    A forwarded message keeps the signature the utility put on it, so a pass
    means the bill is genuine whoever relayed it. A signature from the
    utility's mail vendor proves nothing: anyone can be that vendor's customer.
    """
    try:
        import dkim
    except ImportError:
        return "unknown"
    wanted = billmail.DEFAULT_SENDER.rsplit("@", 1)[1].split(".", 1)[1]   # eversource.com
    msg = email.message_from_bytes(raw)
    found = False
    for index, header in enumerate(msg.get_all("DKIM-Signature") or []):
        match = re.search(r"\bd=([^;\s]+)", header)
        signer = match.group(1).lower() if match else ""
        if signer != wanted and not signer.endswith("." + wanted):
            continue
        found = True
        try:
            if dkim.DKIM(raw).verify(idx=index):
                return "pass"
        except Exception:        # DNS down, malformed key: no verdict, not a forgery
            return "unknown"
    return "fail" if found else "unknown"


def _log(user, outcome: str, subject: str, detail: str,
         confirm_url: str | None = None, confirm_code: str | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO inbound_log (user_id, received_at, outcome, subject, detail, "
            "confirm_url, confirm_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user.id, db.now(), outcome, subject[:200], detail[:500], confirm_url, confirm_code))
        conn.execute(
            "DELETE FROM inbound_log WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM inbound_log WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user.id, user.id, KEEP_LOG))


def log_for(user, limit: int = 8) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM inbound_log WHERE user_id = ? "
                            "ORDER BY id DESC LIMIT ?", (user.id, limit)).fetchall()
    return [dict(r) for r in rows]


def pending_confirmation(user) -> dict | None:
    """Gmail's "confirm this forwarding address" message, if one is waiting.

    Waiting means: nothing has been forwarded since. Once a bill arrives the
    address is plainly confirmed and the link is of no further use.
    """
    for row in log_for(user, KEEP_LOG):
        if row["outcome"] in ("imported", "duplicate", "held", "confirmed"):
            return None
        if row["outcome"] == "confirm":
            return row
    return None


def mark_confirmed(user) -> bool:
    """The person says they clicked Google's link. The app cannot see that
    click, so this is how the box goes away before the first bill arrives."""
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM inbound_log WHERE user_id = ? AND outcome = 'confirm' "
                           "ORDER BY id DESC LIMIT 1", (user.id,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE inbound_log SET outcome = 'confirmed', "
                     "detail = 'Forwarding confirmed in Gmail.' WHERE id = ?", (row["id"],))
    return True


def _body_text(msg) -> str:
    text = ""
    for part in ("plain", "html"):
        body = msg.get_body(preferencelist=(part,))
        if body is not None:
            content = body.get_content()
            text += "\n" + (billmail.visible_text(content) if part == "html" else content)
    return text


# The header block a mail client writes into a message forwarded by hand:
# "---------- Forwarded message --------- From: Eversource <noreply@...> Date: ...
# Subject: ... To: ...". HTML comes to us flattened to one line, so nothing
# here is anchored to a line start.
RE_FWD_FROM = re.compile(r"\bFrom:\s*(?:[^<\n]{0,80}?<)?\s*([\w.+-]+@[\w.-]+\.\w+)", re.I)
RE_FWD_SUBJECT = re.compile(r"\bSubject:\s*(.+?)\s*(?=\bTo:|\bCc:|\n|$)", re.I)


def forwarded_original(msg) -> tuple[str | None, str | None]:
    """The sender and subject of the message this one forwards, if it is a
    forward made by hand. (None, None) when it isn't."""
    text = _body_text(msg)
    if "forwarded message" not in text.lower():
        return None, None
    tail = text[text.lower().index("forwarded message"):]
    sender = RE_FWD_FROM.search(tail)
    subject = RE_FWD_SUBJECT.search(tail)
    return ((sender.group(1).lower() if sender else None),
            (subject.group(1).strip() if subject else None))


def _confirmation(msg) -> tuple[str | None, str | None]:
    text = _body_text(msg)
    link = None
    for url in RE_URL.findall(text):
        host = url.split("/")[2].lower()
        if host in CONFIRM_HOSTS:
            link = url.rstrip(".,")
            break
    code = RE_CODE.search(text)
    return link, (code.group(1) if code else None)


def receive(delivery: Delivery) -> tuple[str, str]:
    """File one delivered message. Returns (outcome, detail).

    Every outcome is a success as far as the email service is concerned - a
    message we chose not to file must not be retried for three days.
    """
    msg = email.message_from_bytes(delivery.raw, policy=policy.default)
    token = token_from(delivery.recipient, msg)
    user = accounts.by_inbound_token(token) if token else None
    if user is None:
        # Says nothing about whether the address ever existed.
        return "unknown-recipient", ""

    subject = str(msg.get("Subject") or "")
    sender = parseaddr(str(msg.get("From") or ""))[1].lower()

    if sender == GOOGLE_FORWARDING:
        link, code = _confirmation(msg)
        _log(user, "confirm", subject, "Gmail is asking to confirm this forwarding address.",
             link, code)
        return "confirm", ""

    # Straight from the utility, or forwarded by hand by the person - in which
    # case the utility's From and Subject are in the body, and the signature
    # on the message is the forwarder's, not the utility's.
    forwarded_by = None
    if sender != billmail.DEFAULT_SENDER:
        original, original_subject = forwarded_original(msg)
        if original != billmail.DEFAULT_SENDER:
            _log(user, "rejected", subject,
                 f"Not from the utility's address, so not filed. It came from {sender or 'nobody'}.")
            return "rejected", "sender"
        forwarded_by = sender
        if original_subject:
            subject = original_subject

    verdict = "forwarded" if forwarded_by else dkim_result(delivery.raw)
    mode = os.environ.get("ELEC_INBOUND_DKIM", "warn").lower()
    if verdict != "pass" and mode == "require" and not forwarded_by:
        _log(user, "rejected", subject,
             "The utility's signature on this message didn't verify, so it wasn't filed.")
        return "rejected", "dkim"

    try:
        notice = billmail.parse_message(msg, subject=subject)
        with workspace.open_(user, write=True) as paths:
            filed = homes.file_notices(paths, [notice])
    except ValueError as exc:
        _log(user, "rejected", subject, f"Couldn't read a bill out of it: {exc}.")
        return "rejected", "parse"
    if filed.skipped:
        _log(user, "rejected", subject,
             "The subject names neither electric nor gas, so it wasn't filed.")
        return "rejected", "fuel"

    note = (f" Forwarded by {forwarded_by}." if forwarded_by
            else "" if verdict == "pass" or mode == "off" else " Signature not verified.")
    outcome = "held" if filed.held else "imported" if filed.added else "duplicate"
    _log(user, outcome, subject,
         f"{notice.fuel.capitalize()} ${notice.amount:,.2f}, due {notice.due}."
         + (" It matches none of your homes yet - say which one it belongs to on the Add bills page."
            if filed.held else "" if filed.added else " Already on file.") + note)
    return outcome, str(notice)
