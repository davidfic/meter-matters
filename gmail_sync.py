"""Importing bills from a connected Gmail mailbox.

Two ways in, and the user picks:

* **Once** - import what's there now and keep nothing. The access token lives
  for the length of one request and the grant is handed straight back.
* **Stay connected** - keep a refresh token, encrypted, so new bills arrive on
  their own. Revocable here or from the user's Google account.
"""

from __future__ import annotations

from dataclasses import dataclass

import billmail
import db
import google_oauth
import homes
import secrets_box

# The recurring check only needs the recent past; the first import takes it all.
RECENT_DAYS = 45


@dataclass
class Imported:
    found: int = 0
    added: int = 0
    unreadable: int = 0
    new_accounts: list = None
    held: int = 0

    @property
    def summary(self) -> str:
        if not self.found:
            return "No bill emails from Eversource were found in that mailbox."
        already = self.found - self.added - self.held
        text = (f"Found {self.found} bill email{'s' if self.found != 1 else ''}: "
                f"{self.added} new" + (f", {already} already on file" if already > 0 else ""))
        if self.held:
            text += (f", {self.held} waiting for you to say which home "
                     f"{'it belongs' if self.held == 1 else 'they belong'} to")
        return text + "."


def import_with(paths, access_token: str, days: int | None = None) -> Imported:
    """Import into the mailbox owner's files - `paths` is their open workspace.

    The mailbox is the person's, not a home's: each bill goes to the home that
    holds its account.
    """
    notices, unreadable = google_oauth.fetch_notices(access_token, days=days)
    filed = homes.file_notices(paths, notices) \
        if notices else billmail.Filed(new_accounts=[])
    return Imported(found=len(notices), added=filed.added, unreadable=unreadable,
                    new_accounts=filed.new_accounts, held=filed.held)


def link(user, mailbox: str, refresh_token: str) -> None:
    """Remember the grant. Raises secrets_box.Unavailable if we can't encrypt it."""
    sealed = secrets_box.seal(refresh_token)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO gmail_links (user_id, email, refresh_token, connected_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "email = excluded.email, refresh_token = excluded.refresh_token, "
            "connected_at = excluded.connected_at, last_error = NULL",
            (user.id, mailbox, sealed, db.now()))


def status(user) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT email, connected_at, checked_at, last_error "
                           "FROM gmail_links WHERE user_id = ?", (user.id,)).fetchone()
    return dict(row) if row else None


def unlink(user) -> None:
    """Forget the grant and hand it back to Google."""
    with db.connect() as conn:
        row = conn.execute("SELECT refresh_token FROM gmail_links WHERE user_id = ?",
                           (user.id,)).fetchone()
        conn.execute("DELETE FROM gmail_links WHERE user_id = ?", (user.id,))
    if row:
        try:
            google_oauth.revoke(secrets_box.open_(row["refresh_token"]))
        except secrets_box.Unavailable:
            pass     # can't decrypt it, so can't use it either - forgetting is enough


def sync(user, paths, days: int | None = RECENT_DAYS) -> Imported | None:
    """Import for a connected user, into `paths`. None if they aren't connected.

    A withdrawn grant is recorded and the link dropped - it will never work
    again, and retrying it daily would only hide that from the user.
    """
    with db.connect() as conn:
        row = conn.execute("SELECT refresh_token FROM gmail_links WHERE user_id = ?",
                           (user.id,)).fetchone()
    if row is None:
        return None
    try:
        access = google_oauth.refresh(secrets_box.open_(row["refresh_token"]))
        result = import_with(paths, access, days)
    except google_oauth.Revoked:
        with db.connect() as conn:
            conn.execute("DELETE FROM gmail_links WHERE user_id = ?", (user.id,))
        raise
    except (google_oauth.GoogleError, secrets_box.Unavailable, ValueError) as exc:
        with db.connect() as conn:
            conn.execute("UPDATE gmail_links SET checked_at = ?, last_error = ? "
                         "WHERE user_id = ?", (db.now(), str(exc)[:300], user.id))
        raise
    with db.connect() as conn:
        conn.execute("UPDATE gmail_links SET checked_at = ?, last_error = NULL "
                     "WHERE user_id = ?", (db.now(), user.id))
    return result
