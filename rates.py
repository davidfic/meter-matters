"""What electricity supply costs around you, and how it compares with what you pay.

A Massachusetts electric bill is two prices added together. *Delivery* - the
wires, the poles, the meter - is set by the utility and is the same whoever
you buy from. *Supply* - the electricity itself - is the part you can shop for:
the utility's Basic Service, your town's community aggregation, or a
competitive supplier. Your statement shows one all-in number; the offers are
quoted for supply alone. So comparing them is a matter of changing the supply
half and leaving the rest, and that is what ``compare`` does.

The offers come from the state's own comparison site, Energy Switch
Massachusetts (energyswitchma.gov), which is run by the Department of Public
Utilities. It has no published API; this reads the same JSON its own pages use.
Set ``ELEC_RATES=off`` to switch it off - everything about your own rate still
works, only the comparison goes.

What leaves the server: a ZIP code, and a fixed 600 kWh, which the site wants
in order to put a monthly figure on each offer and which we ignore. Never your
usage, and never an address. Offers are shared, not per user - they are the
same for everyone in a ZIP code - so they are kept in the database and asked
for again only when a day has passed.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from statistics import median

import db

BASE_URL = "https://www.energyswitchma.gov"
SOURCE = {"text": "Energy Switch Massachusetts", "href": BASE_URL}
TIMEOUT = 10
FRESH_FOR = timedelta(hours=24)
# Asked for on a failure no more often than this, so an outage isn't hammered.
RETRY_AFTER = timedelta(minutes=30)
RESIDENTIAL = 1
PLACEHOLDER_KWH = 600
ZIP = re.compile(r"^\d{5}$")


class Unavailable(Exception):
    """No offers, for a reason worth telling the user. Never fatal."""


class NotCovered(Unavailable):
    """There is no comparison for this place, which is not the same as it being down."""


# Where a supplier comparison exists. Another state is another reader, written
# against that state's own comparison site, and one line here.
COVERED = {"MA"}


def covers(state: str | None) -> bool:
    """Does a comparison exist where this home is? Not known where: then no - a
    ZIP code is only ever sent to a site that is for its state."""
    return bool(state) and state.upper() in COVERED


def enabled() -> bool:
    return os.environ.get("ELEC_RATES", "energyswitchma").strip().lower() not in (
        "", "0", "off", "no", "none", "false")


def _post(path: str, body: dict) -> object:
    request = urllib.request.Request(
        BASE_URL + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "meter-matters"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unavailable(f"the comparison site didn't answer ({type(exc).__name__})") from None


# --------------------------------------------------------------------------
# Reading the site's answer
# --------------------------------------------------------------------------

def _cents(text) -> float | None:
    """'17.323 ¢/kWh|TBD' -> 17.323. The first figure is the one in force."""
    found = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    return float(found.group(1)) if found else None


def _money(text) -> float | None:
    found = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    return float(found.group(1)) if found else None


def _offer(row: dict) -> dict | None:
    cents = _cents(row.get("pricePerUnit"))
    if cents is None:
        return None
    if row.get("rowType") == "DistributionCompany":
        kind = "basic"
    elif (row.get("isMunicipalAggregationDefault") or row.get("municipalAggregationTown")
          or re.match(r"(city|town) of ", str(row.get("supplierName") or ""), re.I)):
        kind = "municipal"
    else:
        kind = "supplier"
    term = str(row.get("contractTerm") or "").strip(" |")
    months = re.match(r"(\d+)\s+month", term)
    fee = str(row.get("earlyTerminationDetail") or "")
    renewable = row.get("renewableEnergyProductPercentage")
    return {
        "id": row.get("supplierProductId"),
        "kind": kind,
        "default": bool(row.get("isMunicipalAggregationDefault")) or kind == "basic",
        "supplier": row.get("supplierName") or "",
        "cents": cents,
        "structure": row.get("pricingStructureDescription") or None,
        "term": term.split("|")[0].strip() or None,
        "term_months": int(months.group(1)) if months else None,
        "ends": (row.get("contractTermEndDate") or "").strip() or None,
        "exit_fee": _money(fee) if "cancellation fee:" in fee.lower() else (
            0.0 if "no cancellation" in fee.lower() else None),
        "auto_renews": bool(row.get("hasAutomaticRenewal")),
        "renewable_pct": float(renewable) if renewable is not None else None,
        "new_customers_only": bool(row.get("isNewCustomerOnly")),
        "monthly_fee": _money(row.get("pricePerMonth")) or 0.0,
        "enrollment_fee": _money(row.get("enrollmentFee")) or 0.0,
        "extras": (row.get("otherProductServices") or "").strip() or None,
        "url": row.get("productWebsiteUrl") or row.get("supplierWebsiteUrl") or None,
        "phone": row.get("productPhone") or row.get("phone") or None,
    }


def parse(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        raise Unavailable("the comparison site sent something unreadable")
    offers = [o for o in (_offer(r) for r in rows if isinstance(r, dict)) if o]
    if not offers:
        raise Unavailable("the comparison site listed no offers")
    return offers


def _fetch(zipcode: str) -> list[dict]:
    where = _post("/consumers/distributioncompaniesbyzipcode",
                  {"zipCode": zipcode, "customerClassId": RESIDENTIAL})
    companies = (where or {}).get("distributionCompanies") if isinstance(where, dict) else None
    if not companies:
        raise NotCovered(f"{zipcode} isn't a Massachusetts ZIP code the comparison site knows")
    company = companies[0]
    body = {"zipCode": zipcode, "customerClassId": RESIDENTIAL,
            "distributionCompanyId": company["distributionCompanyId"],
            "monthlyUsage": PLACEHOLDER_KWH}
    town = (where.get("towns") or [{}])[0].get("townCode")
    if town:
        body["town"] = town
    offers = parse(_post("/consumers/compare", body))
    for offer in offers:
        offer["utility"] = company.get("distributionCompanyName")
    return offers


# --------------------------------------------------------------------------
# Keeping it
# --------------------------------------------------------------------------

def offers_for(zipcode: str, now: datetime | None = None) -> dict:
    """The offers for a ZIP code: ``{"offers": [...], "fetched_at": iso}``.

    A day-old answer is used without asking. If asking fails, whatever is held
    is returned, however old; only with nothing held is it an error.
    """
    zipcode = (zipcode or "").strip()
    if not ZIP.match(zipcode):
        raise Unavailable("that isn't a five-digit ZIP code")
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM supply_offers WHERE zip = ?", (zipcode,)).fetchone()
    held = None
    if row:
        try:
            held = {"offers": json.loads(row["payload"]), "fetched_at": row["fetched_at"]}
            age = now - datetime.fromisoformat(row["fetched_at"])
            backing_off = bool(row["failed_at"]) and \
                now - datetime.fromisoformat(row["failed_at"]) < RETRY_AFTER
            if held["offers"] and (age < FRESH_FOR or backing_off):
                return held
            if backing_off:
                raise Unavailable("the comparison site didn't answer a moment ago")
        except (ValueError, TypeError):
            held = None

    try:
        offers = _fetch(zipcode)
    except NotCovered:
        raise                      # a place with no comparison isn't an outage; don't back off
    except Unavailable:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO supply_offers (zip, payload, fetched_at, failed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (zip) DO UPDATE SET failed_at = excluded.failed_at",
                (zipcode, "[]", "1970-01-01T00:00:00+00:00", now.isoformat(timespec="seconds")))
        if held and held["offers"]:
            return held
        raise
    stamp = now.isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO supply_offers (zip, payload, fetched_at, failed_at) VALUES (?, ?, ?, NULL) "
            "ON CONFLICT (zip) DO UPDATE SET payload = excluded.payload, "
            "fetched_at = excluded.fetched_at, failed_at = NULL",
            (zipcode, json.dumps(offers), stamp))
    return {"offers": offers, "fetched_at": stamp}


# --------------------------------------------------------------------------
# Comparing
# --------------------------------------------------------------------------

def _last_year(series: list[dict]) -> tuple[float, int]:
    """kWh over the most recent twelve statements that have usage, and how many."""
    rows = [r for r in series if r.get("kwh")][-12:]
    return sum(r["kwh"] for r in rows), len(rows)


def default_supply(offers: list[dict]) -> dict | None:
    """What a household that never chose anything is on.

    A town with community aggregation puts everyone in it on that supply
    unless they opt out; otherwise it is the utility's Basic Service.
    """
    for offer in offers:
        if offer["kind"] == "municipal" and offer.get("default"):
            return offer
    return next((o for o in offers if o["kind"] == "basic"), None)


def compare(series: list[dict], offers: list[dict], supply_cents: float | None) -> dict:
    """Every offer, priced against the supply the household is on now.

    ``supply_cents`` is what the user says their supply is, or None for the
    default. Delivery is not touched: an offer saves exactly the difference
    in supply price over the usage, less its own fees.
    """
    kwh, months = _last_year(series)
    mine = default_supply(offers)
    chosen = supply_cents is not None
    cents = supply_cents if chosen else (mine["cents"] if mine else None)
    if cents is None or not kwh:
        return {"offers": [], "kwh": kwh, "months": months, "supply": None}

    rows = []
    for offer in offers:
        # A monthly fee is owed once per statement, so it scales with the same
        # months the usage covers.
        fees = offer["monthly_fee"] * months + offer["enrollment_fee"]
        rows.append({**offer, "change": round((offer["cents"] - cents) / 100 * kwh + fees, 2),
                     "yearly": round(offer["cents"] / 100 * kwh + fees, 2),
                     "current": bool(not chosen and mine and offer["id"] == mine["id"])})
    rows.sort(key=lambda r: (r["cents"], r["supplier"]))

    # Everything you could move to: the utility's own rate and the program
    # you're already on aren't "other offers".
    suppliers = [r["cents"] for r in rows if r["kind"] != "basic" and not r["current"]]
    return {
        "offers": rows,
        "kwh": round(kwh, 1),
        "months": months,
        "supply": {"cents": cents, "id": mine["id"] if mine and not chosen else None, "name": mine["supplier"] if mine and not chosen else None,
                   "assumed": not chosen,
                   "kind": mine["kind"] if mine and not chosen else "chosen"},
        "market": {"count": len(suppliers),
                   "min": min(suppliers) if suppliers else None,
                   "median": round(median(suppliers), 3) if suppliers else None,
                   "max": max(suppliers) if suppliers else None,
                   "cheaper_than_you": sum(1 for c in suppliers if c < cents)},
    }


def breakdown(rate: float | None, supply_cents: float | None, tariff: dict | None) -> dict | None:
    """Where one kWh's price goes: supply, delivery and everything else.

    ``rate`` is the all-in price in dollars per kWh from the latest statement.
    Delivery is what is left once supply is taken out, so it carries any
    taxes and charges that ride on the kWh as well - the estimate is only as
    good as the supply figure it starts from.
    """
    if not rate or supply_cents is None:
        return None
    supply = supply_cents / 100
    if supply >= rate:
        return None
    return {"all_in": round(rate, 4), "supply": round(supply, 4),
            "delivery": round(rate - supply, 4),
            "supply_share": round(supply / rate * 100, 1),
            "standing_charge": tariff.get("standing_charge") if tariff else None}


def seasons(series: list[dict]) -> list[dict]:
    """The average all-in price in each calendar month, over the years held."""
    by_month: dict[int, list[float]] = {}
    for row in series:
        if row.get("rate") and row.get("kwh") and row.get("month"):
            by_month.setdefault(row["month"], []).append(row["rate"])
    return [{"month": m, "rate": round(sum(v) / len(v), 4), "n": len(v)}
            for m, v in sorted(by_month.items())]


def standing(series: list[dict], rate: float | None) -> dict | None:
    """Where the latest price sits among every price paid, and the past year's."""
    rates = [r["rate"] for r in series if r.get("rate") and r.get("kwh")]
    if not rate or len(rates) < 3:
        return None
    below = sum(1 for r in rates if r < rate)
    last = [r for r in series if r.get("rate") and r.get("kwh")]
    year_ago = next((r["rate"] for r in reversed(last[:-1])
                     if r.get("period") and last[-1].get("period")
                     and r["year"] == last[-1]["year"] - 1 and r["month"] == last[-1]["month"]), None)
    return {"percentile": round(below / len(rates) * 100),
            "of": len(rates),
            "year_ago": year_ago,
            "year_pct": round((rate - year_ago) / year_ago * 100, 1) if year_ago else None}
