"""Assemble Ticket + Line Item rows, score every field, and persist.

This is where deterministic device data (barcode + log) and the vision fallback
(handwriting, prices, totals) are merged into the rows that become the workbook,
and where each field's confidence is written to ``field_extractions`` — the
single source of truth the sheet writer reads to colour cells.
"""
from __future__ import annotations

import json
import re

from app.config import settings
from app.db import db, new_id
from app.pipeline import confidence as conf
from app.pipeline.align import align_vision_lines
from app.pipeline.reference import resolve_part, resolve_surgeon

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


def confidence_map_for_ticket(ticket_id: str) -> dict:
    """Reshape field_extractions rows into {header:{field:conf}, lines:{line_id:{field:conf}}}.

    Reuses the same rows sheets/write.py reads to colour workbook cells, so
    on-screen confidence badges (e.g. the Debug Console review form) match
    the exported .xlsx.
    """
    header: dict = {}
    lines: dict = {}
    for fe in db.field_extractions_for_ticket(ticket_id):
        field = fe.get("field_name")
        if not field or field == "raw_blob":
            continue
        c = (fe.get("confidence") or "low").lower()
        line_id = fe.get("line_id")
        if line_id:
            lines.setdefault(line_id, {})[field] = c
        else:
            header[field] = c
    return {"header": header, "lines": lines}


def _is_wasted(vline: dict) -> bool:
    """A handwritten 'W'/'wasted' near a component marks it wasted (still a row)."""
    w = vline.get("wasted")
    val = _v(w) if isinstance(w, dict) else w
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("w", "wasted", "true", "yes", "i/o", "io")
    return False


def _v(field: dict | None):
    """Unwrap a {value, confidence} pair -> value (or None)."""
    if not isinstance(field, dict):
        return None
    return field.get("value")


def _c(field: dict | None) -> str:
    if not isinstance(field, dict):
        return "low"
    return (field.get("confidence") or "low").lower()


_MONEY_RE = re.compile(r"\d+(?:\.\d+)?")


def _num(x):
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _money(x):
    """Parse a handwritten/printed money value into a float.

    Tolerates what the vision read may carry despite the JSON instruction:
    a '$' sign, thousands commas, surrounding text, and accounting parentheses
    for negatives ('($900)'). '$1,900.00' -> 1900.0, '1,900' -> 1900.0,
    'Ø'/'n/a'/'' -> None. Unparseable -> None (cell goes red, never a wrong number).
    """
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace(",", "")
    m = _MONEY_RE.search(s)
    if not m:
        return None
    val = float(m.group())
    return -val if neg else val


def _qty(x) -> int:
    """Parse a handwritten quantity to a positive integer, defaulting to 1.

    Accepts ints/floats and strings like "4", "x4", "Qty 4". Anything missing or
    unparseable (or < 1) becomes 1 — a line is at least one unit.
    """
    if isinstance(x, bool):
        return 1
    if isinstance(x, (int, float)):
        n = int(x)
        return n if n >= 1 else 1
    if x is None:
        return 1
    m = re.search(r"\d+", str(x))
    if not m:
        return 1
    n = int(m.group())
    return n if n >= 1 else 1


