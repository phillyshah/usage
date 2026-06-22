"""Assemble Ticket + Line Item rows, score every field, and persist.

This is where deterministic device data (barcode + log) and the vision fallback
(handwriting, prices, totals) are merged into the rows that become the workbook,
and where each field's confidence is written to ``field_extractions`` — the
single source of truth the sheet writer reads to colour cells.
"""
from __future__ import annotations

from app.config import settings
from app.db import db
from app.pipeline import confidence as conf
from app.pipeline.reference import resolve_part

# Field-name constants (kept in sync with sheets/write.py).
TICKET_FIELDS = [
    "entity",
    "surgery_date",
    "rep",
    "rep_code",
    "surgeon",
    "hospital",
    "po_number",
    "freight",
    "grand_total",
    "sum_line_totals",
]
LINE_FIELDS = [
    "ref",
    "description",
    "size",
    "lot",
    "qty",
    "mfg_date",
    "expiry_date",
    "unit_price",
    "line_total",
]


def _v(field: dict | None):
    """Unwrap a {value, confidence} pair -> value (or None)."""
    if not isinstance(field, dict):
        return None
    return field.get("value")


def _c(field: dict | None) -> str:
    if not isinstance(field, dict):
        return "low"
    return (field.get("confidence") or "low").lower()


