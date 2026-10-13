"""Getting new statements into AccountHistory.csv.

The merge logic used to live inside the Flask upload route, which meant the
only way to add a bill was to drag a file onto a running web page. Every
automated route - Green Button, an email poller, a manual one-liner - needs to
do the same thing from a script, so it lives here instead.

The contract is the same in all cases: merge, never replace. Rows are
de-duplicated on account + date + type + amount, so re-importing an overlapping
export is always a no-op and a short export can never truncate your history.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from metermatters.analytics import analysis
from metermatters.intake import greenbutton
from metermatters.store import storage

# The column order the utility's own export uses. We only fall back to this
# when starting a file from scratch; an existing file keeps its own header.
DEFAULT_FIELDS = [
    "Account# or Nickname",
    "Account Type",
    "Pay/Due Date",
    "Type",
    "Amount",
    "Status",
    # Not in the utility's own export - we add it so a row can always say where
    # it came from. Provenance matters here: a portal export carries the
    # period's actual charge, an email carries the account balance.
    "Source",
]

SOURCE_FIELD = "Source"


@dataclass
class MergeResult:
    added: int
    total: int
    rows: list[dict]
    fieldnames: list[str]
    skipped: int = 0

    @property
    def changed(self) -> bool:
        return self.added > 0


def _norm(row: dict) -> dict:
    """Drop the stray ``None`` key DictReader produces from a trailing comma."""
    return {(k or "").strip(): v for k, v in row.items() if k}


def row_key(row: dict) -> tuple:
    """Identity of a row for de-duplication purposes."""
    row = _norm(row)
    return (
        analysis.clean_cell(row.get("Account# or Nickname", "")),
        (row.get("Pay/Due Date") or "").strip(),
        analysis.clean_cell(row.get("Type", "")),
        (row.get("Amount") or "").strip(),
    )


def _sort_key(row: dict) -> str:
    parsed = analysis.parse_date(_norm(row).get("Pay/Due Date", ""))
    return parsed.isoformat() if parsed else ""


def read_rows(path: Path) -> tuple[list[dict], list[str]]:
    """Existing rows plus the header they were written with."""
    path = Path(path)
    if not path.exists():
        return [], list(DEFAULT_FIELDS)
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [f for f in (reader.fieldnames or []) if f]
        return list(reader), (fields or list(DEFAULT_FIELDS))


def write_rows(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Rewrite the file newest-first, which is how the utility exports it."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    storage.atomic_write_text(path, buffer.getvalue())


def merge(path: Path, incoming: list[dict], incoming_fields: list[str] | None = None,
          *, dry_run: bool = False) -> MergeResult:
    """Fold `incoming` rows into the CSV at `path`, skipping anything already there."""
    # Held across the read and the write: the timer and the web app both merge.
    with storage.locked(path):
        return _merge(path, incoming, incoming_fields, dry_run=dry_run)


def _merge(path: Path, incoming: list[dict], incoming_fields: list[str] | None,
           *, dry_run: bool) -> MergeResult:
    existing, fields = read_rows(path)
    for name in (incoming_fields or []):
        if name and name not in fields:
            fields.append(name)

    if SOURCE_FIELD not in fields:
        fields.append(SOURCE_FIELD)

    merged = list(existing)
    by_key = {row_key(r): r for r in existing}
    seen = set(by_key)
    added = skipped = 0
    for row in incoming:
        key = row_key(row)
        if key in seen:
            skipped += 1
            # A row we already have can still learn where it came from.
            known = by_key.get(key)
            if known is not None and not (known.get(SOURCE_FIELD) or "").strip():
                known[SOURCE_FIELD] = row.get(SOURCE_FIELD, "")
            continue
        seen.add(key)
        merged.append(row)
        added += 1

    retired = _retire_backfill(merged, [r for r in incoming if row_key(r) in seen]) if added else 0
    merged.sort(key=_sort_key, reverse=True)
    if (added or retired) and not dry_run:
        write_rows(path, merged, fields)
    return MergeResult(added=added, total=len(merged), rows=merged,
                       fieldnames=fields, skipped=skipped)


def _retire_backfill(rows: list[dict], arrived: list[dict]) -> int:
    """Drop statements made up from usage once the real one for that month is here.

    A backfilled statement (see backfill.py) stands in until the utility's own
    arrives, due on a date of its own; keeping both would count the month
    twice. The real bill is the same account, the same fuel, due within twenty
    days of the estimate. The payment row made with it goes too.
    """
    real = [r for r in arrived
            if (r.get("Type") or "").strip() == "Current Bill"
            and (r.get("Status") or "").strip().upper() != "BACKFILL"]
    if not real:
        return 0

    def digits(row):
        return re.sub(r"\D", "", analysis.split_account(_norm(row).get("Account# or Nickname", ""))[0])

    def when(row):
        return analysis.parse_date(_norm(row).get("Pay/Due Date", ""))

    # A bill list with no account column names none; then the fuel and the
    # date are all there is to go on, which for one account is enough.
    keys = [(digits(r), (r.get("Account Type") or "").strip().lower(), when(r)) for r in real]
    before = len(rows)
    rows[:] = [r for r in rows
               if (r.get("Status") or "").strip().upper() != "BACKFILL"
               or not any((not d or digits(r) == d)
                          and (r.get("Account Type") or "").strip().lower() == f
                          and when(r) and due and abs((when(r) - due).days) <= 20
                          for d, f, due in keys)]
    return before - len(rows)


def merge_csv_text(path: Path, text: str, *, dry_run: bool = False) -> MergeResult:
    """Merge a whole CSV export. Raises ValueError if it holds nothing usable."""
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("that CSV has no data rows")
    # Only rows the reader will keep go in: one it would throw away is neither
    # history nor an addition, and must not be written or counted as one.
    rows = [r for r in rows if _readable(r)]
    if not rows:
        raise ValueError("no usable bill or payment rows found in that CSV")
    return merge(path, rows, reader.fieldnames or [], dry_run=dry_run)


def _readable(row: dict) -> bool:
    """What analysis.read_entries asks of a row, so the two can't drift apart."""
    row = _norm(row)
    return (analysis.parse_date(row.get("Pay/Due Date", "")) is not None
            and bool(analysis.clean_cell(row.get("Type", "")))
            and analysis.parse_money(row.get("Amount", "")) is not None)


