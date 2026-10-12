"""How a home's usage sits among similar homes - without anyone's home being in it.

What is kept, per home and fuel: the state, the fuel, and the average used per
day over the last twelve metered periods. That is one number in a bucket of a
state and a fuel. No address, no account, no user, no month-by-month shape.
The row is keyed by a salted hash of the home, so a row can be replaced but
not traced back.

A comparison is shown only when the bucket holds at least ``MIN_HOMES`` other
homes: fewer and "the middle of homes in your state" would be one neighbour.

Each home can opt out ("share_benchmark" in its settings); opting out removes
its row the next time the daily check runs, or the next time it uploads.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from statistics import median

from metermatters.store import db
from metermatters import fuels
from metermatters.analytics import insights

MIN_HOMES = 5
MIN_PERIODS = 6


def _key(user_id: int, home_id: str) -> str:
    salt = os.environ.get("ELEC_SECRET_KEY", "local")
    return hashlib.sha256(f"{salt}:{user_id}:{home_id}".encode()).hexdigest()[:32]


def per_day_for(periods: list) -> float | None:
    """Average per day over the last twelve periods of one fuel, or None if too few."""
    recent = [p for p in periods if p.kwh is not None][-12:]
    if len(recent) < MIN_PERIODS:
        return None
    days = sum(p.days for p in recent)
    return sum(p.kwh for p in recent) / days if days else None


def publish(user_id: int, home_id: str, state: str | None, periods: list, allowed: bool = True) -> int:
    """Put this home's numbers in, or take them out. Returns rows written."""
    key = _key(user_id, home_id)
    with db.connect() as conn:
        conn.execute("DELETE FROM benchmarks WHERE home_key = ?", (key,))
        if not allowed or not state:
            return 0
        by_fuel: dict[str, list] = {}
        for p in periods:
            by_fuel.setdefault(insights.fuel_of(p), []).append(p)
        written = 0
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for fuel, ps in by_fuel.items():
            per_day = per_day_for(sorted(ps, key=lambda p: p.end))
            if per_day is None:
                continue
            conn.execute(
                "INSERT INTO benchmarks (home_key, fuel, state, per_day, unit, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, fuel, state.upper(), round(per_day, 4), fuels.by_name(fuel).unit, stamp))
            written += 1
    return written


def compare(user_id: int, home_id: str, state: str | None, fuel: str, per_day: float | None) -> dict:
    """Where this home's per-day usage sits among the others in its state."""
    out = {"state": state, "needed": MIN_HOMES, "count": 0}
    if not state or per_day is None:
        out["status"] = "no-state" if not state else "no-usage"
        return out
    key = _key(user_id, home_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT per_day FROM benchmarks WHERE state = ? AND fuel = ? AND home_key != ?",
            (state.upper(), fuel, key)).fetchall()
    others = sorted(float(r["per_day"]) for r in rows)
    out["count"] = len(others)
    if len(others) < MIN_HOMES:
        out["status"] = "too-few"
        return out
    mid = median(others)
    below = sum(1 for v in others if v < per_day)
    out.update({
        "status": "ok",
        "median": round(mid, 3),
        "you": round(per_day, 3),
        "vs_median_pct": round((per_day - mid) / mid * 100) if mid else None,
        "percentile": round(below / len(others) * 100),
    })
    return out
