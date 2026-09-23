"""The estimate ladder, used when no direct price-list hit is available.

Six sources of evidence in descending order of authority; the first that yields
anything wins. Everything produced here is written rose and is meant to be
reviewed — an estimate is a defensible starting number, not a fact.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median

from app.pricing import normalize as nz
from app.pricing.match import MIN_RATIO, _ratio

# Rungs that aggregate across *hospitals* need more than one witness before the
# median means anything: the Summary tab is only ~9% dense, so a single stray
# cell would otherwise become an authoritative-looking estimate. Rungs keyed to
# the same hospital (1, 2) are trusted on one observation because the hospital
# is the thing that determines the price.
MIN_CROSS_HOSPITAL_OBSERVATIONS = 3
# And if those witnesses disagree by more than this multiple, they aren't
# describing one component's price — treat it as no evidence and fall through.
MAX_CROSS_HOSPITAL_SPREAD = 3.0


def _usable_spread(values: list[float]) -> bool:
    lo, hi = min(values), max(values)
    return lo > 0 and hi / lo <= MAX_CROSS_HOSPITAL_SPREAD


@dataclass
class UsageRow:
    """One Usage-sheet row, with what we know about its component."""
    excel_row: int
    hospital: str | None
    ref: str | None
    description: str | None = None
    part_type: str | None = None
    category: str | None = None
    price: float | None = None          # existing price, if any


@dataclass(frozen=True)
class Estimate:
    value: float | None
    basis: str                          # which rung supported it
    zero_flagged: bool = False

    @property
    def found(self) -> bool:
        return self.value is not None


def typical(values: list[float]) -> float | None:
    """Mode when one value beats every alternative outright, else the median."""
    if not values:
        return None
    counts = Counter(values)
    top, n = counts.most_common(1)[0]
    if n > 1 and list(counts.values()).count(n) == 1:
        return float(top)
    return float(median(values))


def _same_hospital(rows: list[UsageRow], hospital: str | None) -> list[UsageRow]:
    h = nz.normalize_hospital(hospital)
    return [r for r in rows if h and nz.normalize_hospital(r.hospital) == h]


def estimate_price(target: UsageRow, observations: list[UsageRow],
                   tab_prices: dict[str, dict[str, float]],
                   tab_catalog: dict[str, str],
                   component_codes: tuple[str, ...]) -> Estimate:
    """Best supported estimate for ``target``.

    ``observations`` are the rows of the same workbook that already carry a
    price. ``tab_prices`` maps item_code -> {hospital -> price} for the ONE
    configured tab; nothing from another distributor's tab ever reaches here.
    """
    priced = [r for r in observations if r.price is not None]
    ref_u = (target.ref or "").strip().upper()
    pt = (target.part_type or "").strip().lower()
    cat = (target.category or "").strip().lower()

    # 1. Same hospital, exact REF, elsewhere in this dataset.
    same_h = _same_hospital(priced, target.hospital)
    if ref_u:
        vals = [r.price for r in same_h if (r.ref or "").strip().upper() == ref_u]
        if vals:
            v = typical(vals)
            # A zero only survives with this, the strongest possible evidence.
            return Estimate(v, "same hospital, same REF", zero_flagged=(v == 0))

    # 2. Same hospital, same part type.
    if pt:
        vals = [r.price for r in same_h if (r.part_type or "").strip().lower() == pt]
        if vals:
            v = typical([x for x in vals if x])
            if v:
                return Estimate(v, "same hospital, same part type")

    # 3. This exact component across hospitals in the configured tab.
    for code in component_codes:
        vals = [p for p in tab_prices.get(code, {}).values() if p]
        if len(vals) >= MIN_CROSS_HOSPITAL_OBSERVATIONS and _usable_spread(vals):
            return Estimate(typical(vals), "price list, same component, other hospitals")

    # 4. Same part type anywhere in this dataset.
    if pt:
        vals = [r.price for r in priced
                if (r.part_type or "").strip().lower() == pt and r.price]
        if vals:
            return Estimate(typical(vals), "same part type in this file")

    # 5. Strongly matching catalogue description in the configured tab.
    nd = nz.normalize_description(target.description)
    if nd and nz.meaningful_description(nd):
        vals: list[float] = []
        for code, cat_desc in tab_catalog.items():
            ncd = nz.normalize_description(cat_desc)
            if not ncd or nz.attributes_conflict(nd, ncd):
                continue
            if _ratio(nd, ncd) < MIN_RATIO:
                continue
            vals.extend(p for p in tab_prices.get(code, {}).values() if p)
        if len(vals) >= MIN_CROSS_HOSPITAL_OBSERVATIONS and _usable_spread(vals):
            return Estimate(typical(vals), "price list, similar description")

    # 6. Same category anywhere in this dataset.
    if cat:
        vals = [r.price for r in priced
                if (r.category or "").strip().lower() == cat and r.price]
        if vals:
            return Estimate(typical(vals), "same category in this file")

    return Estimate(None, "no supporting evidence")
