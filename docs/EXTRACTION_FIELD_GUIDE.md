# Extraction & Output Instructions

Explicit build instructions for the coding agents: what to read off each ticket, what to
**look up** from the reference tables, the exact output columns, and the validation rules.
Pairs with `LABEL_EXTRACTION_BUILD_SPEC.md` (behavior/architecture) and `DEVELOPER_HANDOFF.md`.

> **Key principle:** most output columns are **joins, not reads.** The agent extracts a few
> values off the ticket header and each device label, then enriches every remaining column by
> looking up the reference tables. Read as little as possible; look up as much as possible.

---

## 1. The deliverable

**One output row per device line item.** Columns are exactly those in `output_columns.csv`
(26 columns, in this order):

`Reload Code, Surgeon, DistCode, Date, Month, Year, Hospital, Quantity, Price, Lot Number,
Ref Number, Expiry Date, Invoice No., Invoice Date, SurgeonName, Distributor, Distributor Rep,
Sales Rep, Maxx Sales Manager, Distributing Company, Distributor Code, Region, Description,
Part Type, Category, SAP Part Number`

A ticket with 6 labels produces 6 rows; the header-derived columns repeat on each row.

**Added column (not in `output_columns.csv`): `Source Image Filename`.** Include the filename of
the uploaded ticket image on every row, as the **first column**, so the accountant can tie each row
back to its source image when editing. (`Reload Code` is **not used** — leave it blank; see below.)

---

## 2. Reference tables and join keys (all read-only, re-ingested when updated)

| Table | Key | Provides |
|-------|-----|----------|
| **`GTIN_Codes.csv`** (product master, 5,413) | `GTIN_14` = decoded `(01)` | `SKU` (= Ref Number), `PRODUCT_DESCRIPTION`, `STATUS` (In Use/PreMarket) |
| **`part_info.csv`** (1,719 parts) | `Part Number` = Ref Number (exact, incl. suffix) | `Description`, `Part Type`, `Category` |
| **`surgeon_info.csv`** (564 records) | `Surgeon-DistCode` = `<SurgeonLastName>` + `<DistCode>` | SurgeonName, Distributor, DistributorRep, Sales Manager, Hospital, Region, Status, … |
| **`Expiry_Log.xlsx`** (~1,682 REF / ~51k lot) | `Lot #`, `Part No` | `expiry` (lot-level), description — used to **validate** lot/expiry |

**Parsing notes:**
- `part_info.csv`: row 1 is junk (`1,2,3,4`); the real header is row 2 (`Part Number, Description, Part Type, Category`); data starts row 3.
- `surgeon_info.csv`: addresses spill onto continuation rows where the key columns are blank. A **record** is any row with a non-empty `DistCode`; following blank-key rows are address overflow — skip them.

---

## 3. PHI redaction (do first, every ticket)

