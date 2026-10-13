"""Turn the bill analytics into a game: levels, streaks and achievements.

Every rule here is deliberately checkable against the statements themselves -
nothing is awarded for effort or for logging in. If a badge is lit, the CSV
says you earned it.
"""

from __future__ import annotations

from metermatters import fuels

# Each level is a banked-savings threshold, in dollars saved against your
# starting baseline. The curve steepens so later levels stay meaningful.
LEVELS = [
    (0, "Meter Reader"),
    (50, "Switch Flipper"),
    (150, "Watt Watcher"),
    (300, "Load Shifter"),
    (500, "Thermostat Tactician"),
    (750, "Grid Whisperer"),
    (1100, "Kilowatt Killer"),
    (1500, "Efficiency Engineer"),
    (2000, "Negawatt Baron"),
    (2750, "Off-Peak Legend"),
]


def level_for(banked: float, fuel: str = "Electric") -> dict:
    """Map banked savings onto a level plus progress toward the next one.

    Banked can be negative - if you're spending more than your baseline year,
    you're in the hole. We report that honestly rather than flooring it at zero
    and showing "$0.00 banked" next to a tile that says otherwise.
    """
    true_banked = float(banked or 0.0)
    banked = max(0.0, true_banked)
    index = 0
    for i, (threshold, _) in enumerate(LEVELS):
        if banked >= threshold:
            index = i
    titles = fuels.by_name(fuel).titles or [t for _, t in LEVELS]
    floor, title = LEVELS[index][0], titles[index]
    if index + 1 < len(LEVELS):
        ceiling, next_title = LEVELS[index + 1][0], titles[index + 1]
        span = ceiling - floor
        progress = (banked - floor) / span if span else 1.0
        remaining = ceiling - banked
    else:
        ceiling, next_title, progress, remaining = None, None, 1.0, 0.0
    return {
        "level": index + 1,
        "title": title,
        "floor": floor,
        "ceiling": ceiling,
        "next_title": next_title,
        "progress": round(min(max(progress, 0.0), 1.0), 4),
        "remaining": round(remaining, 2) if remaining is not None else None,
        "banked": round(true_banked, 2),
        "deficit": round(-true_banked, 2) if true_banked < 0 else 0.0,
        "max_level": len(LEVELS),
    }


def _pct(current, target) -> float:
    """Progress fraction toward a 'reach at least `target`' goal."""
    if not target:
        return 1.0
    return round(min(max(current / target, 0.0), 1.0), 4)


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _nice(amount: float) -> float:
    """Rounded to a figure a person would set as a target: $5 steps under $100, $10 above."""
    if amount <= 0:
        return 0.0
    step = 5 if amount < 100 else 10
    return float(round(amount / step) * step) or float(step)


def _threshold_badge(series, limit, name, icon, blurb):
    """A 'get any bill under $X' badge, with progress from the best bill so far."""
    if not series:
        return None
    best = min(row["amount"] for row in series)
    earned = best <= limit
    hits = [row for row in series if row["amount"] <= limit]
    return {
        "id": f"under-{limit:g}",
        "name": name,
        "icon": icon,
        "blurb": blurb,
        "earned": earned,
        "earned_on": hits[0]["label"] if hits else None,
        # Distance shrinks as the best bill approaches the limit; we show it as
        # a fraction of the gap from the starting best.
        "progress": 1.0 if earned else _pct(limit, best),
        "detail": (
            f"Best so far: ${best:,.2f}"
            if not earned
            else f"{len(hits)} statement{'s' if len(hits) != 1 else ''} under ${limit}"
        ),
    }


def _streak_badge(streak, best, need, name, icon, blurb):
    earned = best >= need
    return {
        "id": f"streak-{need}",
        "name": name,
        "icon": icon,
        "blurb": blurb,
        "earned": earned,
        "earned_on": None,
        "progress": 1.0 if earned else _pct(best, need),
        "detail": f"Best run: {best} month{'s' if best != 1 else ''}"
        + (f" · current {streak}" if streak != best else ""),
    }


