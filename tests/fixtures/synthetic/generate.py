#!/usr/bin/env python3
"""Writes every file in this folder. All of it is invented.

Nobody's bills are in here. The household is "Pat Sample" at "12 Placeholder
Ln, Exampleton MA 01000" (not a real ZIP code), the account numbers start 555,
and every amount comes from the made-up usage curve and made-up rates in the
tables below. The files have the same *shape* as what the utility hands out -
the same headers, quirks and encodings - because the shape is what the parsers
are tested against.

Why it exists: the fixtures the tests were first written against are a real
email and real exports. They are gitignored, so a clean checkout (which is what
the build pipeline tests) has none of them. These stand in for them there, and
are the only fixtures git is allowed to see - look at `tests/fixtures/` in .gitignore.

    python3 tests/fixtures/synthetic/generate.py          # rewrite the files
    python3 tests/fixtures/synthetic/generate.py --check  # exit 1 if any would change

Stdlib only, and nothing is imported from the app: a fixture produced by the
code under test could not catch that code being wrong. If you change a number
here, the expectations in the tests named "Synthetic" change with it.

What is in each file, and what the tests rely on:

  account_history_13.csv
      The portal's statement history: 13 electric statements (due 2025-10 to
      2026-10), a payment four days before each of the first twelve, and the
      "$0.00 NOTREADY" placeholder the portal shows for the one still open.
      The last three statements each came in lower, and the last is the
      lowest of the thirteen.

  usage/demo-electric-BillingData.zip
      "Billing data" layout, current electric account, 26 periods back to
      2024-07-19. Its last 13 periods are the 13 statements above, to the
      cent. The 11th statement costs less than the 10th while using *more* -
      the rate fell - so the dollar streak (3) and the usage streak (2) differ.

  usage/Green_Button.csv
      "Usage history" layout, the electric account as it was numbered before:
      8 periods with temperatures, one of them an estimated read, ending the
      day before the current account begins.

  usage/demo-gas-BillingData.zip
      "Billing data" layout in therms, 24 periods, plus one row the utility's
      own export is known to contain: a period filed twice, a day apart.

  account_history_full.csv
      A history as it looks after months of use: both fuels, rows from the
      portal and rows filed from email, an account seen only by its masked
      number, and the Source column the app adds.

  bill_ready.eml
      The "your bill is ready" notice for the newest statement. The fields live
      only in the HTML part; the plain part names the amount and no due date.
      Its one DKIM-Signature is a mail vendor's, never the utility's own, so
      checking it needs no DNS lookup and no test reaches the network.
"""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

HERE = Path(__file__).resolve().parent

NAME = "PAT SAMPLE"
STREET = "12 PLACEHOLDER LN, EXAMPLETON MA 01000"
STREET_MIXED = "12 Placeholder Ln, Exampleton MA 01000"

ELECTRIC = "55500010001"                 # the current electric account
ELECTRIC_SERVICE = "555000100010000000001"
ELECTRIC_BEFORE = "55500020002"          # the same service, before renumbering
GAS = "55500030003"
GAS_SERVICE = "555000300030000000003"
GAS_BEFORE_MASKED = "******0004"         # only ever seen in email, so only masked
NICKNAME = "demo42"                      # digits in a nickname are a known trap

SENDER = "Eversource Do Not Reply <noreply@notifications.eversource.com>"
MAILBOX = "pat.sample@mailbox.example"


def money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'), ROUND_HALF_UP):,.2f}"


