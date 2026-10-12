"""The database: accounts, credentials, and - when hosted - every user's files.

Two backends behind one interface:

* **Postgres**, when ELEC_DATABASE_URL is set. This is what a host like Cloud
  Run needs: it has no disk of its own and runs several copies of the app, so
  everything has to live somewhere they all share. Supabase is Postgres, and
  its connection string is all this takes.
* **SQLite**, otherwise - one file in the data directory, for running on a
  machine with a disk, and for the tests.

Queries are written once, with ``?`` placeholders, and must stay inside what
both speak.

On Supabase the tables live in their own schema, ``elec``, not ``public``.
Supabase publishes ``public`` through its REST API to anyone holding the
project's anon key; a schema it isn't told about is reachable only with the
database password. Row-level security is switched on as well, with no policies,
so even a misconfigured API exposes nothing.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import sqlite3
import threading
from datetime import datetime, timezone

from metermatters import config

SCHEMA_NAME = "elec"

# {id} and {blob} differ between the two; nothing else does.
TABLES = {
    "users": """
        id            {id},
        email         TEXT NOT NULL UNIQUE,     -- always stored lowercased
        name          TEXT NOT NULL DEFAULT '',
        -- NULL for an account that only ever signs in with Google.
        password_hash TEXT,
        google_sub    TEXT UNIQUE,
        -- The secret half of this user's forwarding address.
        inbound_token TEXT NOT NULL UNIQUE,
        created_at    TEXT NOT NULL,
        -- Bumped when every signed-in session must end: a Google sign-in that
        -- took the account over, a password change. Sessions carry the value
        -- they were made with and stop working when it moves on.
        session_epoch INTEGER NOT NULL DEFAULT 0
    """,
    # A standing grant to read a mailbox. Absent unless the user asked to stay
    # connected; a one-time import never writes a row here.
    "gmail_links": """
        user_id       BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        email         TEXT NOT NULL,
        refresh_token {blob} NOT NULL,          -- encrypted, see secrets_box.py
        connected_at  TEXT NOT NULL,
        checked_at    TEXT,
        last_error    TEXT
    """,
    # What arrived at a user's forwarding address, and what became of it.
    "inbound_log": """
        id           {id},
        user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        received_at  TEXT NOT NULL,
        outcome      TEXT NOT NULL,           -- imported | duplicate | confirm | rejected
        subject      TEXT NOT NULL DEFAULT '',
        detail       TEXT NOT NULL DEFAULT '',
        confirm_url  TEXT,
        confirm_code TEXT
    """,
    "login_attempts": """
        key TEXT NOT NULL,
        at  TEXT NOT NULL
    """,
    # Small facts about the installation itself, such as when the check last ran.
    "meta": """
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    """,
    # Where a ZIP code is, looked up once. Nothing here belongs to a user: a
    # town's weather is the same for everyone in it.
    "places": """
        zip      TEXT PRIMARY KEY,
        name     TEXT NOT NULL,
        lat      DOUBLE PRECISION NOT NULL,
        lon      DOUBLE PRECISION NOT NULL,
        tz       TEXT NOT NULL,
        found_at TEXT NOT NULL
    """,
    # Daily mean temperature (Fahrenheit) by place, kept once fetched.
    "weather_days": """
        place TEXT NOT NULL,
        day   TEXT NOT NULL,
        temp  DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (place, day)
    """,
    # When a place was last asked about, and for which days - so a gap the
    # weather service can't fill yet isn't asked for again on every page view.
    "weather_checks": """
        place      TEXT PRIMARY KEY,
        checked_at TEXT NOT NULL,
        first_day  TEXT NOT NULL,
        last_day   TEXT NOT NULL
    """,
    # What each ZIP code's supply offers were when last asked. Nothing here
    # belongs to a user: the offers are the same for everyone in a ZIP code.
    "supply_offers": """
        zip        TEXT PRIMARY KEY,
        payload    TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        failed_at  TEXT
    """,
    # A link that shows one home's year to anyone who has it. See yearly.py.
    "shares": """
        token      TEXT PRIMARY KEY,
        user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        home_id    TEXT NOT NULL,
        year       INTEGER NOT NULL,
        created_at TEXT NOT NULL
    """,
    # One number per home and fuel, keyed by a salted hash. See benchmarks.py.
    "benchmarks": """
        home_key   TEXT NOT NULL,
        fuel       TEXT NOT NULL,
        state      TEXT NOT NULL,
        per_day    DOUBLE PRECISION NOT NULL,
        unit       TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (home_key, fuel)
    """,
    # A user's statements, usage exports and settings, as the files they are.
    # Only used on Postgres - with a disk, files are simply files.
    "files": """
        user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        content    {blob} NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, name)
    """,
}
INDEXES = (
    "CREATE INDEX IF NOT EXISTS benchmarks_bucket ON benchmarks(state, fuel)",
    "CREATE INDEX IF NOT EXISTS shares_owner ON shares(user_id, home_id, year)",
    "CREATE INDEX IF NOT EXISTS inbound_log_user ON inbound_log(user_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS login_attempts_key ON login_attempts(key, at)",
)

_ready: set[str] = set()
_ready_lock = threading.Lock()
_pool = None
_pool_pid = None


def url() -> str:
    return os.environ.get("ELEC_DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return bool(url())


class BadUrl(ValueError):
    """ELEC_DATABASE_URL can't be read. The message never includes the URL."""


