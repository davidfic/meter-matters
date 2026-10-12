"""Where a home is, and what that changes.

Nothing in the app is tied to one state or one utility. What does depend on
place is gathered here, so the rest can ask "where?" and get an answer, or
"don't know" and carry on:

* the carbon in a kilowatt-hour, which is the grid it was made on;
* which supplier comparison exists (see ``rates.py``).

A home's state comes from, in order: what the person said, the state in the
address on a usage export ("... MA 02144"), and the state the ZIP code is in
according to the geocoder the weather already uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}
_BY_NAME = {name.casefold(): code for code, name in STATES.items()}

# EPA eGRID2023 (summary tables, revision 2, produced 2025-03-27), Table 3:
# state total output emission rates, lb CO2e per MWh. These are what the power
# plants *in* a state emit per unit they generate - a state that imports much
# of what it uses (Vermont, for one) is better described by its region's grid.
EGRID_STATE_LB_PER_MWH = {
    "AK": 814.5, "AL": 714.0, "AR": 998.4, "AZ": 689.1, "CA": 394.8, "CO": 1091.1,
    "CT": 540.7, "DC": 395.0, "DE": 704.2, "FL": 789.1, "GA": 717.4, "HI": 1394.7,
    "IA": 634.2, "ID": 313.9, "IL": 474.4, "IN": 1465.5, "KS": 733.3, "KY": 1747.0,
    "LA": 763.1, "MA": 830.1, "MD": 522.2, "ME": 316.8, "MI": 797.4, "MN": 752.3,
    "MO": 1454.6, "MS": 828.3, "MT": 1063.9, "NC": 626.4, "ND": 1297.9, "NE": 1025.8,
    "NH": 276.2, "NJ": 470.2, "NM": 773.6, "NV": 643.9, "NY": 466.6, "OH": 1068.3,
    "OK": 648.8, "OR": 365.0, "PA": 647.9, "PR": 1548.5, "RI": 840.0, "SC": 560.4,
    "SD": 335.6, "TN": 661.2, "TX": 771.2, "UT": 1421.2, "VA": 539.6, "VT": 52.2,
    "WA": 266.6, "WI": 1163.6, "WV": 1968.9, "WY": 1833.3, "US": 770.9,
}

# eGRID2023 subregion NEWE (New England), total output emission rate. One
# grid serves all six states and power flows freely across it, so it describes
# what a New England household uses better than any single state's plants do.
NEWE_LB_PER_MWH = 543.178
NEW_ENGLAND = {"CT", "MA", "ME", "NH", "RI", "VT"}

LB_PER_MWH_TO_KG_PER_KWH = 0.45359237 / 1000
SOURCE = "EPA eGRID2023"


@dataclass(frozen=True)
class Grid:
    kg_per_kwh: float
    label: str            # what the figure is, for the page
    state: str | None


def grid(state: str | None) -> Grid:
    """Kilograms of CO2e per kWh where `state` is, and what that figure describes."""
    state = (state or "").upper() or None
    if state in NEW_ENGLAND:
        return Grid(NEWE_LB_PER_MWH * LB_PER_MWH_TO_KG_PER_KWH,
                    f"the New England grid ({SOURCE})", state)
    if state in EGRID_STATE_LB_PER_MWH:
        return Grid(EGRID_STATE_LB_PER_MWH[state] * LB_PER_MWH_TO_KG_PER_KWH,
                    f"{STATES.get(state, state)}'s power plants ({SOURCE})", state)
    return Grid(EGRID_STATE_LB_PER_MWH["US"] * LB_PER_MWH_TO_KG_PER_KWH,
                f"the U.S. average ({SOURCE}) - say where the home is for a closer figure", None)


def from_address(text: str | None) -> str | None:
    """The state at the end of a mailing address: '... SOMEWHERE MA 01000'."""
    found = re.search(r"\b([A-Za-z]{2})\s+\d{5}(?:-\d{4})?\s*$", (text or "").strip())
    code = found.group(1).upper() if found else None
    return code if code in STATES else None


def from_place(name: str | None) -> str | None:
    """The state in a geocoder's 'Town, State' name."""
    tail = (name or "").rsplit(",", 1)[-1].strip().casefold()
    return _BY_NAME.get(tail)


def resolve(chosen: str | None = None, address: str | None = None,
            place: str | None = None) -> str | None:
    """The home's state, from the most to the least certain source."""
    chosen = (chosen or "").strip().upper()
    if chosen in STATES:
        return chosen
    return from_address(address) or from_place(place)


def name(state: str | None) -> str | None:
    return STATES.get((state or "").upper())
