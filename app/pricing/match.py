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

# Two candidates that score within this of each other are a tie, not a winner.
CREDIBLE_MARGIN = 0.03
# ...unless the leader is near enough to identical that the gap is a typo rather
# than a real distinction: 'Lehigh Valley Hospital - Hazelton' vs the list's
# 'Lehigh Valley Hosp Hazleton' scores 0.968.
NEAR_EXACT = 0.95


@dataclass(frozen=True)
class HospitalMatch:
    name: str | None          # the price-list hospital, as written
    method: str               # exact | alias | core | fuzzy | ambiguous | none

    @property
    def matched(self) -> bool:
        return self.name is not None

    @property
    def confident(self) -> bool:
        """Only an exact or aliased hospital may back a direct (green) price.

        Neither of the looser tiers can promise what green promises. Fuzzy at
        the spec's 75% threshold demonstrably mis-fires on real data ('Arroyo
        Grande Surgical Institute' -> 'River Surgical Institute'). And ``core``
        compares names with the generic words stripped, so 'Boca Raton Hospital'
        and 'Boca Raton Surg Ctr' collapse together — plausibly two different
        accounts. Green tells the reviewer not to check; both tiers still price
        the row, as a rose estimate.
        """
        return self.method in ("exact", "alias")


@dataclass(frozen=True)
class ComponentMatch:
    codes: tuple[str, ...]
    tier: str                 # exact | wildcard | prefix | description | none
    # The codes from each *less specific* tier that also matched, in order.
    # The winning tier is the only one that may set a direct price; these are
    # for the estimate ladder, which can fall back to a family row when the
    # winner has no price at the hospital in hand. See estimate.py rung 1b.
    fallbacks: tuple[tuple[str, ...], ...] = ()

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

    meaningful = nz.meaningful_hospital(norm)

    # 3. core: identical once the generic words are stripped. 'Baylor Scott &
    # White Medical Center - Sunnyvale' and the list's 'Baylor Scott & White
    # Sunnyvale' both reduce to 'baylor scott white sunnyvale' — the same
    # account, written two ways, which the fuzzy tier below scores at only 0.79
    # because of the words in between. Requires exactly one column to reduce to
    # that form, so a bare 'Baylor Scott & White' (which reduces to a form no
    # single column has) stays unmatched rather than picking a sibling.
    by_core: dict[str, list[str]] = {}
    for cand, names in by_norm.items():
        by_core.setdefault(nz.meaningful_hospital(cand), []).extend(names)
    core_hits = by_core.get(meaningful, []) if meaningful else []
    if len(set(core_hits)) == 1:
        return HospitalMatch(core_hits[0], "core")

    # 4. fuzzy: both the whole name and the name stripped of generic words must
    # clear the threshold.
    scored = sorted(
        ((_ratio(norm, cand), cand, names[0]) for cand, names in by_norm.items()
         if _ratio(norm, cand) >= MIN_RATIO
         and _ratio(meaningful, nz.meaningful_hospital(cand)) >= MIN_RATIO),
        reverse=True)
    if not scored:
        return HospitalMatch(None, "none")
    if len(scored) == 1:
        return HospitalMatch(scored[0][2], "fuzzy")

    (best, best_norm, best_name), (runner, runner_norm, _) = scored[0], scored[1]
    if best - runner < CREDIBLE_MARGIN:
        return HospitalMatch(None, "ambiguous")
    if best >= NEAR_EXACT:
        return HospitalMatch(best_name, "fuzzy")
    # Otherwise the leader has to be picked out BY the query: some meaningful
    # word the query shares with it and not with the runner-up. Without that,
    # a query naming no facility ('Baylor, Scott, & White') would confidently
    # pick whichever of five sibling facilities happened to score highest.
    q = set(meaningful.split())
    if (q & set(nz.meaningful_hospital(best_norm).split())) - \
            set(nz.meaningful_hospital(runner_norm).split()):
        return HospitalMatch(best_name, "fuzzy")
    return HospitalMatch(None, "ambiguous")


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------
# A *run* of stars or lowercase x's, not a fixed-width token. The catalogue
# writes three, four or more: 'MTUUX***-GK' and 'ACLM****-UK' are both real, and
# splitting on a literal '***' leaves the fourth star to be escaped into the
# pattern, which can then never match anything.
_WILDCARD = re.compile(r"(\*+|x{3,})")


def compile_code_pattern(code: str) -> re.Pattern | None:
    """Turn a price-list code family into a regex, or None if it isn't one.

    A run of ``*`` stands for a run of anything; a run of at least three
    lowercase ``x`` for a run of digits.
    The run is deliberately *not* fixed at three characters, despite the three
    glyphs: the catalogue writes ``UFCR***-GK`` for the real REF ``UFCRLA00-GK``
    (a four-character fill) and ``ACLMR***-UK`` for ``ACLMRL100-UK``. A ``{3}``
    quantifier matches neither — it would silently drop both families.

    Case matters: ``DAXX00D-F`` is a literal REF, not a pattern. No part number
    in the master contains an uppercase ``XXX``, so requiring lowercase is safe.
    """
    if not _WILDCARD.search(code):
        return None
    parts = _WILDCARD.split(code)
    body = "".join(
        "[A-Za-z0-9/-]+" if p.startswith("*")
        else r"\d+" if p.startswith("x") and p == "x" * len(p)
        else re.escape(p)
        for p in parts
    )
    return re.compile(f"^{body}$", re.IGNORECASE)


def match_component(ref: str | None, description: str | None,
                    catalog: dict[str, str]) -> ComponentMatch:
    """Resolve a usage REF to price-list item code(s) within one tab.

    ``catalog`` maps item_code -> catalogue description.
    """
    r = (ref or "").strip()
    tiers: list[tuple[str, tuple[str, ...]]] = []

    if r:
        # 1. exact
        exact = [c for c in catalog if c.strip().upper() == r.upper()]
        if exact:
            tiers.append(("exact", tuple(exact)))

        # 2. explicit code family / wildcard
        wild = [c for c in catalog
                if (p := compile_code_pattern(c)) is not None and p.match(r)]
        if wild:
            tiers.append(("wildcard", tuple(wild)))

        # 3. the catalogue code is a prefix of the full REF
        pref = [c for c in catalog
                if compile_code_pattern(c) is None
                and len(c) >= 3 and r.upper().startswith(c.strip().upper())]
        if pref:
            # Longest prefix wins — 'MO-MSFC' beats 'MO-' if both were listed.
            best = max(len(c.strip()) for c in pref)
            tiers.append(("prefix",
                          tuple(c for c in pref if len(c.strip()) == best)))

    # The most specific tier that matched wins outright; the rest are kept as
    # fallbacks. They must never be unioned into the winner: the bare code
    # 'MTUUX' is a prefix of 'MTUUX100-GK', which 'MTUUX***-GK' also matches,
    # and on the real list those two rows disagree two times in three. Unioning
    # reads that as a conflict and throws away a price that was never ambiguous.
    if tiers:
        tier, codes = tiers[0]
        return ComponentMatch(codes, tier, tuple(c for _, c in tiers[1:]))

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
