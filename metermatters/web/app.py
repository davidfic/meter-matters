"""Flask app: an energy-bill dashboard that turns cutting your bill into a game.

Hosted, it is ``gunicorn wsgi:app`` with accounts: every request belongs to a
signed-in user and touches that user's files only. Run directly (``./run.sh``)
with no ELEC_SECRET_KEY, it is one person on localhost and there is no sign-in.

The dashboard routes live here; signing in and the ways bills arrive are in
``connect.py``.
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from statistics import mean

from flask import Flask, abort, g, jsonify, make_response, redirect, render_template, request, session, url_for

from metermatters.store import accounts
from metermatters.analytics import analysis
from metermatters.intake import backfill
from metermatters import config
from metermatters.store import db
from metermatters.analytics import forecast
from metermatters import fuels
from metermatters.analytics import gamify
from metermatters.intake import greenbutton
from metermatters.store import homes
from metermatters.store import site
from metermatters.intake import importers
from metermatters.intake import ingest
from metermatters.analytics import insights
from metermatters.tasks import jobs
from metermatters.analytics import rates
from metermatters import region
from metermatters.store import storage
from metermatters.analytics import benchmarks
from metermatters.analytics import yearly
from metermatters.analytics import longview, tariffs
from metermatters import testreport
from metermatters.tasks import watchdog
from metermatters.analytics import weather
from metermatters.store import workspace

CSV_PATH = config.CSV_PATH
# Usage exports live here; drop as many as you like and they're de-duplicated.
USAGE_DIR = config.USAGE_DIR
STATE_PATH = config.STATE_DIR / "state.json"

DEFAULT_STATE = {
    "goal": None,          # dollar target for the next statement
    "target_pct": 5.0,     # the cut you're aiming to sustain, for the projector
    # A full year, so the baseline isn't skewed by whichever season it happens
    # to start in. Drop it only if you have less than ~2 years of history.
    "baseline_months": 12,
    "account": None,       # which account to show, when the export has several
    # Same house, renumbered account? Treat non-overlapping accounts as one
    # continuous history. Charts use all of it; scoring uses the window below.
    "merge_accounts": True,
    "fuel": "Electric",       # which utility the main dashboard shows
    "game_window_months": 24,
    # Where the home is, for the weather. Normally read off the usage export;
    # this overrides it, or supplies it when the export carries no address.
    "zip": None,
    # Where the home is, and who it buys from. Both can be read off a usage
    # export or left blank; they decide the carbon figure and which comparisons
    # and bill readers apply. A provider is only ever a label to us.
    "state": None,
    "provider": None,
    # What the household pays per kWh for supply (cents), if it isn't the
    # default - town aggregation, else the utility's Basic Service.
    "supply_cents": None,
    # Whether this home's average usage may be one number among its state's,
    # so it and others can see where they sit. See benchmarks.py.
    "share_benchmark": True,
    # What the user says a change in their usage was, keyed by fuel and the
    # day it began - "Electric:2025-10-21": "space heater in the office".
    "notes": {},
}
# Every fuel, not a list of them: a step-change alert can be raised for any.
NOTE_KEY = re.compile(r"^(" + "|".join(fuels.NAMES) + r"):\d{4}-\d{2}-\d{2}$")
MAX_NOTES, MAX_NOTE_LENGTH = 50, 200

app = Flask(__name__)
# Pick up template edits without a restart - this is a local tool you'll tinker with.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


class HomeAddress:
    """Serve /home/<slug>/anything as /anything, remembering which home was asked for.

    Setting SCRIPT_NAME is how a WSGI app is told it lives under a prefix, so
    url_for and redirects come out with the prefix on without anyone adding it.
    Whether the slug is real is for the gate to say: this only reads the address.
    """
    PATTERN = re.compile(r"^/home/([a-z0-9][a-z0-9-]{0,39})(/.*)?$")

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        found = self.PATTERN.match(environ.get("PATH_INFO", ""))
        if found:
            environ["elec.home"] = found.group(1)
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + f"/home/{found.group(1)}"
            environ["PATH_INFO"] = found.group(2) or "/"
        return self.wsgi(environ, start_response)


app.wsgi_app = HomeAddress(app.wsgi_app)
# Exports are kilobytes. Refuse anything that plainly isn't one before reading it.
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# Who gets in
# --------------------------------------------------------------------------

app.secret_key = os.environ.get("ELEC_SECRET_KEY") or None
app.config.update(
    SESSION_COOKIE_NAME="elec_session",
    SESSION_COOKIE_HTTPONLY=True,
    # Lax: the cookie isn't sent on another site's POST, but is on the
    # redirect back from Google - which Strict would drop, mid-sign-in.
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ELEC_BASE_URL", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Reachable signed out. Each of the machine endpoints checks a credential of
# its own; the rest are how a person gets signed in.
# The redirect back from Google is a GET, and it imports bills.
WRITING_GETS = ("/auth/google/callback",)

OPEN_PATHS = ("/health", "/api/tasks/", "/api/inbound/", "/static/",
              "/login", "/register", "/auth/google/", "/test-suite", "/s/", "/docs")


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def paths() -> accounts.Paths:
    """The files of whoever is asking. Nothing reads or writes any others."""
    return g.paths


def _select_home(base: accounts.Paths) -> None:
    """Point this request at one home's files.

    `base` is the person's whole set of files, which is also their first home.
    An address under /home/ names another; anything else means the first.
    """
    g.base_paths = base
    workspace_ = g.get("workspace")
    if workspace_ is not None and workspace_.write:
        homes.purge(base)         # thirty days up: now is as good a time as any
    slug = request.environ.get("elec.home")
    home = homes.find(base, slug) if slug else homes.first(base)
    if home is None:
        abort(404)
    g.home, g.paths = home, homes.paths_for(base, home)


@contextmanager
def in_home(home: homes.Home):
    """Run a block against another home's files, then put this request back."""
    before = g.paths, g.home
    g.paths, g.home = homes.paths_for(g.base_paths, home), home
    try:
        yield
    finally:
        g.paths, g.home = before


