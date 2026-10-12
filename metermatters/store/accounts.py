#!/usr/bin/env python
"""User accounts: signing up, signing in, and where each user's files live.

    ./accounts.py list
    ./accounts.py adopt you@example.com    # copy a single-user install's data
                                           # into that user's folder
"""

from __future__ import annotations

import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from metermatters import config
from metermatters.store import db

MIN_PASSWORD = 10
# bcrypt-era advice was to cap length; argon2 has no such limit, but hashing a
# megabyte on request is a free way to keep the server busy.
MAX_PASSWORD = 256
MAX_FAILURES = 8
FAILURE_WINDOW = timedelta(minutes=15)

_hasher = PasswordHasher()   # argon2id, the library's current defaults
# Verified against when the email is unknown, so "no such user" takes as long
# as "wrong password" and the difference can't be timed.
_DECOY = _hasher.hash(secrets.token_urlsafe(16))

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(ValueError):
    """Something the person signing up or in should be told."""


@dataclass(frozen=True)
class Paths:
    """Where one person's data lives. The analysis code only ever sees these."""
    csv: Path
    usage: Path
    state: Path
    watchdog: Path

    @classmethod
    def under(cls, root: Path) -> "Paths":
        return cls(csv=root / "AccountHistory.csv", usage=root / "usage",
                   state=root / "data" / "state.json",
                   watchdog=root / "data" / "watchdog.json")


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    inbound_token: str
    has_password: bool
    has_google: bool
    epoch: int = 0

    @property
    def root(self) -> Path:
        return config.DATA_DIR / "users" / str(self.id)

    @property
    def paths(self) -> Paths:
        return Paths.under(self.root)


# The person at the keyboard when the app runs on their own machine with no
# accounts at all. Their files are where they have always been.
LOCAL = User(id=0, email="", name="", inbound_token="", has_password=False, has_google=False)


def local_paths() -> Paths:
    return Paths(csv=config.CSV_PATH, usage=config.USAGE_DIR,
                 state=config.STATE_DIR / "state.json",
                 watchdog=config.STATE_DIR / "watchdog.json")


def _user(row) -> User | None:
    if row is None:
        return None
    return User(id=row["id"], email=row["email"], name=row["name"],
                inbound_token=row["inbound_token"],
                has_password=bool(row["password_hash"]),
                has_google=bool(row["google_sub"]),
                epoch=int(row["session_epoch"] or 0))


def _clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL.match(email):
        raise AccountError("That doesn't look like an email address.")
    return email


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"Use a password of at most {MAX_PASSWORD} characters.")


def _new_token() -> str:
    # Lowercase letters and digits only: it has to survive being an email
    # address, and mail systems are free to fold case.
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(24))


def get(user_id) -> User | None:
    with db.connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def by_email(email: str) -> User | None:
    with db.connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE email = ?",
                                  ((email or "").strip().lower(),)).fetchone())


def by_inbound_token(token: str) -> User | None:
    with db.connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE inbound_token = ?",
                                  ((token or "").strip().lower(),)).fetchone())


