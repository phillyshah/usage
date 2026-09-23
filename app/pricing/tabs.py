"""Static configuration for price enrichment.

Lives in a module dict rather than config.py because config.py holds only scalar
settings — the same reasoning as app/partner_parts.py: structured lookup data that
a human edits belongs in code, next to the comment explaining it.
"""
from __future__ import annotations

# Ticket Entity -> the ONE price-list tab that may be used for its rows.
# Normalized (lowercase, collapsed whitespace) on both sides before lookup.
# A row whose entity is not in here is a configuration error: the job stops
# rather than guessing from another distributor's prices.
DISTRIBUTOR_TABS: dict[str, str] = {
    "maxx health": "MH for MO",
    "maxx orthopedics": "Summary Price List",
}

# Known hospital-name exceptions that normalization alone can't bridge.
# Key = normalized usage-sheet hospital, value = normalized price-list hospital.
# Every use is logged so these stay visible rather than becoming folklore.
HOSPITAL_ALIASES: dict[str, str] = {}

# Headers that identify a leading metadata column rather than a hospital column.
# The tabs disagree on how many they have: "MH for MO" is Item/Description (2),
# "Summary Price List" is Item/Class/Part Type (3). Hospitals are everything
# after the last one of these, so the start column is detected, never assumed.
META_HEADERS = {"item", "class", "part type", "description", "sap customer #"}

# Dropped from a hospital name before comparison (legal suffixes).
LEGAL_SUFFIXES = {
    "llc", "inc", "corp", "corporation", "company", "co", "pllc", "lp", "ltd",
}

# Expanded wherever they appear in a hospital name.
HOSPITAL_ABBREVIATIONS = {
    "ctr": "center",
    "surg": "surgery",
    "med": "medical",
    "hosp": "hospital",
    "reg": "regional",
    "ortho": "orthopedic",
    "orthopaedic": "orthopedic",
}

# Too generic to carry a hospital match on their own. The fuzzy tier requires a
# second ratio computed with these removed, so "X Medical Center" and
# "Y Medical Center" can't match on the shared two-thirds.
GENERIC_HOSPITAL_WORDS = {
    "hospital", "medical", "center", "surgery", "surgical", "health",
    "system", "regional", "the", "of", "at", "and",
}

# Expanded in component descriptions before comparison.
COMPONENT_ABBREVIATIONS = {
    "tbp": "tibial base plate",
    "baseplate": "base plate",
    "fem": "femoral",
    "tib": "tibial",
    "patella": "patellar",
}

# Attributes that make two components genuinely different devices. If both sides
# name one of these and they disagree, the components never match however similar
# the rest of the text is — a CR liner is not a PS liner.
CRITICAL_ATTRIBUTES = {
    "cr", "ps", "uc", "mc", "pck", "ppck", "titan", "tinbn", "peek",
    "ceramic", "cocr", "cementless", "stemmed", "constrained", "elevated",
    "offset", "oblique",
}

# A description match may never rest on these alone.
GENERIC_COMPONENT_WORDS = {
    "femoral", "tibial", "liner", "base", "plate", "patellar", "modular",
    "all", "poly", "articular", "surface", "component", "the", "for", "and",
    "or", "r", "l",
}
