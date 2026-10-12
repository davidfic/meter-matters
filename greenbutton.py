#!/usr/bin/env python
"""Parse Eversource MA usage exports.

Eversource hands out two different CSVs, and neither is ESPI XML. Both are
supported here and normalised into the same :class:`Period`.

**Billing data** (from the zip download) - start and end dates, no temperature::

    Name,...
    Address,"12 PLACEHOLDER LN, EXAMPLETON MA 01000"
    Account Number,55500010001
    Service,555000100010000000001

    TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES
    Electric billing,2026-08-21,2026-09-17,714.00,$248.26

**Usage history** (the "Green Button" CSV) - a read date, a day count, and an
average temperature, which is the one thing the billing export lacks::

    Account,Read Date,Usage,Number of Days,Usage per day,Charge,Read Type,Average Temperature
    55500020002,5/20/2024,810.00,32,25.31,$271.59,Actual Read,54.0

Why this matters: your bill is rate x usage. Only the usage half is yours to
change, and only these files carry it.

    ./greenbutton.py usage/*.csv
"""

from __future__ import annotations

import csv
import glob
import io
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean

BALANCE_POINT_F = 65.0  # US convention for degree days

BILLING_HEADER = "TYPE,"
USAGE_HEADER = "Account,"


@dataclass
class Period:
    """One billing/meter period, however it was spelled in the source file."""

    account: str
    start: date
    end: date
    kwh: float
    cost: float
    avg_temp: float | None = None
    read_type: str = ""
    source: str = ""
    # Electric exports are metered in kWh, gas in therms. The header says which,
    # so read it rather than assuming - mislabelling therms as kWh would make
    # every derived rate and per-day figure quietly wrong.
    unit: str = "kWh"
    # False when the file carried no cost for this period: the usage is real,
    # the $0.00 is not, and nothing should be priced off it.
    priced: bool = True
    # Sent back to the grid over the period (solar), in the same unit.
    exported: float = 0.0

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def kwh_per_day(self) -> float:
        """Usage per day, in whatever `unit` this export is metered in."""
        return self.kwh / self.days if self.days else 0.0

    @property
    def rate(self) -> float | None:
        """Dollars per metered unit - the half of the bill you do not control."""
        return self.cost / self.kwh if self.kwh and self.priced else None

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"

    @property
    def estimated(self) -> bool:
        return bool(self.read_type) and "actual" not in self.read_type.lower()

    @property
    def hdd_per_day(self) -> float | None:
        if self.avg_temp is None:
            return None
        return max(0.0, BALANCE_POINT_F - self.avg_temp)

    @property
    def cdd_per_day(self) -> float | None:
        if self.avg_temp is None:
            return None
        return max(0.0, self.avg_temp - BALANCE_POINT_F)


@dataclass
class Export:
    periods: list[Period]
    address: str | None = None
    accounts: set[str] = field(default_factory=set)


def _num(value: str) -> float | None:
    text = re.sub(r"[^0-9.\-]", "", (value or "").strip())
    try:
        return float(text)
    except ValueError:
        return None


def _date(value: str) -> date | None:
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _header_index(lines: list[str], prefix: str) -> int | None:
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    return None


def _preamble(lines: list[str], upto: int) -> tuple[str | None, str | None]:
    """Pull address and account number out of the `key,value` preamble."""
    address = account = None
    for line in lines[:upto]:
        parts = next(csv.reader([line]), [])
        if len(parts) < 2:
            continue
        key, value = parts[0].strip().lower(), parts[1].strip()
        if key == "address" and value:
            address = value
        elif key == "account number" and value:
            account = value
    if address is None:
        address = next((l.strip() for l in lines[:upto]
                        if l.strip() and "," in l and not l.lower().startswith(
                            ("name", "account", "service", "data from", "requested", "meter"))),
                       None)
    return address, account


USAGE_COLUMN = re.compile(r"^USAGE\s*\(([^)]*)\)\s*$", re.I)
# However the header spells it, one spelling downstream: "KWH" and "Therms"
# would otherwise be different fuels to code that compares strings.
CANONICAL_UNIT = {"kwh": "kWh", "therms": "therms", "therm": "therms", "ccf": "ccf",
                  "hcf": "ccf", "mcf": "mcf", "mwh": "MWh",
                  "gal": "gal", "gallons": "gal", "gallon": "gal"}


def canonical_unit(unit: str) -> str:
    text = (unit or "").strip()
    return CANONICAL_UNIT.get(text.lower(), text or "kWh")


