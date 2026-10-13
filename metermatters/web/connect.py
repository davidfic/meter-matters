"""Signing in, and the three ways a user's bills can reach the app.

    upload       they drop the files in. Nothing is connected to anything.
    forwarding   their mailbox forwards the utility's notices to a private
                 address. We hold no credential and see no other mail.
    gmail        they let us read the mailbox - once, or until they say stop.

Which of these exist depends on what the operator configured; a way that isn't
configured isn't offered.
"""

from __future__ import annotations

import os
import secrets
from urllib.parse import urlsplit

from flask import (Blueprint, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

from metermatters.store import accounts
from metermatters import config
from metermatters import fuels
from metermatters.intake import gmail_sync
from metermatters.web import google_oauth
from metermatters.store import homes
from metermatters.intake import importers
from metermatters.intake import inbound
from metermatters.intake import ingest
from metermatters import region
from metermatters.store import secrets_box
from metermatters.analytics import weather

bp = Blueprint("web", __name__)

GMAIL_PURPOSES = ("gmail-once", "gmail-keep")


def signups_open() -> bool:
    return os.environ.get("ELEC_ALLOW_SIGNUP", "1").strip().lower() not in ("0", "false", "no", "off")


def _safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this site."""
    target = target or ""
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith(("//", "/\\")):
        return url_for("index")
    return target


def _sign_in(user: accounts.User) -> None:
    # A fresh session, so one planted before sign-in is worth nothing after it.
    session.clear()
    session["uid"] = user.id
    session["epoch"] = user.epoch
    session.permanent = True


def _needs_accounts():
    if not config.multi_user():
        return redirect(url_for("index"))
    return None


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if (away := _needs_accounts()) is not None:
        return away
    if g.user:
        return redirect(_safe_next(request.values.get("next")))
    error, email = None, ""
    if request.method == "POST":
        email = request.form.get("email", "")
        try:
            user = accounts.authenticate(email, request.form.get("password", ""),
                                         client=request.remote_addr or "")
        except accounts.AccountError as exc:
            error = str(exc)
        else:
            _sign_in(user)
            return redirect(_safe_next(request.form.get("next")))
    return render_template("login.html", mode="login", error=error, email=email,
                           next=request.values.get("next", ""),
                           google=google_oauth.enabled(), signups=signups_open()), \
        (401 if error else 200)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if (away := _needs_accounts()) is not None:
        return away
    if g.user:
        return redirect(url_for("index"))
    if not signups_open():
        return render_template("login.html", mode="closed", error=None, email="", next="",
                               google=False, signups=False), 403
    error, email, name = None, "", ""
    if request.method == "POST":
        email, name = request.form.get("email", ""), request.form.get("name", "")
        try:
            user = accounts.register(email, request.form.get("password", ""), name)
        except accounts.AccountError as exc:
            error = str(exc)
        else:
            _sign_in(user)
            return redirect(url_for("web.connect"))
    return render_template("login.html", mode="register", error=error, email=email, name=name,
                           next="", google=google_oauth.enabled(), signups=True), \
        (400 if error else 200)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login") if config.multi_user() else url_for("index"))


@bp.post("/account/delete")
def delete_account():
    if not g.user or g.user is accounts.LOCAL:
        return redirect(url_for("index"))
    # Typed, not clicked: this takes every bill with it.
    if request.form.get("confirm", "").strip().lower() != g.user.email.lower():
        flash("Type your email address exactly to delete the account.", "err")
        return redirect(url_for("web.connect") + "#account")
    try:
        gmail_sync.unlink(g.user)
    except Exception:
        pass        # a Google outage must not keep someone's data here
    accounts.delete(g.user)
    session.clear()
    return redirect(url_for("web.login"))


# --------------------------------------------------------------------------
# Google
# --------------------------------------------------------------------------

@bp.get("/auth/google/start")
def google_start():
    if not config.multi_user() or not google_oauth.enabled():
        return redirect(url_for("index"))
    purpose = request.args.get("purpose", "signin")
    if purpose in GMAIL_PURPOSES:
        if not g.user:
            return redirect(url_for("web.login", next=url_for("web.connect")))
        if purpose == "gmail-keep" and not secrets_box.available():
            flash("Staying connected isn't available on this server. "
                  "A one-time import is.", "err")
            return redirect(url_for("web.connect"))
    elif purpose != "signin" or (not signups_open() and not g.user):
        purpose = "signin"

    # Proves the redirect back is the answer to a question this browser asked.
    state = secrets.token_urlsafe(24)
    session["oauth"] = {"state": state, "purpose": purpose,
                        "next": _safe_next(request.args.get("next"))}
    try:
        return redirect(google_oauth.authorize_url(
            state, gmail=purpose in GMAIL_PURPOSES,
            login_hint=g.user.email if g.user else ""))
    except google_oauth.GoogleError as exc:
        flash(f"Google sign-in isn't set up correctly: {exc}.", "err")
        return redirect(url_for("web.login"))


@bp.get("/auth/google/callback")
def google_callback():
    asked = session.pop("oauth", None)
    back = url_for("web.connect") if g.user else url_for("web.login")
    if not asked or not request.args.get("state") \
            or not secrets.compare_digest(asked["state"], request.args["state"]):
        flash("That sign-in link had expired. Try again.", "err")
        return redirect(back)
    if request.args.get("error") or not request.args.get("code"):
        flash("Nothing was connected - Google access was declined.", "err")
        return redirect(back)

    try:
        tokens = google_oauth.exchange(request.args["code"])
        who = google_oauth.identity(tokens.get("access_token", ""))
    except google_oauth.GoogleError as exc:
        flash(f"Google sign-in failed: {exc}.", "err")
        return redirect(back)

    if asked["purpose"] == "signin":
        existing = accounts.by_email(who["email"])
        if existing is None and not signups_open():
            flash("New sign-ups are closed on this server.", "err")
            return redirect(url_for("web.login"))
        _sign_in(accounts.from_google(who["sub"], who["email"], who["name"]))
        return redirect(asked["next"] if existing else url_for("web.connect"))

    return _gmail_granted(asked["purpose"], tokens, who)


def _gmail_granted(purpose: str, tokens: dict, who: dict):
    refresh = tokens.get("refresh_token")
    done = redirect(url_for("web.connect"))
    if not g.user:
        return redirect(url_for("web.login"))
    if not google_oauth.granted_gmail(tokens):
        if refresh:
            google_oauth.revoke(refresh)
        flash("Nothing was imported - the box allowing mail to be read wasn't ticked.", "err")
        return done

    keep = purpose == "gmail-keep"
    try:
        # Everything, the first time: this is how a history gets in at all.
        result = gmail_sync.import_with(g.base_paths, tokens["access_token"], days=None)
    except (google_oauth.GoogleError, ValueError) as exc:
        if refresh:
            google_oauth.revoke(refresh)
        flash(f"Couldn't read the mailbox: {exc}.", "err")
        return done

    if keep and refresh:
        try:
            gmail_sync.link(g.user, who["email"], refresh)
            flash(f"{result.summary} Gmail stays connected; new bills will arrive on their own.")
            return done
        except secrets_box.Unavailable:
            keep = False
    # One-time, or nothing to keep it with: hand the grant straight back.
    google_oauth.revoke(refresh or tokens["access_token"])
    flash(f"{result.summary} Nothing was kept - access to the mailbox has been handed back.")
    return done


@bp.post("/connect/gmail/disconnect")
def gmail_disconnect():
    if g.user and g.user is not accounts.LOCAL:
        gmail_sync.unlink(g.user)
        flash("Gmail disconnected. Bills already imported stay where they are.")
    return redirect(url_for("web.connect"))


@bp.post("/connect/gmail/check")
def gmail_check():
    if not g.user or g.user is accounts.LOCAL:
        return redirect(url_for("index"))
    try:
        result = gmail_sync.sync(g.user, g.base_paths)
        flash(result.summary if result else "Gmail isn't connected.")
    except google_oauth.Revoked:
        flash("Access to that mailbox was withdrawn, so it has been disconnected.", "err")
    except Exception as exc:
        flash(f"Couldn't check the mailbox: {exc}.", "err")
    return redirect(url_for("web.connect"))


# --------------------------------------------------------------------------
# Forwarding
# --------------------------------------------------------------------------

@bp.post("/connect/forwarding/rotate")
def forwarding_rotate():
    if g.user and g.user is not accounts.LOCAL:
        accounts.rotate_inbound_token(g.user)
        flash("New forwarding address issued. The old one no longer works - "
              "update the filter in your mailbox.")
    return redirect(url_for("web.connect"))


@bp.post("/api/inbound/email")
def inbound_email():
    """Where the inbound email service delivers. Not for browsers."""
    if not config.multi_user() or not inbound.enabled():
        return jsonify({"error": "Inbound email is not enabled."}), 404
    try:
        delivery = inbound.read(request)
    except inbound.Refused as exc:
        return jsonify({"error": str(exc)}), exc.status
    outcome, _ = inbound.receive(delivery)
    # Always 200 once authenticated, and never says whether the address was
    # real: the service must not retry, and nobody should learn which exist.
    return jsonify({"ok": True, "outcome": outcome if outcome != "unknown-recipient" else "ok"})


# --------------------------------------------------------------------------
# Homes
# --------------------------------------------------------------------------

def _homes_page():
    return redirect(url_for("web.connect") + "#homes")


@bp.post("/homes/add")
def home_add():
    try:
        home = homes.add(g.base_paths, request.form.get("name", ""))
    except homes.HomeError as exc:
        flash(str(exc), "err")
        return _homes_page()
    flash(f"Added {home.name}. Bills you bring in from its pages go into {home.name} only.")
    # Straight to where its bills come in - a home with none is an empty page.
    return redirect(f"/home/{home.slug}/connect" if g.user is not accounts.LOCAL
                    else f"/home/{home.slug}/")


@bp.post("/homes/<home_id>/rename")
def home_rename(home_id):
    try:
        home = homes.rename(g.base_paths, home_id, request.form.get("name", ""))
        flash(f"Renamed to {home.name}.")
    except homes.HomeError as exc:
        flash(str(exc), "err")
    return _homes_page()


@bp.post("/homes/<home_id>/delete")
def home_delete(home_id):
    try:
        home = homes.delete(g.base_paths, home_id, request.form.get("confirm", ""))
        flash(f"{home.name} is deleted. Its bills are kept until {home.purge_on():%B %-d}, "
              "and you can restore it until then.")
    except homes.HomeError as exc:
        flash(str(exc), "err")
    # The page they were on may have been the home itself.
    return redirect("/connect#homes")


@bp.post("/homes/<home_id>/restore")
def home_restore(home_id):
    try:
        home = homes.restore(g.base_paths, home_id)
        flash(f"{home.name} is back.")
    except homes.HomeError as exc:
        flash(str(exc), "err")
    return _homes_page()


@bp.post("/homes/bills/<pending_id>")
def home_place_bill(pending_id):
    try:
        homes.place(g.base_paths, pending_id, request.form.get("home", ""))
        flash("Filed.")
    except homes.HomeError as exc:
        flash(str(exc), "err")
    return _homes_page()


@bp.post("/homes/bills/<pending_id>/dismiss")
def home_dismiss_bill(pending_id):
    homes.dismiss(g.base_paths, pending_id)
    flash("Dropped. It will be filed again if it arrives again.")
    return _homes_page()


# --------------------------------------------------------------------------
# About a home, and a bill typed in
# --------------------------------------------------------------------------

@bp.post("/place")
def home_place():
    """Where this home is and who bills it. Both optional; both only change what's shown."""
    from metermatters.web import app as site
    payload = {"state": request.form.get("state", ""), "provider": request.form.get("provider", "")}
    zipcode = request.form.get("zip", "").strip()
    if zipcode and not weather.ZIP.match(zipcode):
        flash("A ZIP code is five digits.", "err")
        return redirect(url_for("web.connect") + "#place")
    payload["zip"] = zipcode
    payload["share_benchmark"] = bool(request.form.get("share_benchmark"))
    site.save_state(site.coerce_settings(payload, site.load_state()))
    site._publish_benchmark(site.load_state())
    flash("Saved.")
    return redirect(url_for("web.connect") + "#place")


@bp.post("/bills/add")
def bill_add():
    """One bill, typed in. Usage is optional; with it the page can say how much was used."""
    form = request.form
    try:
        row = importers.manual(form.get("fuel", ""), form.get("due", ""), form.get("amount", ""),
                               form.get("account", ""))
        usage = _manual_usage(form, row)         # checked before anything is written
        added = ingest.merge(g.paths.csv, [row], list(row)).added
        if usage:
            _store_manual_usage(*usage)
    except (importers.ImportFailed, ValueError) as exc:
        flash(str(exc).capitalize() + ".", "err")
        return redirect(url_for("web.connect") + "#byhand")
    flash(("Added that bill" if added else "That bill was already on file")
          + (" and its usage." if usage else "."))
    return redirect(url_for("web.connect") + "#byhand")


def _manual_usage(form, row: dict):
    """The optional half: how much was used, over which dates. Checked, not yet written."""
    used, start, end = form.get("used", "").strip(), form.get("start", ""), form.get("end", "")
    if not (used or start or end):
        return None
    from metermatters.analytics import analysis
    amount, first, last = analysis.parse_money(used), analysis.parse_date(start), analysis.parse_date(end)
    if not (amount and amount > 0 and first and last) or last < first or (last - first).days > 120:
        raise ValueError("for usage, give the amount used and the dates it covers, up to 120 days")
    unit = {"kWh": "kWh", "therms": "therms", "ccf": "ccf", "gal": "gal"}.get(form.get("unit", ""))
    if unit == "ccf" and row["Account Type"] == "Water":
        unit, amount = "gal", amount * fuels.GALLONS_PER_CCF
    if unit is None or importers.UNIT_FUEL[unit] != row["Account Type"]:
        raise ValueError("that unit doesn't match the fuel")
    cost = analysis.parse_money(row["Amount"])
    account = analysis.split_account(row["Account# or Nickname"])[0]
    return (importers.Row(account, first, last, amount, cost), unit,
            f"by-hand-{last.isoformat()}-{row['Account Type'].lower()}.csv")


def _store_manual_usage(result, unit: str, name: str) -> None:
    ingest.store_usage(g.paths.usage, name,
                       importers.usage_text([result], unit, result.account).encode())


# --------------------------------------------------------------------------
# The page that offers all of it
# --------------------------------------------------------------------------

@bp.get("/connect")
def connect():
    if g.user is accounts.LOCAL:
        return redirect(url_for("index"))
    paths = lambda: g.paths  # noqa: E731
    forwarding = None
    if inbound.enabled():
        forwarding = {"address": inbound.address_for(g.user),
                      "confirm": inbound.pending_confirmation(g.user),
                      "log": inbound.log_for(g.user)}
    gmail = None
    if google_oauth.enabled():
        gmail = {"link": gmail_sync.status(g.user), "can_keep": secrets_box.available()}
    has_usage = paths().usage.exists() and any(
        p.suffix.lower() in (".zip", ".csv") for p in paths().usage.iterdir())
    return render_template("connect.html", forwarding=forwarding, gmail=gmail,
                           has_bills=paths().csv.exists(), has_usage=has_usage,
                           homes_view=_homes_view(), place=_place_view())


def _place_view() -> dict:
    """What the 'About this home' card shows, and what it worked out for itself."""
    from metermatters.web import app as site
    state = site.load_state()
    address = site._address()
    return {"state": state.get("state") or "", "detected": region.from_address(address),
            "zip": state.get("zip") or weather.zip_in(address) or "",
            "provider": state.get("provider") or "",
            "share_benchmark": bool(state.get("share_benchmark", True)),
            "states": sorted(region.STATES.items(), key=lambda kv: kv[1])}


def _homes_view() -> dict:
    """What the page needs to show homes - and nothing, for the person with one."""
    base = g.base_paths
    each = homes.active(base)
    held = []
    for row in homes.pending(base):
        held.append({**row, "due_text": row.get("due") or "", "fuel_name": (row.get("fuel") or "").capitalize()})
    return {"several": len(each) > 1, "active": each, "current": g.home,
            "deleted": homes.deleted(base), "pending": held,
            "can_add": len(each) < homes.MAX_HOMES, "max": homes.MAX_HOMES}
