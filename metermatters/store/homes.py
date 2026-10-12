"""More than one home, each with bills of its own.

Nearly everyone has one home, and for them none of this exists: their files sit
where they always have and nothing here is ever consulted. A second home is
something a person adds later.

A home is a folder inside the user's own files. The first is the user's root
itself, so no one's data moved; the others are ``homes/<id>/`` under it, laid
out exactly the same way. Everything that reads bills takes paths, so a home
is just a different set of paths - the analysis never knows.

    <root>/AccountHistory.csv, usage/, data/      the first home
    <root>/homes/<id>/...                         any other
    <root>/data/homes.json                        names, addresses, what was deleted
    <root>/data/pending_bills.json                bills that arrived for no home in particular

The first home can be renamed but not deleted - it is where the site's plain
addresses (``/``, ``/electric``) lead. Any other can be deleted, which only
hides it: its files stay for thirty days and can be restored, then they go.

On Postgres a user's files are one set of rows, checked out for the length of a
request, so homes need nothing of their own there: a folder is a name prefix.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from metermatters.store import accounts
from metermatters.intake import billmail
from metermatters.store import storage

MAIN = "main"
DEFAULT_NAME = "My home"
MAX_HOMES = 10
MAX_NAME = 60
KEEP_DELETED = timedelta(days=30)
MAX_PENDING = 100


class HomeError(ValueError):
    """Something a person asked for that can't be done, in words they can read."""


@dataclass(frozen=True)
class Home:
    id: str
    slug: str                   # in the address; empty for the first home
    name: str
    deleted_at: str | None = None

    @property
    def main(self) -> bool:
        return self.id == MAIN

    @property
    def prefix(self) -> str:
        return "" if self.main else f"/home/{self.slug}"

    def purge_on(self) -> date | None:
        if not self.deleted_at:
            return None
        return (datetime.fromisoformat(self.deleted_at) + KEEP_DELETED).date()


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

def _root(base: accounts.Paths) -> Path:
    return base.state.parent.parent


def _registry(base: accounts.Paths) -> Path:
    return base.state.parent / "homes.json"


def _read(base: accounts.Paths) -> dict:
    try:
        stored = json.loads(_registry(base).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"main_name": DEFAULT_NAME, "homes": []}
    if not isinstance(stored, dict):
        return {"main_name": DEFAULT_NAME, "homes": []}
    stored.setdefault("main_name", DEFAULT_NAME)
    stored["homes"] = [h for h in stored.get("homes", []) if isinstance(h, dict)]
    return stored


def _write(base: accounts.Paths, data: dict) -> None:
    storage.atomic_write_text(_registry(base), json.dumps(data, indent=2) + "\n")


def _home(row: dict) -> Home:
    return Home(id=row["id"], slug=row["slug"], name=row["name"],
                deleted_at=row.get("deleted_at"))


def first(base: accounts.Paths) -> Home:
    return Home(MAIN, "", _read(base)["main_name"])


def everything(base: accounts.Paths) -> list[Home]:
    """Every home including deleted ones, the first home first."""
    data = _read(base)
    return [Home(MAIN, "", data["main_name"])] + [_home(h) for h in data["homes"]]


def active(base: accounts.Paths) -> list[Home]:
    return [h for h in everything(base) if not h.deleted_at]


def deleted(base: accounts.Paths) -> list[Home]:
    return [h for h in everything(base) if h.deleted_at]


def several(base: accounts.Paths) -> bool:
    """Is there more than one home to choose between? The switch for all the extra UI."""
    return len(active(base)) > 1


def find(base: accounts.Paths, slug: str) -> Home | None:
    return next((h for h in active(base) if not h.main and h.slug == slug), None)


def by_id(base: accounts.Paths, home_id: str) -> Home | None:
    return next((h for h in everything(base) if h.id == home_id), None)


def paths_for(base: accounts.Paths, home: Home) -> accounts.Paths:
    if home.main:
        return base
    return accounts.Paths.under(_root(base) / "homes" / home.id)


# --------------------------------------------------------------------------
# Changing it
# --------------------------------------------------------------------------

def _clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise HomeError("Give the home a name.")
    if len(name) > MAX_NAME:
        raise HomeError(f"Keep the name to {MAX_NAME} characters or fewer.")
    return name


