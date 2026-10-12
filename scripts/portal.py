#!/usr/bin/env python
"""Download Eversource usage exports with a real browser.

Eversource offers no automated feed an individual can use - Green Button
Connect requires registering as a third-party company - so the usage data
(kWh, therms, metered periods) can only come from the portal. This drives it.

**It does not store your Eversource password.** You log in once, by hand, in a
real window; the session is kept in a browser profile and reused after that.
That sidesteps MFA and CAPTCHA entirely, because a human does the part those
are designed to stop. When the session eventually expires the fetch fails
loudly and you log in again.

    ./portal.py --login      # one time, opens a window - log in, then close it
    ./portal.py --status     # is the saved session still good?
    ./portal.py --fetch      # headless: download exports into usage/
    ./portal.py --explore    # dump what the logged-in portal looks like

The profile lives outside the repo, in ~/.cache/elec-bill/browser - it holds
session cookies and must never be committed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Unlike the rest of the project this needs a third-party package, so re-exec
# into the venv rather than failing when run as ./portal.py under system python.
if "playwright" not in sys.modules:
    try:
        import playwright  # noqa: F401
    except ImportError:
        _root = Path(__file__).resolve().parent.parent / ".venv"
        _venv = _root / "bin" / "python"
        # Compare sys.prefix, not the resolved paths: .venv/bin/python is a
        # symlink to the system interpreter, so resolving makes them identical
        # and the re-exec never fires.
        if _venv.exists() and Path(sys.prefix).resolve() != _root.resolve():
            os.execv(str(_venv), [str(_venv), str(Path(__file__).resolve()), *sys.argv[1:]])

BASE_DIR = Path(__file__).resolve().parent.parent
USAGE_DIR = Path(os.environ.get("ELEC_USAGE_DIR", BASE_DIR / "usage"))
PROFILE_DIR = Path(os.environ.get(
    "ELEC_BROWSER_PROFILE",
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "elec-bill" / "browser"))

LOGIN_URL = ("https://www.eversource.com/security/account/login"
             "?ReturnUrl=/cg/customer/accountoverview")
ACCOUNT_URL = "https://www.eversource.com/cg/customer/accountoverview"

EXIT_OK, EXIT_ERROR, EXIT_NO_SESSION, EXIT_NOTHING_NEW = 0, 1, 3, 2

# Text that only appears when we are NOT logged in.
SIGNED_OUT_MARKERS = ("sign in", "log in", "username", "password")


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: playwright isn't installed - .venv/bin/python -m pip install playwright",
              file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    return sync_playwright


def _context(pw, headless: bool):
    """A persistent context, so the login survives between runs."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1440, "height": 900},
        # A stock UA; nothing evasive, just not advertising automation.
        user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"),
    )


def looks_signed_in(page) -> bool:
    """Best-effort check. Refined once the real portal has been explored."""
    try:
        text = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    if "sign out" in text or "log out" in text:
        return True
    # A login form on screen means we are definitely not in.
    if page.locator("input[type=password]").count() > 0:
        return False
    return not any(m in text[:4000] for m in SIGNED_OUT_MARKERS)


def cmd_login() -> int:
    """Open a real window and wait while the human signs in."""
    sync_playwright = _playwright()
    print("Opening a browser window. Sign in to Eversource - including any")
    print("two-factor step - then CLOSE the window when you see your account.")
    print(f"The session is saved in {PROFILE_DIR}\n")
    with sync_playwright() as pw:
        ctx = _context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"error: couldn't open the login page: {exc}", file=sys.stderr)
            ctx.close()
            return EXIT_ERROR
        # Block until the window is closed by hand.
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()
    print("\nWindow closed. Checking the session...")
    return cmd_status()


def cmd_status() -> int:
    sync_playwright = _playwright()
    with sync_playwright() as pw:
        ctx = _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            ok = looks_signed_in(page)
            where = page.url
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            ctx.close()
            return EXIT_ERROR
        ctx.close()
    if ok:
        print(f"session looks good (landed on {where[:80]})")
        return EXIT_OK
    print(f"not signed in (landed on {where[:80]}) - run ./portal.py --login", file=sys.stderr)
    return EXIT_NO_SESSION


def cmd_explore(out: Path) -> int:
    """Save what the logged-in portal looks like, so selectors can be written."""
    sync_playwright = _playwright()
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        (out / "dashboard.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out / "dashboard.png"), full_page=True)
        links = page.eval_on_selector_all(
            "a", "els => els.map(e => [ (e.innerText||'').trim().slice(0,60), e.href ])")
        seen, lines = set(), []
        for text, href in links:
            if href and href not in seen:
                seen.add(href)
                lines.append(f"{text}\t{href}")
        (out / "links.tsv").write_text("\n".join(lines), encoding="utf-8")
        print(f"signed in: {looks_signed_in(page)}")
        print(f"url: {page.url}")
        print(f"wrote {len(lines)} links + dashboard.html + dashboard.png to {out}")
        ctx.close()
    return EXIT_OK


def cmd_fetch(dest: Path) -> int:
    """Download the usage exports. Requires a live session."""
    sync_playwright = _playwright()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    saved = []
    with sync_playwright() as pw:
        ctx = _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if not looks_signed_in(page):
                print("error: session expired - run ./portal.py --login", file=sys.stderr)
                ctx.close()
                return EXIT_NO_SESSION

            # --- portal navigation goes here -----------------------------
            # Written once ./portal.py --explore has shown the real markup.
            # Every step must fail loudly: a silent no-op would leave the
            # dashboard quietly stale, which is the whole thing we're avoiding.
            print("error: the download steps aren't written yet - run --explore first",
                  file=sys.stderr)
            ctx.close()
            return EXIT_ERROR
            # -------------------------------------------------------------
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            ctx.close()
            return EXIT_ERROR
    return EXIT_OK if saved else EXIT_NOTHING_NEW


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive the Eversource portal to download usage exports.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--login", action="store_true", help="sign in once, by hand")
    group.add_argument("--status", action="store_true", help="is the session still valid?")
    group.add_argument("--fetch", action="store_true", help="download exports")
    group.add_argument("--explore", action="store_true", help="dump the logged-in portal")
    group.add_argument("--forget", action="store_true", help="delete the saved session")
    parser.add_argument("--dest", default=str(USAGE_DIR))
    parser.add_argument("--out", default="/tmp/elec-portal", help="where --explore writes")
    args = parser.parse_args(argv)

    if args.forget:
        if PROFILE_DIR.exists():
            shutil.rmtree(PROFILE_DIR)
            print(f"deleted {PROFILE_DIR}")
        else:
            print("nothing saved")
        return EXIT_OK
    if args.login:
        return cmd_login()
    if args.status:
        return cmd_status()
    if args.explore:
        return cmd_explore(Path(args.out))
    return cmd_fetch(Path(args.dest))


if __name__ == "__main__":
    sys.exit(main())
