#!/usr/bin/env python
"""Take the screenshots the /docs pages show, on the invented household.

    .venv/bin/python scripts/docs_screenshots.py            # writes metermatters/web/static/docs/*.png
    .venv/bin/python scripts/docs_screenshots.py --keep     # ...and leaves the throwaway data dir behind

It runs the app in this process on a throwaway data directory with accounts,
forwarding and scans switched on, signs up "pat.sample@example.com", and drives
a headless browser through the Add bills page: the fresh page, an upload, the
column question, Gmail's confirmation, a few arrivals, a scan, a second home
and a bill waiting for one. Nothing leaves the machine: the weather lookup is
off, the DKIM check and the text reader are replaced with stand-ins, and every
bill is from tests/fixtures/synthetic/.

The two Gmail screenshots (static/docs/gmail-filter-*.png) are not made here:
they are real Gmail dialogs, cropped so nothing personal is in frame, and only
need retaking if Gmail's filter panel changes.

Needs Playwright and its Chromium:  .venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "metermatters" / "web" / "static" / "docs"
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic"
PORT = 5199
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "docs-screenshots-inbound-secret"
DOMAIN = "in.example.com"
EMAIL, PASSWORD = "pat.sample@example.com", "correct horse battery staple"

# What the text reader "reads" off the scanned bill. Invented, like the rest.
SCANNED = """Your electric statement
Service period: Aug 14, 2026 to Sep 13, 2026
Total kWh used: 612 kWh
Previous balance $0.00
Total Amount Due $148.22
Payment due by October 5, 2026
"""

UNCLEAR_CSV = ("Period,Reading,Value\n" + "\n".join(
    f"2026-{m:02d}-15,{500 + m},{100 + m * 2}.50" for m in range(1, 8))).encode()


def configure(data_dir: Path) -> None:
    os.environ.update({
        "ELEC_SECRET_KEY": "docs-screenshots-" + "x" * 40,
        "ELEC_BASE_URL": BASE,
        "ELEC_DATA_DIR": str(data_dir),
        "ELEC_INBOUND_DOMAIN": DOMAIN,
        "ELEC_INBOUND_SECRET": SECRET,
        "ELEC_INBOUND_DKIM": "warn",
        "ELEC_VISION_KEY": "not-a-real-key",
        "ELEC_WEATHER": "off",
    })
    for name in ("ELEC_DATABASE_URL", "ELEC_GOOGLE_CLIENT_ID", "ELEC_GOOGLE_CLIENT_SECRET",
                 "ELEC_SMTP_HOST", "ELEC_SMTP_USER", "ELEC_SCHEDULER"):
        os.environ.pop(name, None)


def serve():
    """The app, in a thread, with the two things that would reach the network replaced."""
    from werkzeug.serving import make_server
    from metermatters.web.app import app
    from metermatters.intake import inbound, scan
    inbound.dkim_result = lambda raw: "pass"
    scan.read_text = lambda data, mime: SCANNED
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1)
            return server
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the app did not come up")


def deliver(raw: bytes, to: str) -> None:
    """What the inbound email service does: hand one message to the app."""
    req = urllib.request.Request(f"{BASE}/api/inbound/email", data=raw, method="POST", headers={
        "Content-Type": "message/rfc822", "Authorization": f"Bearer {SECRET}", "X-Envelope-To": to})
    with urllib.request.urlopen(req, timeout=10) as res:
        assert res.status == 200, res.status


def confirmation(to: str) -> bytes:
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = "Gmail Team <forwarding-noreply@google.com>"
    msg["To"] = to
    msg["Subject"] = "(#482913605) Gmail Forwarding Confirmation"
    msg.set_content(
        "pat.sample@gmail.com has requested to automatically forward mail to your address.\n"
        "Confirmation code: 482913605\n\n"
        "To allow this, click the link below:\n"
        "https://mail-settings.google.com/mail/vf-%5BANGjdJ8example%5D-Example123\n")
    return msg.as_bytes()


def from_someone_else(to: str) -> bytes:
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = "Eversource Rewards <offers@eversource-rewards.example>"
    msg["To"] = to
    msg["Subject"] = "Your Eversource statement is ready"
    msg.set_content("Total Amount Due: $9,999.00  Due date: 10/16/2026  Account Number: ******0001")
    return msg.as_bytes()


def next_months_bill(bill: bytes) -> bytes:
    """The sample bill, moved on a month, so it is one the upload did not already hold."""
    for old, new in ((b"$211.19", b"$198.44"), (b"09/20/2026", b"10/20/2026"), (b"10/16/2026", b"11/16/2026"),
                     (b"synthetic-bill-ready-0001", b"synthetic-bill-ready-0003")):
        bill = bill.replace(old, new)
    return bill


def gas_bill_for_another_account(bill: bytes) -> bytes:
    """The sample bill, made into a gas bill for an account no home has seen."""
    for old, new in ((b"New Electric Bill", b"New Gas Bill"), (b"electric bill", b"gas bill"),
                     (b"******0001", b"******7777"), (b"$211.19", b"$96.40"),
                     (b"synthetic-bill-ready-0001", b"synthetic-bill-ready-0002")):
        bill = bill.replace(old, new)
    return bill


def tiny_png(path: Path) -> None:
    """A real (if dull) PNG, so the browser treats the drop as a scan."""
    width, height = 64, 64
    row = b"\x00" + b"\xf4\xf1\xe8" * width
    raw = row * height

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main() -> None:
    keep = "--keep" in sys.argv
    data_dir = Path(tempfile.mkdtemp(prefix="metermatters-docs-"))
    configure(data_dir)
    sys.path.insert(0, str(ROOT))
    server = serve()
    OUT.mkdir(parents=True, exist_ok=True)
    scratch = data_dir / "scratch"
    scratch.mkdir()
    (scratch / "my-electric-bill.png").write_bytes(b"")
    tiny_png(scratch / "my-electric-bill.png")
    (scratch / "export.csv").write_bytes(UNCLEAR_CSV)

    from playwright.sync_api import sync_playwright

    def shot(page, selector, name, **kw):
        target = page.locator(selector).first
        target.wait_for(state="visible")
        target.screenshot(path=str(OUT / name), **kw)
        print(f"  {name}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1180, "height": 900}, device_scale_factor=2,
                                      color_scheme="light")
        page = context.new_page()

        # -- the account ------------------------------------------------------
        page.goto(f"{BASE}/register")
        page.fill("#name", "Pat Sample")
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        shot(page, ".card.auth", "register.png")
        page.click("button.primary")
        page.wait_for_url(f"{BASE}/connect")

        # -- the Add bills page, fresh --------------------------------------
        address = page.locator("#address").inner_text().strip()
        assert address.startswith("bills-") and address.endswith("@" + DOMAIN), address
        page.screenshot(path=str(OUT / "add-bills.png"), full_page=True)
        print("  add-bills.png")
        shot(page, ".way.featured", "forwarding-card.png")
        page.locator("details.howto").evaluate("d => d.open = true")
        shot(page, ".ways .way:not(.featured)", "upload-card.png")
        page.locator("details.howto").evaluate("d => d.open = false")
        shot(page, "#byhand .card", "byhand.png")
        page.fill("#pl-zip", "01000")
        page.select_option("#pl-state", "MA")
        page.fill("#pl-provider", "Eversource")
        shot(page, "#place .card", "home-card.png")

        # -- an upload: both exports at once --------------------------------
        page.set_input_files("#file", [str(FIXTURES / "account_history_13.csv"),
                                       str(FIXTURES / "usage" / "demo-electric-BillingData.zip"),
                                       str(FIXTURES / "usage" / "demo-gas-BillingData.zip")])
        page.locator("#flash:not([hidden])").wait_for()
        shot(page, "#flash", "upload-done.png")
        page.wait_for_url(f"{BASE}/", timeout=10000)
        page.locator("#fuel-cards .fuel-card").first.wait_for()
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "overview.png"), clip={"x": 0, "y": 0, "width": 1180, "height": 300})
        print("  overview.png")

        # -- Gmail's confirmation -------------------------------------------
        deliver(confirmation(address), address)
        page.goto(f"{BASE}/connect")
        shot(page, ".way.featured", "forwarding-confirm.png")
        page.click(".callout form button")
        page.wait_for_url(f"{BASE}/connect")

        # -- arrivals: a bill, the same bill, and something else ------------
        bill = next_months_bill((FIXTURES / "bill_ready.eml").read_bytes())
        deliver(bill, address)
        deliver(bill, address)
        deliver(from_someone_else(address), address)
        page.goto(f"{BASE}/connect")
        shot(page, "#arrivals", "forwarding-arrivals.png")

        # -- a file it cannot read on its own -------------------------------
        page.set_input_files("#file", str(scratch / "export.csv"))
        page.locator("#mapping:not([hidden])").wait_for()
        shot(page, "#mapping .card", "upload-mapping.png")

        # -- a scan ---------------------------------------------------------
        page.goto(f"{BASE}/connect")
        page.set_input_files("#file", str(scratch / "my-electric-bill.png"))
        page.locator("#scan-result:not([hidden])").wait_for()
        page.wait_for_timeout(400)
        shot(page, "#byhand .card", "scan-result.png")

        # -- a second home, and a bill that fits neither --------------------
        page.goto(f"{BASE}/connect")
        page.locator("details.add-home").evaluate("d => d.open = true")
        page.fill("#home-name", "Lake house")
        page.click("details.add-home button.primary")
        page.wait_for_url("**/connect")
        page.goto(f"{BASE}/connect")
        shot(page, "#homes .card", "homes-card.png")
        deliver(gas_bill_for_another_account(bill), address)
        page.goto(f"{BASE}/connect")
        shot(page, "#held .card", "held-bill.png")

        browser.close()
    server.shutdown()
    if keep:
        print(f"data left in {data_dir}")
    else:
        shutil.rmtree(data_dir, ignore_errors=True)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