def everyone() -> list[User]:
    with db.connect() as conn:
        return [_user(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


def register(email: str, password: str, name: str = "") -> User:
    email = _clean_email(email)
    _check_password(password)
    try:
        with db.connect() as conn:
            new_id = conn.insert(
                "INSERT INTO users (email, name, password_hash, inbound_token, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (email, (name or "").strip()[:100], _hasher.hash(password), _new_token(), db.now()))
    except db.integrity_errors():
        raise AccountError("There is already an account with that email. "
                           "Sign in instead.") from None
    # Read back once the insert has committed - another connection can't see it before.
    return get(new_id)


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------

def _recent_failures(conn, key: str) -> int:
    since = (datetime.now(timezone.utc) - FAILURE_WINDOW).isoformat(timespec="seconds")
    conn.execute("DELETE FROM login_attempts WHERE at < ?", (since,))
    return conn.execute("SELECT COUNT(*) AS n FROM login_attempts WHERE key = ?",
                        (key,)).fetchone()["n"]


def authenticate(email: str, password: str, client: str = "") -> User:
    """The user, or AccountError. Never says which half was wrong."""
    email = (email or "").strip().lower()
    wrong = AccountError("That email and password don't match.")
    # Counted per address and per client: one guards an account against a
    # spread of machines, the other guards every account against one machine.
    keys = [f"email:{email}"] + ([f"client:{client}"] if client else [])

    with db.connect() as conn:
        if any(_recent_failures(conn, key) >= MAX_FAILURES for key in keys):
            raise AccountError("Too many attempts. Wait a quarter of an hour and try again.")
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    stored = row["password_hash"] if row and row["password_hash"] else None
    try:
        _hasher.verify(stored or _DECOY, (password or "")[:MAX_PASSWORD])
        ok = stored is not None
    except (VerificationError, InvalidHashError):
        ok = False

    if not ok:
        # Recorded, then refused - raising inside the transaction would roll
        # the record back, and the limit would never be reached.
        with db.connect() as conn:
            conn.executemany("INSERT INTO login_attempts (key, at) VALUES (?, ?)",
                             [(key, db.now()) for key in keys])
        raise wrong
    with db.connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE key = ?", (f"email:{email}",))
        if _hasher.check_needs_rehash(stored):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (_hasher.hash(password), row["id"]))
    return _user(row)


def from_google(sub: str, email: str, name: str = "") -> User:
    """The account for a Google identity, creating or linking one as needed.

    Only call this with an address Google says is verified.
    """
    email = _clean_email(email)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE google_sub = ?", (sub,)).fetchone()
        if row:
            return _user(row)
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            # Someone signed up with this address and a password, and we never
            # checked they owned it. Google just proved this person does. So
            # the password goes: if it was theirs they no longer need it, and
            # if it was set by someone else lying in wait, it must not work.
            # And every session that password opened ends with it: whoever
            # set it may still be signed in, and must not stay so.
            conn.execute("UPDATE users SET google_sub = ?, password_hash = NULL, "
                         "session_epoch = session_epoch + 1, "
                         "name = CASE WHEN name = '' THEN ? ELSE name END WHERE id = ?",
                         (sub, (name or "")[:100], row["id"]))
            return _user(conn.execute("SELECT * FROM users WHERE id = ?",
                                      (row["id"],)).fetchone())
        new_id = conn.insert(
            "INSERT INTO users (email, name, google_sub, inbound_token, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, (name or "")[:100], sub, _new_token(), db.now()))
        return _user(conn.execute("SELECT * FROM users WHERE id = ?",
                                  (new_id,)).fetchone())


def sign_out_everywhere(user: User) -> None:
    """End every session this account has, including the one asking."""
    with db.connect() as conn:
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?", (user.id,))


def rotate_inbound_token(user: User) -> User:
    """A new forwarding address. The old one stops working at once."""
    with db.connect() as conn:
        conn.execute("UPDATE users SET inbound_token = ? WHERE id = ?",
                     (_new_token(), user.id))
    return get(user.id)


def delete(user: User) -> None:
    """The account and every file it holds. There is no undo."""
    with db.connect() as conn:
        # Takes the user's stored files and links with it, by cascade.
        conn.execute("DELETE FROM users WHERE id = ?", (user.id,))
    shutil.rmtree(user.root, ignore_errors=True)


# --------------------------------------------------------------------------

def adopt(user: User) -> list[str]:
    """Copy a single-user install's data into `user`'s folder. Copies, never moves."""
    from metermatters.store import workspace
    with workspace.open_(user, write=True) as paths:
        return _adopt_into(paths)


def _adopt_into(paths: Paths) -> list[str]:
    copied = []
    legacy = local_paths()
    if paths.csv.exists() or (paths.usage.exists() and any(paths.usage.iterdir())):
        raise AccountError("that user already has data - refusing to write over it")
    if legacy.csv.exists():
        paths.csv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy.csv, paths.csv)
        copied.append(paths.csv.name)
    if legacy.usage.exists():
        paths.usage.mkdir(parents=True, exist_ok=True)
        for item in sorted(legacy.usage.glob("*")):
            if item.suffix.lower() in (".zip", ".csv"):
                shutil.copy2(item, paths.usage / item.name)
                copied.append(f"usage/{item.name}")
    if legacy.state.exists():
        paths.state.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy.state, paths.state)
        copied.append("settings")
    return copied


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["list"]:
        for user in everyone():
            how = "+".join(h for h, on in (("password", user.has_password),
                                           ("google", user.has_google)) if on)
            print(f"{user.id:4}  {user.email}  [{how}]")
        return 0
    if args[:1] == ["adopt"] and len(args) == 2:
        user = by_email(args[1])
        if not user:
            print(f"error: no account for {args[1]} - sign up in the app first", file=sys.stderr)
            return 1
        try:
            copied = adopt(user)
        except AccountError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"copied {len(copied)} item(s) into {user.email}'s account")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
