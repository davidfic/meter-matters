"""What is left out of a file on its way in.

A utility's export says more than the app has any use for: the customer's
name, the street they live on, the account number in full. Those are the parts
worth stealing and the parts the dashboard never reads, so they are reduced
before anything is written, and nothing downstream knows the difference:

    the account number    kept as its last four digits. ``55500010001`` is filed as
                          ``******0001`` - the form a bill email already uses, and
                          what bills and usage are joined on anyway.
    the service address   kept as the state and ZIP code. ``12 Placeholder Ln,
                          Exampleton MA 01000`` becomes ``MA 01000``: all the
                          weather and the grid's carbon figure ever wanted.
    the customer's name   not kept at all.

A subject line going into the forwarding log loses any number of five digits
or more the same way.

``scrub`` does the same to files already on disk, so what was kept before this
module existed is reduced too. Everything here is idempotent: reduced text
comes out unchanged.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path

from metermatters.store import storage

STARS = "******"
ACCOUNT_FIELD = "Account# or Nickname"

_LONG_NUMBER = re.compile(r"\d{6,}")
_SUBJECT_NUMBER = re.compile(r"\d{5,}")
_ADDRESS_TAIL = re.compile(r"\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\s*$")
_ZIP_TAIL = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
# The first line of each Eversource layout; everything above it is preamble.
_HEADERS = ("TYPE,", "Account,")
_NUMBER_KEYS = ("account number", "account", "account no", "account #", "service",
                "service point", "meter", "meter number", "esi id", "esiid")


def _last_four(match: re.Match) -> str:
    return STARS + match.group()[-4:]


def account(text: str | None) -> str:
    """``55500010001 (demo42)`` -> ``******0001 (demo42)``. Masked text is left alone."""
    return _LONG_NUMBER.sub(_last_four, text or "")


def filename(name: str) -> str:
    """A file's name on disk with any long number cut to its last four digits,
    spelled the way ``ingest._safe_name`` would: ``data_555000100010000000001.csv``
    becomes ``data__0001.csv``."""
    return _LONG_NUMBER.sub(lambda m: "_" + m.group()[-4:], name or "")


def subject(text: str | None) -> str:
    """A log line's subject, with any long number reduced to its last four digits."""
    return _SUBJECT_NUMBER.sub(_last_four, text or "")


def address(text: str | None) -> str | None:
    """``12 Placeholder Ln, Exampleton MA 01000`` -> ``MA 01000``; a ZIP alone if that
    is all there is; None when there is not even that."""
    text = (text or "").strip()
    found = _ADDRESS_TAIL.search(text)
    if found:
        return f"{found.group(1).upper()} {found.group(2)}"
    found = _ZIP_TAIL.search(text)
    return found.group(1) if found else None


def _preamble_line(line: str) -> str | None:
    """One line above the header, reduced. None drops it."""
    parts = next(csv.reader([line]), [])
    key = parts[0].strip().lower() if parts else ""
    value = parts[1].strip() if len(parts) > 1 else ""
    if key == "name":
        return "Name,"
    if key in ("address", "service address", "premise address"):
        reduced = address(value)
        return f"Address,{reduced}" if reduced else None
    if key in _NUMBER_KEYS:
        return f"{parts[0]},{account(value)}"
    if len(parts) >= 2 and _ADDRESS_TAIL.search(line):
        # The usage-history layout opens with the bare address, no key.
        return f"Address,{address(line)}"
    return account(line)


def usage_csv(text: str) -> str:
    """A usage export's text with its preamble reduced and its account column masked."""
    lines = text.splitlines()
    head = next((i for i, line in enumerate(lines) if line.startswith(_HEADERS)), None)
    if head is None:
        return text                     # not a layout we know; the parser will say so
    out = [kept for kept in (_preamble_line(line) for line in lines[:head]) if kept is not None]
    out.append(lines[head])
    if lines[head].startswith("Account,"):
        for line in lines[head + 1:]:
            cell, sep, rest = line.partition(",")
            out.append(account(cell) + sep + rest)
    else:
        out.extend(lines[head + 1:])
    return "\n".join(out) + ("\n" if text.endswith(("\n", "\r\n")) else "")


def usage_bytes(data: bytes) -> bytes:
    """An uploaded export - a CSV, or the portal's zip of them - reduced.

    A zip is rebuilt member by member, with the member names masked too (the
    portal puts the service point's number in them) and the dates kept, so the
    same upload always yields the same bytes and is recognised as a repeat.
    Raises ``zipfile.BadZipFile`` for a zip that cannot be opened.
    """
    if data[:2] != b"PK":
        try:
            return usage_csv(data.decode("utf-8-sig")).encode("utf-8")
        except UnicodeDecodeError:
            return data
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            if info.is_dir() or info.filename.startswith("__MACOSX"):
                continue
            raw = source.read(info.filename)
            if info.filename.lower().endswith(".csv"):
                try:
                    raw = usage_csv(raw.decode("utf-8-sig")).encode("utf-8")
                except UnicodeDecodeError:
                    pass
            member = zipfile.ZipInfo(account(info.filename), date_time=info.date_time)
            member.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(member, raw)
    return buffer.getvalue()


def bill_row(row: dict) -> dict:
    """A statement row with its account cell masked. Other cells are untouched."""
    for key in row:
        if (key or "").strip() == ACCOUNT_FIELD and row[key]:
            row[key] = account(row[key])
    return row


def scrub(paths) -> dict:
    """Reduce what is already on disk under one home's paths. Returns what changed."""
    from metermatters.intake import ingest     # ingest imports this module

    changed = {"bills": 0, "usage": 0}
    csv_path = Path(paths.csv)
    if csv_path.exists():
        with storage.locked(csv_path):
            rows, fields = ingest.read_rows(csv_path)
            touched = 0
            for row in rows:
                before = dict(row)
                if bill_row(row) != before:
                    touched += 1
            if touched:
                ingest.write_rows(csv_path, rows, fields)
            changed["bills"] = touched

    usage_dir = Path(paths.usage)
    if usage_dir.is_dir():
        with storage.locked(usage_dir / "usage"):
            for item in sorted(list(usage_dir.glob("*.csv")) + list(usage_dir.glob("*.zip"))):
                data = item.read_bytes()
                try:
                    reduced = usage_bytes(data)
                except zipfile.BadZipFile:
                    continue
                name = filename(item.name)
                if reduced == data and name == item.name:
                    continue
                target = item.with_name(name)
                if target != item and target.exists():
                    # Two exports differing only in the digits now masked: the
                    # loader de-duplicates periods, so keep both under a suffix.
                    stem, n = target.stem, 2
                    while target.exists():
                        target = target.with_name(f"{stem}-{n}{target.suffix}")
                        n += 1
                storage.atomic_write_bytes(target, reduced)
                if target != item:
                    item.unlink()
                changed["usage"] += 1
    return changed
