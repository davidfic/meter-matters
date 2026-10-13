"""The documentation pages: open to anyone, every page renders, every screenshot
and every link in them exists, and the app points at them where it should."""

import re
import unittest
from pathlib import Path

from markupsafe import escape

from metermatters.web import docs
from tests.support import HostedCase

STATIC = Path(__file__).resolve().parent.parent / "metermatters" / "web" / "static"
ALL_PATHS = ["/docs"] + [f"/docs/{p.slug}" for p in docs.PAGES]


def links_in(html: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', html)


def images_in(html: str) -> list[str]:
    return re.findall(r'<img[^>]+src="([^"]+)"', html)


class TestOpenToAnyone(HostedCase):
    def test_every_page_renders_without_signing_in(self):
        for path in ALL_PATHS:
            with self.subTest(path):
                res = self.client.get(path)
                self.assertEqual(res.status_code, 200)
                self.assertIn("Meter Matters", res.get_data(as_text=True))

    def test_a_page_that_does_not_exist_is_a_404_not_a_crash(self):
        self.assertEqual(self.client.get("/docs/nope").status_code, 404)
        self.assertEqual(self.client.get("/docs/../connect").status_code, 404)

    def test_signed_out_it_offers_sign_in_and_no_add_bills(self):
        page = self.client.get("/docs/forwarding").get_data(as_text=True)
        self.assertIn('href="/login"', page)
        self.assertNotIn("/connect", page)

    def test_signed_in_the_same_page_links_into_the_app(self):
        self.sign_up()
        page = self.client.get("/docs/forwarding").get_data(as_text=True)
        self.assertIn('href="/connect"', page)
        self.assertIn("Sign out", page)


class TestNothingDangles(HostedCase):
    def test_every_screenshot_exists(self):
        for path in ALL_PATHS:
            html = self.client.get(path).get_data(as_text=True)
            for src in images_in(html):
                if "/static/docs/" not in src:
                    continue
                with self.subTest(f"{path} -> {src}"):
                    name = src.split("/static/")[1]
                    self.assertTrue((STATIC / name).is_file(), f"{name} is missing; run scripts/docs_screenshots.py")
                    self.assertGreater((STATIC / name).stat().st_size, 1000)

    def test_every_link_between_pages_resolves(self):
        for path in ALL_PATHS:
            html = self.client.get(path).get_data(as_text=True)
            for href in links_in(html):
                if not href.startswith("/docs"):
                    continue
                target, _, anchor = href.partition("#")
                with self.subTest(f"{path} -> {href}"):
                    page = self.client.get(target)
                    self.assertEqual(page.status_code, 200)
                    if anchor:
                        self.assertIn(f'id="{anchor}"', page.get_data(as_text=True))

    def test_the_overview_lists_every_page(self):
        html = self.client.get("/docs").get_data(as_text=True)
        for page in docs.PAGES:
            self.assertIn(f'href="/docs/{page.slug}"', html)
            self.assertIn(str(escape(page.title)), html)

    def test_the_sidebar_marks_the_current_page(self):
        html = self.client.get("/docs/upload").get_data(as_text=True)
        self.assertIn('href="/docs/upload" aria-current="page"', html)
        self.assertNotIn('href="/docs/start" aria-current="page"', html)


class TestTheAppPointsAtThem(HostedCase):
    env = {"ELEC_INBOUND_DOMAIN": "in.example.com", "ELEC_INBOUND_SECRET": "s3cret-hook"}

    def test_the_sign_in_page_offers_the_guide_before_an_account_exists(self):
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn('href="/docs"', html)
        self.assertIn('href="/docs/security"', html)

    def test_add_bills_links_each_card_to_its_page(self):
        self.sign_up()
        html = self.client.get("/connect").get_data(as_text=True)
        for slug in ("start", "upload", "forwarding", "by-hand", "security", "homes"):
            with self.subTest(slug):
                self.assertIn(f'href="/docs/{slug}', html)

    def test_every_dashboard_has_a_docs_button(self):
        self.sign_up()
        for path in ("/", "/electric", "/rates", "/raw", "/year"):
            with self.subTest(path):
                res = self.client.get(path, follow_redirects=True)
                self.assertIn('href="/docs"', res.get_data(as_text=True))

    def test_the_empty_state_points_at_getting_started(self):
        self.sign_up()
        html = self.client.get("/electric").get_data(as_text=True)
        self.assertIn('href="/docs/start"', html)
        self.assertNotIn("Gmail", html)       # connecting Gmail is gone; the page must not promise it


class TestOnOnePersonsMachine(HostedCase):
    multi_user = False

    def test_the_docs_are_there_too(self):
        for path in ALL_PATHS:
            with self.subTest(path):
                res = self.client.get(path)
                self.assertEqual(res.status_code, 200)
        html = self.client.get("/docs/forwarding").get_data(as_text=True)
        self.assertNotIn('href="/connect"', html)   # there is no Add bills page in this mode
        self.assertNotIn('href="/login"', html)


if __name__ == "__main__":
    unittest.main()