def conninfo(value: str | None = None) -> str:
    """Turn the connection string into parameters, reading it leniently.

    A password pasted straight from a password manager is full of characters
    that mean something in a URL. Read strictly, `p@ss` ends the password at
    the first @ and sends the rest off to be looked up as a hostname - which
    fails slowly, and prints half the password in the error. So: the host
    starts after the *last* @, and the password is everything between the
    first colon and that.
    """
    from urllib.parse import parse_qsl, unquote

    from psycopg.conninfo import make_conninfo

    value = (url() if value is None else value).strip()
    scheme, sep, rest = value.partition("://")
    if not sep or scheme not in ("postgresql", "postgres"):
        raise BadUrl("it should start with postgresql://")
    credentials, at, location = rest.rpartition("@")
    if not at:
        raise BadUrl("it has no user and password before the address")
    user, colon, password = credentials.partition(":")
    if not colon or not password:
        raise BadUrl("it has no password")
    if password.startswith("[") and password.endswith("]"):
        raise BadUrl("the password is still the placeholder - replace [YOUR-PASSWORD], "
                     "brackets included, with the database password")
    location, _, query = location.partition("?")
    address, _, name = location.partition("/")
    host, _, port = address.rpartition(":") if ":" in address else (address, "", "")
    if not host or (port and not port.isdigit()):
        raise BadUrl("the address after the @ should look like host:port/database")
    params = {"host": host, "user": unquote(user), "password": unquote(password),
              "dbname": unquote(name) or "postgres", "connect_timeout": "10",
              # Supabase requires TLS; say so rather than rely on a default.
              "sslmode": "require" if host.endswith(".supabase.com")
                         or host.endswith(".supabase.co") else "prefer"}
    if port:
        params["port"] = port
    params.update(dict(parse_qsl(query)))
    return make_conninfo(**params)


def path():
    """The SQLite file. Meaningless on Postgres."""
    return config.DATA_DIR / "app.db"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def integrity_errors() -> tuple:
    errors = [sqlite3.IntegrityError]
    if is_postgres():
        import psycopg
        errors.append(psycopg.errors.IntegrityError)
    return tuple(errors)


