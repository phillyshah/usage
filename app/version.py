"""Single source of truth for the app version and changelog.

Increment VERSION on every meaningful release. Keep CHANGELOG in reverse
chronological order (newest first). The /version API and the UI "What's New"
panel both read from here.
"""

VERSION = "1.1.0"

CHANGELOG: list[dict] = [
    {
        "version": "1.1.0",
        "date": "2026-06-21",
        "notes": [
            "Fixed Expiry Log upload failing with a server error on large files",
            "Improved error messages: the UI now shows exactly what went wrong",
            "Added What's New panel to track updates going forward",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-06-18",
        "notes": [
            "Initial release: upload ticket photos and get a colour-coded spreadsheet",
            "Barcode auto-read (GS1 DataMatrix + linear codes)",
            "Claude AI vision fallback for handwritten fields",
            "White / amber / red confidence coloring",
            "Correction learning loop — the tool improves with every fixed sheet",
            "Expiry Log reference database (upload once, matched automatically)",
        ],
    },
]
