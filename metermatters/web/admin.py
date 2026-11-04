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

One exception: bills that couldn't be read (/admin/failed). They are kept for
30 days only so the reader can be fixed against them, the user is told so on
their own page, and every download, re-read and delete here is logged.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import (Blueprint, abort, flash, g, make_response, redirect, render_template,
                   request, url_for)

from metermatters import config
from metermatters import testreport
from metermatters.intake import failed
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
        {"name": "Reading any utility's email", "on": _set("ELEC_ANTHROPIC_KEY"),
         "how": "ELEC_ANTHROPIC_KEY"},
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
            "failed": failed.count_for(user.id),
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


# --------------------------------------------------------------------------
# Bills that couldn't be read
# --------------------------------------------------------------------------

EXTENSIONS = {"message/rfc822": ".eml", "text/csv": ".csv", "application/xml": ".xml",
              "text/xml": ".xml", "application/zip": ".zip", "application/pdf": ".pdf",
              "image/png": ".png", "image/jpeg": ".jpg", "text/plain": ".txt"}


def _held(failed_id: int) -> dict:
    row = failed.get(failed_id)
    if row is None:
        abort(404)
    return row


@bp.get("/failed")
def failed_list():
    rows = failed.all_rows()
    for row in rows:
        row["received"] = _when(row["received_at"])
        row["expires"] = _when(row["expires_at"])
    return _page("admin/failed.html", rows=rows, keep_days=failed.KEEP_DAYS)


@bp.get("/failed/<int:failed_id>/raw")
def failed_raw(failed_id):
    row = _held(failed_id)
    site.record(g.user.email, "failed-download", f"#{failed_id}",
                f"user {row['user_id']}, {row['kind']}")
    ext = EXTENSIONS.get(row["content_type"], "")
    response = make_response(row["content"])
    response.headers["Content-Type"] = "application/octet-stream"
    response.headers["Content-Disposition"] = f'attachment; filename="failed-{failed_id}{ext}"'
    return response


def _reread(row: dict) -> tuple[bool, str]:
    """Today's reader on the held original. Files nothing."""
    user = accounts.get(row["user_id"])
    if user is None:
        return False, "The account is gone."
    try:
        if row["kind"] == "email":
            from metermatters.intake import inbound
            notice = inbound.reread(user, row["content"])
            return True, f"Reads now: {notice.fuel} ${notice.amount:,.2f}, due {notice.due}."
        if row["kind"] == "upload":
            from metermatters.intake import importers, ingest
            data = row["content"]
            if data[:2] != b"PK":
                try:
                    if ingest.is_bill_history(data.decode("utf-8-sig")):
                        return True, "Reads now as bill history."
                except UnicodeDecodeError:
                    pass
            found = importers.read(data, row["label"])
            return True, (f"Reads now: {found.kind}, {found.fuel}, "
                          f"{len(found.bills) or found.periods} row(s).")
        from metermatters.intake import scan
        if not scan.enabled():
            return False, "Reading scans isn't set up on this server."
        found = scan.guess(scan.read_text(row["content"], row["content_type"]))
        sure = [k for k in ("amount", "due") if k in found["sure"]]
        return bool(sure), (f"Found: amount {found['amount']}, due {found['due']}, "
                            f"fuel {found['fuel']} (sure of: {', '.join(found['sure']) or 'nothing'}).")
    except Exception as exc:                # noqa: BLE001 - whatever it is, it is the answer
        return False, f"Still can't: {exc}"


@bp.post("/failed/<int:failed_id>/reread")
def failed_reread(failed_id):
    row = _held(failed_id)
    ok, said = _reread(row)
    site.record(g.user.email, "failed-reread", f"#{failed_id}", said)
    flash(f"#{failed_id}: {said}", "message" if ok else "err")
    return redirect(url_for("admin.failed_list"))


@bp.post("/failed/<int:failed_id>/file")
def failed_file(failed_id):
    """File a held email for its user, now that it reads. Then it isn't kept."""
    row = _held(failed_id)
    user = accounts.get(row["user_id"])
    if row["kind"] != "email" or user is None:
        abort(404)
    from metermatters.intake import inbound
    try:
        outcome = inbound.refile(user, row["content"])
    except ValueError as exc:
        flash(f"#{failed_id}: still can't: {exc}", "err")
        return redirect(url_for("admin.failed_list"))
    failed.delete_any(failed_id)
    site.record(g.user.email, "failed-file", f"#{failed_id}", f"user {user.id}: {outcome}")
    flash(f"#{failed_id}: {outcome} for user {user.id}, and deleted.")
    return redirect(url_for("admin.failed_list"))


@bp.post("/failed/<int:failed_id>/delete")
def failed_delete(failed_id):
    if failed.delete_any(failed_id):
        site.record(g.user.email, "failed-delete", f"#{failed_id}")
        flash(f"#{failed_id} deleted.")
    return redirect(url_for("admin.failed_list"))