def _parse_billing(lines: list[str], head: int, source: str) -> Export:
    address, account = _preamble(lines, head)
    rows = csv.DictReader(io.StringIO("\n".join(lines[head:])))
    # "USAGE (kWh)" on an electric export, "USAGE (therms)" on a gas one.
    usage_col, unit, exported_col = "USAGE (kWh)", "kWh", None
    for name in (rows.fieldnames or []):
        match = USAGE_COLUMN.match((name or "").strip())
        if match:
            usage_col, unit = name.strip(), canonical_unit(match.group(1))
        elif (name or "").strip().upper().startswith("EXPORTED ("):
            exported_col = name.strip()

    periods = []
    for row in rows:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        start, end = _date(row.get("START DATE", "")), _date(row.get("END DATE", ""))
        kwh, cost = _num(row.get(usage_col, "")), _num(row.get("COST", ""))
        if not (start and end) or kwh is None:
            continue
        # A blank cost is a file that carries none, not a free month.
        periods.append(Period(account=account or "", start=start, end=end,
                              kwh=kwh, cost=cost or 0.0, source=source, unit=unit,
                              priced=cost is not None,
                              exported=_num(row.get(exported_col, "")) or 0.0 if exported_col else 0.0))
    return Export(periods=periods, address=address,
                  accounts={p.account for p in periods if p.account})


def _parse_usage_history(lines: list[str], head: int, source: str) -> Export:
    address, _ = _preamble(lines, head)
    rows = csv.DictReader(io.StringIO("\n".join(lines[head:])))
    periods = []
    for row in rows:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        end = _date(row.get("Read Date", ""))
        kwh, cost = _num(row.get("Usage", "")), _num(row.get("Charge", ""))
        if end is None or kwh is None or cost is None:
            continue
        days = int(_num(row.get("Number of Days", "")) or 0)
        # The read date closes the period, so the start is `days` back inclusive.
        start = end - timedelta(days=max(days, 1) - 1)
        periods.append(Period(account=row.get("Account", "").strip(),
                              start=start, end=end, kwh=kwh, cost=cost,
                              avg_temp=_num(row.get("Average Temperature", "")),
                              read_type=row.get("Read Type", ""), source=source))
    return Export(periods=periods, address=address,
                  accounts={p.account for p in periods if p.account})


def parse(text: str, source: str = "") -> Export:
    """Detect which of the two Eversource layouts this is, and read it."""
    lines = text.splitlines()
    billing = _header_index(lines, BILLING_HEADER)
    usage = _header_index(lines, USAGE_HEADER)

    if billing is not None:
        export = _parse_billing(lines, billing, source)
    elif usage is not None:
        export = _parse_usage_history(lines, usage, source)
    else:
        raise ValueError("no Eversource header row found - not a usage export?")

    if not export.periods:
        raise ValueError("header found but no usable usage rows followed it")
    export.periods.sort(key=lambda p: p.end)
    return export


def load(path) -> Export:
    path = Path(path)
    return parse(path.read_text(encoding="utf-8-sig"), source=path.name)


def load_zip(path) -> list[Export]:
    """Read the CSVs inside a downloaded export without unpacking it first.

    The portal hands you a .zip; making you extract it by hand is a step that
    buys nothing. Anything in there that isn't a usable CSV is skipped.
    """
    path = Path(path)
    exports = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.lower().endswith(".csv") or name.startswith("__MACOSX"):
                    continue
                try:
                    text = archive.read(name).decode("utf-8-sig")
                except (UnicodeDecodeError, KeyError):
                    continue
                try:
                    exports.append(parse(text, source=f"{path.name}:{Path(name).name}"))
                except ValueError:
                    continue
    except (zipfile.BadZipFile, OSError):
        return []
    return exports


def load_all(paths) -> Export:
    """Read several exports - .csv or .zip - de-duplicating on account + period.

    Where two files describe the same period, the one carrying a temperature
    wins - that column only appears in the usage-history export.
    """
    merged: dict[tuple[str, date, date], Period] = {}
    address = None
    for path in sorted(paths):
        if str(path).lower().endswith(".zip"):
            found = load_zip(path)
        else:
            try:
                found = [load(path)]
            except (OSError, ValueError):
                continue
        for export in found:
            address = address or export.address
            for p in export.periods:
                # Keyed on the START only: one account cannot begin two billing
                # periods on the same day, so a second row with that start is a
                # duplicate. Eversource's own export contains one - the same
                # 25 therms and $52.74 filed with both a 05-19 and a 05-20 end.
                key = (p.account, p.unit.lower(), p.start)
                existing = merged.get(key)
                if existing is None:
                    merged[key] = p
                    continue
                # Prefer the row carrying a temperature; failing that the later
                # end, which is the one that keeps the series contiguous.
                if existing.avg_temp is None and p.avg_temp is not None:
                    merged[key] = p
                elif existing.avg_temp is None or p.avg_temp is not None:
                    if p.end > existing.end:
                        merged[key] = p
    periods = sorted(merged.values(), key=lambda p: (p.account, p.end))
    return Export(periods=periods, address=address,
                  accounts={p.account for p in periods if p.account})


def load_dir(directory) -> Export:
    """Every export in a folder: loose CSVs and zips straight from the portal."""
    root = Path(directory)
    return load_all(glob.glob(str(root / "*.csv")) + glob.glob(str(root / "*.zip")))


# --------------------------------------------------------------------------
# Is it you, or is it the weather?
# --------------------------------------------------------------------------

def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """Ordinary least squares -> (slope, intercept, r_squared)."""
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    return slope, my - slope * mx, (0.0 if syy == 0 else sxy ** 2 / (sxx * syy))


