"""Confidence scoring + business-rule validators.

Confidence is EARNED BY VALIDATION, not self-rating (PROJECT_OVERVIEW principle 3):
  HIGH  -> barcode-decoded & GS1-parsed cleanly; OR >=2 independent sources agree;
           OR stable field pulled by exact log match on a confirmed REF.
  MEDIUM-> single-source vision read above threshold but unverified; OR sources
           mostly agree with a minor discrepancy; OR REF resolved only via an
           un-cross-checked vision read.
  LOW   -> nothing to offer: no read at all, and no fallback produced a
           candidate.

Maps to the three cell colours in sheets/write.py: high=no fill, medium=amber,
low=red/blank.

MEDIUM now also covers a read the model was unsure about, and a value supplied
by a fallback (a learned price, a hospital from the surgeon record, a number the
ticket's own total determines). Those used to be scored LOW and therefore
DELETED — the reviewer was shown an empty red cell and asked to type in a value
the tool was holding. Amber says "here is a candidate, confirm it"; red now means
only "there was genuinely nothing to propose". See at_least_amber.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.config import settings

_RANK = {"low": 0, "medium": 1, "high": 2}


def meets_threshold(conf: str) -> bool:
    """Is a model confidence at/above VISION_CONF_THRESHOLD?"""
    return _RANK.get((conf or "low").lower(), 0) >= _RANK.get(settings.vision_conf_threshold, 1)


def at_least_amber(score: str, value) -> str:
    """A value we actually have is never thrown away: present -> at least amber.

    Deliberately applied at the call sites rather than inside score_field.
    score_field answers "what does the evidence support", and its "low" for two
    conflicting sources is a true and useful answer. This answers the different
    question of what to SHOW, and the two should not be confused — each call
    site also attaches the note that explains why the cell is amber.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "low"
    return "medium" if (score or "low").lower() == "low" else score


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


# How far ahead of today a surgery date may sit before we stop believing the
# current year. Small, but not zero: a ticket written the day before surgery, a
# clock skew, or a date entered ahead of the operation all land slightly in the
# future and are not mistakes.
_FUTURE_SLACK = timedelta(days=30)


def correct_surgery_year(value, today: date | None = None) -> tuple[str | None, str | None]:
    """``(corrected, original)`` — the year fixed, and what it was.

    These tickets are never from a prior year: the business runs on the current
    one, and a 2024 on a 2026 ticket is a slip of the pen or a misread digit.
    One real example, from the tickets this was built against: a form reading
    "Sept. 28, 2024" whose own patient sticker gave a DOS of 9/28/2026.

    NOT a blind "force the current year", because that breaks every January.
    A surgery on 28 December processed on 3 January is genuinely from the prior
    year, and stamping this year on it would move it eleven months into the
    future — turning a correct date into a wrong one, which is worse than the
    problem being fixed. So the current year is used unless it lands the date
    implausibly ahead of today, in which case the previous year is right and is
    kept.

    ``original`` is None when nothing was changed, so the caller can tell the
    difference between "already fine" and "rewritten" and say so.
    """
    today = today or date.today()
    sd = _parse_date(value)
    if sd is None:
        return value, None

    try:
        candidate = sd.replace(year=today.year)
    except ValueError:
        # 29 February in a non-leap year. Guessing which way to nudge it is
        # worse than leaving a date somebody can read for themselves.
        return value, None
    if candidate > today + _FUTURE_SLACK:
        try:
            candidate = sd.replace(year=today.year - 1)
        except ValueError:
            return value, None

    if candidate == sd:
        return value, None
    return candidate.isoformat(), sd.isoformat()


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
