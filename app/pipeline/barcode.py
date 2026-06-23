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


import calendar


def available() -> bool:
    return (_HAS_DMTX or _HAS_ZBAR) and _HAS_BIIP


def gtin_check_digit_ok(gtin14: str | None) -> bool:
    """Validate a GTIN-14 mod-10 check digit (FIELD_GUIDE §4)."""
    if not (gtin14 and gtin14.isdigit() and len(gtin14) == 14):
        return False
    body = [int(c) for c in gtin14[:13]]
    check = int(gtin14[13])
    s = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - s % 10) % 10 == check


def _yymmdd(s: str) -> str | None:
    """GS1 YYMMDD -> 'YYYY-MM-DD'. DD=00 means end of month (GS1 convention)."""
    if not (s and s.isdigit() and len(s) == 6):
        return None
    yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
    if not 1 <= mm <= 12:
        return None
    year = 2000 + yy
    if dd == 0:
        dd = calendar.monthrange(year, mm)[1]
    if not 1 <= dd <= 31:
        return None
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def _parse_maxx_gs1(raw: str) -> dict | None:
    """Parse a separator-free Maxx GS1 DataMatrix payload.

    The Maxx labels encode the UDI without FNC1/GS separators, so biip cannot
    delimit the variable-length AIs. The grammar is fixed, though:
        (01)<14> (10)<lot> [(11)<YYMMDD>] [(17)<YYMMDD>] (240)<ref>
    We anchor on the fixed-width pieces: a 14-digit GTIN at the front, the date
    AIs peeled from the right, the (240) REF as the trailing alphanumeric AI, and
    whatever remains between is the (10) lot.
    """
    s = (raw or "").strip()
    if not (s.startswith("01") and len(s) >= 16 and s[2:16].isdigit()):
        return None
    gtin = s[2:16]
    if not gtin_check_digit_ok(gtin):
        return None
    rest = s[16:]

    # (240) REF is the trailing AI; its value carries letters (e.g. MO-MSFC-56/MH).
    ref = None
    idx = rest.rfind("240")
    if idx != -1:
        cand = rest[idx + 3:]
        if cand and any(c.isalpha() for c in cand):
            ref, rest = cand, rest[:idx]

    # Peel the date AIs from the right: (17) expiry, then (11) mfg.
    expiry = mfg = None
    if len(rest) >= 8 and rest[-8:-6] == "17" and rest[-6:].isdigit():
        d = _yymmdd(rest[-6:])
        if d:
            expiry, rest = d, rest[:-8]
    if len(rest) >= 8 and rest[-8:-6] == "11" and rest[-6:].isdigit():
        d = _yymmdd(rest[-6:])
        if d:
            mfg, rest = d, rest[:-8]

    # Whatever is left must be the (10) lot AI. If it isn't a clean "10"-prefixed
    # remainder this payload isn't the separator-free Maxx grammar — bail and let
    # biip try (it handles properly FNC1-separated GS1 in any AI order).
    if not rest.startswith("10"):
        return None
    lot = rest[2:] or None

    fields = {
        "gtin": gtin,
        "lot": lot,
        "expiry": expiry,
        "mfg": mfg,
        "serial": None,
        "ref": ref,
        "raw": raw,
        "decoded": bool(gtin or lot),
    }
    return fields


def _raw_payloads(crop) -> list[str]:
    """Return all decoded raw strings from an image region.

    DataMatrix decode time scales with pixel count, so full-resolution phone
    photos (~24 MP) need an internal ``shrink`` and a longer timeout or nothing
    decodes in time. Pick both from the image size; small synthetic crops stay
    at full resolution.
    """
    out: list[str] = []
    if crop is None:
        return out
    if _HAS_DMTX:
        try:
            shape = getattr(crop, "shape", None)
            px = (shape[0] * shape[1]) if shape else 0
            if px > 4_000_000:
                shrink, timeout = 2, 12000
            else:
                shrink, timeout = 1, 3000
            for r in pylibdmtx.decode(crop, timeout=timeout, shrink=shrink, max_count=40):
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
        "ref": None,
        "raw": raw,
        "decoded": False,
    }
    if not raw:
        return fields

    # Maxx DataMatrix payloads carry no FNC1/GS separators, which makes biip's
    # variable-length parse a greedy guess. For those the structured parser is
    # authoritative; biip is only trusted when real separators are present.
    has_sep = "\x1d" in raw or "\x1e" in raw
    if not has_sep:
        alt = _parse_maxx_gs1(raw)
        if alt and alt["decoded"]:
            return alt

    if _HAS_BIIP:
        try:
            msg = GS1Message.parse(raw)
        except Exception:
            msg = None
        if msg is not None:
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
                elif ai == "240":
                    fields["ref"] = e.value
            fields["decoded"] = any(
                fields[k] for k in ("gtin", "lot", "expiry", "mfg", "serial")
            )

    # Maxx labels encode the UDI without separators, so biip usually fails or
    # comes back without the (240) REF. Fall back to the structured parser and
    # fill anything still missing (it validates the GTIN check digit too).
    if not fields["decoded"] or fields["ref"] is None:
        alt = _parse_maxx_gs1(raw)
        if alt:
            for k in ("gtin", "lot", "expiry", "mfg", "serial", "ref"):
                if fields.get(k) is None and alt.get(k) is not None:
                    fields[k] = alt[k]
            fields["decoded"] = fields["decoded"] or alt["decoded"]
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
            "ref": None,
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