class Conn:
    """The little of a connection the app uses, the same on both backends."""

    def __init__(self, raw, postgres: bool):
        self.raw, self.postgres = raw, postgres

    def _sql(self, sql: str) -> str:
        # psycopg wants %s, and a literal % doubled. No query here contains a
        # '?' that isn't a placeholder.
        return sql.replace("%", "%%").replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params=()):
        return self.raw.execute(self._sql(sql), tuple(params))

    def executemany(self, sql: str, rows) -> None:
        if self.postgres:
            with self.raw.cursor() as cursor:
                cursor.executemany(self._sql(sql), [tuple(r) for r in rows])
        else:
            self.raw.executemany(sql, [tuple(r) for r in rows])

    def insert(self, sql: str, params=()) -> int:
        """Run an INSERT and return the new row's id."""
        return self.execute(sql + " RETURNING id", params).fetchone()["id"]

    def lock(self, key: int) -> None:
        """Hold an exclusive lock on `key` until this transaction ends.

        Postgres only - it is what stands in for a file lock when the app runs
        on several machines at once. Transaction-scoped on purpose: Supabase's
        pooler hands the connection to someone else between transactions, so a
        session-scoped lock would be taken by one client and released by none.
        """
        if self.postgres:
            self.raw.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def _create(conn: Conn) -> None:
    kinds = {"id": "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "blob": "BYTEA"} \
        if conn.postgres else {"id": "INTEGER PRIMARY KEY", "blob": "BLOB"}
    if conn.postgres:
        # Two instances starting together must not both try to create a table.
        conn.lock(0x656C6563)
        conn.raw.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
        conn.raw.execute(f"SET LOCAL search_path TO {SCHEMA_NAME}")
    for name, columns in TABLES.items():
        conn.raw.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns.format(**kinds)})")
        if conn.postgres:
            conn.raw.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    for index in INDEXES:
        conn.raw.execute(index)
    _migrate(conn)


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS leaves
# an existing table as it was, so each is added here if it is missing.
ADDED_COLUMNS = (
    ("users", "session_epoch", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate(conn: Conn) -> None:
    for table, column, definition in ADDED_COLUMNS:
        if conn.postgres:
            conn.raw.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        else:
            have = {r[1] for r in conn.raw.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in have:
                conn.raw.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _postgres_pool():
    """One small pool per process, made after any fork - never inherited."""
    global _pool, _pool_pid
    if _pool is None or _pool_pid != os.getpid():
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            # Enough for every thread to hold one and open a second: a write
            # keeps one for its whole request, and some pages open another.
            conninfo(), min_size=0, max_size=int(os.environ.get("ELEC_DB_POOL", "12")),
            max_idle=120, timeout=20, open=True,
            kwargs={"row_factory": dict_row,
                    # Supabase's pooler shares server connections between
                    # clients, so a statement prepared on one isn't there on
                    # the next. Never prepare.
                    "prepare_threshold": None})
        _pool_pid = os.getpid()
        atexit.register(_pool.close)
    return _pool


@contextlib.contextmanager
def connect():
    """One transaction: committed on success, rolled back on any error."""
    if is_postgres():
        with _postgres_pool().connection() as raw:       # commits or rolls back
            conn = Conn(raw, postgres=True)
            with _ready_lock:
                if url() not in _ready:
                    _create(conn)
                    _ready.add(url())
            # LOCAL, so it lasts exactly this transaction - the only scope
            # that survives a pooler.
            raw.execute(f"SET LOCAL search_path TO {SCHEMA_NAME}")
            yield conn
        return

    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(target, timeout=15)
    raw.row_factory = sqlite3.Row
    try:
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA busy_timeout = 15000")
        with _ready_lock:
            if str(target) not in _ready:
                try:
                    # Sticks once set, so losing the race to set it is harmless.
                    raw.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError:
                    pass
                _create(Conn(raw, postgres=False))
                raw.commit()
                target.chmod(0o600)
                _ready.add(str(target))
        with raw:
            yield Conn(raw, postgres=False)
    finally:
        raw.close()


def ping() -> bool:
    try:
        with connect() as conn:
            conn.execute("SELECT 1 AS ok").fetchone()
        return True
    except Exception:
        return False


def diagnose() -> tuple[bool, str]:
    """Try one connection and say what happened, in words safe to paste anywhere.

        python db.py
    """
    if not is_postgres():
        return False, "ELEC_DATABASE_URL is not set in this terminal."
    import psycopg
    try:
        info = conninfo()
    except BadUrl as exc:
        return False, f"The connection string can't be read: {exc}."
    host = psycopg.conninfo.conninfo_to_dict(info).get("host", "")
    try:
        with psycopg.connect(info) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        text = str(exc).lower()
        if "password authentication failed" in text or "invalid password" in text:
            return False, "Reached the database, but the password was refused."
        if "tenant or user not found" in text:
            return False, ("Reached Supabase, but it doesn't know that user. The user should be "
                           "postgres.<project id>, and the address the pooler for your region.")
        if "resolve" in text or "name or service not known" in text:
            return False, f"Couldn't find the server '{host}'. Check the address."
        if "timeout" in text or "timed out" in text:
            return False, f"No answer from '{host}'. Check the port, and your network."
        return False, "Couldn't connect. (Details withheld - they can contain the password.)"
    return True, f"Connected to {host}."


if __name__ == "__main__":
    import sys
    ok, message = diagnose()
    print(message)
    sys.exit(0 if ok else 1)
