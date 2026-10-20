"""Published residential electricity rates, by area and date.

Used only to estimate usage for statements older than the usage export:
usage = (bill - customer charge) / price per kWh. The estimate is offered
only where the metered statements show the account really is on this
tariff (see longview.published_matches), and the page says it is an estimate.

One area so far: Eversource's eastern Massachusetts territory (NSTAR
Electric, the EMA tariffs), residential rate R-1 on Basic Service. Each row
is the all-in price from its effective date until the next row: the fixed
Basic Service supply rate plus every per-kWh delivery charge, and the
monthly customer charge. Rates change on January 1 and July 1 (lately
February 1 and August 1 for supply). Rows are filled from Eversource's own
rate archives and the DPU's yearly rate summaries; the source of each is
noted beside it, and a row whose figure could not be read off an official
document is marked so.
"""

from __future__ import annotations

from datetime import date

EMA_SOURCE = {"text": "Eversource rate archives and the DPU's yearly summaries of eastern Massachusetts electric rates",
              "href": "https://www.eversource.com/residential/account-billing/manage-bill/about-your-bill/rates-tariffs"}

# (effective date, Basic Service fixed supply $/kWh, total delivery $/kWh, customer charge $/month)
# Delivery is the Greater Boston / Cambridge / South Shore figure (the Cape pays a higher
# efficiency charge). Supply figures are from Eversource's Basic Service rate archives
# (M.D.P.U. 1-18-B through 1-26-F), delivery from the Greater Boston rate sheets and the
# supply-and-delivery summaries, several read from the Internet Archive's copies. Where no
# sheet for a date could be found the delivery is carried forward from the row before, and
# the comment says so. The 2021 and 2022 supply figures come from Eversource press releases
# and a town's notice rather than a tariff sheet. Compiled 2026-10-20.
EMA_R1: list[tuple[date, float, float, float]] = [
    (date(2018, 1, 1), 0.12881, 0.10160, 6.43),
    (date(2018, 2, 1), 0.12888, 0.10160, 7.00),    # delivery carried forward
    (date(2018, 7, 1), 0.11397, 0.10160, 7.00),    # delivery carried forward
    (date(2019, 1, 1), 0.13588, 0.10735, 7.00),
    (date(2019, 7, 1), 0.10836, 0.10735, 7.00),
    (date(2019, 9, 1), 0.10836, 0.10846, 7.00),
    (date(2020, 1, 1), 0.12517, 0.10896, 7.00),
    (date(2020, 7, 1), 0.09877, 0.10896, 7.00),    # delivery carried forward to August's sheet
    (date(2020, 8, 1), 0.09877, 0.11281, 7.00),
    (date(2021, 1, 1), 0.11795, 0.11823, 7.00),
    (date(2021, 4, 1), 0.11795, 0.12827, 7.00),
    (date(2021, 7, 1), 0.10753, 0.12628, 7.00),
    (date(2022, 1, 1), 0.15764, 0.13688, 7.00),
    (date(2022, 7, 1), 0.17871, 0.14319, 7.00),
    (date(2023, 1, 1), 0.25776, 0.14622, 10.00),
    (date(2023, 7, 1), 0.16078, 0.14705, 10.00),
    (date(2024, 1, 1), 0.17216, 0.16639, 10.00),
    (date(2024, 7, 1), 0.17216, 0.17562, 10.00),
    (date(2024, 8, 1), 0.15772, 0.17562, 10.00),
    (date(2025, 1, 1), 0.15772, 0.19122, 10.00),
    (date(2025, 2, 1), 0.13241, 0.19122, 10.00),
    (date(2025, 7, 1), 0.13241, 0.18517, 10.00),
    (date(2025, 8, 1), 0.14884, 0.18517, 10.00),
    (date(2025, 9, 1), 0.14884, 0.18867, 10.00),
    (date(2026, 1, 1), 0.14884, 0.18867, 10.00),   # delivery carried forward; no January sheet found
    (date(2026, 2, 1), 0.15629, 0.09591, 7.50),    # a temporary bill credit sat in distribution, Feb-Mar 2026
    (date(2026, 4, 1), 0.15629, 0.18580, 10.00),
    (date(2026, 7, 1), 0.15629, 0.18366, 10.00),   # only the efficiency charge's change is documented
    (date(2026, 8, 1), 0.17323, 0.18366, 10.00),
    (date(2026, 9, 1), 0.17323, 0.18635, 10.00),
]


def _lookup(table, day: date) -> dict | None:
    chosen = None
    for row in table:
        if row[0] <= day:
            chosen = row
        else:
            break
    if chosen is None:
        return None
    _, supply, delivery, customer = chosen
    return {"per_kwh": round(supply + delivery, 5), "customer": customer, "supply": supply, "delivery": delivery}


def for_area(state: str | None, fuel: str):
    """A lookup function day -> rate for the area, or None when there is none to offer."""
    if fuel == "Electric" and state == "MA" and EMA_R1:
        return (lambda day: _lookup(EMA_R1, day)), EMA_SOURCE
    return None, None
