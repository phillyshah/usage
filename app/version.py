"""Single source of truth for the app version and changelog.

Increment VERSION on every meaningful release. Keep CHANGELOG in reverse
chronological order (newest first). The /version API and the UI "What's New"
panel both read from here.
"""

VERSION = "1.3.0"

CHANGELOG: list[dict] = [
    {
        "version": "1.3.0",
        "date": "2026-06-22",
        "notes": [
            "New 'Usage' output sheet with exactly the columns your accountants "
            "need: Reload Code, Surgeon, Distributor Code, Surgery Date/Month/Year, "
            "Hospital, Quantity, Price, Lot, Reference, Expiration",
            "Added a File column so each row traces back to the source photo "
            "(uses the uploaded file's name)",
            "The reference log card now shows when it was last updated",
            "Fixed the What's New window so it opens and closes reliably (X, Esc, "
            "or click outside)",
        ],
    },
    {
        "version": "1.2.0",
        "date": "2026-06-22",
        "notes": [
            "Device columns (REF, Description, Size, LOT, expiry) now fill in even "
            "when the barcode won't scan: the tool reads the printed REF/LOT and "
            "looks up the rest in the Expiry Log",
            "Descriptions and sizes come straight from your Expiry Log, matched by "
            "REF or LOT",
            "OCR-read references that match the log show amber (double-check) rather "
            "than blank",
        ],
    },
    {
        "version": "1.1.1",
        "date": "2026-06-21",
        "notes": [
            "Expiry Log upload now explains the real problem when Supabase rejects "
            "a write — a wrong service key is named and pointed to the fix, instead "
            "of a cryptic error",
            "Fixed the What's New window not closing when you press the X",
            "Added a self-check (visit /diag) that confirms the database key is set "
            "up correctly, without ever exposing the key",
        ],
    },
    {
        "version": "1.1.0",
        "date": "2026-06-21",
        "notes": [
            "Improved error messages: the UI now shows exactly what went wrong",
            "Added What's New panel to track updates going forward",
            "Expiry Log ingest moved off the request thread for large files",
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
