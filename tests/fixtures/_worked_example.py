"""Worked-example fixture data for ticket MH13366-A (EXTRACTION_FIELD_GUIDE §7).

These are the GS1 DataMatrix payloads decoded from the real fixture's six device
labels — device UDI data only, NO patient information — so the deterministic
barcode -> GTIN -> SKU -> part_info chain can be regression-tested without
committing any PHI-bearing image. Five payloads are the exact bytes pylibdmtx
decoded from the photo; the sixth label (MO-SWCC-65/20) did not decode and is
reconstructed from the documented values in the same separator-free Maxx format.

Correct extraction yields six line items reconciling to a $4,136.00 grand total.
"""

# Separator-free Maxx GS1: 01<GTIN-14>10<lot>11<mfgYYMMDD>17<expYYMMDD>240<REF>
LABEL_PAYLOADS = [
    "010081000812008810S411227071123110117281031240MO-MSFC-56/MH",
    "01008100081211081070119757471125060117300531240MO-HDAI-36/40-",
    "010081000812433810U021027121125030117300228240MO-STVC-35/03",
    "010081000812060610S211327061123050117280430240MO-MLHH-MH/36",
    "010081000812184910U371427061125120117301130240MO-SWCC-65/30",
    "010081000812182510R131427031125030117270331240MO-SWCC-65/20",
]

# Expected per-line resolution: REF (= SKU), lot, expiry, price.
EXPECTED_LINES = [
    {"ref": "MO-SWCC-65/30", "lot": "U37142706", "expiry": "2030-11-30", "price": 68.0},
    {"ref": "MO-SWCC-65/20", "lot": "R13142703", "expiry": "2027-03-31", "price": 68.0},
    {"ref": "MO-MSFC-56/MH", "lot": "S41122707", "expiry": "2028-10-31", "price": 900.0},
    {"ref": "MO-HDAI-36/40-", "lot": "7011975747", "expiry": "2030-05-31", "price": 650.0},
    {"ref": "MO-STVC-35/03", "lot": "U02102712", "expiry": "2030-02-28", "price": 1900.0},
    {"ref": "MO-MLHH-MH/36", "lot": "S21132706", "expiry": "2028-04-30", "price": 550.0},
]

GRAND_TOTAL = 4136.00

# Header (handwritten) reads for the worked example.
HEADER = {
    "entity": "Maxx Health",
    "surgeon": "Montijo",
    "rep_code": "MC-001",
    "surgery_date": "2026-06-15",
    "hospital": "Wellington Regional Medical Center",
}

# Surgeon chain expectations (surgeon_info, key MontijoMC-001). NB: the source
# CSV spells the full name "Harvey Montifo" (a data typo) — assert on the stable
# hospital/region rather than the name spelling.
EXPECTED_HOSPITAL = "Wellington Regional Medical Center"
EXPECTED_REGION = "South"


def price_by_ref(ref: str) -> float | None:
    for ln in EXPECTED_LINES:
        if ln["ref"] == ref:
            return ln["price"]
    return None
