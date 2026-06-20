# Usage — Distributor Label Extraction Pipeline

Turn the implant usage tickets distributors photograph each day (~100/day) into a
clean, color-coded review spreadsheet — and get more accurate over time as
corrected sheets are re-uploaded.

This repo implements **Phase 1 (extraction MVP) + Phase 2 (learning loop)** of the
build spec, plus a friendly browser UI a non-technical reviewer can walk up and
use. See `PROJECT_OVERVIEW.md`, `LABEL_EXTRACTION_BUILD_SPEC.md`, and
`DEVELOPER_HANDOFF.md` for the full design (read in that order).

## Design DNA (non-negotiables)

1. **Deterministic first, AI second.** Device data (lot, expiry, identity) comes
   from GS1 DataMatrix barcodes + the reference Expiry Log. The Claude vision call
   is a fallback for handwriting, header fields, and prices only.
2. **Never guess silently.** Every cell is confident (white), a low-confidence
   guess (amber `FFF2CC`), or blank/unreadable (red `F4CCCC`).
3. **Confidence comes from validation, not self-rating.** A value is confident
   because two independent sources agree or it matches reference data.
4. **No patient data, ever.** The patient sticker is masked at ingest, before the
   image is read, sent to the API, or stored. If it can't be located, the ticket
   goes to a manual queue and **no image is sent anywhere**.

## How it works

```
Ticket photo (held in memory only)
  → preprocess (deskew/denoise/contrast)
  → detect template (Maxx Orthopedics / Maxx Health)
  → REDACT the patient sticker            ← PHI gate (fail safe → manual queue)
  → store ONLY the redacted image
  → decode barcodes → parse GS1 (GTIN/LOT/expiry/mfg)
  → resolve REF → description/size, recover REF from LOT or GTIN crosswalk
  → Claude vision fallback: header, prices, qty, totals
  → score each field's confidence by cross-checking sources
  → write a 3-sheet color-coded workbook (Tickets / Line Items / Legend)
      ↑
  → a human reviews, fixes flagged cells, re-uploads
  → harvest facts into the learning stores + diff for calibration
```

## Project layout

```
app/
  main.py            FastAPI app + routes + static UI mount
  config.py          env-backed settings (pydantic-settings)
  db.py              data access — Supabase OR local JSON (offline)
  storage.py         object storage — Supabase buckets OR local disk
  jobs.py            APScheduler: daily batch + nightly purge
  metrics.py         auto-resolve % per week
  pipeline/          preprocess, template, redact, barcode, reference,
                     vision, confidence, assemble, run
  sheets/            write (colored workbook) + read (parse corrections)
  learning/          harvest, diff, ingest_log
  static/            the browser UI (vanilla HTML/CSS/JS, no build step)
tests/               unit tests + fixtures
supabase_schema.sql  the database (run as-is; names match exactly)
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | health check |
| POST | `/images` | upload 1..N ticket images → redact + queue |
| POST | `/batches/run` | process pending tickets → colored workbook |
| GET  | `/batches` | list batches |
| GET  | `/batches/{id}/sheet` | download the review workbook |
| POST | `/corrections/upload` | re-upload 1..N corrected `.xlsx` |
| POST | `/reference/log` | full-replace the reference tables from the Expiry Log |
| GET  | `/metrics/auto-resolve?weeks=N` | the getting-better curve |
| GET  | `/` | the friendly web UI |

## Running it

### Local dev / demo (no credentials needed)

`OFFLINE_MODE=true` swaps Supabase for a local JSON store under `.localdata/` and
runs the deterministic-only pipeline (no Anthropic call). The UI is fully
functional; you can upload images, run a batch, download the workbook, and
re-upload corrections.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # see note on native libs below
cp .env.example .env                      # set OFFLINE_MODE=true
OFFLINE_MODE=true uvicorn app.main:app --reload
# open http://localhost:8000
```

> Barcode decoding needs the system libs `libdmtx0` and `libzbar0`, and OpenCV
> needs `libgl1`/`libglib2.0-0`. Without them the app still runs — those steps
> degrade gracefully and cells fall back to the vision/blank path. The Docker
> image installs all of them.

### Production (Supabase + Anthropic + Docker/Traefik)

1. Create the managed Supabase project and run `supabase_schema.sql` top to
   bottom. Create the four private Storage buckets listed at the bottom of that
   file. Enable `pg_cron` if you want the SQL-side purge.
2. Fill `.env` from `.env.example` (`OFFLINE_MODE=false`, real Supabase URL +
   service-role key, Anthropic key). **Never commit `.env`.**
3. Point `usage.90ten.life` at the VPS and confirm Traefik's `certresolver` name
   matches `docker-compose.yml`.
4. `docker compose up -d --build`.

Run the Claude API account under a HIPAA BAA (defense in depth — see spec §9).

## Tests

```bash
pip install pytest
pytest -q
```

Drop the 4 sample ticket JPEGs and `Expiry_Log.xlsx` into `tests/fixtures/` to
exercise the full end-to-end path (see `tests/fixtures/README.md`).
