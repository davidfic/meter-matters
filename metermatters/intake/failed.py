"""Bills that couldn't be read, kept for a month so the reader can be fixed.

When a forwarded email, an uploaded file or a scan can't be read, the app used
to say so and throw it away - which left nothing to fix the reader against.
Now the original is kept here, for one reason only: to see why it failed and
make the next one like it work. Nothing else reads it.

    The user sees it    on Add bills, with when it will be deleted, and can
                        type the bill in from it or delete it at once.
    The operator sees   it on /admin/failed, and every download is logged.
    It is deleted       after KEEP_DAYS, when the user deletes it, when the
                        account is deleted, or when a fix files it.

What the user typed in from one is kept beside it: it is the right answer a
test for the fix should give.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from metermatters.intake import redact
from metermatters.store import db

KEEP_DAYS = 30
# Per user. A new one past this drops the oldest.
KEEP_MAX = 20
KINDS = ("email", "upload", "scan")
# What the page shows; content is fetched only by the admin download.
_LISTED = ("id, user_id, kind, received_at, expires_at, reason, label, sender_domain, "
           "content_type, guess, answer, status")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _row(row) -> dict:
    out = dict(row)
    out["guess"] = _json(out.get("guess")) or {}
    out["answer"] = _json(out.get("answer"))
    if "content" in out:
        out["content"] = bytes(out["content"])
    return out


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _guess(found: dict | None) -> dict:
    """Only the three things the by-hand form takes."""
    found = found or {}
    out = {}
    if found.get("fuel"):
        out["fuel"] = str(found["fuel"]).capitalize()
    if isinstance(found.get("amount"), (int, float)):
        out["amount"] = round(float(found["amount"]), 2)
    if found.get("due"):
        out["due"] = str(found["due"])[:10]
    return out


def guess_from_text(text: str) -> dict:
    """What the scan reader can half-make out, sure or not - a prefill, never a filing."""
    from metermatters.intake import scan
    try:
        return _guess(scan.guess(text or ""))
    except Exception:
        return {}


def guess_from_email(msg, subject: str = "") -> dict:
    from metermatters.intake import billmail
    try:
        text = billmail._text_of(msg)
    except ValueError:
        return {}
    found = guess_from_text(f"{subject}\n{text}")
    fuel = billmail.fuel_of(subject)
    if fuel != "unknown":
        found["fuel"] = fuel.capitalize()
    return found


def hold(user, kind: str, content: bytes, *, reason: str, label: str = "",
         content_type: str = "", sender: str = "", guess: dict | None = None,
         now: datetime | None = None) -> dict | None:
    """Keep one bill that couldn't be read. Returns the row, or None for
    nothing to keep. Never raises: a failure to keep it must not turn the
    failure being reported into a different one."""
    if user is None or not getattr(user, "id", None) or kind not in KINDS or not content:
        return None
    now = now or _now()
    expires = now + timedelta(days=KEEP_DAYS)
    domain = sender.rpartition("@")[2].lower()[:120] if sender else ""
    try:
        with db.connect() as conn:
            new_id = conn.insert(
                "INSERT INTO failed_bills (user_id, kind, received_at, expires_at, reason, label, "
                "sender_domain, content_type, content, guess) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user.id, kind, now.isoformat(), expires.isoformat(), (reason or "")[:300],
                 redact.subject(label)[:200], domain, (content_type or "")[:100], content,
                 json.dumps(_guess(guess))))
            conn.execute(
                "DELETE FROM failed_bills WHERE user_id = ? AND id NOT IN "
                "(SELECT id FROM failed_bills WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
                (user.id, user.id, KEEP_MAX))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("couldn't keep a failed bill")
        return None
    return {"id": new_id, "expires_at": expires.isoformat()}


def _short(stamp: str) -> str:
    day = datetime.fromisoformat(stamp).date()
    return f"{day:%b} {day.day}"


def expires_on(row: dict) -> str:
    """'Dec 3': the day this one is deleted by itself."""
    return _short(row["expires_at"])


def received_on(row: dict) -> str:
    return _short(row["received_at"])


def kept_until(row: dict | None) -> str:
    """The sentence a log line or an error adds: when this one goes."""
    if not row:
        return ""
    return f" Kept until {expires_on(row)} so we can fix the reader; see Add bills."


def open_for(user) -> list[dict]:
    """This user's held bills still waiting on them, newest first, without content."""
    if user is None or not getattr(user, "id", None):
        return []
    purge()
    with db.connect() as conn:
        rows = conn.execute(f"SELECT {_LISTED} FROM failed_bills WHERE user_id = ? "
                            "AND status = 'open' ORDER BY id DESC", (user.id,)).fetchall()
    return [_row(r) for r in rows]


def count_open(user) -> int:
    if user is None or not getattr(user, "id", None):
        return 0
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM failed_bills WHERE user_id = ? "
                           "AND status = 'open' AND expires_at > ?",
                           (user.id, _now().isoformat())).fetchone()
    return int(row["n"])


def count_for(user_id: int) -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM failed_bills WHERE user_id = ?",
                           (user_id,)).fetchone()
    return int(row["n"])


def typed_in(user, failed_id, answer: dict) -> bool:
    """The user typed the bill in from this one: it stops asking for attention,
    and what they typed is kept as the right answer."""
    try:
        failed_id = int(failed_id)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        cur = conn.execute("UPDATE failed_bills SET status = 'typed_in', answer = ? "
                           "WHERE id = ? AND user_id = ?",
                           (json.dumps(answer), failed_id, user.id))
        return (cur.rowcount or 0) > 0


def delete(user, failed_id) -> bool:
    """The user's own delete. Only ever their own row."""
    try:
        failed_id = int(failed_id)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM failed_bills WHERE id = ? AND user_id = ?",
                           (failed_id, user.id))
        return (cur.rowcount or 0) > 0


def purge(now: datetime | None = None) -> int:
    """Everything past its month, for everyone."""
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM failed_bills WHERE expires_at <= ?",
                           ((now or _now()).isoformat(),))
        return cur.rowcount or 0


# ------------------------------------------------------------------ operator

def all_rows() -> list[dict]:
    purge()
    with db.connect() as conn:
        rows = conn.execute(f"SELECT {_LISTED}, LENGTH(content) AS size FROM failed_bills "
                            "ORDER BY id DESC").fetchall()
    return [_row(r) for r in rows]


def get(failed_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM failed_bills WHERE id = ?", (failed_id,)).fetchone()
    return _row(row) if row else None


def delete_any(failed_id: int) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM failed_bills WHERE id = ?", (failed_id,))
        return (cur.rowcount or 0) > 0