def bill_row(account: str, due: str, amount: float, *,
             account_type: str = "Electric", kind: str = "Current Bill",
             status: str = "-", source: str = "") -> dict:
    """Build a single statement row - the shape an email or API import produces.

    The account field is written in the export's own escaped form so a row we
    synthesise is indistinguishable from one the utility gave us.
    """
    parsed = analysis.parse_date(due)
    if parsed is None:
        raise ValueError(f"unparseable due date: {due!r}")
    return {
        "Account# or Nickname": account,
        "Account Type": account_type,
        "Pay/Due Date": parsed.strftime("%Y/%m/%d"),
        "Type": kind,
        "Amount": f"${amount:,.2f}",
        "Status": status,
        SOURCE_FIELD: source,
    }


def reconcile_accounts(path: Path, real_accounts, *, dry_run: bool = False) -> dict:
    """Replace masked account numbers with the real ones, once we learn them.

    Bill emails only show the last four digits, so a gas account first seen in
    email is filed as ``******0003``. When a usage export later names it in
    full, the rows should say so - otherwise the bills and the metered periods
    never join, and the dashboard silently shows no usage for that fuel.
    """
    with storage.locked(path):
        return _reconcile_accounts(path, real_accounts, dry_run=dry_run)


def _reconcile_accounts(path: Path, real_accounts, *, dry_run: bool) -> dict:
    rows, fields = read_rows(path)
    by_suffix = {}
    for account in real_accounts:
        digits = "".join(c for c in str(account) if c.isdigit())
        if len(digits) >= 4:
            by_suffix[digits[-4:]] = str(account)

    changed: dict[str, str] = {}
    for row in rows:
        cell = (row.get("Account# or Nickname") or "").strip()
        if "*" not in cell:
            continue
        match = re.search(r"\*+\s*(\d{3,4})", cell)
        if not match:
            continue
        real = by_suffix.get(match.group(1))
        if not real:
            continue
        # Keep any "(nickname)" suffix the cell already carries.
        replacement = re.sub(r"\*+\s*\d{3,4}", real, cell, count=1)
        changed[cell] = replacement
        row["Account# or Nickname"] = replacement

    if changed and not dry_run:
        write_rows(path, rows, fields)
    return changed