def weather_fit(periods: list[Period]) -> dict | None:
    """Regress daily usage on heating degree days, when temperatures exist."""
    usable = [p for p in periods if p.hdd_per_day is not None]
    fitted = _fit([p.hdd_per_day for p in usable], [p.kwh_per_day for p in usable])
    if fitted is None:
        return None
    slope, intercept, r2 = fitted
    return {"slope": slope, "intercept": intercept, "r2": r2,
            "mean_hdd": mean(p.hdd_per_day for p in usable), "n": len(usable)}


# The portal serves a rolling window - roughly 36 months - so periods fall off
# the back. Anything not exported before it ages out is gone for good.
RETENTION_DAYS = 365 * 3


def staleness(export: Export, last_billed: dict | None = None,
              today: date | None = None,
              retention_days: int = RETENTION_DAYS) -> list[dict]:
    """Per account: how far behind the usage data is, and whether any is at risk.

    Two different things, often confused:

    * **Freshness** - statements keep arriving by email, but kWh and therms only
      arrive when you export. Months billed since the last export have a cost
      and no usage. Re-exporting fills them in; nothing is lost meanwhile.
    * **Loss** - the portal serves a rolling window, so a period is only
      retrievable while it sits inside it. Data already downloaded is yours
      permanently. A gap can therefore open in exactly one way: going so long
      without exporting that your **newest saved** period ages out of the
      window. That is the margin worth watching, and it is ~3 years wide.
    """
    today = today or date.today()
    last_billed = last_billed or {}
    window_start = today - timedelta(days=retention_days)
    out = []
    for account in sorted({p.account for p in export.periods}):
        periods = [p for p in export.periods if p.account == account]
        oldest, newest = periods[0].start, periods[-1].end
        billed = last_billed.get(account)
        behind = max(0, round((billed - newest).days / 30.44)) if billed else 0
        out.append({
            "account": account,
            "oldest": oldest,
            "newest": newest,
            # Days before the newest saved period falls out of the window. Once
            # it does, re-exporting can no longer reach back to meet it.
            "days_of_slack": (newest - window_start).days,
            "months_behind": behind,
            "live": (today - billed).days <= 120 if billed else True,
        })
    return out


def staleness_report(export: Export, last_billed: dict | None = None,
                     today: date | None = None) -> tuple[list[str], bool]:
    """Human-readable notes, plus whether any of them is actually urgent."""
    lines, urgent = [], False
    for row in staleness(export, last_billed, today):
        if not row["live"]:
            continue  # a closed account gains nothing from re-exporting
        if row["days_of_slack"] <= 90:
            urgent = True
            lines.append(
                f"{row['account']}: newest saved usage ({row['newest']}) is "
                f"{row['days_of_slack']} days from ageing out of the portal's window - "
                f"export now or a permanent gap opens")
        elif row["months_behind"] >= 4:
            lines.append(
                f"{row['account']}: {row['months_behind']} months billed since the last "
                f"usage export ({row['newest']}) - those have cost but no usage. "
                f"{round(row['days_of_slack'] / 30.44)} months of margin, so nothing is at risk")
    return lines, urgent


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if "--staleness" in args:
        import config
        export = load_dir(config.USAGE_DIR)
        # Statements tell us which accounts are still live.
        last_billed = {}
        try:
            import analysis
            for bill in analysis.build_bills(analysis.load_entries(config.CSV_PATH)):
                digits = "".join(c for c in bill.account if c.isdigit())
                if digits and (digits not in last_billed or bill.due > last_billed[digits]):
                    last_billed[digits] = bill.due
        except (OSError, ValueError):
            pass
        lines, urgent = staleness_report(export, last_billed)
        for line in lines:
            print(line)
        if not lines:
            print("usage exports are current")
        return 1 if urgent else 0

    paths = [a for a in args if not a.startswith("-")] or \
        glob.glob("usage/*.csv") + glob.glob("usage/*.zip")
    if not paths:
        print(__doc__)
        return 1
    export = load_all(paths)
    if not export.periods:
        print("error: nothing parsed", file=sys.stderr)
        return 1

    for account in sorted(export.accounts):
        periods = [p for p in export.periods if p.account == account]
        print(f"\naccount {account}  ({len(periods)} periods, "
              f"{periods[0].start} -> {periods[-1].end})")
        print(f"{'service period':<26}{'kWh':>7}{'d':>4}{'kWh/d':>8}{'cost':>10}{'$/kWh':>9}{'avgF':>6}")
        for p in periods:
            temp = f"{p.avg_temp:6.0f}" if p.avg_temp is not None else f"{'-':>6}"
            print(f"{p.label:<26}{p.kwh:7.0f}{p.days:4d}{p.kwh_per_day:8.2f}"
                  f"{p.cost:10.2f}{p.rate:9.4f}{temp}")
        fit = weather_fit(periods)
        if fit:
            print(f"  usage vs heating degree days: r2={fit['r2']:.2f} over {fit['n']} periods")
        else:
            print("  (no temperatures in this export - weather fit unavailable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
