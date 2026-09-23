"""Hospital and component matching against one price-list tab.

Both matchers are tiered and stop at the first tier that yields anything. That
short-circuit is load-bearing for components: the bare code ``MTUUX`` is a prefix
of ``MTUUX100-GK``, which the pattern ``MTUUX***-GK`` also matches — and those two
catalogue rows can hold different prices. Unioning the tiers would read that as
"conflicting prices, refuse to price it"; evaluating them in order picks the more
specific rule and moves on.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.pricing import normalize as nz
from app.pricing.tabs import HOSPITAL_ALIASES

log = logging.getLogger("pricing.match")

MIN_RATIO = 0.75
# A second fuzzy candidate only makes the match *ambiguous* if it scores about as
# well as the winner. The Summary tab lists both 'Advanced Surg Ctr of North
# County' and '... North County HIgh Demand'; a usage row naming the former
# scores ~1.00 and ~0.86, and calling that a tie would throw away a certain match.
CREDIBLE_MARGIN = 0.03


@dataclass(frozen=True)
class HospitalMatch:
    name: str | None          # the price-list hospital, as written
    method: str               # exact | alias | fuzzy | ambiguous | none

    @property
    def matched(self) -> bool:
        return self.name is not None

    @property
    def confident(self) -> bool:
        """Only an exact or aliased hospital may back a direct (green) price.

        A fuzzy match at the spec's 75% threshold demonstrably mis-fires on real
        data ('Arroyo Grande Surgical Institute' -> 'River Surgical Institute'),
        and green tells the reviewer not to check. Fuzzy still prices the row —
        as a rose estimate.
        """
        return self.method in ("exact", "alias")


@dataclass(frozen=True)
class ComponentMatch:
    codes: tuple[str, ...]
    tier: str                 # exact | wildcard | prefix | description | none

    @property
    def matched(self) -> bool:
        return bool(self.codes)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------
# Hospitals
# --------------------------------------------------------------------------
def match_hospital(usage_hospital: str | None, candidates: list[str]) -> HospitalMatch:
    """Resolve a usage-sheet hospital to one price-list hospital column."""
    norm = nz.normalize_hospital(usage_hospital)
    if not norm:
        return HospitalMatch(None, "none")

    by_norm: dict[str, list[str]] = {}
    for c in candidates:
        by_norm.setdefault(nz.normalize_hospital(c), []).append(c)

    # 1. exact. Several columns can share a normalized name (the Summary tab has
    # 'Surgcenter of Plano' twice); that is only ambiguous if their prices
    # disagree, which ingest resolves, so any of them will do here.
    if norm in by_norm:
        return HospitalMatch(by_norm[norm][0], "exact")

    # 2. configured alias
    alias = HOSPITAL_ALIASES.get(norm)
    if alias and alias in by_norm:
        log.info("hospital alias used: %r -> %r", usage_hospital, by_norm[alias][0])
        return HospitalMatch(by_norm[alias][0], "alias")

    # 3. fuzzy: both the whole name and the name stripped of generic words must
    # clear the threshold, and exactly one candidate may survive.
    meaningful = nz.meaningful_hospital(norm)
    scored = [
        (_ratio(norm, cand), names[0]) for cand, names in by_norm.items()
        if _ratio(norm, cand) >= MIN_RATIO
        and _ratio(meaningful, nz.meaningful_hospital(cand)) >= MIN_RATIO
    ]
    if not scored:
        return HospitalMatch(None, "none")
    best = max(score for score, _ in scored)
    credible = [name for score, name in scored if best - score <= CREDIBLE_MARGIN]
    if len(credible) == 1:
        return HospitalMatch(credible[0], "fuzzy")
    return HospitalMatch(None, "ambiguous")


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------
_WILDCARD = re.compile(r"(\*\*\*|xxx)")


def compile_code_pattern(code: str) -> re.Pattern | None:
    """Turn a price-list code family into a regex, or None if it isn't one.

    ``***`` stands for a run of anything; lowercase ``xxx`` for a run of digits.
    The run is deliberately *not* fixed at three characters, despite the three
    glyphs: the catalogue writes ``UFCR***-GK`` for the real REF ``UFCRLA00-GK``
    (a four-character fill) and ``ACLMR***-UK`` for ``ACLMRL100-UK``. A ``{3}``
    quantifier matches neither — it would silently drop both families.

    Case matters: ``DAXX00D-F`` is a literal REF, not a pattern. No part number
    in the master contains an uppercase ``XXX``, so requiring lowercase is safe.
    """
    if "***" not in code and "xxx" not in code:
        return None
    parts = _WILDCARD.split(code)
    body = "".join(
        "[A-Za-z0-9/-]+" if p == "***" else r"\d+" if p == "xxx" else re.escape(p)
        for p in parts
    )
    return re.compile(f"^{body}$", re.IGNORECASE)


def match_component(ref: str | None, description: str | None,
                    catalog: dict[str, str]) -> ComponentMatch:
    """Resolve a usage REF to price-list item code(s) within one tab.

    ``catalog`` maps item_code -> catalogue description.
    """
    r = (ref or "").strip()

    if r:
        # 1. exact
        exact = [c for c in catalog if c.strip().upper() == r.upper()]
        if exact:
            return ComponentMatch(tuple(exact), "exact")

        # 2. explicit code family / wildcard
        wild = [c for c in catalog
                if (p := compile_code_pattern(c)) is not None and p.match(r)]
        if wild:
            return ComponentMatch(tuple(wild), "wildcard")

        # 3. the catalogue code is a prefix of the full REF
        pref = [c for c in catalog
                if "*" not in c and "xxx" not in c
                and len(c) >= 3 and r.upper().startswith(c.strip().upper())]
        if pref:
            # Longest prefix wins — 'MO-MSFC' beats 'MO-' if both were listed.
            best = max(len(c.strip()) for c in pref)
            return ComponentMatch(
                tuple(c for c in pref if len(c.strip()) == best), "prefix")

    # 4. description, with a hard veto on conflicting critical attributes
    nd = nz.normalize_description(description)
    if nd and nz.meaningful_description(nd):
        hits = []
        for code, cat_desc in catalog.items():
            ncd = nz.normalize_description(cat_desc)
            if not ncd or nz.attributes_conflict(nd, ncd):
                continue
            if _ratio(nd, ncd) < MIN_RATIO:
                continue
            # Never let a match rest on generic anatomy words alone.
            if not nz.meaningful_description(ncd):
                continue
            if _ratio(nz.meaningful_description(nd),
                      nz.meaningful_description(ncd)) < MIN_RATIO:
                continue
            hits.append(code)
        if hits:
            return ComponentMatch(tuple(hits), "description")

    return ComponentMatch((), "none")