def _num(x):
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def assemble_and_persist(ticket_row: dict, vision: dict, labels: list[dict]) -> dict:
    """Build + persist line items and the ticket header from the merged sources.

    `labels` is the list of decoded barcode dicts (one per readable label).
    `vision` is the parsed Claude result (may be empty).
    Returns a summary {ticket_id, line_count, flags}.
    """
    ticket_id = ticket_row["ticket_id"]
    vheader = vision.get("header", {}) if vision else {}
    vlines = vision.get("lines", []) if vision else []

    # ---- ticket header fields ----
    header_vals: dict = {}
    header_conf: dict = {}
    for f in ["entity", "rep", "rep_code", "surgeon", "hospital", "surgery_date", "po_number"]:
        vf = vheader.get(f)
        val = _v(vf)
        score = conf.score_field({"vision": val, "vision_conf": _c(vf)})
        # Drop sub-threshold vision reads (write nothing, colour red).
        if val is not None and not conf.meets_threshold(_c(vf)) and score != "high":
            val, score = None, "low"
        header_vals[f] = val
        header_conf[f] = score

    # Rep recovery from learned rep map (raises confidence when it agrees).
    rep_code = header_vals.get("rep_code")
    if rep_code:
        learned_rep = db.rep_for_code(rep_code)
        if learned_rep:
            if not header_vals.get("rep"):
                header_vals["rep"], header_conf["rep"] = learned_rep, "medium"
            elif str(learned_rep).strip().lower() == str(header_vals["rep"]).strip().lower():
                header_conf["rep"] = "high"

    freight = _num(_v(vision.get("freight")))
    grand_total = _num(_v(vision.get("grand_total")))
    header_vals["freight"] = freight
    header_conf["freight"] = conf.score_field(
        {"vision": freight, "vision_conf": _c(vision.get("freight"))}
    )
    header_vals["grand_total"] = grand_total
    header_conf["grand_total"] = conf.score_field(
        {"vision": grand_total, "vision_conf": _c(vision.get("grand_total"))}
    )

    hospital = header_vals.get("hospital")

    # ---- line items: merge barcode labels with vision price/qty by order ----
    lines: list[dict] = []
    line_conf: list[dict] = []
    for i, label in enumerate(labels):
        vline = vlines[i] if i < len(vlines) else {}

        # Device identity: prefer the barcode (deterministic), fall back to the
        # REF/LOT that vision read off the printed label. Either one lets the
        # reference log fill in description/size (and LOT recovers the REF).
        vref = _v(vline.get("ref"))
        vlot = _v(vline.get("lot"))
        ref_in = label.get("ref") or vref
        lot_in = label.get("lot") or vlot
        # Did the barcode actually establish identity, or is this OCR-only?
        from_barcode = bool(label.get("ref") or label.get("lot") or label.get("gtin"))

        part = resolve_part(ref_in, label.get("gtin"), lot_in)

        qty = _v(vline.get("qty"))
        qty = int(qty) if isinstance(qty, (int, float)) else (int(qty) if str(qty).isdigit() else None)
        unit_price = _num(_v(vline.get("unit_price")))

        # Hospital price memory: suggestion only, never override.
        price_conf = conf.score_field(
            {"vision": unit_price, "vision_conf": _c(vline.get("unit_price"))}
        )
        if unit_price is not None and part.get("ref") and hospital:
            suggested = db.price_suggestion(part["ref"], hospital)
            if suggested is not None:
                if abs(suggested - unit_price) < settings.sum_tolerance:
                    price_conf = "high"  # learned price agrees -> confident
                else:
                    price_conf = "medium"  # disagreement -> eyeball it (never replace)

        line_total = round(qty * unit_price, 2) if (qty and unit_price is not None) else None

        # Expiry: prefer barcode (exact), cross-check log.
        expiry = label.get("expiry") or part.get("expiry_ref")
        expiry_conf = "high" if label.get("expiry") else ("high" if part.get("expiry_ref") else "low")
        if label.get("expiry") and part.get("expiry_ref") and label["expiry"] != part["expiry_ref"]:
            expiry_conf = "low"

        row = {
            "ticket_id": ticket_id,
            "ref": part.get("ref"),
            "gtin": label.get("gtin"),
            "description": part.get("description"),
            "size": part.get("size"),
            "lot": lot_in,
            "qty": qty,
            "mfg_date": label.get("mfg"),
            "expiry_date": expiry,
            "unit_price": unit_price,
            "line_total": line_total,
            "in_log": part.get("in_log", False),
            "expiry_ref": part.get("expiry_ref"),
            "flags": [],
        }
        # Confidence is earned by validation. A barcode-confirmed, in-log REF is
        # high; an OCR-read REF that still matches the log is medium (legible but
        # a character could be misread — eyeball it); anything unresolved is low.
        in_log = part.get("in_log")
        if in_log:
            ref_conf = "high" if from_barcode else "medium"
            desc_conf = "high" if from_barcode else "medium"
            size_conf = ("high" if from_barcode else "medium") if part.get("size") else "low"
        elif part.get("ref"):
            ref_conf = "medium" if from_barcode else "low"
            desc_conf = "low"
            size_conf = "low"
        else:
            ref_conf = desc_conf = size_conf = "low"
        cmap = {
            "ref": ref_conf,
            "description": desc_conf,
            "size": size_conf,
            "lot": "high" if label.get("lot") else ("medium" if lot_in else "low"),
            "qty": conf.score_field({"vision": qty, "vision_conf": _c(vline.get("qty"))}),
            "mfg_date": "high" if label.get("mfg") else "low",
            "expiry_date": expiry_conf,
            "unit_price": price_conf,
            "line_total": "high" if line_total is not None else "low",
        }
        # GTIN->REF crosswalk: learn when a label gives both a GTIN and a log-confirmed REF.
        if label.get("gtin") and part.get("ref") and part.get("in_log"):
            db.learn_gtin_xref(label["gtin"], part["ref"])

        lines.append(row)
        line_conf.append(cmap)

    # ---- validate (mutates ticket sum_line_totals, returns flags) ----
    ticket_for_validation = {
        "surgery_date": header_vals.get("surgery_date"),
        "grand_total": grand_total,
        "freight": freight,
    }
    flags = conf.validate_ticket(ticket_for_validation, lines)
    sum_line_totals = ticket_for_validation.get("sum_line_totals")
    header_vals["sum_line_totals"] = sum_line_totals
    header_conf["sum_line_totals"] = "high" if sum_line_totals is not None else "low"

    # If totals don't reconcile, drop price/line_total cells to amber for review.
    if any("Grand total" in f for f in flags):
        header_conf["grand_total"] = "medium"
        for cm in line_conf:
            if cm.get("unit_price") == "high":
                cm["unit_price"] = "medium"
            if cm.get("line_total") == "high":
                cm["line_total"] = "medium"

    # ---- persist line items + per-field snapshots ----
    persisted_lines = []
    for row, cmap in zip(lines, line_conf):
        store_row = {k: row[k] for k in (
            "ticket_id", "ref", "gtin", "description", "size", "lot", "qty",
            "mfg_date", "expiry_date", "unit_price", "line_total", "flags",
        )}
        created = db.create_line_item(store_row)
        line_id = created["line_id"]
        persisted_lines.append(created)
        for fname in LINE_FIELDS:
            db.add_field_extraction({
                "ticket_id": ticket_id,
                "line_id": line_id,
                "field_name": fname,
                "orig_value": None if row.get(fname) is None else str(row.get(fname)),
                "confidence": cmap.get(fname, "low"),
                "source": _source_for(fname),
            })

    # ---- persist ticket header + snapshots ----
    ticket_patch = {
        "entity": header_vals.get("entity"),
        "surgery_date": header_vals.get("surgery_date"),
        "rep": header_vals.get("rep"),
        "rep_code": header_vals.get("rep_code"),
        "surgeon": header_vals.get("surgeon"),
        "hospital": header_vals.get("hospital"),
        "po_number": header_vals.get("po_number"),
        "freight": header_vals.get("freight"),
        "grand_total": header_vals.get("grand_total"),
        "sum_line_totals": sum_line_totals,
        "flags": flags,
        "status": "pending_review",
    }
    db.update_ticket(ticket_id, ticket_patch)
    for fname in TICKET_FIELDS:
        db.add_field_extraction({
            "ticket_id": ticket_id,
            "line_id": None,
            "field_name": fname,
            "orig_value": None if header_vals.get(fname) is None else str(header_vals.get(fname)),
            "confidence": header_conf.get(fname, "low"),
            "source": "vision" if fname not in ("sum_line_totals",) else "computed",
        })

    return {"ticket_id": ticket_id, "line_count": len(persisted_lines), "flags": flags}


def _source_for(field: str) -> str:
    if field in ("ref", "description", "size"):
        return "log"
    if field in ("lot", "mfg_date", "expiry_date"):
        return "barcode"
    if field == "line_total":
        return "computed"
    return "vision"
