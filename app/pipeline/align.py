"""Align vision-read lines to decoded barcode labels by content, not position.

The barcode libraries return symbols in decode order (all DataMatrix first,
then linear) while the vision model reads the ticket top-to-bottom, so pairing
the two lists by index attaches handwritten prices/quantities to the wrong
implant whenever the orders diverge. This module re-pairs them by what was
actually read: exact LOT match first, then exact REF match, and only the
leftovers fall back to positional order.

Exact matches only — a misread lot/ref simply falls through to the next pass,
so alignment can never do worse than the old positional behavior.
"""
from __future__ import annotations

from app.pipeline import tracer


def _norm(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip().upper()
    return s or None


def _vval(field) -> str | None:
    """Vision fields are {value, confidence} dicts; tolerate bare values."""
    if isinstance(field, dict):
        return _norm(field.get("value"))
    return _norm(field)


def align_vision_lines(labels: list[dict], vlines: list[dict]) -> list[dict]:
    """Return a list of len(labels): aligned[i] is the vision line for labels[i].

    Labels with no matching vision line get {}. Pass 3 assigns the unmatched
    vision lines to the unmatched labels in original order, which preserves the
    positional behavior for barcode-less lines (padded empty labels never
    content-match, so trailing partner/UNIKO lines land on them in order).
    """
    aligned: list[dict] = [{} for _ in labels]
    label_claimed = [False] * len(labels)
    vline_used = [False] * len(vlines)
    methods = {"lot": 0, "ref": 0, "positional": 0}

    # Pass 1: exact LOT match (the strongest key — lots are unique per label).
    for vi, vline in enumerate(vlines):
        vlot = _vval(vline.get("lot"))
        if not vlot:
            continue
        for li, label in enumerate(labels):
            if label_claimed[li]:
                continue
            if _norm(label.get("lot")) == vlot:
                aligned[li] = vline
                label_claimed[li], vline_used[vi] = True, True
                methods["lot"] += 1
                break

    # Pass 2: exact REF match — against the (240) ref and the GTIN-derived SKU
    # (callers may enrich labels with "_sku" for GTIN-only barcodes).
    for vi, vline in enumerate(vlines):
        if vline_used[vi]:
            continue
        vref = _vval(vline.get("ref"))
        if not vref:
            continue
        for li, label in enumerate(labels):
            if label_claimed[li]:
                continue
            if vref in {_norm(label.get("ref")), _norm(label.get("_sku"))} - {None}:
                aligned[li] = vline
                label_claimed[li], vline_used[vi] = True, True
                methods["ref"] += 1
                break

    # Pass 3: remaining vision lines onto remaining labels, both in order.
    free_labels = [li for li in range(len(labels)) if not label_claimed[li]]
    free_vlines = [vi for vi in range(len(vlines)) if not vline_used[vi]]
    for li, vi in zip(free_labels, free_vlines):
        aligned[li] = vlines[vi]
        methods["positional"] += 1

    if labels or vlines:
        tracer.record(
            "line_alignment",
            "Vision line ↔ barcode alignment",
            "ok",
            f"{methods['lot']} matched by LOT, {methods['ref']} by REF, "
            f"{methods['positional']} positional",
            {"pairs": [
                {
                    "label_ref": labels[li].get("ref") or labels[li].get("_sku"),
                    "label_lot": labels[li].get("lot"),
                    "vision_ref": _vval(aligned[li].get("ref")) if aligned[li] else None,
                    "vision_lot": _vval(aligned[li].get("lot")) if aligned[li] else None,
                }
                for li in range(len(labels))
            ]},
        )
    return aligned
