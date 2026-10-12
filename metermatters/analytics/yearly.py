"""Your energy year on one page - and a link to show it to someone.

The page is built from the same reports the dashboard uses, for one calendar
year: what each utility cost, what was used, the carbon, the dearest and
cheapest months, how it compares with the year before, and what the game
handed out. Nothing new is computed; it is the year's slice of what exists.

A share link is a token that opens a copy of the page with nothing on it that
names the person: no address, no account numbers, no email, no navigation. It
can be stopped at any time, and it is the only way the page is reachable
without signing in.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from metermatters.store import db
from metermatters import fuels
from metermatters.analytics import insights
from metermatters import region

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def years_with_bills(reports: dict) -> list[int]:
    seen = set()
    for report in reports.values():
        for row in report.get("series", []):
            seen.add(row["year"])
    return sorted(seen, reverse=True)


def _year_rows(report: dict, year: int) -> list[dict]:
    return [r for r in report.get("series", []) if r["year"] == year]


def build(reports: dict, year: int, state: str | None, badges_by_fuel: dict | None = None) -> dict | None:
    """The year, from one report per fuel (``build_report`` output keyed by fuel name)."""
    grid = region.grid(state)
    out_fuels, months = [], {m: {"month": m, "short": MONTHS[m - 1], "total": 0.0} for m in range(1, 13)}
    any_rows = False
    for name, report in reports.items():
        rows = _year_rows(report, year)
        if not rows:
            continue
        any_rows = True
        kind = fuels.by_name(name)
        prior = _year_rows(report, year - 1)
        spend = sum(r["amount"] for r in rows)
        prior_spend = sum(r["amount"] for r in prior) if len(prior) >= 10 else None
        used = [r for r in rows if r.get("kwh") is not None]
        usage = sum(r["kwh"] for r in used) if used else None
        unit = (used[-1].get("unit") if used else None) or kind.unit
        with insights.grid(grid.kg_per_kwh):
            kg = (usage or 0) * insights.kg_per_unit(unit)
        dearest = max(rows, key=lambda r: r["amount"])
        cheapest = min(rows, key=lambda r: r["amount"])
        rates = [r["rate"] for r in used if r.get("rate")]
        for r in rows:
            months[r["month"]][kind.key] = months[r["month"]].get(kind.key, 0.0) + r["amount"]
            months[r["month"]]["total"] += r["amount"]
        earned = [b for b in (badges_by_fuel or {}).get(name, [])
                  if b.get("earned") and b.get("earned_on") and str(year) in str(b["earned_on"])]
        out_fuels.append({
            "fuel": name, "key": kind.key, "noun": kind.noun, "tagline": kind.tagline,
            "statements": len(rows), "spend": round(spend, 2),
            "prior_spend": round(prior_spend, 2) if prior_spend is not None else None,
            "change_pct": round((spend - prior_spend) / prior_spend * 100, 1) if prior_spend else None,
            "usage": round(usage, 1) if usage is not None else None, "unit": unit,
            "usage_months": len(used),
            "kg": round(kg, 1) if kg else 0.0,
            "dearest": {"label": dearest["label"], "amount": dearest["amount"]},
            "cheapest": {"label": cheapest["label"], "amount": cheapest["amount"]},
            "rate_first": rates[0] if rates else None, "rate_last": rates[-1] if rates else None,
            "rate_change_pct": round((rates[-1] - rates[0]) / rates[0] * 100, 1)
            if len(rates) > 1 and rates[0] else None,
            "badges": [{"name": b["name"], "icon": b.get("icon"), "when": b["earned_on"]} for b in earned],
        })
    if not any_rows:
        return None
    total = sum(f["spend"] for f in out_fuels)
    priors = [f["prior_spend"] for f in out_fuels if f["prior_spend"] is not None]
    kg_total = sum(f["kg"] for f in out_fuels)
    month_rows = [months[m] for m in range(1, 13) if months[m]["total"]]
    peak = max(month_rows, key=lambda m: m["total"]) if month_rows else None
    return {
        "year": year,
        "fuels": out_fuels,
        "total": round(total, 2),
        "prior_total": round(sum(priors), 2) if priors and len(priors) == len(out_fuels) else None,
        "kg": round(kg_total, 1),
        "grid": grid.label,
        "months": month_rows,
        "peak_month": peak["short"] if peak else None,
        "badges": sum(len(f["badges"]) for f in out_fuels),
    }


# --------------------------------------------------------------------------
# Share links
# --------------------------------------------------------------------------

def share(user_id: int, home_id: str, year: int) -> str:
    """A token for this home's year. One per (home, year); asking again returns it."""
    with db.connect() as conn:
        row = conn.execute("SELECT token FROM shares WHERE user_id = ? AND home_id = ? AND year = ?",
                           (user_id, home_id, year)).fetchone()
        if row:
            return row["token"]
        token = secrets.token_urlsafe(18)
        conn.execute("INSERT INTO shares (token, user_id, home_id, year, created_at) VALUES (?, ?, ?, ?, ?)",
                     (token, user_id, home_id, year, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return token


def shared(user_id: int, home_id: str, year: int) -> str | None:
    with db.connect() as conn:
        row = conn.execute("SELECT token FROM shares WHERE user_id = ? AND home_id = ? AND year = ?",
                           (user_id, home_id, year)).fetchone()
    return row["token"] if row else None


def stop(user_id: int, home_id: str, year: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM shares WHERE user_id = ? AND home_id = ? AND year = ?",
                     (user_id, home_id, year))


def lookup(token: str) -> dict | None:
    if not token or len(token) > 64:
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# The picture
# --------------------------------------------------------------------------

def _money(v: float) -> str:
    return f"${v:,.0f}"


def card_svg(summary: dict, title: str = "My energy year") -> str:
    """A 1200x630 card of the year, for saving or sharing. Plain SVG, no fonts to load."""
    colors = {"electric": "#2a78d6", "gas": "#eb6834", "water": "#1baf7a"}
    months = summary["months"]
    top = max((m["total"] for m in months), default=1) or 1
    bars = []
    x0, y0, w, h = 80, 330, 1040, 220
    band = w / 12
    for m in months:
        x = x0 + (m["month"] - 1) * band + 6
        y = y0 + h
        for f in summary["fuels"]:
            v = m.get(f["key"], 0)
            if not v:
                continue
            bh = v / top * h
            y -= bh
            bars.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{band - 12:.0f}" height="{bh:.0f}" '
                        f'fill="{colors.get(f["key"], "#888")}" rx="3"/>')
        bars.append(f'<text x="{x + (band - 12) / 2:.0f}" y="{y0 + h + 22}" font-size="16" fill="#898781" '
                    f'text-anchor="middle">{m["short"]}</text>')
    lines = []
    y = 150
    for f in summary["fuels"]:
        used = f"{f['usage']:,.0f} {f['unit']}" if f["usage"] is not None else "usage not on file"
        lines.append(f'<circle cx="92" cy="{y - 6}" r="7" fill="{colors.get(f["key"], "#888")}"/>'
                     f'<text x="110" y="{y}" font-size="26" fill="#0b0b0b">{f["fuel"]}: {_money(f["spend"])}'
                     f' <tspan fill="#52514e" font-size="20">· {used}</tspan></text>')
        y += 40
    change = ""
    if summary["prior_total"]:
        pct = (summary["total"] - summary["prior_total"]) / summary["prior_total"] * 100
        change = f'<text x="1120" y="110" font-size="26" fill="{"#006300" if pct < 0 else "#d03b3b"}" text-anchor="end">{pct:+.0f}% on {summary["year"] - 1}</text>'
    carbon = f'<text x="1120" y="150" font-size="20" fill="#52514e" text-anchor="end">{summary["kg"] / 1000:.1f} t CO₂e</text>' if summary["kg"] else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif">
  <rect width="1200" height="630" fill="#f9f9f7"/>
  <text x="80" y="90" font-size="40" font-weight="700" fill="#0b0b0b">{title} · {summary["year"]}</text>
  <text x="80" y="118" font-size="20" fill="#52514e">{_money(summary["total"])} across {len(summary["fuels"])} utilit{"y" if len(summary["fuels"]) == 1 else "ies"}{" · dearest month " + summary["peak_month"] if summary["peak_month"] else ""}</text>
  {change}{carbon}
  {"".join(lines)}
  {"".join(bars)}
  <text x="80" y="610" font-size="16" fill="#898781">Made with Meter Matters · metermatters.app</text>
</svg>'''
