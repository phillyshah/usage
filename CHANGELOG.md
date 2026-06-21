# Changelog

All notable changes to Usage are recorded here.
Versions follow [Semantic Versioning](https://semver.org/).

---

## [1.1.0] — 2026-06-21

### Fixed
- Expiry Log upload returned "Internal Server Error" on large files (63 k rows).
  Root causes: Supabase delete filter used string `"null"` instead of Python `None`,
  causing the `not.is.null` PostgREST filter to be generated incorrectly; storage
  upload used `upsert: "true"` (string) where supabase-py 2.x requires `True` (bool).
- Added per-route error handling so the UI now shows the actual failure reason
  instead of a bare "Internal Server Error".
- Expiry Log ingest now runs off the asyncio event loop (ThreadPoolExecutor) so
  a 63 k-row Supabase insert can't block other requests.
- Insert chunk size increased from 500 → 1,000 rows (fewer round-trips).
- Storage upload failure is now non-fatal (logged + skipped) so a bucket permission
  issue won't prevent the reference data from being updated.

### Added
- `GET /version` endpoint returns current version and full changelog.
- "What's New" button in the header opens a changelog modal.
- Version number displayed in the page footer.
- `app/version.py` — single source of truth for version + changelog data.

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
