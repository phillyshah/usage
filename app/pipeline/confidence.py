"""Confidence scoring + business-rule validators.

Confidence is EARNED BY VALIDATION, not self-rating (PROJECT_OVERVIEW principle 3):
  HIGH  -> barcode-decoded & GS1-parsed cleanly; OR >=2 independent sources agree;
           OR stable field pulled by exact log match on a confirmed REF.
  MEDIUM-> single-source vision read above threshold but unverified; OR sources
           mostly agree with a minor discrepancy; OR REF resolved only via an
           un-cross-checked vision read.
  LOW   -> no read / below threshold / sources materially conflict.

Maps to the three cell colours in sheets/write.py: high=no fill, medium=amber,
low=red/blank.
"""
from __future__ import annotations

from datetime import date

from app.config import settings

_RANK = {"low": 0, "medium": 1, "high": 2}


def meets_threshold(conf: str) -> bool:
    """Is a model confidence at/above VISION_CONF_THRESHOLD?"""
    return _RANK.get((conf or "low").lower(), 0) >= _RANK.get(settings.vision_conf_threshold, 1)


def score_field(sources: dict) -> str:
    """Score one field given the evidence available.

    `sources` keys (all optional):
      barcode: value from barcode (exact)
      log:     value from reference log
      vision:  value read by Claude
      vision_conf: model's self-reported confidence for that value
      agree:   explicit bool — caller already determined cross-source agreement
    Returns "high" | "medium" | "low".
    """
    barcode = sources.get("barcode")
    log = sources.get("log")
    vision = sources.get("vision")
    vision_conf = sources.get("vision_conf")

    present = [v for v in (barcode, log, vision) if v not in (None, "")]
    if not present:
        return "low"

    # Count independent sources that agree on a value.
    def _norm(v):
        return str(v).strip().lower()

    distinct = {_norm(v) for v in present}

    # Two+ independent sources agree -> HIGH.
    if len(present) >= 2 and len(distinct) == 1:
        return "high"

    # Barcode-decoded value with no contradiction -> HIGH (exact source).
    if barcode not in (None, "") and len(distinct) == 1:
        return "high"

    # Exact log match on a confirmed REF (caller passes log + agree=True) -> HIGH.
    if sources.get("agree") and log not in (None, ""):
        return "high"

    # Sources present but materially conflict -> LOW.
    if len(present) >= 2 and len(distinct) > 1:
        return "low"

    # Single-source vision read: gated by threshold.
    if vision not in (None, "") and len(present) == 1:
        return "medium" if meets_threshold(vision_conf or "low") else "low"

    # Single log/barcode-only stable field.
    if barcode not in (None, "") or log not in (None, ""):
        return "high"

    return "medium"


# ---------------------------------------------------------------------------
# Business-rule validators
# ---------------------------------------------------------------------------
def _parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def validate_ticket(ticket: dict, lines: list[dict]) -> list[str]:
    """Run every-ticket business rules. Returns a list of flag strings.

    Rules (spec §6):
      * REF exists in log? unknown -> flag.
      * LOT/expiry agreement between barcode and log -> mismatch flag.
      * Dates parse and sit in a sane range -> flag if not.
      * sum(line_total) == grand_total within SUM_TOLERANCE -> flag price cells.
    """
    flags: list[str] = []

    # REF resolves in the part_info master
    for ln in lines:
        if ln.get("ref") and not ln.get("in_part_info", False):
            flags.append(f"REF {ln['ref']} not found in part_info master")

    # LOT expiry agreement (barcode expiry vs log expiry)
    for ln in lines:
        be = _parse_date(ln.get("expiry_date"))
        le = _parse_date(ln.get("expiry_ref"))
        if be and le and be != le:
            flags.append(
                f"Expiry mismatch for lot {ln.get('lot')}: barcode {be} vs log {le}"
            )

    # Date sanity
    today = date.today()
    sd = _parse_date(ticket.get("surgery_date"))
    if sd and (sd.year < 2015 or sd > today):
        flags.append(f"Surgery date {sd} outside sane range")
    for ln in lines:
        ed = _parse_date(ln.get("expiry_date"))
        if ed and ed.year < today.year:
            flags.append(f"Expired lot on line for REF {ln.get('ref')}: expiry {ed}")

    # Sum-to-total reconciliation
    grand = ticket.get("grand_total")
    freight = ticket.get("freight") or 0
    line_sum = sum((ln.get("line_total") or 0) for ln in lines)
    ticket["sum_line_totals"] = round(line_sum, 2)
    if grand is not None:
        try:
            diff = abs(float(grand) - (float(line_sum) + float(freight)))
            if diff > settings.sum_tolerance:
                flags.append(
                    f"Grand total {grand} != sum of lines {line_sum} + freight {freight}"
                )
        except (TypeError, ValueError):
            flags.append("Grand total not numeric")

    return flags
