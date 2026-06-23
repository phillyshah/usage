# Distributor Label Extraction Pipeline — Build Spec

**Owner:** Andy (Maxx Orthopedics / Maxx Health)
**Audience:** Coding agent(s) building the system
**Status:** Ready for build
**Rev:** 2 (adds Supabase datastore, VPS/Traefik deployment, async re-upload + retention model)

---

## 1. What we're building

A pipeline that ingests the implant usage tickets our distributors photograph and send us each day (~100/day), extracts the data into a review spreadsheet, and flags anything it isn't sure about so a human can verify or fill it in. Corrected spreadsheets are re-uploaded later and the system uses them to get more accurate over time.

**Hard design principles (do not violate):**

1. **Deterministic first, AI second.** Pull structured device data from barcodes and our reference log wherever possible. The Claude API vision call is a *fallback* for what those can't provide (handwriting, header fields, unreadable labels) — not the primary engine.
2. **Never guess silently.** Every cell is one of three states: confident, low-confidence guess, or blank. The two non-confident states are color-coded so a human knows exactly what to check or fill.
3. **Confidence comes from validation, not self-rating.** A value is "confident" because two independent sources agree or it matches known reference data — not because the model said so.
4. **The reference log is read-only.** We re-upload a fresh copy periodically; the system ingests it but never edits it.
5. **No patient data, ever.** PHI is redacted at ingest and never read, sent to the API, or stored (§9).

---

## 2. Inputs

### 2.1 Daily ticket images
- Phone photos (JPEG), one ticket per image (assumed; see Open Questions).
- Two templates in circulation: **Maxx Orthopedics** and **Maxx Health**. Layouts differ; the parser must handle both.
- Each ticket = a header block (mostly handwritten, plus a patient sticker we deliberately ignore) plus a grid of printed device labels.
- Each device label carries: printed REF, LOT, mfg/expiry dates, description/size, **a GS1 DataMatrix (UDI) 2D barcode**, and usually a linear barcode.
- Handwritten fields: prices per line, freight, grand total, and most of the header (rep, code, surgeon, hospital, surgery date, PO). Quality varies — skew, blur, glare.
- Tickets can be partial: blank slots labeled "Place Implant Label" must be skipped, not hallucinated into lines.

> **PHI exclusion (mandatory).** Hospitals keep including patient identifiers (name, MRN, DOB) despite being asked not to. **We do not process this information at all.** The patient sticker region is located and **redacted (masked) at ingest — before** the image is sent to the vision API, read, or stored. No patient field is ever extracted, output, or persisted. See §9.

### 2.2 Reference log (`Expiry_Log.xlsx`)
Maintained by the team, re-uploaded on a regular cadence, treated as read-only reference. Only the **`Expiry Log History`** tab matters (ignore `Missing` and `Each Lot Expiry Update`).

`Expiry Log History` schema (data starts row 4; row 3 is headers):

| Col | Field | Use |
|-----|-------|-----|
| A | Part No | = **REF / catalog number.** Primary join key. |
| B | Description | Includes size baked in (e.g. "Tibial Augment, Size 4"). |
| C | Lot # | Secondary join key. |
| D | Total Qty Released | Reference only. |
| E | Lot Pallet | Reference only. |
| F | Expiry Date | Authoritative expiry for that lot. |
| G | Notes | Ignore. |

Current snapshot: **~1,682 unique REFs**, **~51,000 unique lot numbers** — the system starts warm, not cold.

**What the log gives us:** `REF → Description (+ size)` (autofill stable fields, no reading); `Lot # → Expiry + Part No` (second independent lookup — recover a missing REF from a known lot, validate expiry); REF/LOT validation (not in log → flag).

**What it does NOT give us:** pricing. There is no price catalog and there won't be — **pricing is account-based, and the account is the hospital.** Price is read from the ticket via the vision call, then cross-checked (§6).

---

## 3. Architecture & stack

### 3.1 Stack
- **Language:** Python 3.11+
- **Barcode decode:** `pylibdmtx` (DataMatrix), `pyzbar` (linear/QR), `opencv-python` + `Pillow` (preprocessing). `biip` for parsing GS1 element strings / GTINs.
- **AI fallback:** Claude API (vision) — **Sonnet** for extraction (handwriting accuracy matters; volume is low, ~100 images/day = negligible spend).
- **Datastore: Supabase.**
  - **Postgres** for all records: reference data, learning stores, ticket/line extraction records, audit.
  - **Supabase Storage** buckets for redacted images, generated output sheets, and incoming corrected sheets.
  - Use the Supabase Python client / connection string; service-role key kept server-side only.