def assemble_and_persist(ticket_row: dict, vision: dict, labels: list[dict]) -> dict:
    """Build + persist line items and the ticket header from the merged sources.

    `labels` is the list of decoded barcode dicts (one per readable label).
    `vision` is the parsed Claude result (may be empty).
    Returns a summary {ticket_id, line_count, flags}.
    """
    ticket_id = ticket_row["ticket_id"]
    # Idempotent re-processing: drop any prior line items + field snapshots for
    # this ticket so a re-run replaces them instead of stacking duplicates.
    db.clear_ticket_extractions(ticket_id)
    vheader = vision.get("header", {}) if vision else {}
    vlines = vision.get("lines", []) if vision else []

    # Re-pair vision lines with barcode labels by content (LOT, then REF) —
    # the two lists arrive in different orders (decode order vs top-to-bottom).
    # GTIN-only labels first get a matchable SKU from the GTIN master (or the
    # learned crosswalk) so REF matching isn't blind for them.
    for lbl in labels:
        if lbl.get("gtin") and not lbl.get("ref"):
            grow = db.sku_for_gtin(lbl["gtin"])
            lbl["_sku"] = (grow or {}).get("sku") or db.ref_for_gtin(lbl["gtin"])
    vlines = align_vision_lines(labels, vlines)

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
        from app.pipeline import tracer
        tracer.record(
            "rep_enrichment", "Rep code lookup",
            "ok" if learned_rep else "miss",
            (f"Code '{rep_code}' → '{learned_rep}' (from learning store)"
             if learned_rep else f"Code '{rep_code}' not found in learning store"),
            {"rep_code": rep_code, "vision_rep": header_vals.get("rep"), "learned_rep": learned_rep},
        )

    freight = _money(_v(vision.get("freight")))
    grand_total = _money(_v(vision.get("grand_total")))
    header_vals["freight"] = freight
    header_conf["freight"] = conf.score_field(
        {"vision": freight, "vision_conf": _c(vision.get("freight"))}
    )
    header_vals["grand_total"] = grand_total
    header_conf["grand_total"] = conf.score_field(
        {"vision": grand_total, "vision_conf": _c(vision.get("grand_total"))}
    )

    # Canonical hospital for the price memory: prefer the handwritten hospital,
    # else resolve it from the surgeon+DistCode chain (master, then learned).
    # Used only to key learned prices — the header output is unchanged.
    hospital = header_vals.get("hospital")
    if not hospital:
        _surg = resolve_surgeon(header_vals.get("surgeon"), header_vals.get("rep_code"))
        if _surg.get("matched"):
            hospital = _surg.get("hospital")

    # ---- line items: merge each barcode label with its aligned vision line ----
    lines: list[dict] = []
    line_conf: list[dict] = []
    raw_blobs: list[dict] = []  # exactly what each source produced, pre-resolution
    for i, label in enumerate(labels):
        vline = vlines[i] if i < len(vlines) else {}

        # Capture the raw extraction (device UDI + vision OCR, no PHI) so the
        # workbook's Raw Extraction sheet can show what was actually read before
        # any lookup/resolution — the diagnostic view when output looks empty.
        raw_blobs.append({
            "decoded": bool(label.get("decoded")),
            "payload": label.get("raw"),
            "gtin": label.get("gtin"),
            "lot": label.get("lot"),
            "mfg": label.get("mfg"),
            "expiry": label.get("expiry"),
            "ref": label.get("ref"),
            "vis_ref": _v(vline.get("ref")),
            "vis_lot": _v(vline.get("lot")),
            "vis_price": _v(vline.get("unit_price")),
            "vis_wasted": _is_wasted(vline),
        })

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
        wasted = _is_wasted(vline)

        # Quantity is 1 per labeled physical unit, but when a count is written on
        # the ticket (e.g. "4 pins" for an unlabeled item) we honor it.
        qty_read = _v(vline.get("qty"))
        qty = _qty(qty_read)
        unit_price = _money(_v(vline.get("unit_price")))

        # Hospital price memory: fills a blank price for the SAME hospital
        # (amber + note, so it's always reviewed); never overrides a read price
        # — a read price that disagrees is flagged for an eyeball instead.
        price_conf = conf.score_field(
            {"vision": unit_price, "vision_conf": _c(vline.get("unit_price"))}
        )
        price_note = None
        _suggested_price = None
        _price_filled = False
        if part.get("ref") and hospital:
            _suggested_price = db.price_suggestion(part["ref"], hospital)
        if _suggested_price is not None:
            if unit_price is None:
                unit_price = _suggested_price
                price_conf = "medium"  # learned fill -> always eyeball it
                _price_filled = True
                price_note = (f"Price ${_suggested_price:,.2f} filled from the "
                              f"learned price for this hospital — verify")
            elif abs(_suggested_price - unit_price) < settings.sum_tolerance:
                price_conf = "high"  # learned price agrees -> confident
            else:
                price_conf = "medium"  # disagreement -> eyeball it (never replace)
                price_note = (f"Price differs from the learned price "
                              f"${_suggested_price:,.2f} for this hospital")

        line_total = round(qty * unit_price, 2) if unit_price is not None else None

        # Expiry: prefer barcode (exact), cross-check the Expiry Log.
        expiry = label.get("expiry") or part.get("expiry_ref")
        expiry_conf = "high" if label.get("expiry") else ("high" if part.get("expiry_ref") else "low")
        if label.get("expiry") and part.get("expiry_ref") and label["expiry"] != part["expiry_ref"]:
            expiry_conf = "low"

        # Per-line flags (review signals).
        lflags: list[str] = []
        if wasted:
            lflags.append("WASTED")
        if part.get("gtin") and not part.get("in_gtin_master"):
            lflags.append("GTIN not in product master")
        elif part.get("gtin_status") and part["gtin_status"].strip().lower() != "in use":
            lflags.append(f"GTIN status {part['gtin_status']}")
        if part.get("ref") and not part.get("in_part_info"):
            lflags.append("REF not in part_info")
        if part.get("ref_crosscheck_ok") is False:
            lflags.append("Read REF disagrees with GTIN master")
        if lot_in and not part.get("in_expiry_log"):
            lflags.append("LOT not in Expiry Log")
        if label.get("expiry") and part.get("expiry_ref") and label["expiry"] != part["expiry_ref"]:
            lflags.append("Barcode expiry disagrees with Expiry Log")
        if price_note:
            lflags.append(price_note)
        if unit_price is not None and unit_price >= settings.price_sanity_max:
            if price_conf == "high":
                price_conf = "medium"  # implausible magnitude -> eyeball it
            lflags.append(
                f"Unusually large price ${unit_price:,.2f} — check for a misread digit")

        row = {
            "ticket_id": ticket_id,
            "ref": part.get("ref"),
            "gtin": part.get("gtin"),
            "description": part.get("description"),
            "size": part.get("size"),
            "lot": lot_in,
            "qty": qty,
            "mfg_date": label.get("mfg"),
            "expiry_date": expiry,
            "unit_price": unit_price,
            "line_total": line_total,
            "in_part_info": part.get("in_part_info", False),
            "part_type": part.get("part_type"),
            "category": part.get("category"),
            "expiry_ref": part.get("expiry_ref"),
            "wasted": wasted,
            "flags": lflags,
        }
        # Confidence is earned by validation. A GTIN-master-confirmed REF (exact,
        # deterministic) is high; an OCR-read REF that still resolves in part_info
        # is medium (legible but a character could be misread); unresolved is low.
        if part.get("in_part_info"):
            ref_conf = "high" if part.get("ref_source") == "gtin" else "medium"
            desc_conf = ref_conf
        elif part.get("ref"):
            ref_conf = "medium" if part.get("ref_source") in ("gtin", "gtin_learned") else "low"
            # A description recovered from a correction / the Expiry Log is a
            # real value (worth showing) but not master-confirmed -> medium.
            desc_conf = "medium" if part.get("description") else "low"
        else:
            ref_conf = desc_conf = "low"
        cmap = {
            "ref": ref_conf,
            "description": desc_conf,
            "size": "medium" if part.get("size") else "low",
            "lot": "high" if label.get("lot") else ("medium" if lot_in else "low"),
            # 1-by-default is high; a vision-read count is scored like other reads.
            "qty": (conf.score_field({"vision": qty_read, "vision_conf": _c(vline.get("qty"))})
                    if qty_read not in (None, "") else "high"),
            "mfg_date": "high" if label.get("mfg") else "low",
            "expiry_date": expiry_conf,
            "unit_price": price_conf,
            "line_total": "high" if line_total is not None else "low",
        }
        # GTIN->REF crosswalk: learn when a label gives both a GTIN and a
        # part_info-confirmed REF.
        if part.get("gtin") and part.get("ref") and part.get("in_part_info"):
            db.learn_gtin_xref(part["gtin"], part["ref"])

        # Trace: per-line resolution + confidence for the debug console.
        from app.pipeline import tracer
        _price_trace: dict = {
            "vision_read": None if _price_filled else unit_price,
        }
        if _price_filled:
            _price_trace.update({
                "suggested": _suggested_price,
                "outcome": "filled_from_learned",
            })
        elif _suggested_price is not None and unit_price is not None:
            _diff = abs(_suggested_price - unit_price)
            _price_trace.update({
                "suggested": _suggested_price,
                "diff": round(_diff, 2),
                "outcome": "matches_learned" if _diff < settings.sum_tolerance else "disagrees_with_learned",
            })
        elif unit_price is not None:
            _price_trace["outcome"] = "not_in_learning_store"
        else:
            _price_trace["outcome"] = "no_price_read"
        _ref_label = part.get("ref") or "unknown REF"
        _src = part.get("ref_source") or "none"
        _in_pi = "in product master" if part.get("in_part_info") else "NOT in product master"
        _price_str = f"${unit_price:.2f} ({price_conf})" if unit_price is not None else "no price"
        _flag_str = f" — flags: {', '.join(lflags)}" if lflags else ""
        tracer.record(
            f"line_{i + 1}",
            f"Line {i + 1} — REF {_ref_label}",
            "ok" if part.get("in_part_info") else "warn",
            f"REF {_ref_label} (source: {_src}), {_in_pi}, price {_price_str}{_flag_str}",
            {
                "barcode": {k: label.get(k) for k in
                            ("gtin", "lot", "expiry", "mfg", "ref", "decoded", "raw")},
                "vision": {
                    "ref": _v(vline.get("ref")),
                    "lot": _v(vline.get("lot")),
                    "qty": qty_read,
                    "unit_price": _v(vline.get("unit_price")),
                    "wasted": _is_wasted(vline),
                },
                "part_resolution": part,
                "price": _price_trace,
                "confidence": cmap,
                "wasted": wasted,
                "flags": lflags,
            },
        )

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

    # Reconciliation is an independent cross-check on the handwritten prices.
    totals_off = any("Grand total" in f for f in flags)
    if totals_off:
        # Don't trust the prices if they don't add up — amber for review.
        header_conf["grand_total"] = "medium"
        for cm in line_conf:
            if cm.get("unit_price") == "high":
                cm["unit_price"] = "medium"
            if cm.get("line_total") == "high":
                cm["line_total"] = "medium"
    elif grand_total is not None:
        # Line prices sum to the handwritten Grand Total -> that agreement
        # validates them (spec: independent sources agree -> high).
        header_conf["grand_total"] = "high"
        for cm in line_conf:
            if cm.get("unit_price") == "medium":
                cm["unit_price"] = "high"
            if cm.get("line_total") == "medium":
                cm["line_total"] = "high"

    from app.pipeline import tracer
    _freight_v = freight or 0
    if grand_total is not None:
        _diff_v = round(abs(float(grand_total) - (float(sum_line_totals or 0) + float(_freight_v))), 2)
        _recon_summary = (
            f"Lines ${sum_line_totals} + freight ${_freight_v:.2f} ≠ grand total ${grand_total} "
            f"(diff ${_diff_v}) — prices ↓ amber"
            if totals_off else
            f"Lines ${sum_line_totals} + freight ${_freight_v:.2f} = ${grand_total} ✓ — prices ↑ confident"
        )
    else:
        _diff_v = None
        _recon_summary = "No grand total written — reconciliation skipped"
    tracer.record(
        "totals", "Price reconciliation",
        "warn" if totals_off else "ok",
        _recon_summary,
        {
            "grand_total": grand_total,
            "sum_line_totals": sum_line_totals,
            "freight": freight,
            "diff": _diff_v,
            "reconciled": not totals_off and grand_total is not None,
            "flags": flags,
        },
    )

    # ---- persist line items + per-field snapshots (bulk, 2 round-trips) ----
    # Pre-generate line ids so the field-extraction rows can reference them
    # without a per-line insert/return cycle.
    line_rows: list[dict] = []
    fe_rows: list[dict] = []
    for row, cmap, raw in zip(lines, line_conf, raw_blobs):
        line_id = new_id()
        line_rows.append({
            "line_id": line_id,
            **{k: row[k] for k in (
                "ticket_id", "ref", "gtin", "description", "size", "lot", "qty",
                "mfg_date", "expiry_date", "unit_price", "line_total", "flags",
            )},
        })
        # Raw extraction snapshot (source="raw") read back by the Raw sheet.
        fe_rows.append({
            "ticket_id": ticket_id, "line_id": line_id, "field_name": "raw_blob",
            "orig_value": json.dumps(raw), "confidence": "high", "source": "raw",
        })
        for fname in LINE_FIELDS:
            fe_rows.append({
                "ticket_id": ticket_id, "line_id": line_id, "field_name": fname,
                "orig_value": None if row.get(fname) is None else str(row.get(fname)),
                "confidence": cmap.get(fname, "low"), "source": _source_for(fname),
            })

    db.create_line_items(line_rows)

    # ---- persist ticket header ----
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
        fe_rows.append({
            "ticket_id": ticket_id, "line_id": None, "field_name": fname,
            "orig_value": None if header_vals.get(fname) is None else str(header_vals.get(fname)),
            "confidence": header_conf.get(fname, "low"),
            "source": "vision" if fname not in ("sum_line_totals",) else "computed",
        })

    # One bulk insert for every field snapshot on this ticket.
    db.add_field_extractions(fe_rows)

    return {"ticket_id": ticket_id, "line_count": len(line_rows), "flags": flags}


def _source_for(field: str) -> str:
    if field in ("ref", "description", "size"):
        return "log"
    if field in ("lot", "mfg_date", "expiry_date"):
        return "barcode"
    if field == "line_total":
        return "computed"
    return "vision"
