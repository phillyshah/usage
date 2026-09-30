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
from app.pricing.suggest import suggest_price

# Field-name constants (kept in sync with sheets/write.py).
TICKET_FIELDS = [
    "entity",
    "surgery_date",
    "rep",
    "rep_code",
    "surgeon",
    # Two letters off the patient sticker, and only when
    # EXTRACT_PATIENT_INITIALS is on. Nothing else about the patient is read,
    # returned or stored.
    "patient_initials",
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
    # align_vision_lines returns one entry PER LABEL, so any vision line beyond
    # the barcode count is discarded. That silently lost every handwritten line:
    # a ticket with five implant labels and three hand-written pin lines came
    # back with five rows, and the three pin lines — real billable items, $175
    # of a $6,665 ticket — never reached the workbook at all.
    #
    # Padding gives each vision line a slot. An empty label carries no ref, lot
    # or gtin, so it never content-matches in passes 1 and 2 and the extra lines
    # land on the padded slots in order, which is exactly what align.py's
    # docstring describes.
    if len(vlines) > len(labels):
        labels = list(labels) + [{} for _ in range(len(vlines) - len(labels))]
    vlines = align_vision_lines(labels, vlines)

    # ---- ticket header fields ----
    header_vals: dict = {}
    header_conf: dict = {}
    # Fields the model read but was not sure about. Named in the ticket notes so
    # an amber cell always says why it is amber.
    weak_reads: list[str] = []
    # Notes raised before validate_ticket builds the ticket's flag list.
    flags_early: list[str] = []
    for f in ["entity", "rep", "rep_code", "surgeon", "hospital", "surgery_date", "po_number"]:
        vf = vheader.get(f)
        val = _v(vf)
        score = conf.score_field({"vision": val, "vision_conf": _c(vf)})
        # A sub-threshold read used to be DELETED here — the reviewer got an
        # empty red cell and was asked to find a value the tool was holding all
        # along. Keep it, show it amber, and say the model was unsure. Deciding
        # is cheaper than looking up.
        if val is not None and not conf.meets_threshold(_c(vf)) and score != "high":
            score = "medium"
            weak_reads.append(f)
        header_vals[f] = val
        header_conf[f] = conf.at_least_amber(score, val)

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
    header_conf["freight"] = conf.at_least_amber(conf.score_field(
        {"vision": freight, "vision_conf": _c(vision.get("freight"))}), freight)
    header_vals["grand_total"] = grand_total
    header_conf["grand_total"] = conf.at_least_amber(conf.score_field(
        {"vision": grand_total, "vision_conf": _c(vision.get("grand_total"))}),
        grand_total)

    # ---- hospital: resolved once, here, and actually published ----
    #
    # This used to compute the surgeon-chain fallback, use it as a price key and
    # then throw it away ("the header output is unchanged"), while the workbook
    # separately re-ran the same lookup and reddened the Hospital column whenever
    # the surgeon chain missed — on tickets that plainly read ENLOE, SCHNEIDER
    # HOSPITAL, PARAGON SURGICAL CENTER. Resolving once and publishing the result
    # also stops the Usage and Tickets sheets disagreeing about the same field.
    #
    # The handwritten value outranks the master: it is what the surgeon wrote on
    # THIS ticket, where the master is what was true when it was last exported.
    from app.pricing import normalize as nz

    _surg = resolve_surgeon(header_vals.get("surgeon"), header_vals.get("rep_code"))
    _chain = _surg.get("hospital") if _surg.get("matched") else None
    hospital = header_vals.get("hospital")
    if hospital and _chain:
        if nz.normalize_hospital(hospital) == nz.normalize_hospital(_chain):
            header_conf["hospital"] = "high"      # two independent sources agree
        else:
            header_conf["hospital"] = "medium"
            flags_early.append(
                f"Hospital read as '{hospital}' but the surgeon record says "
                f"'{_chain}' — check which is right")
    elif hospital:
        header_conf["hospital"] = conf.at_least_amber(
            header_conf.get("hospital", "low"), hospital)
    elif _chain:
        hospital = _chain
        header_vals["hospital"] = _chain
        header_conf["hospital"] = "medium" if _surg.get("source") == "learned" else "high"
        flags_early.append(
            f"Hospital taken from the surgeon record for "
            f"{header_vals.get('surgeon') or 'this surgeon'} — not written on the ticket")
    else:
        # Neither. The DistCode alone may still be unambiguous.
        _by_code = db.hospital_for_dist_code(header_vals.get("rep_code"))
        if _by_code:
            hospital = _by_code
            header_vals["hospital"] = _by_code
            header_conf["hospital"] = "medium"
            flags_early.append(
                f"Hospital inferred from DistCode {header_vals.get('rep_code')}, "
                f"which has only ever been {_by_code} — verify")

    # ---- line items: merge each barcode label with its aligned vision line ----
    lines: list[dict] = []
    line_conf: list[dict] = []
    line_source: list[dict] = []
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

        part = resolve_part(ref_in, label.get("gtin"), lot_in,
                            ref_from_barcode=bool(label.get("ref")))
        wasted = _is_wasted(vline)

        # A handwritten line carries its own description, and usually a part
        # number the masters have never heard of — pins, screws, instruments and
        # other disposables trail the implant master permanently. Requiring a
        # master match would drop them, and they are real money: three pin lines
        # on one real ticket accounted for $175 of a $6,665 total, and dropping
        # them is exactly the gap the grand-total reconciliation then reports.
        hand_desc = _v(vline.get("description"))
        if hand_desc and not part.get("description"):
            part["description"] = str(hand_desc).strip()
            part["desc_source"] = "handwritten"

        # Quantity is 1 per labeled physical unit, but when a count is written on
        # the ticket (e.g. "4 pins" for an unlabeled item) we honor it.
        qty_read = _v(vline.get("qty"))
        qty = _qty(qty_read)
        unit_price = _money(_v(vline.get("unit_price")))

        # Price memory. Fills a blank price (amber + a note saying where the
        # number came from, so it is always reviewed) and never overrides a
        # read price — a read price that disagrees is flagged for an eyeball
        # instead.
        price_conf = conf.score_field(
            {"vision": unit_price, "vision_conf": _c(vline.get("unit_price"))}
        )
        price_note = None
        price_source = None
        _price_filled = False
        _sugg = None
        if part.get("ref"):
            _sugg = suggest_price(db.learning_prices_for_part(part["ref"]), hospital)
        if _sugg is not None:
            if unit_price is None:
                unit_price = _sugg.value
                price_conf = "medium"          # a fill is always eyeballed
                price_source = _sugg.source
                _price_filled = True
                price_note = _sugg.basis + " — verify"
            elif _sugg.source == "learned_price_cross":
                # A cross-hospital figure fills a blank and says nothing more.
                # Prices legitimately differ by hospital, so disagreement with
                # one is not evidence of a misread, and flagging it would fill
                # the Notes column with noise.
                pass
            elif abs(_sugg.value - unit_price) < settings.sum_tolerance:
                # Agreement only counts when we are sure it is the same account.
                if _sugg.may_confirm:
                    price_conf = "high"
                else:
                    price_conf = "medium"
                    price_note = (f"Matches a learned price, but for a hospital "
                                  f"name only loosely matched to '{hospital}' "
                                  f"— confirm")
            else:
                price_conf = "medium"          # never replace a read price
                price_note = (f"Price differs from the learned price "
                              f"${_sugg.value:,.2f} for this hospital")

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
            ref_conf = ("high" if part.get("ref_source") in ("gtin", "barcode_240")
                        else "medium")
            desc_conf = ref_conf
        elif part.get("ref"):
            # A GS1-decoded REF is certain whether or not the master knows the
            # part yet; the master's ignorance is a gap in our reference data,
            # not a doubt about the number.
            if part.get("ref_source") == "barcode_240":
                ref_conf = "high"
            else:
                # Read off the label or written on the form, and the master has
                # never heard of it — which is routine for disposables and for
                # hip components the master hasn't caught up with. We still have
                # the number: show it amber with the "not in part_info" flag
                # beside it, rather than blanking a REF we were handed.
                ref_conf = "medium"
            # A description recovered from a correction / the Expiry Log is a
            # real value (worth showing) but not master-confirmed -> medium.
            # One read off the operator's own handwriting is worth showing too,
            # but it is a read of handwriting, so it stays amber for review.
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
                "suggested": _sugg.value,
                "suggestion_source": _sugg.source,
                "outcome": "filled_from_learned",
            })
        elif _sugg is not None and unit_price is not None:
            _diff = abs(_sugg.value - unit_price)
            _price_trace.update({
                "suggested": _sugg.value,
                "suggestion_source": _sugg.source,
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

        # Where each value came from, when it wasn't the obvious place. Read
        # back by the learning harvest, which must not mistake a suggestion
        # nobody touched for a human asserting a price.
        smap = {}
        if price_source:
            smap["unit_price"] = price_source

        lines.append(row)
        line_conf.append(cmap)
        line_source.append(smap)

    # ---- the grand total as arithmetic, not just a cross-check ----
    apportioned = _apportion_grand_total(
        lines, line_conf, line_source, grand_total, freight, flags_early)

    # ---- validate (mutates ticket sum_line_totals, returns flags) ----
    ticket_for_validation = {
        "surgery_date": header_vals.get("surgery_date"),
        "grand_total": grand_total,
        "freight": freight,
    }
    flags = conf.validate_ticket(ticket_for_validation, lines)
    flags = flags_early + flags
    # A failed extraction must never look like a blank ticket. Without this the
    # two are indistinguishable in the workbook: same empty cells, same red,
    # same "batch complete". One whole batch of nine went out that way.
    if weak_reads:
        _pretty = {"rep_code": "DistCode", "po_number": "PO number",
                   "surgery_date": "Surgery date", "patient_initials": "Initials"}
        _names = ", ".join(_pretty.get(f, f.replace("_", " ").capitalize())
                           for f in weak_reads)
        flags.append(f"Read but the model was unsure: {_names} — confirm before use")

    vision_error = vision.get("error") if isinstance(vision, dict) else None
    if vision_error:
        flags.insert(0, (
            f"EXTRACTION FAILED ({vision_error}) — nothing handwritten on this "
            f"ticket was read: no surgeon, hospital, date, prices or totals. "
            f"Re-run it; do not treat the blanks as a blank ticket."
        ))
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
    elif grand_total is not None and not apportioned:
        # Line prices sum to the handwritten Grand Total -> that agreement
        # validates them (spec: independent sources agree -> high).
        #
        # Only when the agreement is INDEPENDENT. If a price was just derived
        # from that same total, the ticket reconciles by construction and the
        # agreement proves nothing — promoting every price on the strength of it
        # would be the tool citing its own arithmetic back to itself.
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
    for row, cmap, smap, raw in zip(lines, line_conf, line_source, raw_blobs):
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
                "confidence": cmap.get(fname, "low"),
                "source": smap.get(fname) or _source_for(fname),
            })

    db.create_line_items(line_rows)

    # Patient initials come from the one extraction call, like every other
    # header field. They used to need a second, separate API call on a cropped
    # sticker, because the stored image was masked and this module never saw the
    # patient area; with the mask gone that crop — and the fixed fractional
    # coordinates it depended on — is unnecessary.
    #
    # EXTRACT_PATIENT_INITIALS still decides whether the two letters are KEPT.
    # It can no longer decide whether they are read: the sticker is in the image
    # the model is shown either way. Off means the value is discarded here and
    # never reaches the database or the workbook.
    _inits = _clean_initials(_v(vheader.get("patient_initials")))
    if not settings.extract_patient_initials:
        _inits = None
    header_vals["patient_initials"] = _inits
    header_conf["patient_initials"] = conf.at_least_amber(
        conf.score_field({"vision": _inits,
                          "vision_conf": _c(vheader.get("patient_initials"))}),
        _inits,
    )

    # ---- persist ticket header ----
    ticket_patch = {
        "entity": header_vals.get("entity"),
        "surgery_date": header_vals.get("surgery_date"),
        "rep": header_vals.get("rep"),
        "rep_code": header_vals.get("rep_code"),
        "surgeon": header_vals.get("surgeon"),
        "patient_initials": header_vals.get("patient_initials"),
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

    return {"ticket_id": ticket_id, "line_count": len(line_rows), "flags": flags,
            "vision_error": vision_error}


def _apportion_grand_total(lines, line_conf, line_source, grand_total, freight,
                           ticket_flags: list) -> bool:
    """Fill the one unpriced line from what the grand total leaves over.

    The circled Grand Total is the biggest, clearest, most deliberate figure on
    these tickets, and when exactly one line has no price the arithmetic gives
    that price exactly. The residual was already being computed — for a log
    message — and then thrown away, while the blank cell went red and the
    reviewer was asked to work out a number the tool was holding.

    Returns whether a price was written, because the caller must NOT then treat
    the ticket's reconciliation as independent evidence: after this, the lines
    add up to the total by construction, and promoting every price to confident
    on the strength of that would be circular.
    """
    if grand_total is None:
        return False
    priced = sum((ln.get("line_total") or 0) for ln in lines)
    residual = round(float(grand_total) - float(freight or 0) - float(priced), 2)
    blanks = [i for i, ln in enumerate(lines) if ln.get("unit_price") is None]

    if not blanks:
        return False
    if len(blanks) > 1:
        if residual > settings.sum_tolerance:
            each = round(residual / len(blanks), 2)
            ticket_flags.append(
                f"The grand total leaves ${residual:,.2f} unaccounted across "
                f"{len(blanks)} lines with no price (about ${each:,.2f} each if "
                f"they are equal) — fill them in")
        return False

    i = blanks[0]
    row, cm = lines[i], line_conf[i]
    qty = row.get("qty") or 1
    unit = round(residual / qty, 2)

    if row.get("wasted"):
        row["flags"].append(
            f"The grand total implies ${unit:,.2f} for this wasted line — whether "
            f"a wasted component is billed is a business decision, so it was "
            f"left blank")
        return False
    if residual <= settings.sum_tolerance:
        # Never write a zero: a zero price asserts "this was free", which is a
        # different claim from "we don't know".
        row["flags"].append(
            "The other lines already account for the whole grand total — this "
            "line may not be billable")
        return False
    if unit >= settings.price_sanity_max:
        row["flags"].append(
            f"The grand total implies ${unit:,.2f} for this line, which is too "
            f"large to be plausible — left blank rather than filled in")
        return False

    row["unit_price"] = unit
    row["line_total"] = round(qty * unit, 2)
    cm["unit_price"] = "medium"
    # Derived, not read: the Line Total must not render as confident either.
    cm["line_total"] = "medium"
    line_source[i]["unit_price"] = "grand_total_residual"

    note = (f"Price ${unit:,.2f} derived from the grand total — this is the only "
            f"line without a price")
    if freight is None:
        note += ". No freight was read, so this assumes there is none"
        ticket_flags.append(
            "A price was derived from the grand total with no freight figure "
            "read — if this ticket has a delivery fee, that money is currently "
            "inside that line's price")
    if qty > 1 and abs(round(qty * unit, 2) - residual) > settings.sum_tolerance:
        note += f". ${residual:,.2f} does not divide evenly across {qty} units"
    note += ". Verify"
    row["flags"].append(note)
    return True


def _clean_initials(value) -> str | None:
    """Exactly two letters, or nothing.

    Rejects rather than truncates. Truncating "John Doe" to "JO" would be the
    worst possible outcome — a wrong answer wearing the shape of a right one,
    in a column nobody has any way to check. Punctuation and spacing are
    tolerated ("J.D.", "j d") because they are formatting, not content.
    """
    if value is None:
        return None
    letters = re.sub(r"[^A-Za-z]", "", str(value))
    return letters.upper() if len(letters) == 2 else None


def _source_for(field: str) -> str:
    if field in ("ref", "description", "size"):
        return "log"
    if field in ("lot", "mfg_date", "expiry_date"):
        return "barcode"
    if field == "line_total":
        return "computed"
    return "vision"