- **Spreadsheet I/O:** `openpyxl`.
- **Service layer:** FastAPI — endpoints to receive ticket images, run/return the daily batch, download the review sheet, and **re-upload one or many corrected sheets at once**.
- **Scheduling:** APScheduler (or cron) for the daily batch run and the retention-purge job. No Celery/Redis needed at this volume; add later only if it grows.

### 3.2 Deployment (VPS + Traefik)
The service runs on the existing VPS, fronted by the existing reverse proxy (assumed **Traefik** — "managed DNS + traffic"; confirm).
- Containerize the FastAPI app + worker with **Docker** (single image is fine at this scale).
- **Traefik** handles DNS-based routing to the container and automatic TLS (Let's Encrypt) on its subdomain (e.g. `labels.<yourdomain>`).
- Supabase is a **managed project on supabase.com** (Supabase Cloud); the container reaches it over the network via env-configured project URL + service-role key.
- Secrets (`ANTHROPIC_API_KEY`, Supabase URL + service key) injected as environment variables, never committed.

### 3.3 Pipeline stages
```
Ingest image
   -> Preprocess (deskew, denoise, enhance contrast)
   -> Detect template (Maxx Ortho vs Maxx Health)
   -> REDACT patient sticker region (mask before any read/API/store)   <- PHI gate
   -> Segment label grid + header region
   -> Per label: decode barcode(s) -> parse GS1 (GTIN, LOT, expiry, mfg)
   -> Enrich/validate against reference (REF->desc/size, LOT->expiry)
   -> Vision fallback (Claude API) for: header fields, prices, totals,
        quantity, and any label whose barcode failed
   -> Score confidence per field (see §6)
   -> Assemble Ticket + Line Item rows; persist extraction records (Supabase)
   -> Write color-coded review workbook; store it in Supabase Storage
```
Re-upload path (asynchronous — see §7):
```
Corrected workbook(s) uploaded (1..many, any time within retention)
   -> For each sheet: read Ticket IDs
   -> Harvest ground-truth facts from each corrected row -> update learning stores
   -> If original still within retention window: diff vs stored originals -> audit + calibrate
   -> Mark tickets verified
```

---

## 4. Extraction strategy (detail)

### 4.1 Barcode = primary for device fields
Maxx device labels use **GS1 DataMatrix** for the FDA UDI. Decode and parse the GS1 Application Identifiers with `biip` (don't hand-roll FNC1/GS handling):

| AI | Field | Maps to |
|----|-------|---------|
| (01) | GTIN-14 | product identity (see crosswalk note) |
| (10) | Batch/Lot | **LOT** (directly usable) |
| (17) | Expiration (YYMMDD) | **Expiration Date** (directly usable) |
| (11) | Production date (YYMMDD) | Mfg Date (if present) |
| (21) | Serial | capture if present |

**GTIN vs REF (important):** the barcode's (01) is a GTIN, *not* the human REF/Part No. LOT and expiry come straight out, but tying a line to a Part No needs either (a) the printed REF read via vision + validated against the log, or (b) a **GTIN → REF crosswalk** built over time (store the pair whenever a label yields both a decoded GTIN and a confirmed REF). Until the crosswalk matures, rely on printed REF + log validation. Do not assume barcode = REF.

### 4.2 Reference log = enrichment + validation
- `REF → Description, Size` — fill from log; don't read off the photo.
- `LOT → Expiry, Part No` — cross-check barcode expiry; recover a missing REF.
- REF or LOT absent from log → flag.

### 4.3 Vision (Claude API) = fallback only
Fires for fields barcodes/log can't supply:
- **Header:** Sales Rep/Distributor, Rep/Distributor Code, Surgeon, Hospital, Surgery Date, PO Number. (**No patient fields** — that region is redacted before the image reaches the API.)
- **Money:** Unit Price per line, Freight/Delivery Fee, Grand Total.
- **Quantity** (if not encoded).
- Any label whose barcode failed — read REF/LOT/dates as text and validate against the log.

**Vision call contract:** send the redacted image; system prompt instructs the model to return **JSON only** (no prose, no markdown fences), with **per-field values and a per-field confidence**, and to **return null for anything it cannot read rather than guessing**. Parse defensively (strip fences, try/except). Don't spend the call re-reading fields the barcode already nailed except as a cheap cross-check.

Example expected shape:
```json
{
  "header": {
    "rep": {"value": "Sam Earl", "confidence": "high"},
    "rep_code": {"value": "GR-MO-001", "confidence": "medium"},
    "surgeon": {"value": null, "confidence": "low"},
    "hospital": {"value": "...", "confidence": "high"},
    "surgery_date": {"value": "2026-06-11", "confidence": "medium"},
    "po_number": {"value": null, "confidence": "low"}
  },
  "lines": [
    {"unit_price": {"value": 600, "confidence": "medium"}, "qty": {"value": 1, "confidence": "high"}}
  ],
  "freight": {"value": null, "confidence": "low"},
  "grand_total": {"value": 5050, "confidence": "medium"}
}
```
Model confidence is an input to scoring, **not** the final word — §6 governs the cell color.

---

## 5. Output: the review workbook

Generated per daily batch with `openpyxl`, stored in the `output-sheets` Storage bucket. **Three sheets:**

### Sheet 1 — `Tickets` (one row per ticket)
| Column | Notes |
|--------|-------|
| Ticket ID | system-generated, stable — the join key for re-upload |
| Source Image | filename / storage path |
| Entity | Maxx Orthopedics / Maxx Health |
| Surgery Date | |
| Sales Rep / Distributor | |
| Rep/Distributor Code | |
| Surgeon | |
| Hospital | **also the account key for price memory** |
| PO Number | |
| Freight/Delivery Fee | |
| Grand Total | |
| Sum of Line Totals | computed; for the reconciliation check |
| Flags / Notes | ticket-level flag summary |

### Sheet 2 — `Line Items` (one row per device, FK = Ticket ID)
| Column | Source |
|--------|--------|
| Ticket ID | FK |
| Line ID | system-generated, stable |
| REF (Part No) | barcode-crosswalk / vision + log validation |
| Description | log lookup |
| Size | log lookup (or split from description) |
| LOT | barcode |
| Qty | vision |
| Mfg Date | barcode (if present) |
| Expiration Date | barcode / log |
| Unit Price | vision |
| Line Total | Qty × Unit Price (computed) |
| Flags / Notes | per-line flags |

### Sheet 3 — `Legend`
Explains the color states.

### 5.1 Color coding (the whole point)
Per-cell `PatternFill` on the data cells:

| State | Meaning | Fill |
|-------|---------|------|
| **Confident** | Validated / agreed across sources | none (default) |
| **Low-confidence guess** | Value present, single-source/minor disagreement — **eyeball it** | amber `FFF2CC` |
| **Blank / unreadable** | No confident read — **left blank for human to fill** | red `F4CCCC` |

Keys (Ticket ID, Line ID, Source Image) stay uncolored. The human edits values directly in the colored cells; those edits are the learning signal on re-upload.

---

## 6. Confidence model

Each field scored **high / medium / low**, mapping to the three colors. Earned by validation, not self-rating.

**HIGH (no fill):** barcode-decoded and GS1-parsed cleanly (LOT, expiry); OR value agrees across ≥2 independent sources (barcode vs log, barcode vs vision, log vs vision); OR stable field pulled by exact log match via a confirmed REF.

**MEDIUM (amber):** single-source vision read above threshold but unverified; OR sources mostly agree with a minor discrepancy; OR REF resolved only via an uncross-checked vision read.

**LOW (red, blank):** no read / below threshold / sources materially conflict. Write nothing, color the cell.

**Business-rule validators (every ticket, day one):**
- REF exists in log? Unknown → flag.
- LOT exists in log and its expiry matches the barcode expiry? Mismatch → flag.
- Dates parse and sit in a sane range? No → flag.
- **Σ(Line Totals) == Grand Total** within tolerance? Mismatch → flag price cells amber + ticket note. Catches a lot of price misreads.

---

## 7. Learning loop — asynchronous, batched, retention-bounded

Corrected sheets will **not** come back promptly or in order. A user might sit on them and upload five at once, a week later. The design handles this without assuming any timing.

### 7.1 What gets persisted, and for how long
- On output, the system stores in Supabase, per ticket/line: the extracted values, **its own copy of the original value + confidence per field**, and a `expires_at` timestamp (`created_at + retention window`, **default 14 days, configurable**).
- The generated workbook is stored in the `output-sheets` bucket.
- A scheduled purge job runs daily: after `expires_at`, it drops the redacted image and the per-field original snapshot. **Learned facts are never purged** — only the raw originals used for diffing age out.

### 7.2 Re-upload handling (the important part)
User uploads one or more corrected `.xlsx` files (endpoint accepts a multi-file batch; or drop into a `corrected-uploads` bucket that the worker polls). For each sheet, for each row, match by **Ticket ID / Line ID** — so order, timing, and batching are irrelevant:

**A. Harvest ground-truth facts (always works, even after retention expires).**
Each corrected row is self-contained — it already holds REF, description, size, hospital, price, rep, code. So we ingest those directly as truth, no original needed:

| Corrected value | Learning store updated | Key |
|------------------|------------------------|-----|
| Description / Size | catalog supplement | REF |
| Rep name | rep map | Rep/Distributor Code |
| Unit Price | price memory | REF + Hospital |
| REF (where a GTIN was decoded) | GTIN→REF crosswalk | GTIN |

**B. Diff for calibration (only if the original is still within the retention window).**
If the stored original snapshot still exists, compare each corrected cell against it. This tells us *which low-confidence guesses were actually wrong* and *which blanks got filled* — the data that tunes confidence thresholds over time. Write to a `corrections_audit` table. If the original has aged out, skip the diff; we still got the facts from step A.

**C. Resolution of unmatched / late tickets.**
- Ticket ID in DB, within window → harvest + diff + mark verified.
- Ticket ID in DB, window expired → harvest only + mark verified (note: "learned, no diff").
- Ticket ID not in DB at all → log and surface to the user as "unknown ticket"; do not invent records.
- Same corrected sheet uploaded twice → idempotent (re-harvesting the same facts is harmless; audit dedupes on ticket+field+timestamp).

**Effect:** facts accumulate regardless of upload timing; the retention window only sets how long we keep the richer diff. Set the window to comfortably exceed your real upload lag — start at 14 days; bump to 21–28 if the team runs later than that.

**Hospital-price memory is a suggestion, never an override.** Pricing is account-based (account = hospital) and shifts with contracts, so a learned price only *raises confidence when it agrees with the read* or *flags when it disagrees* — it never silently replaces the vision read.

**Metric:** track **% of cells auto-resolved (confident) per week** — the "getting better" curve, and a clean success measure.

---

## 8. Edge cases to handle explicitly
- Blank label slots ("Place Implant Label") → skip, no line emitted.
- Barcode unreadable → vision text read + log validation.
- REF read but not in log → emit, flag amber (new product or misread).
- Qty > 1 on a line.
- Grand total ≠ sum of lines → flag, don't auto-correct.
- Rotated / blurry / glare → preprocess; if still unreadable, emit the ticket with mostly-red cells rather than failing the batch.
- Patient region can't be located on a known template → route to manual queue (§9), do not best-guess the crop.
- Corrected sheet for a ticket whose original has aged out → harvest facts, skip diff.
- Corrected sheet with an unknown Ticket ID → flag, don't create records.
- Duplicate ticket image / duplicate corrected upload → detect; idempotent on re-upload.
- Two templates with different field positions.

---

## 9. PHI handling — exclude, don't secure

**Policy: we do not process patient information at all.** The system removes it before it enters the pipeline, which keeps the rest of the system out of scope for most PHI handling.

- **Redact at ingest.** Right after template detection, locate the patient sticker (by template anchor) and mask it. Everything downstream — vision API, extraction records, stored images — sees only the redacted image.
- **Fail safe, not open.** If the patient region can't be confidently located on a recognized template, route the ticket to a **manual queue** rather than risk sending an unredacted image. Don't best-guess the crop.
- **Defense in depth.** Even with redaction, run the Claude API account **under a HIPAA BAA** in case redaction ever misses (Anthropic offers BAAs for the API). Keep incidental text out of logs.
- Surgery date and hospital are retained — operational/billing fields, not patient identifiers (hospital is the price-memory key).

---

## 10. Build phases
- **Phase 1 (MVP):** Ingest → redact → barcode decode → log lookup → vision fallback → confidence scoring → color-coded workbook in Supabase Storage. Manual review only. Proves accuracy end to end.
- **Phase 2:** Extraction-record persistence + retention model + async multi-file re-upload + fact harvesting + diff/calibration + learning stores (REF/GTIN crosswalk, rep map, hospital price). The system starts improving.
- **Phase 3:** Web UI polish, batch dashboard, auto-resolve metrics, crosswalk maturity, duplicate detection.

---

## 11. Open questions / assumptions (confirm before/early in build)
1. **Intake:** How do the ~100 images arrive each day — Smartsheet, email, shared folder, Supabase upload? (Tickets reference a Smartsheet "Orders & Reload Form.") Determines the front end.
2. **One ticket per image?** Assumed yes. Confirm no multi-page tickets.
3. ~~Account definition for price memory~~ — **Resolved: the account is the hospital.** Price keyed REF + Hospital.
4. ~~Reverse proxy = Traefik?~~ — **Resolved: Traefik.** Container exposes Traefik routing/TLS labels.
5. ~~Supabase managed or self-hosted?~~ — **Resolved: managed Supabase Cloud (supabase.com).** Connect via project URL + service-role key.
6. **Retention window:** default 14 days. Confirm it comfortably exceeds how late corrected sheets actually come back (bump to 21–28 if needed).
7. **Mfg date in output?** Captured if present; confirm whether required.
8. **Patient-region anchors:** redaction needs reliable anchor points per template. Confirm sticker placement is consistent; if it wanders, the manual-queue fallback fires more.
