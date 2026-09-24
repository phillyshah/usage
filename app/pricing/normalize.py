"""Normalization for hospital names and component descriptions.

Both sides of every comparison go through here, so a price-list column header and
a usage-sheet cell that mean the same place (or the same device) end up as the
same string. Originals are never mutated — callers keep them for display and the
audit trail.
"""
from __future__ import annotations

import re

from app.pricing.tabs import (
    COMPONENT_ABBREVIATIONS,
    CRITICAL_ATTRIBUTES,
    GENERIC_COMPONENT_WORDS,
    GENERIC_HOSPITAL_WORDS,
    HOSPITAL_ABBREVIATIONS,
    LEGAL_SUFFIXES,
)

# "Blake Hospital (HCA)" / "St Rose Sienna (Dignity)" — the health-system tag is
# not part of the hospital's identity for pricing, and the usage sheet never
# carries it. Not in the written spec; documented here because dropping it is
# what makes the real price-list headers match at all.
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def collapse(text: str | None) -> str:
    """Lowercase, single-spaced — the form DISTRIBUTOR_TABS keys are written in."""
    return " ".join((text or "").lower().split())


def normalize_hospital(name: str | None) -> str:
    """'Centerpoint Med Ctr (HCA)' -> 'centerpoint medical center'."""
    if not name:
        return ""
    s = _PARENTHETICAL.sub(" ", str(name).lower())
    s = _NON_ALNUM.sub(" ", s)
    out = []
    for tok in s.split():
        if tok in LEGAL_SUFFIXES:
            continue
        out.append(HOSPITAL_ABBREVIATIONS.get(tok, tok))
    return " ".join(out)


def meaningful_hospital(normalized: str) -> str:
    """Drop the words nearly every hospital shares, so the fuzzy tier can't pass
    on 'Medical Center' alone."""
    return " ".join(t for t in normalized.split() if t not in GENERIC_HOSPITAL_WORDS)


def normalize_description(text: str | None) -> str:
    """'TIBIAL BASE PLATE (TITAN)' -> 'tibial base plate titan'."""
    if not text:
        return ""
    s = _NON_ALNUM.sub(" ", str(text).lower())
    out = []
    for tok in s.split():
        expanded = COMPONENT_ABBREVIATIONS.get(tok, tok)
        out.extend(expanded.split())
    s = " ".join(out)
    # "liner" reads as a tibial articular surface when a liner type is named --
    # the price list calls the same part both things (MLUCX is "Tibial Articular
    # Surface UC" while the ticket says "TIBIAL LINER UC").
    if "liner" in s.split() and critical_attributes(s):
        s = s.replace("liner", "tibial articular surface")
        s = " ".join(dict.fromkeys(s.split()))  # de-dup repeated 'tibial'
    return s


def meaningful_description(normalized: str) -> str:
    return " ".join(t for t in normalized.split() if t not in GENERIC_COMPONENT_WORDS)


def critical_attributes(normalized: str) -> set[str]:
    """The attributes present in a normalized description that make a component a
    materially different device (CR vs PS, PEEK vs Titan, ...)."""
    return {t for t in normalized.split() if t in CRITICAL_ATTRIBUTES}


def attributes_conflict(a: str, b: str) -> bool:
    """True when both sides name critical attributes and they disagree.

    One side being silent is not a conflict (a ticket often omits what the
    catalogue spells out); naming *different* ones is.
    """
    ca, cb = critical_attributes(a), critical_attributes(b)
    if not ca or not cb:
        return False
    return ca != cb
