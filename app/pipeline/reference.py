"""Device + surgeon resolution against the reference masters.

Most output columns are joins, not reads (FIELD_GUIDE §5). The deterministic
chain per device line:

  (01) GTIN-14  --reference_gtin-->  SKU (= Ref Number) + product status
  Ref Number    --part_info------->  Description / Part Type / Category
  (10) LOT      --Expiry Log------>  authoritative lot expiry (validation)

(240) and any printed/vision REF are cross-checks on the GTIN-derived SKU. When
the barcode is unreadable, a vision-read REF/LOT still drives the same joins.

The surgeon header drives a second chain:

  <SurgeonLastName><DistCode>  --surgeon_info-->  SurgeonName / Hospital /
                                                  Region / canonical DistCode
"""
from __future__ import annotations

from app.db import db
from app.learning.ingest_reference import surgeon_key


def resolve_part(ref: str | None, gtin: str | None, lot: str | None) -> dict:
    """Resolve a device line to its Ref Number and reference attributes.

    `ref` is any printed/vision/(240)-read REF; `gtin` is the decoded (01);
    `lot` is the decoded/read (10). Returns the joined attributes plus the
    provenance and validation flags the confidence model needs.
    """
    result: dict = {
        "ref": None,
        "ref_source": None,        # gtin | gtin_learned | printed | lot | None
        "gtin": gtin,
        "gtin_status": None,
        "in_gtin_master": False,
        "description": None,
        "size": None,
        "part_type": None,
        "category": None,
        "in_part_info": False,
        "desc_source": None,       # part_info | correction | expiry_log | None
        "expiry_ref": None,
        "in_expiry_log": False,
        "ref_crosscheck_ok": None,  # gtin-SKU vs printed/(240) REF agree?
    }

    # 1. Ref Number — GTIN master is primary; the learned GTIN→REF crosswalk
    # (built from your corrections) covers master misses; (240)/printed/lot
    # remain the fallbacks.
    sku = None
    ref_source = "gtin"
    if gtin:
        grow = db.sku_for_gtin(gtin)
        if grow:
            result["in_gtin_master"] = True
            result["gtin_status"] = grow.get("status")
            sku = grow.get("sku")
        else:
            learned_sku = db.ref_for_gtin(gtin)
            if learned_sku:
                sku, ref_source = learned_sku, "gtin_learned"
    if sku:
        result["ref"], result["ref_source"] = sku, ref_source
        if ref:  # cross-check the read REF against the authoritative SKU
            result["ref_crosscheck_ok"] = (str(ref).strip() == str(sku).strip())
    elif ref:
        result["ref"], result["ref_source"] = ref, "printed"
    elif lot:
        lot_row = db.lot_lookup(lot)
        if lot_row and lot_row.get("part_no"):
            result["ref"], result["ref_source"] = lot_row["part_no"], "lot"

    # 2. Description / Part Type / Category via part_info (exact REF, incl. +/-).
    # On a part_info miss, fall back to the learned descriptions (from your
    # corrections) or the Expiry Log parts sheet.
    if result["ref"]:
        pinfo = db.part_info_for_ref(result["ref"])
        if pinfo:
            result["in_part_info"] = True
            result["desc_source"] = "part_info"
            result["description"] = pinfo.get("description")
            result["part_type"] = pinfo.get("part_type")
            result["category"] = pinfo.get("category")
        else:
            prow = db.resolve_part_desc(result["ref"])
            if prow and (prow.get("description") or prow.get("size")):
                result["description"] = prow.get("description")
                result["size"] = prow.get("size")
                result["desc_source"] = (
                    "correction" if prow.get("from_correction") else "expiry_log"
                )

    # 3. Authoritative lot expiry from the Expiry Log (cross-check vs barcode).
    if lot:
        lot_row = db.lot_lookup(lot)
        if lot_row:
            result["in_expiry_log"] = True
            result["expiry_ref"] = lot_row.get("expiry_date")

    return result


def resolve_surgeon(surgeon_last_name: str | None, dist_code: str | None) -> dict:
    """Resolve the header surgeon+DistCode to the surgeon_info record.

    Key = <SurgeonLastName><DistCode>. A miss is the "Distributor Code must match
    surgeon" flag (FIELD_GUIDE §6). Returns the joined columns + a matched flag.
    """
    result: dict = {
        "matched": False,
        "source": None,             # master | learned | None
        "key": surgeon_key(surgeon_last_name, dist_code),
        "surgeon_full_name": None,
        "hospital": None,
        "region": None,
        "dist_code": None,
        "distributor_rep": None,
        "sales_manager": None,
        "status": None,
    }
    row = db.surgeon_for_key(result["key"]) if result["key"] else None
    if row:
        result.update({
            "matched": True,
            "source": "master",
            "surgeon_full_name": row.get("surgeon_full_name"),
            "hospital": row.get("hospital"),
            "region": row.get("region"),
            "dist_code": row.get("dist_code"),
            "distributor_rep": row.get("distributor_rep"),
            "sales_manager": row.get("sales_manager"),
            "status": row.get("status"),
        })
        return result

    # Master miss: fall back to the surgeon links learned from corrections.
    learned = db.learned_surgeon_for_key(result["key"]) if result["key"] else None
    if learned:
        result.update({
            "matched": True,
            "source": "learned",
            "surgeon_full_name": learned.get("surgeon_full_name"),
            "hospital": learned.get("hospital"),
            "dist_code": learned.get("dist_code"),
        })
    return result