def default_account(path: Path, fuel: str | None = None) -> str | None:
    """The account string already in the file, so imports match its formatting.

    With `fuel`, only an account of that fuel: a gas bill must never be filed
    under the electric account because that happened to be the first row.
    """
    rows, _ = read_rows(path)
    for row in rows:
        row = _norm(row)
        value = row.get("Account# or Nickname", "")
        if fuel and (row.get("Account Type") or "").strip().lower() != fuel.lower():
            continue
        if value.strip():
            return value
    return None


# --------------------------------------------------------------------------
# Usage exports
# --------------------------------------------------------------------------

# An export is a few kilobytes. Anything far beyond that is not one, and a zip
# can claim to be small while unpacking to gigabytes.
MAX_UNPACKED_BYTES = 20 * 1024 * 1024


def is_bill_history(text: str) -> bool:
    """Is this the statement history, rather than a usage export?"""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return "Pay/Due Date" in first


def _safe_name(filename: str, is_zip: bool) -> str:
    stem = Path((filename or "").replace("\\", "/")).name
    stem = re.sub(r"\.(zip|csv)$", "", stem, flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "usage-export"
    return f"{stem[:100]}{'.zip' if is_zip else '.csv'}"


def store_usage(usage_dir: Path, filename: str, data: bytes) -> dict:
    """Validate a usage export and file it in `usage_dir`.

    Accepts the .zip straight from the portal or a loose CSV in either layout.
    Nothing is written under a name the loader would pick up until it has
    parsed, so a bad upload can never poison the folder. Raises ValueError with
    a message fit to show the person who uploaded it.
    """
    usage_dir = Path(usage_dir)
    usage_dir.mkdir(parents=True, exist_ok=True)
    is_zip = data[:2] == b"PK"

    fd, tmp = tempfile.mkstemp(dir=usage_dir, prefix=".upload-", suffix=".tmp")
    tmp = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if is_zip:
            try:
                with zipfile.ZipFile(tmp) as archive:
                    if sum(i.file_size for i in archive.infolist()) > MAX_UNPACKED_BYTES:
                        raise ValueError("that zip unpacks to far more than an export should")
            except zipfile.BadZipFile:
                raise ValueError("that zip is damaged") from None
            exports = greenbutton.load_zip(tmp)
            if not exports:
                raise ValueError("no usage export found inside that zip")
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("that file isn't UTF-8 text") from None
            exports = [greenbutton.parse(text, source=filename)]

        periods = [p for e in exports for p in e.periods]
        accounts = sorted({p.account for p in periods if p.account})
        result = {
            "kind": "usage",
            "periods": len(periods),
            "accounts": accounts,
            "first": min(p.start for p in periods).isoformat(),
            "last": max(p.end for p in periods).isoformat(),
        }

        digest = hashlib.sha256(data).digest()
        with storage.locked(usage_dir / "usage"):
            for existing in list(usage_dir.glob("*.zip")) + list(usage_dir.glob("*.csv")):
                if hashlib.sha256(existing.read_bytes()).digest() == digest:
                    return {**result, "stored": False, "name": existing.name}
            name = _safe_name(filename, is_zip)
            target, n = usage_dir / name, 1
            while target.exists():
                # Same name, different content: a later export. Keep both -
                # the loader de-duplicates periods, and the old file may reach
                # further back than the portal's window now does.
                n += 1
                target = usage_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
            before = _by_start(greenbutton.load_dir(usage_dir).periods)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
            notes = _kept_note(periods, before, _by_start(greenbutton.load_dir(usage_dir).periods)) \
                if before else []
        return {**result, "stored": True, "name": target.name, "notes": notes}
    finally:
        tmp.unlink(missing_ok=True)


def _by_start(periods) -> dict:
    return {(p.account, p.unit.lower(), p.start): p for p in periods}


def _kept_note(periods, before: dict, after: dict) -> list[str]:
    """Periods this file figures differently from the file already holding them.

    The loader keeps the first figure it has for a period (see load_all), so a
    re-export with corrected numbers changes nothing on the page. Say so, or
    the person sees the old figures and takes it that the upload didn't land.
    """
    kept = 0
    for p in periods:
        key = (p.account, p.unit.lower(), p.start)
        if key in before and key in after and (after[key].kwh, after[key].cost) != (p.kwh, p.cost):
            kept += 1
    if not kept:
        return []
    return [f"Figures for {kept} period{'s' if kept != 1 else ''} already on file differ in this "
            "file; the ones already on file were kept."]
