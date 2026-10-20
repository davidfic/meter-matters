"""Site-wide settings an operator can change while the app runs, who may change
them, and a record of what they did.

Each setting began life as an environment variable, and still works as one. A
value set from the admin panel is kept in the database and wins over the
variable, so changing it needs no redeploy; handing it back to the variable is
clearing it. The three are:

    signups   open | closed          ELEC_ALLOW_SIGNUP
    dkim      warn | require | off   ELEC_INBOUND_DKIM
    notice    a line of text shown to everyone signed in, or nothing

Admins are the accounts named in ELEC_ADMIN_EMAILS, comma-separated. That one
is an environment variable only: the list of who may change settings must not
itself be changeable from the panel.
"""

from __future__ import annotations

import os

from metermatters.store import db

PREFIX = "setting:"
NOTICE_MAX = 300
LOG_KEEP = 200

CHOICES = {
    "signups": ("open", "closed"),
    "dkim": ("warn", "require", "off"),
}
VARIABLES = {"signups": "ELEC_ALLOW_SIGNUP", "dkim": "ELEC_INBOUND_DKIM"}


# --------------------------------------------------------------------------
# Who
# --------------------------------------------------------------------------

def admins() -> set[str]:
    raw = os.environ.get("ELEC_ADMIN_EMAILS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_admin(user) -> bool:
    return bool(user) and bool(getattr(user, "email", "")) and user.email.lower() in admins()


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def from_environment(key: str) -> str:
    """What the variable says, or the default when it is unset or unreadable."""
    if key == "signups":
        value = os.environ.get("ELEC_ALLOW_SIGNUP", "1").strip().lower()
        return "closed" if value in ("0", "false", "no", "off") else "open"
    if key == "dkim":
        value = os.environ.get("ELEC_INBOUND_DKIM", "warn").strip().lower()
        return value if value in CHOICES["dkim"] else "warn"
    return ""


def stored() -> dict[str, str]:
    """Every value set from the panel, by key. Empty when none has been."""
    with db.connect() as conn:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE ?",
                            (PREFIX + "%",)).fetchall()
    return {row["key"][len(PREFIX):]: row["value"] for row in rows}


def setting(key: str) -> tuple[str, str]:
    """The value in force and where it came from: "panel" or "environment"."""
    value = stored().get(key)
    if key in CHOICES and value in CHOICES[key]:
        return value, "panel"
    return from_environment(key), "environment"


def set_(key: str, value: str | None) -> None:
    """Set a value from the panel, or hand the setting back to its variable with None."""
    if key in CHOICES and value is not None and value not in CHOICES[key]:
        raise ValueError(f"{value!r} is not a {key} setting")
    with db.connect() as conn:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (PREFIX + key,))
        else:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                         (PREFIX + key, value))


def signups_open() -> bool:
    return setting("signups")[0] == "open"


def dkim_mode() -> str:
    return setting("dkim")[0]


def notice() -> str:
    """The operator's line to everyone signed in. Empty when there is none."""
    return stored().get("notice", "")


def set_notice(text: str) -> str:
    text = " ".join((text or "").split())[:NOTICE_MAX]
    set_("notice", text or None)
    return text


# --------------------------------------------------------------------------
# What was done from the panel
# --------------------------------------------------------------------------

def record(admin_email: str, action: str, target: str = "", detail: str = "") -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO admin_log (at, admin_email, action, target, detail) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (db.now(), admin_email, action[:60], target[:254], detail[:500]))
        conn.execute("DELETE FROM admin_log WHERE id NOT IN "
                     "(SELECT id FROM admin_log ORDER BY id DESC LIMIT ?)", (LOG_KEEP,))


def recent(limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT at, admin_email, action, target, detail FROM admin_log "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