@app.before_request
def gate():
    """Work out who is asking, and turn away anyone who has to sign in first."""
    # A browser says where a request came from. Another site submitting a form
    # here would ride on the visitor's session, so writes must be our own.
    if request.method not in ("GET", "HEAD", "OPTIONS") \
            and not request.path.startswith(("/api/tasks/", "/api/inbound/")) \
            and request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        return jsonify({"error": "Cross-site requests are not accepted."}), 403

    if not config.multi_user():
        # One person, their own machine, no accounts.
        g.user = accounts.LOCAL
        _select_home(accounts.Paths(csv=CSV_PATH, usage=USAGE_DIR, state=STATE_PATH,
                                    watchdog=STATE_PATH.with_name("watchdog.json")))
        return None

    g.user = accounts.get(session["uid"]) if "uid" in session else None
    if g.user is not None and session.get("epoch", 0) != g.user.epoch:
        g.user = None                  # signed out everywhere since this session began
    if g.user is None:
        session.pop("uid", None)       # an account that has since been deleted
        if request.path.startswith(OPEN_PATHS):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Sign in to continue."}), 401
        return redirect(url_for("web.login",
                                next=request.script_root + request.full_path.rstrip("?")))
    # The machine endpoints act for whoever their own credential says, and
    # open those files themselves. A browser session that happens to ride
    # along must not hold its user's lock while they wait for it.
    # The admin pages are about the installation, not the admin's own bills:
    # they open nobody's files, and hold nobody's lock.
    if request.path.startswith(("/static/", "/api/tasks/", "/api/inbound/", "/health", "/admin")):
        return None
    # The user's files, for the length of this request. Anything that might
    # change them takes the user's lock; a plain read doesn't wait for it.
    g.workspace = workspace.Workspace(g.user, write=request.method != "GET"
                                      or request.path in WRITING_GETS).enter()
    _select_home(g.workspace.paths)
    return None


@app.after_request
def keep(response):
    """Save what the request changed - and only then say it worked.

    The write and the transaction's commit both happen here, before the
    response leaves. If either fails, the answer is a failure: nothing was
    kept, and a "saved" that wasn't would be worse than an error.
    """
    space = g.pop("workspace", None)
    if space is None:
        return response
    if response.status_code >= 500:
        space.close(failed=True)
        return response
    try:
        space.commit()
        space.close()
    except Exception as exc:                                   # noqa: BLE001 - whatever the database threw
        app.logger.error("could not save the request's changes: %s: %s", type(exc).__name__, exc)
        space.close(failed=True)
        message = "Your changes couldn't be saved. Nothing was changed - try again."
        if request.path.startswith("/api/"):
            failed = jsonify({"error": message})
        else:
            failed = make_response(render_template("login.html", mode="login", error=message,
                                                   email="", next="", google=False, signups=False))
        failed.status_code = 500
        return failed
    return response


@app.teardown_request
def release(error):
    space = g.pop("workspace", None)
    if space is not None:                   # only if the request never reached keep()
        space.close(failed=True)


@app.context_processor
def who():
    user = getattr(g, "user", None)
    return {"user": user, "accounts_on": config.multi_user(),
            "is_admin": config.multi_user() and site.is_admin(user)}


@app.context_processor
def word_from_the_operator():
    """The admin panel's notice, on every page of anyone signed in."""
    user = getattr(g, "user", None)
    if not config.multi_user() or user is None or user is accounts.LOCAL:
        return {"site_notice": None}
    try:
        return {"site_notice": site.notice() or None}
    except Exception:                       # noqa: BLE001 - a page must render even if this can't be read
        return {"site_notice": None}


@app.context_processor
def switcher():
    """The home switcher's options. Nothing at all unless there are two homes."""
    base = g.get("base_paths")
    if base is None or not homes.several(base):
        return {"homes_nav": None}
    on_all = request.endpoint == "all_homes"
    here = "/" if on_all else request.path
    mine = g.get("home")
    return {"homes_nav": {
        "all": {"url": "/homes", "current": on_all},
        "items": [{"name": h.name, "url": (h.prefix + here) if h.prefix else here,
                   "current": not on_all and mine is not None and mine.id == h.id}
                  for h in homes.active(base)]}}


def fuels_on_file() -> set[str]:
    """Which utilities this home has any bill for. Empty when there are none."""
    p = g.get("paths")
    if p is None or not p.csv.exists():
        return set()
    try:
        return {analysis.fuel_name(e.account_type) for e in analysis.load_entries(p.csv)}
    except (OSError, ValueError):
        return set()


@app.context_processor
def utilities():
    """The utility switcher's options, on every page: each utility the app
    knows, whether this home has bills for it, and which page this is."""
    if g.get("paths") is None:
        return {"fuels_nav": None}
    present = fuels_on_file()
    current = fuels.BY_SLUG.get((request.view_args or {}).get("fuel", "")) \
        if request.endpoint == "fuel_page" else None
    return {"fuels_nav": {
        "overview": {"url": url_for("index"), "current": request.endpoint == "index"},
        "placeholder": current is None and request.endpoint != "index",
        "items": [{"name": f.name, "tagline": f.tagline, "url": url_for("fuel_page", fuel=f.slug),
                   "has_bills": f.name in present, "current": f is current}
                  for f in fuels.ORDER]}}


@app.after_request
def private(response):
    # Every page is somebody's bill. Nothing in between should keep a copy.
    if not request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "private, no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# --------------------------------------------------------------------------
# Persisted settings
# --------------------------------------------------------------------------

def inline(data) -> str:
    """JSON that is safe to write inside a <script> element.

    Account nicknames come out of uploaded files, and a "</script>" in one
    would end the element and start running whatever followed it.
    """
    return json.dumps(data).replace("<", "\\u003c")