def _slug(name: str, taken: set[str]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-") or "home"
    slug, n = stem, 2
    while slug in taken:
        slug, n = f"{stem}-{n}", n + 1
    return slug


def _check_unique(base: accounts.Paths, name: str, ignoring: str | None = None) -> None:
    for home in active(base):
        if home.id != ignoring and home.name.casefold() == name.casefold():
            raise HomeError(f"You already have a home called {home.name}.")


def add(base: accounts.Paths, name: str, now: datetime | None = None) -> Home:
    purge(base, now)
    name = _clean_name(name)
    if len(active(base)) >= MAX_HOMES:
        raise HomeError(f"That's the most homes one account can hold ({MAX_HOMES}).")
    _check_unique(base, name)
    data = _read(base)
    # A deleted home's address stays reserved until it is gone for good.
    taken = {h["slug"] for h in data["homes"]}
    row = {"id": "h" + secrets.token_hex(4), "slug": _slug(name, taken), "name": name,
           "created": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
           "deleted_at": None}
    data["homes"].append(row)
    _write(base, data)
    return _home(row)


def rename(base: accounts.Paths, home_id: str, name: str) -> Home:
    name = _clean_name(name)
    home = by_id(base, home_id)
    if home is None or home.deleted_at:
        raise HomeError("There's no such home.")
    _check_unique(base, name, ignoring=home_id)
    data = _read(base)
    if home.main:
        data["main_name"] = name
    else:
        for row in data["homes"]:
            if row["id"] == home_id:
                row["name"] = name        # the address stays as it was: links keep working
    _write(base, data)
    return by_id(base, home_id)


def delete(base: accounts.Paths, home_id: str, typed: str,
           now: datetime | None = None) -> Home:
    """Hide a home for thirty days. The name has to be typed: this takes its bills."""
    home = by_id(base, home_id)
    if home is None or home.deleted_at:
        raise HomeError("There's no such home.")
    if home.main:
        raise HomeError("Your first home can be renamed but not deleted.")
    if " ".join(str(typed or "").split()).casefold() != home.name.casefold():
        raise HomeError(f"Type the home's name, {home.name}, exactly to delete it.")
    data = _read(base)
    for row in data["homes"]:
        if row["id"] == home_id:
            row["deleted_at"] = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    _write(base, data)
    return by_id(base, home_id)


def restore(base: accounts.Paths, home_id: str, now: datetime | None = None) -> Home:
    purge(base, now)
    home = by_id(base, home_id)
    if home is None or not home.deleted_at:
        raise HomeError("That home can't be restored - it may have been deleted for good.")
    if len(active(base)) >= MAX_HOMES:
        raise HomeError(f"That's the most homes one account can hold ({MAX_HOMES}).")
    _check_unique(base, home.name)
    data = _read(base)
    for row in data["homes"]:
        if row["id"] == home_id:
            row["deleted_at"] = None
    _write(base, data)
    return by_id(base, home_id)


def purge(base: accounts.Paths, now: datetime | None = None) -> list[str]:
    """Remove, for good, every home deleted more than thirty days ago."""
    if not _registry(base).exists():
        return []
    now = now or datetime.now(timezone.utc)
    data = _read(base)
    gone, kept = [], []
    for row in data["homes"]:
        stamp = row.get("deleted_at")
        try:
            expired = bool(stamp) and now - datetime.fromisoformat(stamp) >= KEEP_DELETED
        except ValueError:
            expired = False
        (gone if expired else kept).append(row)
    if not gone:
        return []
    for row in gone:
        shutil.rmtree(_root(base) / "homes" / row["id"], ignore_errors=True)
    data["homes"] = kept
    _write(base, data)
    return [row["id"] for row in gone]


# --------------------------------------------------------------------------
# Bills that arrive for "the user", not for a home
# --------------------------------------------------------------------------

def _digits(account: str) -> str:
    from metermatters.analytics import analysis
    return re.sub(r"\D", "", analysis.split_account(account)[0])


def _owns(paths: accounts.Paths, suffix: str) -> bool:
    try:
        known = billmail.known_accounts(paths.csv, paths.usage)
    except Exception:
        return False
    return any(_digits(a).endswith(suffix) for a in known)


def route(base: accounts.Paths, notices: list) -> tuple[dict[str, list], list]:
    """Which home each bill belongs to, by the utility's account number.

    With one home there is nothing to decide. With several, a bill goes to the
    home that already holds its account; a bill for an account none of them has
    seen - or one more than one claims - is held for the person to place,
    rather than guessed at.
    """
    homes = active(base)
    if len(homes) == 1:
        return {homes[0].id: list(notices)}, []
    owners = {}
    placed: dict[str, list] = {}
    held = []
    for notice in notices:
        suffix = notice.account_suffix
        matches = []
        if suffix:
            for home in homes:
                key = (home.id, suffix)
                if key not in owners:
                    owners[key] = _owns(paths_for(base, home), suffix)
                if owners[key]:
                    matches.append(home)
        if len(matches) == 1:
            placed.setdefault(matches[0].id, []).append(notice)
        else:
            held.append(notice)
    return placed, held


def file_notices(base: accounts.Paths, notices: list) -> billmail.Filed:
    """File bills into whichever homes they belong to; hold the ones that fit none."""
    placed, held = route(base, notices)
    total = billmail.Filed(added=0, skipped=0, new_accounts=[])
    for home_id, group in placed.items():
        home = by_id(base, home_id)
        paths = paths_for(base, home)
        done = billmail.file_notices(group, paths.csv, paths.usage)
        total.added += done.added
        total.skipped += done.skipped
        total.new_accounts += done.new_accounts or []
    total.held = hold(base, [n for n in held if n.fuel != "unknown"])
    total.skipped += sum(1 for n in held if n.fuel == "unknown")
    return total


def _pending_path(base: accounts.Paths) -> Path:
    return base.state.parent / "pending_bills.json"


def _pending_read(base: accounts.Paths) -> list[dict]:
    try:
        rows = json.loads(_pending_path(base).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _notice_row(notice) -> dict:
    row = asdict(notice)
    for key in ("due", "bill_date", "received"):
        row[key] = row[key].isoformat() if row.get(key) else None
    return row


def _notice_from(row: dict):
    kept = {k: row.get(k) for k in ("amount", "account_suffix", "subject", "fuel", "message_id")}
    dates = {k: date.fromisoformat(row[k]) if row.get(k) else None
             for k in ("due", "bill_date", "received")}
    return billmail.BillNotice(**kept, **dates)


def hold(base: accounts.Paths, notices: list) -> int:
    """Keep bills no home claims, until the person says where they go. Returns how many are new."""
    if not notices:
        return 0
    rows = _pending_read(base)
    seen = {(r.get("account_suffix"), r.get("due"), r.get("amount"), r.get("fuel")) for r in rows}
    added = 0
    for notice in notices:
        row = _notice_row(notice)
        key = (row["account_suffix"], row["due"], row["amount"], row["fuel"])
        if key in seen or len(rows) >= MAX_PENDING:
            continue
        seen.add(key)
        rows.append({"id": secrets.token_hex(4), **row})
        added += 1
    if added:
        storage.atomic_write_text(_pending_path(base), json.dumps(rows, indent=2) + "\n")
    return added


def pending(base: accounts.Paths) -> list[dict]:
    return _pending_read(base)


def place(base: accounts.Paths, pending_id: str, home_id: str) -> billmail.Filed:
    rows = _pending_read(base)
    row = next((r for r in rows if r["id"] == pending_id), None)
    home = by_id(base, home_id)
    if row is None:
        raise HomeError("That bill has already been dealt with.")
    if home is None or home.deleted_at:
        raise HomeError("There's no such home.")
    paths = paths_for(base, home)
    done = billmail.file_notices([_notice_from(row)], paths.csv, paths.usage)
    _drop(base, rows, pending_id)
    return done


def dismiss(base: accounts.Paths, pending_id: str) -> None:
    _drop(base, _pending_read(base), pending_id)


def _drop(base: accounts.Paths, rows: list[dict], pending_id: str) -> None:
    rows = [r for r in rows if r["id"] != pending_id]
    path = _pending_path(base)
    if rows:
        storage.atomic_write_text(path, json.dumps(rows, indent=2) + "\n")
    else:
        path.unlink(missing_ok=True)
