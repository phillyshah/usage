"""Barcode decode + GS1 parse. Deterministic, primary source for device fields.

Maxx device labels carry a GS1 DataMatrix (FDA UDI) and usually a linear barcode.
We decode with pylibdmtx (DataMatrix) and pyzbar (linear/QR), then parse the GS1
Application Identifiers with biip (don't hand-roll FNC1/GS handling).

Every heavy import is guarded so the module imports cleanly where the native libs
aren't installed; decoding then simply yields nothing and the vision fallback
covers the labels.
"""
from __future__ import annotations

try:
    from pylibdmtx import pylibdmtx

    _HAS_DMTX = True
except Exception:  # pragma: no cover
    pylibdmtx = None  # type: ignore
    _HAS_DMTX = False

try:
    from pyzbar import pyzbar

    _HAS_ZBAR = True
except Exception:  # pragma: no cover
    pyzbar = None  # type: ignore
    _HAS_ZBAR = False

try:
    from biip import ParseError
    from biip.gs1_messages import GS1Message

    _HAS_BIIP = True
except Exception:  # pragma: no cover
    GS1Message = None  # type: ignore
    ParseError = Exception  # type: ignore
    _HAS_BIIP = False


def available() -> bool:
    return (_HAS_DMTX or _HAS_ZBAR) and _HAS_BIIP


def _raw_payloads(crop) -> list[str]:
    """Return all decoded raw strings from a single label crop."""
    out: list[str] = []
    if crop is None:
        return out
    if _HAS_DMTX:
        try:
            for r in pylibdmtx.decode(crop, timeout=2000):
                out.append(r.data.decode("utf-8", "replace"))
        except Exception:
            pass
    if _HAS_ZBAR:
        try:
            for r in pyzbar.decode(crop):
                out.append(r.data.decode("utf-8", "replace"))
        except Exception:
            pass
    return out


def _parse_gs1(raw: str) -> dict:
    """Parse a GS1 element string into our device-field dict."""
    fields: dict = {
        "gtin": None,
        "lot": None,
        "expiry": None,
        "mfg": None,
        "serial": None,
        "raw": raw,
        "decoded": False,
    }
    if not raw:
        return fields
    if not _HAS_BIIP:
        # Without biip we still keep the raw payload for audit; no parse.
        return fields
    try:
        msg = GS1Message.parse(raw)
    except ParseError:
        return fields
    except Exception:
        return fields

    for e in msg.element_strings:
        ai = e.ai.ai
        if ai == "01" and e.gtin is not None:
            fields["gtin"] = e.gtin.value
        elif ai == "10":
            fields["lot"] = e.value
        elif ai == "17" and e.date is not None:
            fields["expiry"] = e.date.isoformat()
        elif ai == "11" and e.date is not None:
            fields["mfg"] = e.date.isoformat()
        elif ai == "21":
            fields["serial"] = e.value

    fields["decoded"] = any(
        fields[k] for k in ("gtin", "lot", "expiry", "mfg", "serial")
    )
    return fields


def decode_labels(label_crops: list) -> list[dict]:
    """Per label: decode DataMatrix/linear, parse GS1 via biip.

    -> list of {gtin, lot, expiry, mfg, serial, raw, decoded}
    One entry per crop (decoded=False when nothing readable), so callers can
    line up crops with the label grid and send only the failures to vision.
    """
    results: list[dict] = []
    for crop in label_crops:
        payloads = _raw_payloads(crop)
        best = {
            "gtin": None,
            "lot": None,
            "expiry": None,
            "mfg": None,
            "serial": None,
            "raw": None,
            "decoded": False,
        }
        for raw in payloads:
            parsed = _parse_gs1(raw)
            # Prefer a GS1-parsed payload over a bare linear code.
            if parsed["decoded"]:
                best = parsed
                break
            if best["raw"] is None:
                best = parsed
        results.append(best)
    return results


def decode_region(grid_img) -> list[dict]:
    """Decode every barcode found anywhere in a grid region (no pre-segmentation).

    pyzbar/pylibdmtx return all symbols in the image, so each physical device
    label that carries a readable barcode yields one entry. Deduplicates on raw
    payload. Returns [] when nothing is readable.
    """
    payloads = _raw_payloads(grid_img)
    seen: set[str] = set()
    results: list[dict] = []
    for raw in payloads:
        if raw in seen:
            continue
        seen.add(raw)
        results.append(_parse_gs1(raw))
    return results


def decode_single(raw_payload: str) -> dict:
    """Parse one already-decoded raw GS1 string (used in tests/tooling)."""
    return _parse_gs1(raw_payload)