def load_state() -> dict:
    state = copy.deepcopy(DEFAULT_STATE)
    try:
        stored = json.loads(paths().state.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            # Checked as if it had just been posted: the file can be edited by
            # hand, and a goal of "200" would otherwise fail every report.
            state = coerce_settings({k: v for k, v in stored.items() if k in DEFAULT_STATE}, state)
            state["notes"] = stored["notes"] if isinstance(stored.get("notes"), dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return state


def save_state(state: dict) -> None:
    storage.atomic_write_text(paths().state, json.dumps(state, indent=2) + "\n")


def coerce_settings(payload: dict, state: dict) -> dict:
    """Validate an incoming settings patch, ignoring anything unrecognised."""
    updated = dict(state)
    if "goal" in payload:
        goal = payload["goal"]
        if goal in (None, "", "null"):
            updated["goal"] = None
        else:
            try:
                value = float(goal)
                # "inf" passes "> 0", and json writes it as Infinity, which no browser reads.
                updated["goal"] = round(value, 2) if math.isfinite(value) and value > 0 else None
            except (TypeError, ValueError):
                pass
    if "target_pct" in payload:
        try:
            updated["target_pct"] = max(0.0, min(50.0, float(payload["target_pct"])))
        except (TypeError, ValueError):
            pass
    if "baseline_months" in payload:
        try:
            updated["baseline_months"] = max(1, min(24, int(payload["baseline_months"])))
        except (TypeError, ValueError):
            pass
    if "account" in payload:
        value = payload["account"]
        updated["account"] = str(value)[:100] if isinstance(value, (str, int)) and value else None
    if "fuel" in payload:
        # Only a utility the app knows, under its own name. Anything else kept
        # here would be written to disk and then crash every report.
        value = payload["fuel"]
        known = {name.lower(): name for name in fuels.NAMES}
        if isinstance(value, str) and value.strip().lower() in known:
            updated["fuel"] = known[value.strip().lower()]
    if "merge_accounts" in payload:
        updated["merge_accounts"] = bool(payload["merge_accounts"])
    if "game_window_months" in payload:
        try:
            updated["game_window_months"] = max(6, min(600, int(payload["game_window_months"])))
        except (TypeError, ValueError):
            pass
    if "zip" in payload:
        value = str(payload["zip"] or "").strip()
        if not value:
            updated["zip"] = None
        elif weather.ZIP.match(value):
            updated["zip"] = value
    if "state" in payload:
        value = str(payload["state"] or "").strip().upper()
        updated["state"] = value if value in region.STATES else None
    if "provider" in payload:
        updated["provider"] = " ".join(str(payload["provider"] or "").split())[:60] or None
    if "share_benchmark" in payload:
        updated["share_benchmark"] = bool(payload["share_benchmark"]) \
            and str(payload["share_benchmark"]).lower() not in ("0", "false", "no", "off")
    if "supply_cents" in payload:
        try:
            value = float(payload["supply_cents"])
            updated["supply_cents"] = round(value, 3) if 0 < value < 100 else None
        except (TypeError, ValueError):
            updated["supply_cents"] = None
    return updated


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------

def _empty_report(fuel: str, present: list[str]) -> dict:
    """What a page gets when the home has no bills for the fuel it asked for.

    The page draws its empty state from this - the fuel it is about, the
    fuels that do have bills (for the tabs), and nothing else to mislead.
    """
    return {
        "error": f"No {fuel.lower()} bills on file yet." if present else "No bills on file yet.",
        "empty": True,
        "fuel": fuel,
        "fuels": present,
        "accounts": [],
        "series": [],
    }


def build_report(state: dict, fuel: str | None = None) -> dict:
    if not paths().csv.exists():
        return _empty_report(fuel or "Electric", [])

    entries = analysis.load_entries(paths().csv)
    bills = analysis.build_bills(entries)

    # Usage is optional - the dashboard degrades to dollars-only without it.
    try:
        usage = greenbutton.load_dir(paths().usage).periods if paths().usage.exists() else []
    except (OSError, ValueError):
        usage = []

    # Electric and gas run side by side, so they are never merged with each
    # other - only accounts of the SAME fuel that never overlap in time, which
    # is what a renumbered account looks like.
    all_bills = list(bills)
    present = sorted({b.fuel for b in all_bills})
    if fuel and fuel not in present:
        # The water page, with no water bills: say so, rather than quietly
        # showing the electric ones under a water heading.
        return _empty_report(fuel, present)
    if not present:
        return _empty_report("Electric", [])
    wanted = fuel or state.get("fuel")
    fuel = wanted if wanted in present else (
        "Electric" if "Electric" in present else present[0])
    bills = [b for b in all_bills if b.fuel == fuel]

    latest_due: dict[str, object] = {}
    labels: dict[str, str] = {}
    for bill in bills:
        if bill.account not in latest_due or bill.due > latest_due[bill.account]:
            latest_due[bill.account] = bill.due
        labels.setdefault(bill.account, bill.nickname or bill.account)
    order = sorted(latest_due, key=lambda a2: latest_due[a2], reverse=True)
    accounts = [{"id": a2, "label": labels[a2],
                 "last_billed": latest_due[a2].isoformat()} for a2 in order]
    seen = set(order)

    selected = state.get("account")
    if selected not in seen:
        selected = order[0] if order else None

    merged = False
    if state.get("merge_accounts") and len(order) > 1:
        spans = {}
        for bill in bills:
            lo, hi = spans.get(bill.account, (bill.due, bill.due))
            spans[bill.account] = (min(lo, bill.due), max(hi, bill.due))
        ranges = sorted(spans.values())
        if all(ranges[i][1] < ranges[i + 1][0] for i in range(len(ranges) - 1)):
            merged = True

    if not merged:
        bills = [b for b in bills if b.account == selected]

    report = analysis.analyse(bills, state, usage)
    state = {**state, "_address": _address()}
    mine = [p for p in usage if insights.fuel_of(p) == fuel]
    _take_the_weather_out(report, fuel, mine, state)
    report["carbon_factor"] = insights.kg_per_unit(fuels.by_name(fuel).unit) \
        if fuel != "Electric" else region.grid(home_state(state)).kg_per_kwh
    report["forecast"] = forecast.next_bill(
        report["series"],
        price=insights.unit_price(mine) if len(mine) >= 4 else None,
        standing_per_day=insights.standing_charge(mine) if mine else 0.0)
    # Every account of this fuel, not just the one on show - the watchdog has
    # to see a renumbered account's successor.
    report["missing"] = watchdog.overdue_alerts([b for b in all_bills if b.fuel == fuel])
    report["scoreboard"] = gamify.scoreboard(report, fuel)
    report["has_usage"] = report.get("usage_matched", 0) > 0
    report["accounts"] = accounts
    report["account"] = selected
    report["merged_accounts"] = merged
    report["fuel"] = fuel
    report["fuels"] = present
    report["energy"] = analysis.combined_energy(all_bills)
    report["suspect_balances"] = analysis.suspect_balances(all_bills)
    # Pair the other fuel's bills with their usage too, or its discrepancies
    # would be judged against periods that were never matched.
    analysis.attach_usage([b for b in all_bills if b not in bills], usage)
    report["discrepancies"] = analysis.charge_discrepancies(all_bills, usage)
    report["settings"] = state
    report["source"] = paths().csv.name
    if not report["series"]:
        report["error"] = "The CSV parsed, but it holds no 'Current Bill' rows."
    return report


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _weather_model(fuel: str, periods: list, state: dict):
    """(weather, model, place name) for this fuel, from temperatures already on file - or None.

    Never waits on the weather service: this runs while pages are drawn.
    """
    if not fuels.by_name(fuel).weather or len(periods) < insights.MIN_TO_MODEL:
        return None
    try:
        export = greenbutton.load_dir(paths().usage) if paths().usage.exists() else None
    except (OSError, ValueError):
        export = None
    zipcode = state.get("zip") or (weather.zip_in(export.address) if export else None)
    place = weather.known_place(zipcode) if weather.enabled() else None
    if place is None:
        return None
    try:
        temps, rain, need = weather.held_all(place, min(p.start for p in periods),
                                             max(p.end for p in periods))
    except (sqlite3.Error, OSError):
        return None
    wx = insights.Weather(temps, rain, need)
    return wx, insights.Model.learn(wx, periods, fuel), place.name


def _take_the_weather_out(report: dict, fuel: str, periods: list, state: dict) -> None:
    """Give each statement its weather-adjusted change on a year earlier, where known.

    With them, the streak and the streak badges rest on what the person did
    rather than on the season.
    """
    found = _weather_model(fuel, periods, state)
    if found is None:
        return
    wx, model, _ = found
    if model is None or not model.matters:
        return
    by_end = {row["end"]: row for row in insights.compare(periods, model, wx) if row["weather_known"]}
    for r in report["series"]:
        row = by_end.get(r.get("service_end"))
        if row is not None:
            r["you_delta"] = round(row["you"], 3)
            r["you_pct"] = round(row["you_pct"], 1) if row["you_pct"] is not None else None
    current, best, basis = analysis.honest_streak(report["series"])
    report["summary"].update({"streak": current, "best_streak": best, "streak_basis": basis})
    game = report.get("game_series") or []
    if game:
        current, best, basis = analysis.honest_streak(game)
        report["summary"].update({"streak": current, "best_streak": best, "streak_basis": basis})


def build_overview(state: dict) -> dict:
    """Both fuels side by side, plus anything worth acting on.

    Deliberately not a third analysis: each fuel's own report is built exactly
    as its page builds it, and only the headline parts are lifted out. One code
    path means the overview can never disagree with the page it links to.
    """
    reports = {}
    for fuel in fuels.NAMES:
        report = build_report(state, fuel=fuel)
        # Asking for a fuel the home has no bills for falls back to the one it
        # has, which would show up here as a second card for the same bills.
        if report.get("series") and report.get("fuel") == fuel:
            reports[fuel] = report
    if not reports:
        return {"fuels": [], "energy": [], "alerts": []}

    any_report = next(iter(reports.values()))
    cards, alerts = [], []
    for fuel, report in reports.items():
        summary = report["summary"]
        latest = summary["latest"]
        rates = report.get("rates") or {}
        cards.append({
            "fuel": fuel,
            "href": url_for("fuel_page", fuel=fuel.lower()),
            "latest": latest,
            "previous": summary.get("previous"),
            "statements": len(report["series"]),
            "first": report["series"][0]["label"],
            "total_12": summary.get("total_12"),
            "avg_12": summary.get("avg_12"),
            "usage_12": summary.get("kwh_12"),
            "unit": (report.get("tariff") or {}).get("unit") or fuels.by_name(fuel).unit,
            "key": fuels.by_name(fuel).key,
            "rate": summary.get("rate_latest"),
            "forecast": report.get("forecast"),
            "streak": summary.get("streak", 0),
            "streak_basis": summary.get("streak_basis", "dollars"),
            "usage_streak": summary.get("usage_streak", 0),
            "level": report["scoreboard"]["level"],
            "badges": {"earned": report["scoreboard"]["earned_count"],
                       "total": report["scoreboard"]["total_count"]},
            "tariff": report.get("tariff"),
            "baseload": report.get("baseload"),
            "verified": sum(1 for r in report["series"] if r.get("verified")),
        })

        for row in report.get("anomalies", [])[:3]:
            alerts.append({
                "fuel": fuel, "kind": "anomaly", "href": url_for("fuel_page", fuel=fuel.lower()),
                "title": f"{row['label']}: {'well above' if row['high'] else 'well below'} "
                         f"the same month nearby",
                "detail": f"{row['actual']} vs {row['expected']} {row['unit']} "
                          f"({row['pct']:+.1f}%) — compared on {row['basis']}",
            })

    # A bill that never arrived is the one problem no chart can show.
    alerts[:0] = [row for r in reports.values() for row in r.get("missing", [])]

    # These two are computed over every account, so read them once.
    for run in any_report.get("suspect_balances", []):
        alerts.append({
            "fuel": run["fuel"], "kind": "balance",
            "href": url_for("fuel_page", fuel=run["fuel"].lower()),
            "title": f"{run['label']}: looks like a rolled-up balance",
            "detail": f"{run['months']} statements climbing ${run['low']:,.2f} → "
                      f"${run['peak']:,.2f}, then ${run['then']:,.2f}. "
                      f"Summing those double-counts the carried part.",
        })
    for row in any_report.get("discrepancies", []):
        alerts.append({
            "fuel": row["fuel"], "kind": "discrepancy", "href": url_for("raw_page"),
            "title": f"{row['label']}: metered charge ≠ amount billed",
            "detail": f"Export says ${row['charge']:,.2f} for {row['service']}, "
                      f"the bill asked ${row['billed']:,.2f} ({row['diff']:+.2f}).",
        })

    return {
        "fuels": cards,
        "energy": any_report.get("energy", []),
        "alerts": alerts,
        "accounts": any_report.get("accounts", []),
        "merged_accounts": any_report.get("merged_accounts", False),
    }


def _address() -> str | None:
    """The street address a usage export carries, if one has been uploaded."""
    try:
        return greenbutton.load_dir(paths().usage).address if paths().usage.exists() else None
    except (OSError, ValueError):
        return None


def home_state(state: dict) -> str | None:
    """The state a home is in: what was chosen, else what its usage export says."""
    return region.resolve(state.get("state"), state.get("_address"))


def build_all_homes() -> dict:
    """Every home side by side, and what they add up to.

    Only totals are added: spend, usage and the carbon that usage makes. The
    comparison with last year and the search for what changed are about one
    house in one place, so each stays on its own home's pages, which this links
    to. Each home's numbers come out of the very same build its own overview
    uses, so the two can't disagree.
    """
    rows, alerts = [], []
    by_fuel: dict[str, dict] = {}
    months: dict[str, dict] = {}
    for home in homes.active(g.base_paths):
        with in_home(home):
            state_ = load_state()
            overview = build_overview(state_)
            address = _address()
        state_["_address"] = address
        link = lambda href, prefix=home.prefix: prefix + href  # noqa: E731
        fuel_cards = []
        for card in overview["fuels"]:
            fuel_cards.append({
                "fuel": card["fuel"], "href": link(card["href"]), "label": card["latest"]["label"],
                "latest": card["latest"]["amount"], "total_12": card["total_12"] or 0.0,
                "usage_12": card["usage_12"], "unit": card["unit"], "rate": card["rate"]})
            tally = by_fuel.setdefault(card["fuel"], {"total_12": 0.0, "usage_12": 0.0,
                                                      "unit": card["unit"]})
            tally["total_12"] += card["total_12"] or 0.0
            tally["usage_12"] += card["usage_12"] or 0.0
        for row in overview["energy"]:
            month = months.setdefault(row["period"], {"period": row["period"],
                                                      "short": row["short"], "values": {}})
            month["values"][home.id] = row["total"]
        for alert in overview["alerts"]:
            alerts.append({**alert, "home": home.name, "href": link(alert["href"])})
        with insights.grid(region.grid(home_state(state_)).kg_per_kwh):
            kg_12 = sum((f["usage_12"] or 0) * insights.kg_per_unit(f["unit"]) for f in fuel_cards)
        rows.append({"id": home.id, "name": home.name, "url": home.prefix or "/", "kg_12": kg_12,
                     "fuels": fuel_cards, "total_12": sum(f["total_12"] for f in fuel_cards),
                     "alerts": len(overview["alerts"]), "has_bills": bool(fuel_cards)})

    kg = sum(row["kg_12"] for row in rows)
    return {
        "homes": rows,
        "totals": {"total_12": sum(r["total_12"] for r in rows), "by_fuel": by_fuel,
                   "carbon_kg_12": kg},
        "monthly": [months[k] for k in sorted(months)[-12:]],
        "alerts": alerts[:8],
    }


@app.get("/homes")
def all_homes():
    """Every home at once. Only exists for someone with more than one."""
    if request.environ.get("elec.home"):
        return redirect("/homes")          # this one isn't any single home's
    if not homes.several(g.base_paths):
        return redirect(url_for("index"))
    return render_template("homes.html", bootstrap=inline(build_all_homes()))


@app.get("/api/homes/overview")
def api_all_homes():
    if not homes.several(g.base_paths):
        return jsonify({"error": "There is only one home."}), 404
    return jsonify(build_all_homes())


@app.get("/")
def index():
    # Nothing to chart yet: start where the bills come in, not at an empty page.
    if g.user is not accounts.LOCAL and not paths().csv.exists():
        return redirect(url_for("web.connect"))
    # Inlined so the page paints populated rather than flashing an empty shell.
    return render_template("overview.html", bootstrap=inline(build_overview(load_state())))


@app.get("/api/overview")
def api_overview():
    return jsonify(build_overview(load_state()))


@app.get("/<any(electric, gas, water):fuel>")
def fuel_page(fuel):
    kind = fuels.BY_SLUG[fuel]
    report = build_report(load_state(), fuel=kind.name)
    years = {r["year"] for r in report.get("series", [])}
    return render_template("index.html", fuel=kind.name, tagline=kind.tagline, long_view=len(years) >= 2,
                           bootstrap=inline(report))


def build_long_view(state: dict, fuel: str) -> dict | None:
    """Year by year, usage against price, for one fuel."""
    report = build_report(state, fuel=fuel)
    if report.get("fuel") != fuel or not report.get("series"):
        return None
    with_address = {**state, "_address": _address()}
    published, source = tariffs.for_area(home_state(with_address), fuel)
    weather = None
    try:
        usage = greenbutton.load_dir(paths().usage).periods if paths().usage.exists() else []
    except (OSError, ValueError):
        usage = []
    mine = [p for p in usage if insights.fuel_of(p) == fuel]
    found = _weather_model(fuel, mine, with_address)
    if found is not None and found[1] is not None and found[1].matters:
        wx, model, _ = found
        covered = [p for p in mine if model.covers(p)]
        if covered:
            # A typical period's weather: the mean of the features over every period known.
            typical = [mean(f) for f in zip(*(model.features(p) for p in covered))]
            usual = sum(b * v for b, v in zip(model.beta, (1.0, *typical)))
            by_end = {p.end.isoformat(): p for p in mine}

            def weather(r: dict):
                p = by_end.get(r.get("service_end"))
                if p is None or not model.covers(p):
                    return None
                return (model.expect(p) - usual) * p.days
    return longview.build(report["series"], published, weather, source)


@app.get("/<any(electric, gas, water):fuel>/years")
def long_view_page(fuel):
    kind = fuels.BY_SLUG[fuel]
    view = build_long_view(load_state(), kind.name)
    if view is None:
        abort(404)
    return render_template("years.html", fuel=kind.name, slug=kind.slug, noun=kind.noun, unit=kind.unit, view=view,
                           home_name=g.home.name if homes.several(g.base_paths) else None)


def build_raw() -> dict:
    """Every record the app knows about, with provenance and derived fields."""
    if not paths().csv.exists():
        return {"error": "No bills on file yet.", "statements": [], "usage": []}
    entries = analysis.load_entries(paths().csv)
    bills = analysis.build_bills(entries)
    try:
        periods = greenbutton.load_dir(paths().usage).periods if paths().usage.exists() else []
    except (OSError, ValueError):
        periods = []
    analysis.attach_usage(bills, periods)
    return {
        "statements": analysis.raw_rows(entries, bills),
        "usage": analysis.usage_rows(periods),
        "discrepancies": analysis.charge_discrepancies(bills, periods),
        "source": paths().csv.name,
        "usage_files": sorted(p.name for ext in ("*.csv", "*.zip")
                              for p in paths().usage.glob(ext)) if paths().usage.exists() else [],
    }


@app.get("/raw")
def raw_page():
    # Inlined so the table paints populated rather than flashing an empty shell.
    return render_template("raw.html", bootstrap=inline(build_raw()))


@app.get("/api/raw")
def api_raw():
    return jsonify(build_raw())


@app.get("/api/report")
def api_report():
    return jsonify(build_report(load_state(), fuel=request.args.get("fuel")))


def build_insights(state: dict) -> dict:
    """Usage against last year with the weather taken out, carbon, and tips.

    Kept apart from the report on purpose: it may have to fetch temperatures,
    and nothing about a slow or absent weather service should hold up the
    bills. The pages draw themselves first and ask for this afterwards.
    """
    try:
        export = greenbutton.load_dir(paths().usage) if paths().usage.exists() else None
    except (OSError, ValueError):
        export = None
    periods = export.periods if export else []
    zipcode = state.get("zip") or (weather.zip_in(export.address) if export else None)

    temps, rain, need, place, status = None, None, None, None, "ok"
    if not periods:
        status = "no-usage"
    elif not weather.enabled():
        status = "off"
    elif not zipcode:
        status = "no-location"
    else:
        try:
            where = weather.locate(zipcode)
            first, last = min(p.start for p in periods), max(p.end for p in periods)
            temps = weather.temperatures(where, first, last)
            _, rain, need = weather.held_all(where, first, last)
            place = where.name
        except (weather.Unavailable, sqlite3.Error, OSError) as exc:
            app.logger.warning("weather unavailable: %s", exc)
        if not temps:
            status = "unavailable"

    where = region.resolve(state.get("state"), export.address if export else None, place)
    here = region.grid(where)
    with insights.grid(here.kg_per_kwh):
        out = insights.build(periods, temps, rain=rain, need=need, notes=state.get("notes"),
                             place=place, weather_status=status)
    out["zip"] = zipcode
    out["state"] = where
    out["grid"] = here.label
    # Where this home sits among others in its state, for each fuel with usage.
    if g.user is not accounts.LOCAL and g.user is not None:
        by_fuel: dict[str, list] = {}
        for p in periods:
            by_fuel.setdefault(insights.fuel_of(p), []).append(p)
        for fuel, block in out.get("fuels", {}).items():
            try:
                block["similar"] = benchmarks.compare(
                    g.user.id, g.home.id, where, fuel,
                    benchmarks.per_day_for(sorted(by_fuel.get(fuel, []), key=lambda p: p.end)))
            except (sqlite3.Error, OSError):
                block["similar"] = None
        out["benchmark_shared"] = bool(state.get("share_benchmark", True))
    out["attribution"] = weather.ATTRIBUTION if temps else None
    return out


def build_rates(state: dict) -> dict:
    """Your price per kWh, taken apart, and what the supply half could be.

    Like the insights this reaches out, so the page draws first and asks
    afterwards: a slow comparison site never holds up your own numbers.
    """
    report = build_report(state, fuel="Electric")
    series = [r for r in report.get("series", []) if r.get("unit", "kWh") == "kWh"]
    settings = {"zip": state.get("zip"), "supply_cents": state.get("supply_cents")}
    out = {"error": report.get("error"), "series": [], "settings": settings}
    if not series:
        return out
    try:
        export = greenbutton.load_dir(paths().usage) if paths().usage.exists() else None
    except (OSError, ValueError):
        export = None
    zipcode = state.get("zip") or (weather.zip_in(export.address) if export else None)
    rate_now = (report.get("rates") or {}).get("current")

    where = region.resolve(state.get("state"), export.address if export else None)
    status, offers, fetched = "ok", [], None
    if not rates.enabled():
        status = "off"
    elif not zipcode:
        status = "no-location"
    elif where is None:
        status = "no-state"
    elif not rates.covers(where):
        status = "not-covered"
    else:
        try:
            found = rates.offers_for(zipcode)
            offers, fetched = found["offers"], found["fetched_at"]
        except rates.NotCovered:
            status = "not-covered"
        except (rates.Unavailable, sqlite3.Error, OSError) as exc:
            app.logger.warning("supply offers unavailable: %s", exc)
            status = "unavailable"

    comparison = rates.compare(series, offers, state.get("supply_cents")) if offers else None
    supply = comparison and comparison["supply"]
    out.update({
        "zip": zipcode, "status": status, "fetched_at": fetched, "source": rates.SOURCE,
        "state": where, "state_name": region.name(where),
        "unit": "kWh",
        "now": rate_now,
        "latest": series[-1]["label"],
        "min": (report.get("rates") or {}).get("min"),
        "max": (report.get("rates") or {}).get("max"),
        "series": (report.get("rates") or {}).get("series", []),
        "changes": (report.get("rates") or {}).get("changes", []),
        "tariff": report.get("tariff"),
        "seasons": rates.seasons(series),
        "standing": rates.standing(series, rate_now),
        "breakdown": rates.breakdown(rate_now, supply and supply["cents"], report.get("tariff")),
        "comparison": comparison,
    })
    return out


def _publish_benchmark(state: dict) -> None:
    """This home's number into its state's bucket - or out of it, if it opted out."""
    if g.user is accounts.LOCAL or g.user is None:
        return
    try:
        export = greenbutton.load_dir(paths().usage) if paths().usage.exists() else None
        where = region.resolve(state.get("state"), export.address if export else None)
        benchmarks.publish(g.user.id, g.home.id, where, export.periods if export else [],
                           allowed=bool(state.get("share_benchmark", True)))
    except (OSError, ValueError, sqlite3.Error) as exc:
        app.logger.warning("benchmark not published: %s", exc)


# --------------------------------------------------------------------------
# Your year
# --------------------------------------------------------------------------

def _year_reports(state: dict) -> dict:
    reports = {}
    for fuel in fuels.NAMES:
        report = build_report(state, fuel=fuel)
        if report.get("series") and report.get("fuel") == fuel:
            reports[fuel] = report
    return reports


def _year_summary(state: dict, reports: dict, year: int) -> dict | None:
    badges = {fuel: report["scoreboard"]["badges"] for fuel, report in reports.items()}
    return yearly.build(reports, year, home_state({**state, "_address": _address()}), badges)


def build_year(state: dict, year: int | None) -> dict:
    reports = _year_reports(state)
    years = yearly.years_with_bills(reports)
    if not years:
        return {"years": [], "summary": None, "year": None}
    year = year if year in years else years[0]
    return {"years": years, "year": year, "summary": _year_summary(state, reports, year)}


def _statement_weather(fuel: str, periods: list, state: dict):
    """A function from a statement's series row to its weather block, or None when there is none to tell."""
    found = _weather_model(fuel, periods, state)
    if found is None:
        return None
    wx, model, place = found
    by_end = {p.end.isoformat(): p for p in periods}
    compared = {row["end"]: row for row in insights.compare(periods, model, wx)}
    heat = model.heat if model is not None and model.heat is not None else greenbutton.BALANCE_POINT_F

    def block(r: dict) -> dict | None:
        p = by_end.get(r.get("service_end"))
        if p is None or not wx.covers(p):
            return None
        row = compared.get(p.end.isoformat())
        return {
            "place": place,
            "average": round(wx.average(p), 1),
            "coldest": round(min(wx.temps[d] for d in wx.days(p)), 1),
            "warmest": round(max(wx.temps[d] for d in wx.days(p)), 1),
            # Degree-days over the whole period, at the balance points this home showed.
            "heating": round(wx.heating(p, heat) * p.days),
            "cooling": round(wx.cooling(p, model.cool) * p.days) if model is not None and model.cool is not None else None,
            "rain": round(wx.rainfall(p), 2) if wx.covers_dryness(p) else None,
            "days": [{"label": f"{d:%b %-d}", "temp": round(wx.temps[d], 1)} for d in wx.days(p)],
            "then": round(row["temp_then"], 1) if row and row.get("temp_then") is not None else None,
            # The change on a year earlier, split three ways - when the weather is known to matter here.
            "split": {"label": row["label"], "total": round(row["d_total"], 2), "weather": round(row["d_weather"], 2),
                      "you": round(row["d_you"], 2), "rate": round(row["d_rate"], 2),
                      "you_pct": round(row["you_pct"], 1) if row["you_pct"] is not None and row["pct_ok"] else None}
            if row and row["weather_known"] and model is not None and model.matters else None,
        }
    return block


def build_month(state: dict, year: int, month: int) -> dict | None:
    """One month of one year, close up. None when there is nothing in it."""
    reports = _year_reports(state)
    if year not in yearly.years_with_bills(reports):
        return None
    summary = _year_summary(state, reports, year)
    if not summary:
        return None
    try:
        usage = greenbutton.load_dir(paths().usage).periods if paths().usage.exists() else []
    except (OSError, ValueError):
        usage = []
    with_address = {**state, "_address": _address()}
    blocks: dict = {}

    def weather_of(fuel: str, r: dict):
        if fuel not in blocks:
            blocks[fuel] = _statement_weather(fuel, [p for p in usage if insights.fuel_of(p) == fuel], with_address)
        return blocks[fuel](r) if blocks[fuel] else None

    detail = yearly.month_detail(reports, year, month, summary, weather_of)
    return {"summary": summary, "detail": detail} if detail else None


@app.get("/year")
@app.get("/year/<int:year>")
def year_page(year=None):
    data = build_year(load_state(), year)
    token = None
    if data["year"] and g.user is not accounts.LOCAL:
        token = yearly.shared(g.user.id, g.home.id, data["year"])
    return render_template("year.html", **data, token=token, shared_view=False,
                           home_name=g.home.name if homes.several(g.base_paths) else None,
                           site=os.environ.get("ELEC_BASE_URL", "").rstrip("/") or request.url_root.rstrip("/"))


@app.get("/year/<int:year>/<int:month>")
def month_page(year, month):
    data = build_month(load_state(), year, month)
    if data is None:
        abort(404)
    return render_template("month.html", **data, shared_view=False,
                           home_name=g.home.name if homes.several(g.base_paths) else None)


@app.post("/api/share/year/<int:year>")
def api_share_year(year):
    if g.user is accounts.LOCAL:
        return jsonify({"error": "Sharing needs an account."}), 400
    data = build_year(load_state(), year)
    if data["year"] != year:
        return jsonify({"error": "There's nothing for that year."}), 404
    if request.args.get("stop"):
        yearly.stop(g.user.id, g.home.id, year)
        return jsonify({"ok": True, "token": None})
    return jsonify({"ok": True, "token": yearly.share(g.user.id, g.home.id, year)})


def _as_owner(token: str, build):
    """What a share token points at, built as the owner, read-only: build(state, year)."""
    link = yearly.lookup(token.removesuffix(".svg"))
    owner = accounts.get(link["user_id"]) if link else None
    if owner is None:
        abort(404)
    with workspace.open_(owner) as base:
        g.user, g.base_paths = owner, base
        home = homes.by_id(base, link["home_id"])
        if home is None or home.deleted_at:
            abort(404)
        g.home, g.paths = home, homes.paths_for(base, home)
        return build(load_state(), link["year"])


def _shared_year(token: str):
    return _as_owner(token, build_year)


@app.get("/s/<token>/<int:month>")
def shared_month(token, month):
    """One month of a shared year. The same page the owner sees, less the ways out of it."""
    data = _as_owner(token, lambda state, year: build_month(state, year, month))
    if data is None:
        abort(404)
    response = make_response(render_template("month.html", **data, shared_view=True, home_name=None,
                                             share_token=token))
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.get("/s/<token>")
def shared_year(token):
    """One home's year, for whoever has the link. Nothing that names anyone."""
    if token.endswith(".svg"):
        data = _shared_year(token)
        if not data["summary"]:
            abort(404)
        response = make_response(yearly.card_svg(data["summary"]))
        response.headers["Content-Type"] = "image/svg+xml"
        return response
    data = _shared_year(token)
    if not data["summary"]:
        abort(404)
    response = make_response(render_template("year.html", years=[data["year"]], year=data["year"],
                                             summary=data["summary"], token=None, shared_view=True,
                                             home_name=None, site="", share_token=token))
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.get("/rates")
def rates_page():
    return render_template("rates.html")


@app.get("/api/rates")
def api_rates():
    out = build_rates(load_state())
    # Bills without usage: there is a page's worth of nothing to draw, and
    # the one thing to say is what would fill it.
    if not out["series"] and not out.get("error"):
        out["error"] = "No usage on file yet - upload a usage export to see your price per kWh."
    return jsonify(out)


@app.post("/api/rates/supply")
def api_rates_supply():
    """Say what supply you're on, or - with nothing - go back to the default."""
    payload = request.get_json(silent=True) or {}
    state = load_state()
    state = coerce_settings({k: payload.get(k) for k in ("supply_cents", "zip") if k in payload}, state)
    save_state(state)
    return jsonify(build_rates(state))


@app.get("/api/insights")
def api_insights():
    return jsonify(build_insights(load_state()))


@app.post("/api/notes")
def api_notes():
    """Say what a change in usage was - or, with no text, take the note back."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Send the note as a JSON object."}), 400
    key = str(payload.get("key") or "")
    text = " ".join(str(payload.get("text") or "").split())[:MAX_NOTE_LENGTH]
    if not NOTE_KEY.match(key):
        return jsonify({"error": "That isn't something a note can be attached to."}), 400
    state = load_state()
    notes = dict(state.get("notes") or {})
    if text:
        notes[key] = text
    else:
        notes.pop(key, None)
    if len(notes) > MAX_NOTES:
        return jsonify({"error": "That is more notes than can be kept."}), 400
    state["notes"] = notes
    save_state(state)
    return jsonify(build_insights(state))


@app.post("/api/settings")
def api_settings():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Send the settings as a JSON object."}), 400
    state = coerce_settings(payload, load_state())
    save_state(state)
    # The fuel as checked, not as sent: the report must not be built for a fuel that was refused.
    return jsonify(build_report(state, fuel=state["fuel"] if "fuel" in payload else None))


def _sentence(text: str) -> str:
    """An error written in lower case, as a sentence - without str.capitalize(),
    which would lower-case everything after the first letter ("csv or xml")."""
    text = text.strip()
    return (text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")) if text else "Something went wrong."


def ingest_upload(filename: str, data: bytes, mapping: dict | None = None) -> dict:
    """File one uploaded export, whichever kind it turns out to be.

    Decided by content, not by name: the portal's filenames are opaque, and the
    statement history and the usage history are both "a CSV from the utility".
    The layouts we know by heart are tried first; anything else goes to the
    general importer, which either understands it or asks the person to say
    what the columns are (``kind: "unsure"``) and is sent the file again with
    their answer as ``mapping``.
    """
    if mapping is None:
        try:
            return _ingest_known(filename, data)
        except ValueError:
            pass            # not a layout we know; the general importer decides

    try:
        found = importers.read(data, filename, mapping)
        _adopt_account(found)
    except importers.Unsure as unsure:
        return {"kind": "unsure", "reason": unsure.reason, "headers": unsure.headers,
                "sample": unsure.sample, "guess": unsure.guess}
    except importers.ImportFailed as exc:
        raise ValueError(str(exc)) from None

    if found.kind == "bills":
        result = ingest.merge(paths().csv, found.bills, list(found.bills[0]))
        return {"kind": "bills", "added": result.added, "total": result.total,
                "notes": found.notes}
    stored = ingest.store_usage(paths().usage, importers_name(filename), found.text.encode())
    made = backfill.fill_missing(paths().csv, paths().usage)
    # The store's own notes (a figure already on file that this file disagrees
    # with) come before the importer's, which are about the file itself.
    return {**stored, "notes": list(stored.get("notes") or []) + found.notes, "bills_made": made}


def _adopt_account(found: importers.Result) -> None:
    """A file that names no account joins the account this fuel already uses.

    Otherwise a bill list and a usage file from the same utility would sit
    under two names and never be shown together.
    """
    if not found.account_invented:
        return
    known = ingest.default_account(paths().csv, fuel=found.fuel)
    if not known and paths().usage.exists():
        try:
            export = greenbutton.load_dir(paths().usage)
            known = next((p.account for p in export.periods
                          if insights.fuel_of(p) == found.fuel and p.account), None)
        except (OSError, ValueError):
            known = None
    if not known:
        return
    invented = found.accounts[0]
    if found.kind == "bills":
        for row in found.bills:
            row["Account# or Nickname"] = known
    else:
        digits = analysis.split_account(known)[0]
        found.text = found.text.replace(f"Account Number,{invented}\n", f"Account Number,{digits}\n", 1)
    found.accounts = [known]


def importers_name(filename: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", (filename or "").replace("\\", "/").rsplit("/", 1)[-1])
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "import") + ".csv"


def _ingest_known(filename: str, data: bytes) -> dict:
    if data[:2] != b"PK":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("that file isn't UTF-8 text") from None
        if ingest.is_bill_history(text):
            result = ingest.merge_csv_text(paths().csv, text)
            return {"kind": "bills", "added": result.added, "total": result.total}

    stored = ingest.store_usage(paths().usage, filename, data)
    return stored


@app.post("/api/upload")
def api_upload():
    """Take any export the utility hands out and put it where it belongs.

    Statement history is merged into the tracked CSV; usage exports - the .zip
    straight from the portal, or a loose CSV - are filed in ``usage/``. The
    work lives in ``ingest`` so scheduled jobs can do the same without HTTP.
    """
    uploads = [f for f in request.files.getlist("file") if f and f.filename]
    if not uploads:
        return jsonify({"error": "No file was attached."}), 400

    mapping = None
    if request.form.get("mapping"):
        try:
            mapping = json.loads(request.form["mapping"])
            if not isinstance(mapping, dict) or len(uploads) != 1:
                raise ValueError
        except ValueError:
            return jsonify({"error": "That column choice couldn't be read."}), 400

    results = []
    for upload in uploads:
        try:
            outcome = ingest_upload(upload.filename, upload.read(), mapping)
        except ValueError as exc:
            outcome = {"kind": "error", "error": _sentence(str(exc))}
        results.append({"file": upload.filename, **outcome})

    if all(r["kind"] == "error" for r in results):
        return jsonify({"error": " ".join(f"{r['file']}: {r['error']}" for r in results)
                        if len(results) > 1 else results[0]["error"],
                        "uploads": results}), 400

    _publish_benchmark(load_state())
    report = build_report(load_state(), fuel=request.form.get("fuel"))
    report["uploads"] = results
    # Kept for callers that only ever sent the statement history.
    bills = [r for r in results if r["kind"] == "bills"]
    if bills:
        report["merge"] = {"added": sum(r["added"] for r in bills),
                           "total": bills[-1]["total"]}
    return jsonify(report)


# Two names for one check. Cloud Run keeps paths ending in "z" for itself and
# answers /healthz with a 404 before the app is ever asked, so from outside
# only /health works there. /healthz stays for the container's own health
# check and for hosts that expect it.
@app.get("/health")
@app.get("/healthz")
def healthz():
    """Is the process up and can it reach its data? Says nothing about the data."""
    if db.is_postgres():
        ok = db.ping()
    else:
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            ok = os.access(config.DATA_DIR, os.W_OK)
        except OSError:
            ok = False
    return jsonify({"ok": ok}), (200 if ok else 503)


@app.post("/api/tasks/check")
def api_task_check():
    """Run the recurring check now. For a platform's scheduler to call.

    Guarded by its own token rather than the dashboard password, so the
    scheduler holds a credential that can do this and nothing else.
    """
    token = os.environ.get("ELEC_TASK_TOKEN")
    if not token:
        return jsonify({"error": "Tasks are not enabled."}), 404
    given = request.headers.get("Authorization", "")
    if not _same(given, f"Bearer {token}"):
        return jsonify({"error": "Bad task token."}), 403
    result = jobs.run_check()
    # Counts only: whose bill did what is nobody's business but theirs.
    return jsonify({k: result[k] for k in ("ok", "ran_at", "people", "steps")}), \
        (200 if result["ok"] else 500)


@app.get("/test-suite")
def test_suite():
    """What the last test run found. Open to anyone, but not linked from anywhere.

    There is nothing private in it: the tests run on invented data, so what it
    holds is test names, timings and - for a failure - the code that failed. The
    results are those of a run before this was deployed (see testreport.py);
    nothing is run by visiting.
    """
    report = testreport.load()
    view = None
    if report:
        started = datetime.fromisoformat(report["started"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        view = {"counts": testreport.totals(report), "modules": testreport.modules(report),
                "started": started, "age_days": (datetime.now(timezone.utc) - started).days}
    response = make_response(render_template(
        "test_suite.html", report=report, view=view, revision=os.environ.get("K_REVISION")))
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


from metermatters.web import admin, connect, docs  # noqa: E402 - the routes need `app` and `paths` to exist first

app.register_blueprint(connect.bp)
app.register_blueprint(docs.bp)
app.register_blueprint(admin.bp)


@app.errorhandler(413)
def too_large(_error):
    # The same limit guards two very different uploads; say which one was hit.
    if request.path.endswith("/api/scan"):
        return jsonify({"error": "That image is too large - under 5 MB, please."}), 413
    return jsonify({"error": "That file is too large to be an export."}), 413
