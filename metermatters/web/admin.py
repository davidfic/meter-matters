"""The admin panel: the few things an operator needs to do while the app runs,
done from a page instead of a redeploy or a database client.

Who may use it is ELEC_ADMIN_EMAILS, a comma-separated list of account emails;
anyone else gets a 404, as if there were no such page. It only exists when
accounts are on.

It is a view of the installation, not of anyone's bills. Nothing here opens a
user's files: an account is shown as its email, how it signs in, when it
joined, how much it stores and how its forwarding has gone, all as counts and
sizes. A bill, a home's name or ZIP, a utility account number, a message
subject or a forwarding address never appears on these pages. Everything done
here is written to a log the panel shows.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from metermatters import config
from metermatters import testreport
from metermatters.store import accounts
from metermatters.store import db
from metermatters.store import site
from metermatters.store import workspace
from metermatters.tasks import jobs
from metermatters.web import google_oauth

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
def only_admins():
    if not config.multi_user() or not site.is_admin(g.get("user")):
        abort(404)


@bp.after_request
def unlisted(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# --------------------------------------------------------------------------
# What is shown
# --------------------------------------------------------------------------

def _set(*names: str) -> bool:
    return all(os.environ.get(name) for name in names)


def features() -> list[dict]:
    """Which optional parts are configured. Whether, never what."""
    forwarding = _set("ELEC_INBOUND_DOMAIN") and (_set("ELEC_INBOUND_SECRET")
                                                   or _set("ELEC_MAILGUN_SIGNING_KEY"))
    return [
        {"name": "Forwarding", "on": forwarding,
         "how": "ELEC_INBOUND_DOMAIN and an inbound secret"},
        {"name": "Google sign-in", "on": google_oauth.enabled(),
         "how": "ELEC_GOOGLE_CLIENT_ID and secret"},
        {"name": "Scans", "on": _set("ELEC_VISION_KEY"), "how": "ELEC_VISION_KEY"},
        {"name": "Email to users", "on": _set("ELEC_SMTP_USER", "ELEC_SMTP_PASS"),
         "how": "ELEC_SMTP_USER and ELEC_SMTP_PASS"},
        {"name": "Weather", "on": os.environ.get("ELEC_WEATHER", "").strip().lower() != "off",
         "how": "on unless ELEC_WEATHER=off"},
        {"name": "Daily check", "on": config.flag("ELEC_SCHEDULER") or _set("ELEC_TASK_TOKEN"),
         "how": "ELEC_SCHEDULER=1, or ELEC_TASK_TOKEN and an outside scheduler"},
    ]


def _when(text: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def storage_of(user: accounts.User) -> dict:
    """Counts and sizes of what an account holds. Not one file is read."""
    items = workspace.inventory(user)
    kinds = {"statements": 0, "usage": 0, "other": 0}
    for item in items:
        name = item["name"]
        if name.endswith("AccountHistory.csv"):
            kinds["statements"] += 1
        elif "/usage/" in f"/{name}":
            kinds["usage"] += 1
        else:
            kinds["other"] += 1
    return {**kinds, "files": len(items), "bytes": sum(i["size"] for i in items),
            "last": _when(max((i["updated"] for i in items), default=""))}


def arrivals_of(user: accounts.User) -> dict:
    """How forwarding has gone for an account: how many of each outcome, and when last."""
    with db.connect() as conn:
        rows = conn.execute("SELECT outcome, COUNT(*) AS n, MAX(received_at) AS last "
                            "FROM inbound_log WHERE user_id = ? GROUP BY outcome",
                            (user.id,)).fetchall()
    counts = {row["outcome"]: int(row["n"]) for row in rows}
    return {"counts": counts, "total": sum(counts.values()),
            "last": _when(max((row["last"] for row in rows), default=""))}


def account_view(user: accounts.User) -> dict:
    return {"id": user.id, "email": user.email,
            "signin": " and ".join(h for h, on in (("password", user.has_password),
                                                   ("Google", user.has_google)) if on) or "none",
            "joined": _when(user.created_at), "storage": storage_of(user),
            "arrivals": arrivals_of(user), "admin": site.is_admin(user),
            "me": g.user is not None and user.id == g.user.id}


def settings_view() -> dict:
    view = {}
    for key, choices in site.CHOICES.items():
        value, where = site.setting(key)
        view[key] = {"value": value, "where": where, "choices": choices,
                     "variable": site.VARIABLES[key], "env": site.from_environment(key)}
    return view


def check_view() -> dict | None:
    last = jobs.last_run()
    if not last:
        return None
    return {"ran_at": _when(last.get("ran_at", "")), "seconds": last.get("seconds"),
            "ok": last.get("ok"), "people": last.get("people"),
            "steps": last.get("steps") or {}}


def _page(template: str, **extra):
    report = testreport.load()
    tests = testreport.totals(report) if report else None
    return render_template(template, features=features(), settings=settings_view(),
                           notice=site.notice(), notice_max=site.NOTICE_MAX,
                           check=check_view(), tests=tests,
                           log=[{**row, "at": _when(row["at"])} for row in site.recent()],
                           database="Postgres" if db.is_postgres() else "SQLite",
                           database_ok=db.ping(), revision=os.environ.get("K_REVISION"),
                           **extra)


@bp.get("")
def index():
    people = [account_view(u) for u in accounts.everyone()]
    return _page("admin/index.html", people=people)


@bp.get("/accounts/<int:user_id>")
def account(user_id):
    user = accounts.get(user_id)
    if user is None:
        abort(404)
    return _page("admin/account.html", person=account_view(user))


# --------------------------------------------------------------------------
# What can be done
# --------------------------------------------------------------------------

@bp.post("/accounts/<int:user_id>/delete")
def account_delete(user_id):
    user = accounts.get(user_id)
    if user is None:
        abort(404)
    if user.id == g.user.id:
        flash("Delete your own account from the Add bills page, as anyone else would.", "err")
        return redirect(url_for("admin.account", user_id=user.id))
    if request.form.get("confirm", "").strip().lower() != user.email.lower():
        flash("Type the account's email address exactly to delete it.", "err")
        return redirect(url_for("admin.account", user_id=user.id))
    held = storage_of(user)
    accounts.delete(user)
    site.record(g.user.email, "deleted account", user.email,
                f"{held['files']} file(s), {held['bytes']} bytes")
    flash(f"Deleted {user.email} and everything it held.")
    return redirect(url_for("admin.index"))


@bp.post("/settings")
def settings():
    changed = []
    wanted = {key: request.form.get(key, "").strip().lower() for key in site.CHOICES}
    for key, given in wanted.items():
        if given not in site.CHOICES[key] and given != "":
            flash(f"{given!r} is not a choice for {key}.", "err")
            return redirect(url_for("admin.index"))
    for key, given in wanted.items():
        before = site.setting(key)
        site.set_(key, given or None)
        after = site.setting(key)
        if before != after:
            changed.append(f"{key}: {after[0]} ({after[1]})")
    if changed:
        site.record(g.user.email, "changed settings", detail="; ".join(changed))
        flash("Settings saved. " + "; ".join(changed) + ".")
    else:
        flash("Nothing changed.")
    return redirect(url_for("admin.index"))


@bp.post("/notice")
def notice():
    if request.form.get("clear"):
        site.set_notice("")
        site.record(g.user.email, "cleared the notice")
        flash("The notice is gone.")
    else:
        text = site.set_notice(request.form.get("text", ""))
        if text:
            site.record(g.user.email, "set the notice", detail=text)
            flash("Everyone signed in now sees the notice.")
        else:
            site.record(g.user.email, "cleared the notice")
            flash("The notice is gone.")
    return redirect(url_for("admin.index"))


@bp.post("/check")
def check():
    result = jobs.run_check()
    tally = result.get("steps", {})
    failed = sum(t.get("failed", 0) for t in tally.values())
    summary = (f"{result.get('people', 0)} account(s) in {result.get('seconds', 0)}s"
               + (f", {failed} step(s) failed" if failed else ""))
    site.record(g.user.email, "ran the daily check", detail=summary)
    flash(("The check ran: " if result.get("ok") else "The check ran, with failures: ") + summary + ".",
          "" if result.get("ok") else "err")
    return redirect(url_for("admin.index"))