def achievements(report: dict) -> list[dict]:
    """Evaluate every badge against the analysis output."""
    # Score on the recent window - see analyse().
    series = report.get("game_series") or report.get("series") or []
    summary = report.get("summary") or {}
    savings = report.get("savings") or []
    yoy = report.get("yoy") or []
    banked = float(report.get("banked") or 0.0)

    streak = summary.get("streak", 0)
    best = summary.get("best_streak", 0)
    drops = [row for row in series if row["improved"]]
    pcts = [row["pct"] for row in series if row["pct"] is not None]
    big_drops = [row for row in series if row["pct"] is not None and row["pct"] <= -10]
    # A credit is not a bill: the lowest statement, and the "under $X" marks,
    # are judged over what was actually charged.
    charged = [row for row in series if row["amount"] > 0]
    yoy_wins = [row for row in yoy if row["delta"] is not None and row["delta"] < 0]

    badges: list[dict] = []

    badges.append(
        {
            "id": "first-cut",
            "name": "First Cut",
            "icon": "scissors",
            "blurb": "Bring in any statement lower than the one before it.",
            "earned": bool(drops),
            "earned_on": drops[0]["label"] if drops else None,
            "progress": 1.0 if drops else 0.0,
            "detail": f"{len(drops)} of {max(len(series) - 1, 0)} statements came in lower",
        }
    )

    badges.append(
        {
            "id": "big-swing",
            "name": "Big Swing",
            "icon": "bolt",
            "blurb": "Cut a statement by 10% or more in a single month.",
            "earned": bool(big_drops),
            "earned_on": big_drops[0]["label"] if big_drops else None,
            "progress": 1.0 if big_drops else 0.0,
            # A statement after a $0.00 one has no percentage, so two
            # statements need not mean one comparison.
            "detail": (
                f"Biggest cut: {min(pcts):.1f}%" if pcts else "Need a second statement"
            ),
        }
    )

    how = {"weather": "below the same weeks last year, weather taken out",
           "usage": "using less than the statement before",
           "dollars": "cheaper than the statement before"}[summary.get("streak_basis", "dollars")]
    badges.append(_streak_badge(streak, best, 3, "Hat Trick", "flame", f"Three in a row {how}."))
    badges.append(_streak_badge(streak, best, 5, "On Fire", "flame", f"Five in a row {how}."))
    badges.append(_streak_badge(streak, best, 8, "Unstoppable", "flame", f"Eight in a row {how}."))

    # A record is set when a statement comes in below everything before it.
    # Once set it stays earned: a badge that unlocks in May and re-locks in
    # June isn't a badge, it's the weather.
    records = [row for i, row in enumerate(charged)
               if i > 0 and row["amount"] < min(r["amount"] for r in charged[:i])]
    lowest = min(charged, key=lambda r: r["amount"]) if charged else None
    badges.append(
        {
            "id": "record-low",
            "name": "New Record",
            "icon": "trophy",
            "blurb": "Post a statement lower than every one before it.",
            "earned": bool(records),
            "earned_on": records[-1]["label"] if records else None,
            "progress": 1.0 if records else 0.0,
            "detail": (
                f"Record: ${lowest['amount']:,.2f} in {lowest['label']}"
                if lowest
                else "No statements yet"
            ),
        }
    )

    badges.append(
        {
            "id": "yoy",
            "name": "Beat Last Year",
            "icon": "calendar",
            "blurb": "Come in under the same calendar month a year earlier.",
            "earned": bool(yoy_wins),
            "earned_on": yoy_wins[0]["label"] if yoy_wins else None,
            "progress": 1.0 if yoy_wins else 0.0,
            "detail": (
                f"{len(yoy_wins)} of {len(yoy)} year-over-year matchup"
                f"{'s' if len(yoy) != 1 else ''} won"
                if yoy
                else "Needs 13+ months of history"
            ),
        }
    )

    # Relative to this home's own typical statement: a fixed dollar line is
    # free for a gas account and out of reach for a big electric one.
    typical = _median([row["amount"] for row in charged]) if charged else 0.0
    for share, name in ((0.9, "Ten Off"), (0.8, "Twenty Off"), (0.7, "Thirty Off")):
        limit = _nice(typical * share)
        if not limit:
            break
        badge = _threshold_badge(charged, limit, name, "target",
                                 f"Land a statement under ${limit:,.0f} - "
                                 f"{round((1 - share) * 100)}% below your typical.")
        if badge:
            badges.append(badge)

    for amount, name in ((100, "Century Saver"), (500, "Five Hundred Club"), (1000, "Grand Total")):
        badges.append(
            {
                "id": f"banked-{amount}",
                "name": name,
                "icon": "piggy",
                "blurb": f"Bank ${amount:,} against your starting baseline.",
                "earned": banked >= amount,
                "earned_on": next(
                    (s["label"] for s in savings if s["cumulative"] >= amount), None
                ),
                "progress": _pct(banked, amount),
                "detail": f"Banked {'−' if banked < 0 else ''}${abs(banked):,.2f}",
            }
        )

    # Usage badges - these are the honest ones. A rate cut can flatter the
    # dollar figure; it cannot move a kWh.
    if summary.get("usage_count"):
        usage_best = max(
            (r for r in series if r.get("kwh") is not None),
            key=lambda r: r["kwh"], default=None)
        usage_low = min(
            (r for r in series if r.get("kwh") is not None),
            key=lambda r: r["kwh"], default=None)
        u_streak = summary.get("usage_streak", 0)

        badges.append({
            "id": "real-reduction",
            "name": "The Real Thing",
            "icon": "bolt",
            "blurb": "Three statements in a row using fewer kWh - not just costing less.",
            "earned": u_streak >= 3,
            "earned_on": None,
            "progress": _pct(u_streak, 3),
            "detail": f"Current kWh streak: {u_streak} month{'s' if u_streak != 1 else ''}",
        })

        # Beating a rate rise means the usage cut was big enough to outrun it.
        beat_rate = [r for r in series
                     if r.get("rate_effect") is not None
                     and r["rate_effect"] > 0 and r["delta"] is not None and r["delta"] < 0]
        badges.append({
            "id": "beat-the-rate",
            "name": "Outran the Rate",
            "icon": "flame",
            "blurb": "Cut the bill in a month when the rate per kWh went up.",
            "earned": bool(beat_rate),
            "earned_on": beat_rate[0]["label"] if beat_rate else None,
            "progress": 1.0 if beat_rate else 0.0,
            "detail": (f"Done it {len(beat_rate)} time{'s' if len(beat_rate) != 1 else ''}"
                       if beat_rate else "Needs a month where the rate rose and the bill fell"),
        })

        if usage_low and usage_best:
            with_kwh = [r for r in series if r.get("kwh") is not None]
            light = [row for i, row in enumerate(with_kwh)
                     if i > 0 and row["kwh"] < min(r["kwh"] for r in with_kwh[:i])]
            badges.append({
                "id": "kwh-record",
                "name": "Lightest Month",
                "icon": "trophy",
                "blurb": "Use less than in every statement before it.",
                "earned": bool(light),
                "earned_on": light[-1]["label"] if light else None,
                "progress": 1.0 if light else 0.0,
                "detail": f"Lowest: {usage_low['kwh']:,.0f} kWh ({usage_low['label']}) · "
                          f"highest {usage_best['kwh']:,.0f}",
            })

    goal = summary.get("goal")
    months_below = summary.get("months_below_goal", 0)
    badges.append(
        {
            "id": "goal-hit",
            "name": "Called Your Shot",
            "icon": "target",
            "blurb": "Come in at or under the monthly goal you set.",
            "earned": bool(summary.get("goal_hit")),
            "earned_on": summary.get("latest", {}).get("label") if summary.get("goal_hit") else None,
            "progress": 1.0 if summary.get("goal_hit") else 0.0,
            "detail": (
                f"{months_below} statement{'s' if months_below != 1 else ''} at or under ${goal:,.2f}"
                if goal
                else "Set a monthly goal to unlock"
            ),
        }
    )

    badges.append(
        {
            "id": "paid-up",
            "name": "Paid Up",
            "icon": "check",
            "blurb": "No overdue statements on the account.",
            "earned": not any(
                row for row in summary.get("unpaid", []) if row["status"] not in {"NOTREADY", "OPEN"}
            ),
            "earned_on": None,
            "progress": 1.0,
            "detail": (
                f"{len(summary.get('unpaid', []))} statement not yet due"
                if summary.get("unpaid")
                else "Every statement settled"
            ),
        }
    )

    return badges


def scoreboard(report: dict, fuel: str = "Electric") -> dict:
    """The gamification block handed to the front end."""
    badges = achievements(report)
    earned = [b for b in badges if b["earned"]]
    return {
        "level": level_for(report.get("banked") or 0.0, fuel),
        "badges": badges,
        "earned_count": len(earned),
        "total_count": len(badges),
        "next_up": sorted(
            (b for b in badges if not b["earned"]),
            key=lambda b: -b["progress"],
        )[:3],
    }
