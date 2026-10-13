"""The utilities the app knows, defined once.

Everything that used to say "Electric or Gas" asks here instead, so a third
utility is a row in a table rather than a hunt through the code.

    name      how the fuel is spelled in the bill history's Account Type column
    key       lower-case form, used in JSON and as a CSS/colour slot
    slug      the page's address: /electric, /gas, /water
    unit      what usage is kept in. Water is always gallons, however the
              utility metered it, because a bill in CCF and one in gallons
              must come out on one axis
    units     every spelling that means this fuel, lower-cased
    tagline   the page's own name
    weather   whether the weather explains the usage. Electric and gas follow
              the cold (degree days); water follows the dry, what a garden
              lost to sun and air and didn't get back as rain
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fuel:
    name: str
    key: str
    slug: str
    unit: str
    units: frozenset
    tagline: str
    weather: bool = True
    # Dollar-free name for prose: "electricity", "gas", "water".
    noun: str = ""
    # Level titles for the game, in order; None uses the electric ones.
    titles: tuple | None = None


ELECTRIC = Fuel("Electric", "electric", "electric", "kWh", frozenset({"kwh"}),
                "⚡ Watt's the Damage", noun="electricity")
GAS = Fuel("Gas", "gas", "gas", "therms", frozenset({"therms", "therm", "ccf", "mcf"}),
           "🔥 Therms & Conditions", noun="gas",
           titles=("Meter Reader", "Pilot Light", "Therm Thrifter", "Draft Dodger",
                   "Thermostat Tactician", "Flame Tamer", "Therm Terminator",
                   "Efficiency Engineer", "Insulation Nation", "Furnace Whisperer"))
WATER = Fuel("Water", "water", "water", "gal", frozenset({"gal", "gallons", "gallon", "kgal"}),
             "💧 Tapped Out", noun="water",
             titles=("Meter Reader", "Drip Stopper", "Short Showerer", "Leak Seeker",
                     "Low-Flow Pro", "Rain Barrel Baron", "Gallon Guardian",
                     "Efficiency Engineer", "Drought Proof", "Reservoir Whisperer"))

ORDER = (ELECTRIC, GAS, WATER)
BY_NAME = {f.name: f for f in ORDER}
BY_SLUG = {f.slug: f for f in ORDER}
NAMES = tuple(f.name for f in ORDER)
SLUGS = tuple(f.slug for f in ORDER)

# Water metered in hundred cubic feet: one CCF is 748.05 US gallons.
GALLONS_PER_CCF = 748.05


def of_unit(unit: str | None) -> Fuel:
    """The fuel a usage unit belongs to. Unknown units are read as electricity,
    which is what the app always assumed before there was a choice."""
    key = (unit or "").strip().lower()
    for fuel in ORDER:
        if key in fuel.units:
            return fuel
    return ELECTRIC


def by_name(name: str | None) -> Fuel:
    return BY_NAME.get((name or "").strip().capitalize(), ELECTRIC)


def noun(name: str) -> str:
    return by_name(name).noun


def present(fuel_names) -> list:
    """The fuels in `fuel_names`, in the app's standing order."""
    wanted = {str(n) for n in fuel_names}
    return [f for f in ORDER if f.name in wanted]
