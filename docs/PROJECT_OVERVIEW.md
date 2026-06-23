# Project Overview — Distributor Label Extraction Pipeline

*Start here. This is the orientation doc: what we're building, why it matters, and the
principles that should guide every decision you make while building it. The technical
detail lives in the companion docs (see the end).*

---

## The problem

Every day, our distributors send us roughly **100 implant usage tickets** — the forms
that record which orthopedic implants were used in which surgery. They arrive as **phone
photos**: a mix of printed device labels and handwritten notes, often skewed, blurry, or
glare-streaked, on two different ticket templates (Maxx Orthopedics and Maxx Health).

Right now, a person reads each one and types the data into a spreadsheet by hand. At 100
tickets a day — each with several device lines — that's a large, repetitive, error-prone
manual load. It doesn't scale, it burns staff time on transcription instead of judgment,
and every misread REF or lot number is a problem downstream.

This data isn't trivia. It's how we know **what was implanted, in whom, from which lot** —
which drives billing, consigned-inventory replenishment, and device traceability (the lot
and expiry on every label exist because of FDA UDI requirements). Getting it right, and
getting it captured without a human keying every field, is the whole point.

## What we're building

A pipeline that takes those daily ticket photos and produces a **review spreadsheet** with
the data already extracted — and, crucially, with every field it isn't sure about clearly
flagged so a human can verify or fill it in. Over time, as corrected spreadsheets are
re-uploaded, the system **learns** and needs less and less human correction.

The goal is not "AI reads the tickets." The goal is **to shrink manual data entry to a
quick review pass**, while never silently putting a wrong value into a system of record.

## Who uses it

- **Distributors** photograph and send the tickets (the input).
- **Our internal team** reviews the generated spreadsheet, fixes anything flagged, and
  re-uploads the corrected version — which both finalizes the data and teaches the system.

The human stays in the loop on purpose. This is medical and billing data; a fast,
trustworthy review beats a slow, blind automation every time.

---

## The guiding principles (the design DNA)

These are the non-negotiables. When you hit an implementation fork the docs don't cover,
decide in the direction of these:

1. **Deterministic first, AI second.** Device labels carry GS1 DataMatrix UDI barcodes.
   Decoding a barcode is exact; OCR'ing a blurry photo is a guess. So we pull structured
   device data (lot, expiry, product identity) from **barcodes and our reference data**
   wherever possible, and reserve the Claude vision call for what only it can do —
   handwriting, header fields, prices. AI is the fallback, not the engine.

2. **Never guess silently.** Every cell ends up in one of three states: confident,
   low-confidence guess (flagged), or blank (flagged). A wrong value that *looks*
   confident is far more dangerous than an honest blank. When unsure, we leave it blank
   and color it for a human.

3. **Confidence comes from validation, not vibes.** A value is "confident" because two
   independent sources agree (barcode vs. log vs. vision) or it matches known reference
   data — not because a model said it felt sure. Models sound confident even when wrong;
   we don't trust self-reported certainty as the final word.

4. **Learn over time, deterministically.** The system gets smarter by accumulating a local
   store of facts harvested from corrected spreadsheets (this REF means this product, this
   hospital pays this price, this code maps to this rep). That's lookup tables that grow —
   explainable and auditable — not opaque model retraining.

5. **Exclude patient data entirely.** Hospitals keep including patient identifiers on these
   tickets even though we ask them not to. We **do not process that information at all** —
   the patient region is masked at ingest, before anything reads, sends, or stores the
   image. This is a hard line, not a best-effort.

6. **Respect what's authoritative.** The team's Expiry Log is read-only reference we
   re-ingest as-is. Pricing is account-based (account = hospital) and is never treated as
   a fixed catalog. We don't overwrite ground truth with guesses.

---

## How it works, at a glance

```
Ticket photo
   → mask the patient area (PHI never enters the pipeline)
   → decode the barcodes        → exact lot / expiry / product identity
   → look up our reference log   → product description, size, validation
   → ask Claude (vision) only for what's left: handwriting, prices, header
   → score each field's confidence by cross-checking the sources
   → write a color-coded spreadsheet: confident / verify / fill-in
       ↑
   → a human reviews, corrects, and re-uploads
   → the system harvests those corrections into its memory and improves
```

Two data sources make this work and let the system start **warm, not cold**:

- **The UDI barcodes** on every device label — structured, exact, FDA-mandated.
- **The team's Expiry Log** — already maps ~1,682 products and ~51,000 lot numbers to
  descriptions, sizes, and expiry dates. The system knows most of the catalog on day one.

What the system **can't** get for free is pricing (account-based, read from the ticket) and
anything handwritten — which is exactly where the human review and the learning loop earn
their keep.

---

## What "good" looks like

- **Manual entry collapses into a review pass.** Most cells come back confident; the team
  only touches the flagged ones.
- **The flagged pile shrinks week over week** as the learning store fills. We track this
  directly: percentage of cells auto-resolved per week. That curve going up is success.
- **No wrong value ever lands silently** in the output — uncertainty is always visible.
- **No patient data is ever processed or stored.**
- **Device data is trustworthy** — REF, lot, expiry, and quantity per surgery are accurate,
  because they come from barcodes and validated reference data, not from reading a photo.

---

## How the documents fit together

Read in this order:

1. **`PROJECT_OVERVIEW.md`** (this file) — the why and the mental model.
2. **`LABEL_EXTRACTION_BUILD_SPEC.md`** — the detailed *what*: inputs, extraction strategy,
   confidence model, output format, the learning/retention loop, PHI handling. Authoritative
   on behavior.
3. **`DEVELOPER_HANDOFF.md`** — the *how*: repo structure, dependencies, API contract,
   module responsibilities, the vision prompt, Docker/Traefik deploy, and a phased task
   list with acceptance criteria.
4. **`supabase_schema.sql`** — the database. Run as-is; match its names exactly.

---

## Where it's headed

- **Phase 1** proves the extraction end to end and produces the review spreadsheet.
- **Phase 2** adds the persistence, the re-upload loop, and the learning stores — the system
  starts improving.
- **Phase 3** is polish: a clean upload/download UI, a dashboard for the auto-resolve metric,
  a maturing GTIN→REF crosswalk, and duplicate detection.

Beyond that, the same backbone supports feeding the cleaned data straight into downstream
billing and inventory workflows, and accepting tickets from automated intake (Smartsheet,
email) instead of manual upload. Build Phase 1 well and the rest is additive.
