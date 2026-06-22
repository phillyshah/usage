# Changelog

All notable changes to Usage are recorded here.
Versions follow [Semantic Versioning](https://semver.org/).

---

## [1.3.0] — 2026-06-22

### Added
- **New "Usage" output sheet** — the flat, one-row-per-line deliverable with the
  exact accountant columns: `File`, `Reload Code`, `Surgeon Name`,
  `Distributor Code`, `Surgery Date`, `Surgery Month`, `Year`, `Hospital Name`,
  `Quantity`, `Price`, `Lot Number`, `Reference Number`, `Expiration Date`, `Notes`.
  Confidence coloring (white/amber/red) carries over per field. The `Tickets`
  sheet is kept for header-level reconciliation; the corrections round-trip reads
  the `Usage` sheet (and still accepts the legacy `Line Items` layout).
- **`File` column** — traces each row back to the source photo, using the uploaded
  file's name (e.g. `MO083596.jpg` → `MO083596`). Stored as `tickets.source_filename`.
- **`Surgery Month` / `Year`** — derived from the surgery date for easy grouping.
- **Reference-log "last updated" banner** on the main screen, backed by a new
  `GET /reference/status` endpoint (date + parts/lots counts).

### Fixed
- **What's New window** no longer gets stuck. Replaced the native `<dialog>` (whose
  user-agent display rules caused it to either never close or never show) with a
  plain overlay toggled by the `hidden` attribute. Closes via X, Esc, or clicking
  the backdrop.

### Migration
- Run `db/08_add_source_filename.sql` in Supabase (adds `tickets.source_filename`)
  before deploying. Safe to run more than once.

### Mapping notes
- `Reload Code` and `Distributor Code` are both pre-filled from the ticket's single
  Rep/Distributor Code (e.g. `GR-ME-001`); `Distributor Code` is a starting point
  for the "must match surgeon" check.
- `Surgery Month` is the month name (e.g. "June").

## [1.2.0] — 2026-06-22

### Fixed / Changed
- **Device columns were blank on real photos** (REF, Description, Size, LOT, Mfg/
  Expiry). Root cause: line items were built **only** from decoded DataMatrix
  barcodes, and the vision step was deliberately not asked to read REF/LOT — so
  when barcode decode failed on phone photos (the common case), there was no key
  to look up in the Expiry Log and every device field came back empty.
- **Vision now reads the printed REF and LOT** for each device label
  (`vision.py` line shape gains `ref` + `lot`). These feed the existing reference
  resolver, which fills Description/Size from the Expiry Log and can recover a
  missing REF from the LOT (and the authoritative expiry date for that lot).
- **Confidence reflects the source**: barcode-confirmed + in-log REF = high
  (white); OCR-read REF that still matches the log = medium (amber, "double-check");
  unresolved = low (red). Descriptions remain authoritative from the log, never
  guessed by the model.

### Added
- `tests/test_reference_fallback.py` — OCR-REF → log description/size, OCR-LOT →
  REF + expiry recovery, and unknown-REF stays blank/low.

### Note
Barcode decode is still the preferred, high-confidence path; this makes the
printed text a reliable fallback so phone-photo tickets stop coming back empty.

## [1.1.1] — 2026-06-21

### Fixed
- **Expiry Log upload failing with "Internal Server Error" — real root cause.**
  The actual failure was a Supabase **row-level-security rejection** (`42501`):
  the `SUPABASE_SERVICE_KEY` configured on the server was the publishable/anon key,
  which cannot bypass RLS. This is an operator configuration issue (set the
  `service_role` secret), but the app now **detects and explains it**:
  - The 42501 error is translated into an actionable message naming the wrong key
    and pointing to Supabase → Project Settings → API.
  - Startup logs a clear warning when `SUPABASE_SERVICE_KEY` doesn't look like a
    `service_role` key (decodes the key's role claim; never logs the key).
  - New `GET /diag` reports the datastore mode and the configured key's *role*
    (e.g. `service_role` vs `anon`) so the key can be verified without exposing it.
- **Reverted an incorrect v1.1.0 change**: storage `upsert` must be the string
  `"true"`, not the bool `True` — storage3 2.x copies it straight into the
  `x-upsert` HTTP header and httpx rejects non-string header values. Added a
  regression test pinning this.
- **What's New modal could not be closed.** The base `.changelog-modal` rule set
  `display: flex`, overriding the native `<dialog>` hidden state so `close()` had
  no effect. Now scoped to `.changelog-modal[open]`.

### Added
- `app/supabase_key.py` — decode a Supabase key's role (JWT claim or `sb_*` prefix)
  to detect a non-privileged key. Fully unit-tested.
- `tests/` — key-role detection, 42501 error translation, storage upsert-header
  contract, large-log ingest (60 k rows), `/version` + `/diag`, and a conditional
  test that loads the **real** Expiry Log to 63,214 / 1,682 / 51,365.

### Note (1.1.0)
1.1.0 added the What's New panel and better error handling but its Supabase
delete-filter / storage-upsert changes were no-ops or incorrect; 1.1.1 supersedes it.

## [1.1.0] — 2026-06-21

### Added
- `GET /version` endpoint returns current version and full changelog.
- "What's New" button in the header opens a changelog modal.
- Version number displayed in the page footer.
- `app/version.py` — single source of truth for version + changelog data.
- Per-route error handling so the UI shows the failure reason, not a bare 500.
- Expiry Log ingest runs off the asyncio event loop (ThreadPoolExecutor).

---

## [1.0.0] — 2026-06-18

### Added
- Initial release: upload ticket photos → colour-coded review spreadsheet.
- GS1 DataMatrix + linear barcode auto-read (pylibdmtx + pyzbar + biip).
- Claude AI vision fallback for handwritten fields, header text, and totals.
- Confidence coloring: white (high), amber (medium), red (low/blank).
- Correction learning loop — tool improves with every re-uploaded fixed sheet.
- Expiry Log reference database (full replace on each upload).
- PHI gate: patient sticker redacted before storage; fails safe to manual queue.
- Supabase backend (14 tables + part_resolved view + pg_cron nightly purge).
- Offline / dev mode (local JSON + disk storage, no credentials needed).
- Docker + Traefik deploy to usage.90ten.life with Let's Encrypt TLS.
