"""Single source of truth for the app version and changelog.

Increment VERSION on every meaningful release. Keep CHANGELOG in reverse
chronological order (newest first). The /version API and the UI "What's New"
panel both read from here.
"""

VERSION = "2.8.0"

CHANGELOG: list[dict] = [
    {
        "version": "2.8.0",
        "date": "2026-07-06",
        "notes": [
            "Upgraded the reading model to Claude Sonnet 5: it works from a much "
            "sharper view of your photos (roughly 2.7x the detail), so handwritten "
            "prices, quantities, and small printed REF codes read more accurately — "
            "expect more white cells and fewer amber ones on the same tickets",
        ],
    },
    {
        "version": "2.7.4",
        "date": "2026-07-02",
        "notes": [
            "UNIKO billing labels are now picked up: when a UNIKO instrument kit "
            "sticker is pasted on the ticket, the tool reads its printed part number "
            "(e.g. UKI0201-L) and adds it as a line — description, part type, and "
            "category filled automatically — even though the UNIKO label has no barcode",
        ],
    },
    {
        "version": "2.7.3",
        "date": "2026-07-02",
        "notes": [
            "New Debug Console tab: upload any ticket and see every step the pipeline took — "
            "which barcodes decoded (GTIN, LOT, REF), what the AI read for each field, which "
            "product master rows matched, whether the price agreed with what the tool learned, "
            "and exactly why each cell ended up white, amber, or red. Use it to answer "
            "'why did this come out wrong?' without guessing.",
        ],
    },
    {
        "version": "2.7.2",
        "date": "2026-06-25",
        "notes": [
            "Added a learning-safety banner at the top of 'What the tool has "
            "learned': it confirms at a glance that everything the tool has "
            "learned is still intact, and turns red to alert you if the learned "
            "facts ever shrink below the most it has ever held",
        ],
    },
    {
        "version": "2.7.1",
        "date": "2026-06-24",
        "notes": [
            "The 'What the tool has learned' blocks in the History tab are now "
            "clickable — tap any category (prices, part descriptions, reps, "
            "barcode→part links) to see the full list of what was learned and when",
        ],
    },
    {
        "version": "2.7.0",
        "date": "2026-06-24",
        "notes": [
            "Quantities greater than 1 are now captured: when a hospital writes a "
            "count for an item (e.g. '4 pins'), the row records that quantity and "
            "the line total scales accordingly, instead of always showing 1",
            "Added UNIKO as a recognized partner: the two UNIKO PointCloud Knee "
            "Instrument kits (UKI0201-L / UKI0201-R) now auto-fill their "
            "description, part type and category — and they keep working even after "
            "a monthly parts-file update",
        ],
    },
    {
        "version": "2.6.1",
        "date": "2026-06-24",
        "notes": [
            "Dates now read as Month/Day/Year (MM/DD/YYYY) everywhere — the "
            "spreadsheet's Expiry / Mfg / Surgery dates and the dates shown in the "
            "app — instead of the Year-Month-Day format",
            "The app now always loads the latest version after a deploy (no more "
            "needing a hard refresh to see new features like the History tab)",
        ],
    },
    {
        "version": "2.6.0",
        "date": "2026-06-24",
        "notes": [
            "New 'History' tab: see the work done (past batches), what you uploaded "
            "for retraining, and — most importantly — the impact of that learning",
            "'What the tool has learned' shows the running totals the tool has "
            "picked up from your corrections: prices, part descriptions, rep codes, "
            "and barcode→part links",
            "Retraining activity shows, per day, how many fields you corrected "
            "(blanks filled, guesses fixed) and what the tool learned from them",
            "'Getting better over time' is now a daily view with real dates instead "
            "of week numbers, and lives in the History tab",
        ],
    },
    {
        "version": "2.5.1",
        "date": "2026-06-23",
        "notes": [
            "Fixed tickets occasionally coming back empty with a 'Processing "
            "error' when several were extracted at once (a transient connection "
            "drop). The tool now retries automatically, so every ticket in a "
            "batch — including each page of a multi-page PDF — comes through",
        ],
    },
    {
        "version": "2.5.0",
        "date": "2026-06-23",
        "notes": [
            "You can now upload PDF tickets, not just photos. A multi-page PDF is "
            "treated as one ticket per page, so a single PDF holding several "
            "surgeries is split into the right number of tickets automatically",
            "A handwritten 'I/O' is now recognized as wasted (same as 'W')",
            "Fixed the 'Clear list' button in step 1 — it now properly clears your "
            "staged files and the result message",
            "Added a 'Start over' button that wipes the current session — your "
            "uploaded tickets and generated spreadsheet — and returns you to step "
            "1, after a confirmation prompt",
            "The Usage sheet now ends at Expiry Date (column M), with the Notes "
            "review column alongside it; the unused columns after it were removed",
        ],
    },
    {
        "version": "2.4.0",
        "date": "2026-06-23",
        "notes": [
            "Uploading tickets is much faster: the slow image-cleanup step that "
            "ran on every upload was removed — it wasn't needed for privacy "
            "masking and was actually making barcodes harder to read",
            "Extracting a batch is much faster: tickets are now processed in "
            "parallel instead of one at a time, and each ticket's results are "
            "saved in a single write instead of dozens of separate ones",
        ],
    },
    {
        "version": "2.3.0",
        "date": "2026-06-23",
        "notes": [
            "Much better at the handwritten prices: a price written with a '$' or "
            "commas (like $1,900.00) is now read correctly instead of being "
            "dropped, and a crossed-out / 'no charge' price is read as 0",
            "When the line prices add up to the handwritten Grand Total, the tool "
            "now trusts them (shown white/confident); when they don't add up it "
            "flags the prices amber for a quick check",
            "Sharpened the instructions the reader uses for handwritten prices, "
            "the freight fee, and the grand total",
        ],
    },
    {
        "version": "2.2.4",
        "date": "2026-06-23",
        "notes": [
            "Buttons for steps that aren't ready yet now appear clearly greyed "
            "out (not just faintly dimmed), so it's obvious at a glance which "
            "step you can act on — 'Extract data' and 'Download' stay grey until "
            "their step is reached",
        ],
    },
    {
        "version": "2.2.3",
        "date": "2026-06-23",
        "notes": [
            "Re-running 'Extract data' on the same tickets now replaces the "
            "previous results instead of adding a duplicate set of rows — so "
            "quantities and totals can't be silently doubled by extracting twice",
        ],
    },
    {
        "version": "2.2.2",
        "date": "2026-06-23",
        "notes": [
            "The guided steps now light up in order: 'Extract data' stays dimmed "
            "until you've uploaded tickets, and 'Download review spreadsheet' stays "
            "dimmed until the data has been extracted — so it's always clear which "
            "step is next",
        ],
    },
    {
        "version": "2.2.1",
        "date": "2026-06-23",
        "notes": [
            "Fixed the main reason recent spreadsheets came out blank: the data "
            "was being extracted and saved correctly, but a database read limit "
            "was dropping it before it reached the spreadsheet once enough tickets "
            "had been processed — every value is now read back in full",
            "This also restores the colour confidence shading and the Raw "
            "Extraction tab for all tickets",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-06-23",
        "notes": [
            "New 'Raw Extraction' tab in the review spreadsheet: for every line it "
            "shows exactly what was read — whether the barcode decoded, the raw "
            "barcode contents (GTIN, Lot, Mfg, Expiry, Ref), what the photo-reader "
            "saw, and the final resolved values side by side",
            "Lines where the barcode didn't decode are flagged in red, so it's "
            "obvious at a glance when a label needs a clearer photo",
        ],
    },
    {
        "version": "2.1.1",
        "date": "2026-06-23",
        "notes": [
            "Uploading tickets is now resilient: if one photo can't be processed, "
            "the rest still go through and the page tells you exactly which one "
            "failed and why — instead of the whole upload erroring out",
            "Photos in an upload are now processed in parallel, so a batch of "
            "tickets finishes noticeably faster",
            "Hardened the patient-privacy gate: a ticket image is only ever stored "
            "after its patient sticker is confirmed masked — there is no path that "
            "could store an unredacted photo",
        ],
    },
    {
        "version": "2.1.0",
        "date": "2026-06-23",
        "notes": [
            "The app is now split into two clearly separated workflows you switch "
            "between with tabs: 'Process Tickets' for the daily photo-to-spreadsheet "
            "flow, and 'Reference Data' for the lookup sheets — so the two no longer "
            "get in each other's way",
            "You can now update each reference lookup sheet directly from the page — "
            "GTIN Codes, Part Info (Part Type & Category), Surgeon Info, and the "
            "Expiry Log — each with its own upload tile and 'last updated' line",
            "Each reference sheet shows how fresh it is (when it was last updated and "
            "how many rows it has), and updates take effect immediately for new batches",
        ],
    },
    {
        "version": "2.0.0",
        "date": "2026-06-22",
        "notes": [
            "New accountant deliverable: a flat 26-column output (plus a Source "
            "Image Filename) — one row per device unit — matching the agreed "
            "output_columns.csv exactly",
            "Device identity now comes from the barcode GTIN: GTIN → SKU (Ref "
            "Number) → Description / Part Type / Category, looked up from the new "
            "product master (no more reading descriptions off the photo)",
            "Surgeon + distributor code now resolve the surgeon, hospital and "
            "region automatically, and flag when the code doesn't match the surgeon",
            "Reads the printed REF code (240) straight off the barcode, and reads "
            "barcodes reliably on full-size phone photos",
            "Wasted components (a handwritten 'W') are kept as a usage row, "
            "highlighted yellow with a WASTED note — and still count toward the total",
            "One row per implant unit so every lot stays traceable",
        ],
    },
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