The Maxx Health template carries a **patient sticker in the top-right of the header** (name,
DOB, MR#, AC#, provider — e.g. the `TERRELL, ISABEL M …` block on the sample). **Mask this
region before any read, API call, or storage.** If it can't be confidently located on a known
template, route the ticket to the manual queue. Never read or output any patient field.

---

## 4. Extraction method hierarchy (per field, in order)

1. **GS1 DataMatrix decode** (`pylibdmtx` → parse with `biip`) — primary for device fields. Free, exact.
2. **Printed GS1 text OCR** (Haiku 4.5) — fallback when the DataMatrix won't decode.
3. **Header vision** (Haiku 4.5) — DistCode, Surgeon, Surgery Date off the handwritten header.
4. **Handwriting vision** (Sonnet 4.6) — prices and annotations.

DataMatrix AIs: `(01)` GTIN-14, `(10)` lot, `(11)` mfg `YYMMDD`, `(17)` expiry `YYMMDD`, `(240)` REF.
Validate the GTIN-14 **mod-10 check digit**; parse `YYMMDD`→`20YY-MM-DD`. Never let OCR override a clean decode.

---

## 5. Output columns — source of truth (all 26)

Legend: **READ** = off the ticket · **DECODE** = from the DataMatrix · **DERIVE** = computed ·
**LOOKUP** = joined from a reference table · ⚠ = mapping needs Andy's confirmation (§8).

| # | Column | Source | Key / method & rules |
|---|--------|--------|----------------------|
| ★ | Source Image Filename | SYSTEM | Filename of the uploaded ticket image. **First column.** Reference anchor for the accountant. Not in `output_columns.csv`. |
| 1 | Reload Code | — | **Not used — leave blank** (per Andy). |
| 2 | Surgeon | READ | Surgeon name as written (last name, e.g. `Montijo`). Used to build the surgeon_info key. |
| 3 | DistCode | READ | Rep/Distributor code (e.g. `MC-001`). Normalize spacing → `MC-001`. |
| 4 | Date | READ | Surgery Date as a numeric date `MM/DD/YYYY` (e.g. `06/15/2026`). |
| 5 | Month | DERIVE | Numeric month from Date (e.g. `6`). |
| 6 | Year | DERIVE | Numeric 4-digit year from Date (e.g. `2026`). |
| 7 | Hospital | LOOKUP | `surgeon_info.Hospital`. The handwritten hospital is only a cross-check; **flag if it disagrees** with the looked-up value. |
| 8 | Quantity | per row | **One row per physical unit (per label/lot).** Quantity is **1 on each row**; a REF used N times yields N separate rows, each with its own Lot Number. Never collapse duplicates. |
| 9 | Price | READ (Sonnet) | Handwritten near each label. Validate Σ(prices) = Grand Total. **Wasted item** — a handwritten **"W" or "wasted"** near the component — still produces a usage row; add a `WASTED` note + highlight the cell yellow. |
| 10 | Lot Number | DECODE `(10)` | Fallback OCR. Validate vs Expiry Log lot set. OCR-only confusables: `S↔5, O↔0, I↔1, B↔8`. |
| 11 | Ref Number | DECODE `(01)`→`GTIN_Codes.SKU` | `(240)` and printed REF box are cross-checks. **Preserve trailing `+`/`-`** (`MO-HDAI-36/40-` ≠ `…/40+`). GTIN not in master → flag. |
| 12 | Expiry Date | DECODE `(17)` | Validate vs `Expiry_Log` lot→expiry. |
| 13 | Invoice No. | — | Leave blank (deferred to a future version). |
| 14 | Invoice Date | — | Leave blank (deferred to a future version). |
| 15 | SurgeonName | LOOKUP | `surgeon_info.Surgeon Full Name` (e.g. `Harvey Montijo`). |
| 16 | Distributor | — | Leave blank (deferred to a future version). |
| 17 | Distributor Rep | — | Leave blank (deferred to a future version). |
| 18 | Sales Rep | — | Leave blank (deferred to a future version). |
| 19 | Maxx Sales Manager | — | Leave blank (deferred to a future version). |
| 20 | Distributing Company | — | Leave blank (deferred to a future version). |
| 21 | Distributor Code | LOOKUP | `surgeon_info.DistCode` (canonical; should equal col 3). |
| 22 | Region | LOOKUP | `surgeon_info.Region`. |
| 23 | Description | LOOKUP | `part_info.Description` (by Ref Number). |
| 24 | Part Type | LOOKUP | `part_info.Part Type`. |
| 25 | Category | LOOKUP | `part_info.Category`. |
| 26 | SAP Part Number | — | Leave blank (deferred to a future version). |

---

## 6. Validation & business rules

- **Surgeon ↔ DistCode must match.** Build the key `<SurgeonLastName><DistCode>` and require it to
  exist in `surgeon_info.Surgeon-DistCode`. Resolves → all the LOOKUP columns; **no match → flag the
  row** (this is the "Distributor Code must match surgeon" rule). Prefer Active records.
- **Hospital cross-check.** Output Hospital comes from `surgeon_info`; if the handwritten hospital
  disagrees, flag for review rather than overwriting silently.
- **Price reconciliation.** Σ(line Price) + Freight must equal Grand Total (tolerance configurable);
  mismatch → flag the price cells.
- **Lot/expiry cross-check.** Decoded `(10)`/`(17)` validated against the Expiry Log; GTIN check digit
  must pass; GTIN must be in the master and `STATUS = In Use` (else flag).
- **One row per unit.** Each physical component (each label/lot) is its own row with `Quantity = 1`. The same
  REF used multiple times produces multiple rows — one per lot — never a single row with `Quantity = N`. Keeps every lot traceable.
- **Wasted items.** A handwritten **"W" or "wasted"** near a component marks it wasted. **Still emit the
  line as a usage row** — add a `WASTED` note and highlight the cell yellow. **If a price is written, it
  still counts** toward the Grand Total (wasted status does not exclude it; a wasted line with no price contributes 0).
- **Coloring:** confident → no fill; single-source guess → amber; blank/unreadable → red; wasted → yellow + note.

---

## 7. Worked example — ticket `MH13366-A` (use as the end-to-end test fixture)

**Header reads:** DistCode `MC-001`, Surgeon `Montijo`, Surgery Date `2026-06-15` (→ Month `06`, Year `2026`).
**Key `MontijoMC-001` resolves** in `surgeon_info` → SurgeonName `Harvey Montijo`, DistributorRep
`Michael Mauger`, Maxx Sales Manager `Maxx Health - Shane`, Hospital `Wellington Regional Medical
Center`, Region `South`, Status `Active`. (Note: the header's handwritten hospital does **not** clearly
read "Wellington" — exactly the case the cross-check flag is for.)

**Six line items** (decode → master/part_info lookup → price read):

| Lot (10) | GTIN (01) | Ref (SKU) | Expiry (17) | Description / Type / Category (part_info) | Price |
|----------|-----------|-----------|-------------|-------------------------------------------|-------|
| U37142706 | 00810008121849 | MO-SWCC-65/30 | 2030-11-30 | Bone Screw Ø6.5 x 30mm / Libertas Screw / Screw | $68.00 |
| R13142703 | 00810008121825 | MO-SWCC-65/20 | 2027-03-31 | Bone Screw Ø6.5 x 20mm / Libertas Screw / Screw | $68.00 |
| S41122707 | 00810008120088 | MO-MSFC-56/MH | 2028-10-31 | Modular Shell 56/MH - 3 Holes / Libertas Shell / Acetabular Cup | $900.00 |
| 7011975747 | 00810008121108 | MO-HDAI-36/40- | 2030-05-31 | Ceramic Femoral Head 36/-4.0mm / Libertas Head Ceramic / Head | $650.00 |
| U02102712 | 00810008124338 | MO-STVC-35/03 | 2030-02-28 | Uncemented Stem 03/135° Collared / Libertas Stem 135 Collared / Hip Stem | $1,900.00 |
| S21132706 | 00810008120606 | MO-MLHH-MH/36 | 2028-04-30 | E-XLPE Liner MH/36 / Libertas E-XLPE Liner / Hip Liner | $550.00 |

**Reconciliation:** 68 + 68 + 900 + 650 + 1900 + 550 = **$4,136.00 = Grand Total ✓.** Every device
field resolves at high confidence from the decode + lookups; only the six prices need the vision model.

---

## 8. Open items (need Andy to confirm before final build)

1. ~~Reload Code~~ — **Resolved: not used, leave blank.** Added a `Source Image Filename` first column as the accountant's reference anchor.
2. ~~Distributor field cluster~~ — **Deferred: leave columns 16–20 (Distributor, Distributor Rep, Sales Rep, Maxx Sales Manager, Distributing Company) blank for this version.** Revisit in a future version.
3. ~~SAP Part Number~~ — **Deferred: leave blank for this version.**
4. ~~Invoice No. / Invoice Date~~ — **Deferred: leave both blank for this version.**
5. ~~Month format~~ — **Resolved: Date, Month, and Year are all numeric** (`06/15/2026`, `6`, `2026`).
6. ~~Wasted-item marker~~ — **Resolved: a handwritten "W" or "wasted" near the component.** The line is still emitted as a usage row, with a `WASTED` note + yellow cell.
7. ~~Quantity~~ — **Resolved: one row per physical unit, `Quantity = 1` per row.**
8. ~~qty vs. lot granularity~~ — **Resolved: option (b)** — a REF used N times = N rows of `Quantity = 1`, one per lot number.
