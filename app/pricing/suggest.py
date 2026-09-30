"""What price can we offer for a line we couldn't read one for?

Extraction-time only, and deliberately separate from step 5. Step 5 works on a
finished workbook and has the hospital price list to look things up in; this
works during extraction and has only what previous tickets taught us.

Pure: no database, no settings, no I/O. The caller supplies the learned rows.
That keeps the policy testable on its own and keeps pricing judgement out of
the storage layer.

The rungs, best evidence first:

  1. a learned price for THIS hospital, matched exactly or by a known alias
  2. the same, matched only loosely (core words or fuzzy) — a real price for a
     name we are less sure refers to this account
  3. the typical price for this REF across other hospitals, when enough of them
     agree closely

Every rung is a suggestion, never a fact, so every one of them is amber and
carries a sentence saying where it came from. ``may_confirm`` is the one thing
that varies: only an exact or aliased hospital match is trustworthy enough that
agreement with a read price should raise that price to confident. A ``core``
match collapses 'Boca Raton Hospital' and 'Boca Raton Surg Ctr', which are
plausibly two different accounts with two different prices, so it must never
promote anything.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.pricing import estimate as est
from app.pricing import match as mt


@dataclass(frozen=True)
class PriceSuggestion:
    value: float
    basis: str          # one sentence for the Notes column
    source: str         # learned_price | learned_price_fuzzy | learned_price_cross
    may_confirm: bool   # may agreement with a read price promote it to high?


def _money(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def suggest_price(learned_rows: list[dict], hospital: str | None) -> PriceSuggestion | None:
    """Best available suggestion for one REF, or None.

    ``learned_rows`` are the learning_price rows for that REF, each with at
    least ``hospital`` and ``unit_price``.
    """
    rows = [r for r in (learned_rows or []) if _money(r.get("unit_price")) is not None]
    if not rows:
        return None

    names = [str(r.get("hospital") or "") for r in rows]
    m = mt.match_hospital(hospital, names) if hospital else None

    if m is not None and m.matched:
        at = [_money(r["unit_price"]) for r in rows
              if str(r.get("hospital") or "") == m.name]
        vals = {round(v, 2) for v in at if v is not None}
        if len(vals) == 1:
            value = vals.pop()
            if m.confident:
                return PriceSuggestion(
                    value=value,
                    basis=f"Price ${value:,.2f} filled from the learned price for this hospital",
                    source="learned_price",
                    may_confirm=True,
                )
            return PriceSuggestion(
                value=value,
                basis=(f"Price ${value:,.2f} filled from the learned price for "
                       f"'{m.name}', matched loosely to this ticket's "
                       f"'{hospital}' — check it is the same account"),
                source="learned_price_fuzzy",
                may_confirm=False,
            )

    # Nothing for this hospital. Do the others agree closely enough to be
    # evidence? Prices genuinely differ by hospital, so this bar is high.
    values = [v for v in (_money(r.get("unit_price")) for r in rows) if v is not None]
    typical = est.cross_hospital_typical(values)
    if typical is None:
        return None
    return PriceSuggestion(
        value=round(typical, 2),
        basis=(f"Price ${typical:,.2f} is the typical price for this part across "
               f"{len(values)} other hospitals — nothing on file for this one"),
        source="learned_price_cross",
        may_confirm=False,
    )
