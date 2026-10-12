"""Tests for more than one home: the registry, the addresses, and keeping each home's bills apart."""

import io
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from metermatters.store import accounts
from metermatters.intake import billmail
from metermatters.store import homes
from tests.support import HostedCase

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

HISTORY = """Account# or Nickname,Account Type,Pay/Due Date,Type,Amount,Status,
{acct}&lt;br&gt;(electric10),Electric,2026/03/20,Current Bill,${a},-
{acct}&lt;br&gt;(electric10),Electric,2026/02/16,Current Bill,${b},-
"""


def history(acct, a="300.00", b="400.00"):
    return HISTORY.format(acct=acct, a=a, b=b)


def notice(suffix, amount=100.0, fuel="electric", due=date(2026, 10, 16)):
    return billmail.BillNotice(amount=amount, due=due, account_suffix=suffix, fuel=fuel,
                               subject="Your bill is ready")


class Tree(unittest.TestCase):
    """A bare user folder, no web app."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = accounts.Paths.under(self.root)

    def give(self, home, acct, **amounts):
        paths = homes.paths_for(self.base, home)
        paths.csv.parent.mkdir(parents=True, exist_ok=True)
        paths.csv.write_text(history(acct, **amounts), encoding="utf-8")
        return paths


class TestRegistry(Tree):
    def test_everyone_starts_with_one_home_and_nothing_on_disk(self):
        self.assertEqual([h.name for h in homes.active(self.base)], [homes.DEFAULT_NAME])
        self.assertFalse(homes.several(self.base))
        self.assertFalse((self.root / "data" / "homes.json").exists())

    def test_the_first_home_is_the_users_own_files(self):
        self.assertEqual(homes.paths_for(self.base, homes.first(self.base)), self.base)

    def test_another_home_is_a_folder_laid_out_the_same_way(self):
        lake = homes.add(self.base, "Lake house")
        paths = homes.paths_for(self.base, lake)
        self.assertEqual(paths.csv, self.root / "homes" / lake.id / "AccountHistory.csv")
        self.assertEqual(paths.state, self.root / "homes" / lake.id / "data" / "state.json")
        self.assertTrue(homes.several(self.base))

    def test_the_address_comes_from_the_name_and_is_unique(self):
        a = homes.add(self.base, "Lake house!")
        homes.delete(self.base, a.id, "Lake house!")
        b = homes.add(self.base, "Lake  house")
        self.assertEqual(a.slug, "lake-house")
        self.assertEqual(b.slug, "lake-house-2")        # the deleted one still holds its address
        self.assertEqual(a.prefix, "/home/lake-house")
        self.assertEqual(homes.first(self.base).prefix, "")

    def test_names_are_checked(self):
        homes.add(self.base, "Cabin")
        for bad in ("", "   ", "x" * 61, "cabin", "My home"):
            with self.subTest(bad), self.assertRaises(homes.HomeError):
                homes.add(self.base, bad)

    def test_there_is_a_limit(self):
        for n in range(homes.MAX_HOMES - 1):
            homes.add(self.base, f"Home {n}")
        with self.assertRaisesRegex(homes.HomeError, "most homes"):
            homes.add(self.base, "One too many")

    def test_renaming_keeps_the_address_so_links_still_work(self):
        cabin = homes.add(self.base, "Cabin")
        again = homes.rename(self.base, cabin.id, "Mountain cabin")
        self.assertEqual((again.name, again.slug), ("Mountain cabin", "cabin"))
        self.assertEqual(homes.rename(self.base, homes.MAIN, "Main St").name, "Main St")
        self.assertEqual(homes.first(self.base).name, "Main St")

    def test_a_home_found_by_address_is_one_that_exists(self):
        cabin = homes.add(self.base, "Cabin")
        self.assertEqual(homes.find(self.base, "cabin"), cabin)
        self.assertIsNone(homes.find(self.base, "nowhere"))
        self.assertIsNone(homes.find(self.base, ""))


class TestDeleting(Tree):
    def setUp(self):
        super().setUp()
        self.cabin = homes.add(self.base, "Cabin", NOW)
        self.give(self.cabin, "55500020002")

    def test_the_name_has_to_be_typed(self):
        for typed in ("", "cab", "yes", None):
            with self.subTest(typed), self.assertRaisesRegex(homes.HomeError, "exactly"):
                homes.delete(self.base, self.cabin.id, typed, NOW)
        self.assertEqual(len(homes.active(self.base)), 2)

    def test_typing_it_in_any_case_is_enough(self):
        homes.delete(self.base, self.cabin.id, "  CABIN ", NOW)
        self.assertEqual([h.name for h in homes.active(self.base)], [homes.DEFAULT_NAME])

    def test_the_first_home_cannot_be_deleted(self):
        with self.assertRaisesRegex(homes.HomeError, "first home"):
            homes.delete(self.base, homes.MAIN, homes.DEFAULT_NAME, NOW)

    def test_deleting_hides_a_home_but_keeps_its_bills(self):
        homes.delete(self.base, self.cabin.id, "Cabin", NOW)
        self.assertIsNone(homes.find(self.base, "cabin"))
        self.assertTrue(homes.paths_for(self.base, self.cabin).csv.exists())
        gone, = homes.deleted(self.base)
        self.assertEqual(gone.purge_on(), (NOW + timedelta(days=30)).date())

    def test_it_can_be_restored_with_everything_in_it(self):
        homes.delete(self.base, self.cabin.id, "Cabin", NOW)
        back = homes.restore(self.base, self.cabin.id, NOW + timedelta(days=29))
        self.assertEqual(homes.find(self.base, "cabin"), back)
        self.assertIn("55500020002", homes.paths_for(self.base, back).csv.read_text())

    def test_after_thirty_days_it_is_gone_for_good(self):
        homes.delete(self.base, self.cabin.id, "Cabin", NOW)
        folder = self.root / "homes" / self.cabin.id
        self.assertEqual(homes.purge(self.base, NOW + timedelta(days=29, hours=23)), [])
        self.assertTrue(folder.exists())
        self.assertEqual(homes.purge(self.base, NOW + timedelta(days=30)), [self.cabin.id])
        self.assertFalse(folder.exists())
        with self.assertRaisesRegex(homes.HomeError, "can't be restored"):
            homes.restore(self.base, self.cabin.id, NOW + timedelta(days=31))
        self.assertEqual(homes.deleted(self.base), [])

    def test_purging_touches_only_what_has_expired(self):
        other = homes.add(self.base, "Flat", NOW)
        homes.delete(self.base, self.cabin.id, "Cabin", NOW)
        homes.purge(self.base, NOW + timedelta(days=45))
        self.assertEqual([h.id for h in homes.active(self.base)], [homes.MAIN, other.id])

    def test_a_name_that_has_since_been_taken_is_not_restored_over(self):
        homes.delete(self.base, self.cabin.id, "Cabin", NOW)
        homes.add(self.base, "Cabin", NOW)
        with self.assertRaisesRegex(homes.HomeError, "already have"):
            homes.restore(self.base, self.cabin.id, NOW)


class TestRouting(Tree):
    def setUp(self):
        super().setUp()
        self.main = homes.first(self.base)
        self.give(self.main, "55500010001")
        self.cabin = homes.add(self.base, "Cabin")
        self.give(self.cabin, "55500020002")

    def test_with_one_home_every_bill_goes_there_untouched(self):
        solo = accounts.Paths.under(self.root / "solo")
        placed, held = homes.route(solo, [notice("9999"), notice(None)])
        self.assertEqual(list(placed), [homes.MAIN])
        self.assertEqual((len(placed[homes.MAIN]), held), (2, []))
        self.assertFalse((self.root / "solo").exists())

    def test_a_bill_goes_to_the_home_that_holds_its_account(self):
        placed, held = homes.route(self.base, [notice("0001"), notice("0002"), notice("0002", 5)])
        self.assertEqual({k: len(v) for k, v in placed.items()},
                         {homes.MAIN: 1, self.cabin.id: 2})
        self.assertEqual(held, [])

    def test_a_bill_for_an_account_no_home_has_seen_is_held_not_guessed(self):
        placed, held = homes.route(self.base, [notice("7777"), notice(None)])
        self.assertEqual(placed, {})
        self.assertEqual(len(held), 2)

    def test_an_account_two_homes_claim_is_held_too(self):
        self.give(self.cabin, "55500010001")        # the same account in both
        _, held = homes.route(self.base, [notice("0001")])
        self.assertEqual(len(held), 1)

    def test_filing_puts_each_bill_in_its_own_homes_history(self):
        done = homes.file_notices(self.base, [notice("0001", 111.0), notice("0002", 222.0),
                                              notice("7777", 333.0)])
        self.assertEqual((done.added, done.held), (2, 1))
        self.assertIn("111.00", homes.paths_for(self.base, self.main).csv.read_text())
        self.assertNotIn("222.00", homes.paths_for(self.base, self.main).csv.read_text())
        self.assertIn("222.00", homes.paths_for(self.base, self.cabin).csv.read_text())
        self.assertNotIn("333.00", homes.paths_for(self.base, self.cabin).csv.read_text())

    def test_a_held_bill_waits_until_placed_and_is_not_held_twice(self):
        homes.file_notices(self.base, [notice("7777", 333.0)])
        homes.file_notices(self.base, [notice("7777", 333.0)])
        waiting, = homes.pending(self.base)
        self.assertEqual((waiting["account_suffix"], waiting["amount"]), ("7777", 333.0))
        homes.place(self.base, waiting["id"], self.cabin.id)
        self.assertIn("333.00", homes.paths_for(self.base, self.cabin).csv.read_text())
        self.assertEqual(homes.pending(self.base), [])
        with self.assertRaisesRegex(homes.HomeError, "already been dealt with"):
            homes.place(self.base, waiting["id"], self.cabin.id)

    def test_a_held_bill_can_be_dropped(self):
        homes.file_notices(self.base, [notice("7777")])
        homes.dismiss(self.base, homes.pending(self.base)[0]["id"])
        self.assertEqual(homes.pending(self.base), [])

    def test_a_bill_cannot_be_placed_in_a_deleted_home(self):
        homes.file_notices(self.base, [notice("7777")])
        homes.delete(self.base, self.cabin.id, "Cabin")
        with self.assertRaisesRegex(homes.HomeError, "no such home"):
            homes.place(self.base, homes.pending(self.base)[0]["id"], self.cabin.id)

    def test_a_bill_of_unknown_fuel_is_counted_skipped_never_held(self):
        done = homes.file_notices(self.base, [notice("7777", fuel="unknown")])
        self.assertEqual((done.skipped, done.held), (1, 0))
        self.assertEqual(homes.pending(self.base), [])


class WebCase(HostedCase):
    def setUp(self):
        super().setUp()
        self.user = self.sign_up()
        self.base = self.user.paths
        self.base.csv.parent.mkdir(parents=True, exist_ok=True)
        self.base.csv.write_text(history("55500010001"), encoding="utf-8")

    def add_home(self, name="Cabin", client=None):
        return (client or self.client).post("/homes/add", data={"name": name})

    def report(self, prefix="", client=None):
        res = (client or self.client).get(f"{prefix}/api/report?fuel=Electric")
        self.assertEqual(res.status_code, 200)
        return res.get_json()


class TestOneHome(WebCase):
    """The person with one house must not be able to tell any of this exists."""

    def test_no_switcher_and_no_combined_view(self):
        for page in ("/", "/electric", "/raw", "/rates", "/connect"):
            with self.subTest(page):
                html = self.client.get(page).get_data(as_text=True)
                self.assertNotIn("home-switch", html)
                self.assertNotIn("All homes", html)
        res = self.client.get("/homes")
        self.assertEqual((res.status_code, res.headers["Location"]), (302, "/"))
        self.assertEqual(self.client.get("/api/homes/overview").status_code, 404)

    def test_a_home_with_only_electric_bills_gets_one_card_not_two(self):
        cards = self.client.get("/api/overview").get_json()["fuels"]
        self.assertEqual([c["fuel"] for c in cards], ["Electric"])

    def test_the_connect_page_offers_only_a_quiet_way_to_add_another(self):
        html = self.client.get("/connect").get_data(as_text=True)
        self.assertIn("Add another home", html)
        self.assertNotIn("Your homes", html)
        self.assertNotIn("Adding to", html)

    def test_their_data_is_where_it_always_was(self):
        self.assertEqual(len(self.report()["series"]), 2)
        self.assertEqual(self.base.csv.read_text(), history("55500010001"))
        self.assertFalse((self.base.state.parent / "homes.json").exists())

    def test_a_home_address_without_a_trailing_slash_is_the_home(self):
        self.client.post("/homes/add", data={"name": "Cabin"})
        res = self.client.get("/home/cabin")
        self.assertIn(res.status_code, (200, 302))
        self.assertNotEqual(res.status_code, 404)

    def test_the_address_is_case_sensitive_and_bounded(self):
        self.client.post("/homes/add", data={"name": "Cabin"})
        self.assertEqual(self.client.get("/home/CABIN/electric").status_code, 404)
        self.assertEqual(self.client.get("/home/" + "a" * 60 + "/electric").status_code, 404)

    def test_the_machine_endpoints_are_the_same_under_a_home_prefix(self):
        # Each checks a credential of its own; a prefix must not open a way round that.
        self.assertEqual(self.client.post("/home/anything/api/tasks/check").status_code, 404)

    def test_an_address_for_a_home_that_does_not_exist_is_not_found(self):
        self.assertEqual(self.client.get("/home/nowhere/").status_code, 404)
        self.assertEqual(self.client.get("/home/nowhere/api/report").status_code, 404)

    def test_the_links_in_the_pages_are_the_plain_ones(self):
        html = self.client.get("/electric").get_data(as_text=True)
        self.assertIn('data-base=""', html)
        self.assertIn('href="/raw"', html)
        self.assertIn('href="/rates"', html)


class TestAddingAHome(WebCase):
    def test_adding_goes_straight_to_where_its_bills_come_in(self):
        res = self.add_home("Lake house")
        self.assertEqual((res.status_code, res.headers["Location"]), (302, "/home/lake-house/connect"))

    def test_a_bad_name_says_so_and_adds_nothing(self):
        res = self.add_home("   ")
        self.assertEqual(res.headers["Location"], "/connect#homes")
        self.assertIn("Give the home a name", self.client.get("/connect").get_data(as_text=True))
        self.assertFalse(homes.several(self.base))

    def test_a_second_home_brings_the_switcher_everywhere(self):
        self.add_home("Cabin")
        for page in ("/", "/electric", "/raw", "/rates", "/connect", "/homes"):
            with self.subTest(page):
                html = self.client.get(page).get_data(as_text=True)
                self.assertIn('id="home-switch"', html)
                self.assertIn("All homes", html)
                self.assertIn("Cabin", html)

    def test_the_switcher_keeps_the_page_you_are_on(self):
        self.add_home("Cabin")
        html = self.client.get("/electric").get_data(as_text=True)
        self.assertIn('value="/home/cabin/electric"', html)
        html = self.client.get("/home/cabin/electric").get_data(as_text=True)
        self.assertIn('value="/electric"', html)
        self.assertIn('data-base="/home/cabin"', html)

    def test_links_inside_a_home_carry_its_prefix(self):
        self.add_home("Cabin")
        html = self.client.get("/home/cabin/connect").get_data(as_text=True)
        self.assertIn('action="/home/cabin/homes/add"', html)
        self.assertIn("Adding to <strong>Cabin</strong>", html)
        # and what the overview hands the page to link to
        self.base_for_cabin_bills()
        overview = self.client.get("/home/cabin/api/overview").get_json()
        self.assertEqual(overview["fuels"][0]["href"], "/home/cabin/electric")

    def base_for_cabin_bills(self):
        cabin = homes.find(self.base, "cabin")
        paths = homes.paths_for(self.base, cabin)
        paths.csv.parent.mkdir(parents=True, exist_ok=True)
        paths.csv.write_text(history("55500020002"), encoding="utf-8")

    def test_signed_out_you_are_sent_back_to_the_home_you_asked_for(self):
        self.add_home("Cabin")
        from metermatters.web import app as webapp
        res = webapp.app.test_client().get("/home/cabin/electric")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["Location"], "/home/cabin/login?next=/home/cabin/electric")


class TestKeepingHomesApart(WebCase):
    def setUp(self):
        super().setUp()
        self.add_home("Cabin")
        self.cabin = homes.find(self.base, "cabin")
        self.cabin_paths = homes.paths_for(self.base, self.cabin)

    def test_a_new_home_starts_empty(self):
        self.assertIn("No bills", self.report("/home/cabin")["error"])
        self.assertEqual(len(self.report()["series"]), 2)

    def test_what_is_uploaded_goes_only_into_the_home_it_was_uploaded_from(self):
        res = self.client.post("/home/cabin/api/upload", content_type="multipart/form-data",
                               data={"file": [(io.BytesIO(history("55500020002", "50.00", "60.00").encode()),
                                               "AccountHistory.csv")]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual([r["amount"] for r in self.report("/home/cabin")["series"]], [60.0, 50.0])
        self.assertEqual([r["amount"] for r in self.report()["series"]], [400.0, 300.0])
        self.assertNotIn("55500020002", self.base.csv.read_text())
        self.assertNotIn("55500010001", self.cabin_paths.csv.read_text())

    def test_each_home_keeps_its_own_settings(self):
        import json
        self.client.post("/home/cabin/api/settings", json={"zip": "01000"})
        self.assertEqual(json.loads(self.cabin_paths.state.read_text())["zip"], "01000")
        self.assertIsNone(self.report()["settings"]["zip"])
        self.assertEqual(self.report()["settings"]["zip"], None)

    def test_another_users_address_is_not_found(self):
        from metermatters.web import app as webapp
        other = webapp.app.test_client()
        self.sign_up("bea@example.com", client=other)
        self.assertEqual(other.get("/home/cabin/electric").status_code, 404)
        self.assertEqual(other.get("/home/cabin/api/report").status_code, 404)

    def test_a_deleted_home_is_not_found_until_restored(self):
        res = self.client.post(f"/homes/{self.cabin.id}/delete", data={"confirm": "Cabin"})
        self.assertEqual(res.headers["Location"], "/connect#homes")
        self.assertEqual(self.client.get("/home/cabin/electric").status_code, 404)
        self.assertNotIn('id="home-switch"', self.client.get("/electric").get_data(as_text=True))
        self.assertIn("Restore", self.client.get("/connect").get_data(as_text=True))
        self.client.post(f"/homes/{self.cabin.id}/restore")
        self.assertEqual(self.client.get("/home/cabin/").status_code, 302)   # no bills: to Add bills

    def test_deleting_with_the_wrong_name_does_nothing(self):
        self.client.post(f"/homes/{self.cabin.id}/delete", data={"confirm": "cabin house"})
        self.assertEqual(self.client.get("/home/cabin/connect").status_code, 200)
        self.assertEqual(len(homes.deleted(self.base)), 0)

    def test_the_first_home_cannot_be_deleted_from_the_web_either(self):
        self.client.post(f"/homes/{homes.MAIN}/delete", data={"confirm": homes.DEFAULT_NAME})
        self.assertEqual(len(homes.active(self.base)), 2)

    def test_renaming_from_the_page(self):
        self.client.post(f"/homes/{self.cabin.id}/rename", data={"name": "Mountain cabin"})
        self.assertIn("Mountain cabin", self.client.get("/electric").get_data(as_text=True))
        self.assertEqual(self.client.get("/home/cabin/connect").status_code, 200)

    def test_deleting_the_account_takes_every_home(self):
        from metermatters.store import accounts
        folder = self.user.root
        self.cabin_paths.csv.parent.mkdir(parents=True, exist_ok=True)
        self.cabin_paths.csv.write_text(history("55500020002"), encoding="utf-8")
        self.assertTrue((folder / "homes").exists())
        accounts.delete(self.user)
        self.assertFalse(folder.exists())


class TestAllHomes(WebCase):
    def setUp(self):
        super().setUp()
        self.add_home("Cabin")
        cabin = homes.find(self.base, "cabin")
        paths = homes.paths_for(self.base, cabin)
        paths.csv.parent.mkdir(parents=True, exist_ok=True)
        paths.csv.write_text(history("55500020002", "50.00", "60.00"), encoding="utf-8")

    def combined(self):
        res = self.client.get("/api/homes/overview")
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def test_totals_are_the_homes_added_up(self):
        d = self.combined()
        by = {h["name"]: h for h in d["homes"]}
        self.assertEqual(by["My home"]["total_12"], 700.0)
        self.assertEqual(by["Cabin"]["total_12"], 110.0)
        self.assertEqual(d["totals"]["total_12"], 810.0)
        self.assertEqual(d["totals"]["by_fuel"]["Electric"]["total_12"], 810.0)

    def test_it_is_the_same_numbers_each_homes_own_overview_shows(self):
        d = self.combined()
        own = self.client.get("/home/cabin/api/overview").get_json()
        mine = {h["name"]: h for h in d["homes"]}["Cabin"]["fuels"][0]
        self.assertEqual(mine["latest"], own["fuels"][0]["latest"]["amount"])

    def test_each_home_links_to_its_own_pages(self):
        d = self.combined()
        by = {h["name"]: h for h in d["homes"]}
        self.assertEqual(by["My home"]["url"], "/")
        self.assertEqual(by["Cabin"]["url"], "/home/cabin")
        self.assertEqual(by["Cabin"]["fuels"][0]["href"], "/home/cabin/electric")
        self.assertEqual(by["My home"]["fuels"][0]["href"], "/electric")

    def test_the_monthly_chart_splits_spend_by_home(self):
        rows = {m["period"]: m["values"] for m in self.combined()["monthly"]}
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(v) == 2 for v in rows.values()))

    def test_a_home_with_no_bills_is_still_listed(self):
        self.add_home("Flat")
        by = {h["name"]: h for h in self.combined()["homes"]}
        self.assertFalse(by["Flat"]["has_bills"])
        self.assertEqual(by["Flat"]["total_12"], 0)

    def test_the_page_renders_and_the_address_is_not_nested(self):
        self.assertEqual(self.client.get("/homes").status_code, 200)
        res = self.client.get("/home/cabin/homes")
        self.assertEqual((res.status_code, res.headers["Location"]), (302, "/homes"))

    def test_deleted_homes_are_left_out_of_the_totals(self):
        cabin = homes.find(self.base, "cabin")
        self.client.post(f"/homes/{cabin.id}/delete", data={"confirm": "Cabin"})
        self.assertEqual(self.client.get("/api/homes/overview").status_code, 404)


class TestBillsThatArriveForNoHome(WebCase):
    def setUp(self):
        super().setUp()
        self.add_home("Cabin")
        self.cabin = homes.find(self.base, "cabin")

    def test_a_held_bill_is_offered_for_placing_and_then_filed(self):
        homes.file_notices(self.base, [notice("7777", 123.0)])
        html = self.client.get("/connect").get_data(as_text=True)
        self.assertIn("1 bill waiting for a home", html)
        pending, = homes.pending(self.base)
        self.client.post(f"/homes/bills/{pending['id']}", data={"home": self.cabin.id})
        self.assertNotIn("waiting for a home", self.client.get("/connect").get_data(as_text=True))
        self.assertIn("123.00", homes.paths_for(self.base, self.cabin).csv.read_text())

    def test_it_can_be_dropped(self):
        homes.file_notices(self.base, [notice("7777")])
        pending, = homes.pending(self.base)
        self.client.post(f"/homes/bills/{pending['id']}/dismiss")
        self.assertEqual(homes.pending(self.base), [])


if __name__ == "__main__":
    unittest.main()
