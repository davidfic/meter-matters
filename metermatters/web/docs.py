"""The documentation: how to get bills in, step by step, and what each way can see.

Served at /docs, open to anyone - someone deciding whether to forward their
bill emails wants to read what that means before they have an account. The
pages are Jinja templates under templates/docs/, one per entry in PAGES, and
their screenshots live in static/docs/ (taken by scripts/docs_screenshots.py
against the invented "Pat Sample" household, never real bills).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flask import Blueprint, abort, render_template

bp = Blueprint("docs", __name__)
SHOTS = Path(__file__).resolve().parent / "static" / "docs"


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    blurb: str
    part: str            # which group it sits in on the index and in the sidebar
    parent: str | None = None      # a step-by-step page under one of the others


PAGES = (
    Page("start", "Getting started",
         "An account, a word about your home, and the first bill. Ten minutes.", "Setting up"),
    Page("upload", "Upload your utility's files",
         "Where the export is on your utility's site, what to drop in, and what happens next.", "Setting up"),
    Page("forwarding", "Forward your bill emails",
         "A private address for your bills. How it works, and the steps for Gmail, Outlook and iCloud.", "Setting up"),
    Page("forwarding-gmail", "Gmail, step by step",
         "Add the address, confirm it, make the filter, send a test. Every screen.", "Setting up",
         parent="forwarding"),
    Page("forwarding-outlook", "Outlook, step by step",
         "One rule in Outlook.com or Microsoft 365, and a test.", "Setting up", parent="forwarding"),
    Page("forwarding-icloud", "iCloud Mail, step by step",
         "One rule at icloud.com, and a test.", "Setting up", parent="forwarding"),
    Page("by-hand", "Type a bill in, or scan it",
         "For a bill on paper, or a utility with nothing to download.", "Setting up"),
    Page("homes", "More than one home",
         "A second house, a flat, a relative's place, each kept apart.", "Setting up"),
    Page("security", "Security and privacy",
         "What each way can see, what is kept, what never leaves your mailbox, and how to undo it.", "Your data"),
    Page("troubleshooting", "When something doesn't file",
         "A bill that arrived but wasn't filed, a file that was turned away, a column it couldn't tell.", "Your data"),
    Page("operators", "Running your own server",
         "Every setting, and the steps to switch on forwarding, scans and Google sign-in.", "Behind the scenes"),
)

BY_SLUG = {p.slug: p for p in PAGES}
PARTS = []
for _p in PAGES:
    if _p.part not in PARTS:
        PARTS.append(_p.part)


def shot_exists(name: str) -> bool:
    """Whether a screenshot has been taken. A page shows the ones there are;
    one not taken yet (the mail providers' own screens are taken by hand) is
    left out rather than shown broken."""
    return (SHOTS / name).is_file()


@bp.record_once
def _globals(state) -> None:
    state.app.jinja_env.globals["shot_exists"] = shot_exists


def _context(page: Page | None) -> dict:
    index = PAGES.index(page) if page else -1
    return {"page": page, "pages": PAGES, "parts": PARTS,
            "prev": PAGES[index - 1] if index > 0 else None,
            "next": PAGES[index + 1] if 0 <= index < len(PAGES) - 1 else None}


@bp.get("/docs")
def index():
    return render_template("docs/index.html", **_context(None))


@bp.get("/docs/<slug>")
def page(slug):
    page = BY_SLUG.get(slug)
    if page is None:
        abort(404)
    return render_template(f"docs/{slug}.html", **_context(page))