def cost(units: int, rate: str, standing: str) -> Decimal:
    total = Decimal(standing) + Decimal(units) * Decimal(rate)
    return total.quantize(Decimal("0.01"), ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Electric, current account: (period end, kWh). Periods are contiguous, so
# each starts the day after the one before it ended.
# --------------------------------------------------------------------------

ELECTRIC_FIRST_DAY = date(2024, 7, 19)
ELECTRIC_PERIODS = [
    (date(2024, 8, 19), 815), (date(2024, 9, 18), 735), (date(2024, 10, 17), 605),
    (date(2024, 11, 18), 665), (date(2024, 12, 18), 750), (date(2025, 1, 20), 820),
    (date(2025, 2, 18), 725), (date(2025, 3, 19), 655), (date(2025, 4, 17), 600),
    (date(2025, 5, 19), 670), (date(2025, 6, 18), 740), (date(2025, 7, 18), 795),
    (date(2025, 8, 19), 830),
    # From here on, each period is one of the 13 statements in the history.
    (date(2025, 9, 18), 720), (date(2025, 10, 17), 610), (date(2025, 11, 18), 680),
    (date(2025, 12, 18), 770), (date(2026, 1, 20), 800), (date(2026, 2, 18), 710),
    (date(2026, 3, 19), 640), (date(2026, 4, 17), 590), (date(2026, 5, 19), 660),
    (date(2026, 6, 18), 730), (date(2026, 7, 20), 745), (date(2026, 8, 19), 690),
    (date(2026, 9, 18), 590),
]
ELECTRIC_STANDING = "10.00"


def electric_rate(end: date) -> str:
    """Dollars per kWh, by when the period closed. It falls once, in mid-2026."""
    if end < date(2025, 1, 1):
        return "0.295"
    if end < date(2025, 7, 1):
        return "0.318"
    if end < date(2026, 1, 1):
        return "0.334"
    if end < date(2026, 7, 1):
        return "0.352"
    return "0.341"


# Days from a period closing to its statement falling due, for the 13 portal
# statements, oldest first. The median is 28.
STATEMENT_LAGS = [28, 28, 28, 28, 28, 27, 28, 28, 29, 28, 28, 28, 28]
PAID_DAYS_BEFORE_DUE = 4

# --------------------------------------------------------------------------
# Electric, as it was numbered before: (read date, days, kWh, average F, read)
# --------------------------------------------------------------------------

BEFORE_READS = [
    (date(2023, 12, 18), 32, 700, "36.0", "Actual Read"),
    (date(2024, 1, 18), 31, 760, "29.0", "Actual Read"),
    (date(2024, 2, 16), 29, 640, "31.0", "Estimated Read"),
    (date(2024, 3, 18), 31, 655, "41.0", "Actual Read"),
    (date(2024, 4, 17), 30, 600, "50.0", "Actual Read"),
    (date(2024, 5, 17), 30, 640, "60.0", "Actual Read"),
    (date(2024, 6, 18), 32, 720, "69.0", "Actual Read"),
    (date(2024, 7, 18), 30, 830, "76.0", "Actual Read"),
]
BEFORE_RATE, BEFORE_STANDING = "0.290", "10.00"

# --------------------------------------------------------------------------
# Gas: (period end, therms), contiguous from GAS_FIRST_DAY.
# --------------------------------------------------------------------------

GAS_FIRST_DAY = date(2024, 8, 21)
GAS_PERIODS = [
    (date(2024, 9, 19), 9), (date(2024, 10, 21), 14), (date(2024, 11, 19), 38),
    (date(2024, 12, 19), 92), (date(2025, 1, 21), 148), (date(2025, 2, 19), 161),
    (date(2025, 3, 20), 118), (date(2025, 4, 21), 71), (date(2025, 5, 20), 33),
    (date(2025, 6, 19), 15), (date(2025, 7, 21), 10), (date(2025, 8, 20), 8),
    (date(2025, 9, 19), 9), (date(2025, 10, 21), 16), (date(2025, 11, 19), 41),
    (date(2025, 12, 19), 97), (date(2026, 1, 21), 155), (date(2026, 2, 19), 149),
    (date(2026, 3, 20), 109), (date(2026, 4, 21), 64), (date(2026, 5, 20), 29),
    (date(2026, 6, 19), 14), (date(2026, 7, 21), 9), (date(2026, 8, 20), 8),
]
GAS_STANDING = "12.00"
# The period the export files twice: once ending a day early, identical otherwise.
GAS_FILED_TWICE = date(2025, 5, 20)
GAS_LAG = 27
# Gas statements before this due date arrived under the old, masked number.
GAS_RENUMBERED = date(2025, 6, 1)


def gas_rate(end: date) -> str:
    return "1.72" if end < date(2025, 11, 1) else "1.94"


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------

def spans(first_day: date, ends_and_units):
    """[(start, end, units)] for contiguous periods."""
    out, start = [], first_day
    for end, units in ends_and_units:
        out.append((start, end, units))
        start = end + timedelta(days=1)
    return out


def electric_spans():
    return [(s, e, u, cost(u, electric_rate(e), ELECTRIC_STANDING))
            for s, e, u in spans(ELECTRIC_FIRST_DAY, ELECTRIC_PERIODS)]


def gas_spans():
    return [(s, e, u, cost(u, gas_rate(e), GAS_STANDING))
            for s, e, u in spans(GAS_FIRST_DAY, GAS_PERIODS)]


def statements():
    """The 13 portal statements, oldest first: (due, amount)."""
    recent = electric_spans()[-len(STATEMENT_LAGS):]
    return [(end + timedelta(days=lag), amount)
            for (_, end, _, amount), lag in zip(recent, STATEMENT_LAGS)]


def escaped(account: str, nickname: str) -> str:
    """The portal writes an HTML-escaped <br> into the account column."""
    return f"{account}&lt;br&gt;({nickname})"


def csv_line(cells) -> str:
    return ",".join(f'"{c}"' if "," in c else c for c in cells)


def slashed(day: date) -> str:
    return day.strftime("%Y/%m/%d")


def us(day: date) -> str:
    return f"{day.month}/{day.day}/{day.year}"


# --------------------------------------------------------------------------
# The files
# --------------------------------------------------------------------------

def portal_rows(with_source: bool) -> list[tuple[date, int, str]]:
    """Statement and payment rows for the 13 portal statements.

    Returned with a sort key, because both files list newest first and the
    portal puts a statement above the payment that shares its date.
    """
    account = escaped(ELECTRIC, NICKNAME)
    tail = ["portal"] if with_source else []
    rows = []
    every = statements()
    for index, (due, amount) in enumerate(every):
        rows.append((due, 1, csv_line(
            [account, "Electric", slashed(due), "Current Bill", money(amount), "-", *tail])))
        if index == len(every) - 1:
            # Still open: the portal shows a scheduled payment of nothing.
            rows.append((due, 0, csv_line(
                [account, "Electric", slashed(due), "Payment", "$0.00", "NOTREADY", *tail])))
        else:
            paid = due - timedelta(days=PAID_DAYS_BEFORE_DUE)
            rows.append((paid, 0, csv_line(
                [account, "Electric", slashed(paid), "Payment", money(amount), "APPROVED", *tail])))
    return rows


def newest_first(rows) -> list[str]:
    return [line for _, _, line in sorted(rows, key=lambda r: (r[0], r[1]), reverse=True)]


def account_history_13() -> bytes:
    # The portal's header ends in a comma, so it has one more column than its rows.
    lines = ["Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,"]
    lines += newest_first(portal_rows(with_source=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def account_history_full() -> bytes:
    rows = portal_rows(with_source=True)
    median_lag = sorted(STATEMENT_LAGS)[len(STATEMENT_LAGS) // 2]

    # Older statements for the current account, each filed from its email.
    for _, end, _, amount in electric_spans()[:-len(STATEMENT_LAGS)]:
        due = end + timedelta(days=median_lag)
        rows.append((due, 1, csv_line([escaped(ELECTRIC, NICKNAME), "Electric", slashed(due),
                                       "Current Bill", money(amount), "-", "email"])))
    # And for the account as it was numbered before.
    for read, _, kwh, _, _ in BEFORE_READS:
        due = read + timedelta(days=median_lag)
        rows.append((due, 1, csv_line(
            [escaped(ELECTRIC_BEFORE, NICKNAME), "Electric", slashed(due), "Current Bill",
             money(cost(kwh, BEFORE_RATE, BEFORE_STANDING)), "-", "email"])))
    # Gas only ever arrived by email. Early ones show the masked old number.
    for _, end, _, amount in gas_spans():
        due = end + timedelta(days=GAS_LAG)
        account = f"{GAS_BEFORE_MASKED} (gas)" if due < GAS_RENUMBERED else f"{GAS} (gas)"
        rows.append((due, 1, csv_line(
            [account, "Gas", slashed(due), "Current Bill", money(amount), "-", "email"])))

    lines = ["Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,Source"]
    lines += newest_first(rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def billing_csv(account, service, kind, unit, rows, filed_twice=None) -> str:
    lines = [f"Name,{NAME}", f'Address,"{STREET}"', f"Account Number,{account}",
             f"Service,{service}", "", f"TYPE,START DATE,END DATE,USAGE ({unit}),COST,NOTES"]
    for start, end, units, amount in reversed(rows):            # newest first
        lines.append(f"{kind},{start.isoformat()},{end.isoformat()},{units:.2f},{money(amount)}")
        if end == filed_twice:
            early = end - timedelta(days=1)
            lines.append(
                f"{kind},{start.isoformat()},{early.isoformat()},{units:.2f},{money(amount)}")
    return "\n".join(lines) + "\n"


def zipped(member: str, text: str) -> bytes:
    """One CSV in a zip, with a fixed timestamp so rebuilding changes nothing."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(member, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, text.encode("utf-8"))
    return buffer.getvalue()


def electric_zip() -> bytes:
    return zipped(f"demo_electric_billing_billing_data_{ELECTRIC_SERVICE}_1_1.csv",
                  billing_csv(ELECTRIC, ELECTRIC_SERVICE, "Electric billing", "kWh",
                              electric_spans()))


def gas_zip() -> bytes:
    return zipped(f"demo_natural_gas_billing_billing_data_{GAS_SERVICE}_1_1.csv",
                  billing_csv(GAS, GAS_SERVICE, "Gas billing", "therms", gas_spans(),
                              filed_twice=GAS_FILED_TWICE))


def usage_history() -> bytes:
    first = BEFORE_READS[0][0] - timedelta(days=BEFORE_READS[0][1] - 1)
    lines = [STREET_MIXED,
             f"Data from {us(first)} to {us(BEFORE_READS[-1][0])}",
             "Account Nickname :",
             "Requested By  on 9/21/2026",
             "Meter No: ,",
             "",
             "Account,Read Date,Usage,Number of Days,Usage per day,Charge,Read Type,"
             "Average Temperature"]
    for read, days, kwh, temp, kind in reversed(BEFORE_READS):   # newest first
        per_day = (Decimal(kwh) / Decimal(days)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        lines.append(",".join([ELECTRIC_BEFORE, us(read), f"{kwh:.2f}", str(days), str(per_day),
                               money(cost(kwh, BEFORE_RATE, BEFORE_STANDING)), kind, temp]))
    return ("\n".join(lines) + "\n").encode("utf-8")


PLAIN = """\
Hello,

Your new Eversource electric bill is ready to view. You have a total amount due of {amount}.

Sign in to your account to view your bill, or to pay it.

Thank you,
Eversource

This is an invented message, used only for testing.
"""

HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">
<title>Your bill is ready</title>
<style type="text/css">
  body {{ margin: 0; padding: 0; background-color: #f4f4f4; }}
  .label {{ font-family: Arial, sans-serif; font-size: 14px; color: #333333; }}
</style>
</head>
<body>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="center">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0">
      <tr><td class="label"><h1>Your&nbsp;bill is ready</h1></td></tr>
      <tr><td class="label">Hello {name},</td></tr>
      <tr><td class="label">Your new electric bill is ready to view. You have a
        total amount due of {amount}.</td></tr>
      <tr><td>
        <table role="presentation" cellpadding="4" cellspacing="0" border="0">
          <tr><td class="label"><strong>Account Number:</strong></td>
              <td class="label">******{suffix}</td></tr>
          <tr><td class="label"><strong>Bill date:</strong></td>
              <td class="label">{billed}</td></tr>
          <tr><td class="label"><strong>Due date:</strong></td>
              <td class="label">{due}</td></tr>
          <tr><td class="label"><strong>Total&nbsp;Amount Due:</strong></td>
              <td class="label">{amount}</td></tr>
        </table>
      </td></tr>
      <tr><td class="label"><a href="https://utility.example/account">View your bill</a></td></tr>
      <tr><td class="label">This is an invented message, used only for testing.</td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>
"""


def bill_ready() -> bytes:
    due, amount = statements()[-1]
    fields = {"name": NAME.title(), "amount": money(amount), "suffix": ELECTRIC[-4:],
              "billed": "09/20/2026", "due": due.strftime("%m/%d/%Y")}

    msg = EmailMessage()
    msg["Delivered-To"] = MAILBOX
    msg["Return-Path"] = "<bounces@mail-vendor.example>"
    msg["Received"] = ("from outbound.mail-vendor.example (outbound.mail-vendor.example "
                       "[192.0.2.25]) by mx.mailbox.example with ESMTPS id synthetic0001 "
                       "for <pat.sample@mailbox.example>; Mon, 21 Sep 2026 14:05:11 +0000")
    # A vendor's signature, as real notices carry. Deliberately not the
    # utility's own domain: that one would be looked up in DNS and verified.
    msg["DKIM-Signature"] = ("v=1; a=rsa-sha256; c=relaxed/relaxed; d=mail-vendor.example; "
                             "s=s1; h=from:subject:date:to; bh=c3ludGhldGljLWJvZHktaGFzaA==; "
                             "b=c3ludGhldGljLXNpZ25hdHVyZS1ub3QtcmVhbA==")
    msg["Date"] = "Mon, 21 Sep 2026 14:05:09 +0000"
    msg["From"] = SENDER
    msg["To"] = MAILBOX
    msg["Subject"] = "New Electric Bill from Eversource"
    msg["Message-ID"] = "<synthetic-bill-ready-0001@mail-vendor.example>"
    msg.set_content(PLAIN.format(**fields), charset="us-ascii", cte="quoted-printable")
    msg.add_alternative(HTML.format(**fields), subtype="html", charset="iso-8859-1",
                        cte="quoted-printable")
    msg.set_boundary("=_synthetic_0001")
    return msg.as_bytes(policy=SMTP)


FILES = {
    "account_history_13.csv": account_history_13,
    "account_history_full.csv": account_history_full,
    "bill_ready.eml": bill_ready,
    "usage/Green_Button.csv": usage_history,
    "usage/demo-electric-BillingData.zip": electric_zip,
    "usage/demo-gas-BillingData.zip": gas_zip,
}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, build in FILES.items():
        path, data = HERE / name, build()
        if path.exists() and path.read_bytes() == data:
            continue
        stale.append(name)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path.relative_to(HERE)}")
    if check and stale:
        print("out of date: " + ", ".join(stale), file=sys.stderr)
        return 1
    if not stale:
        print("everything is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
