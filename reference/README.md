# Reference masters (read-only lookup sources)

Full-replaced on each upload via `POST /reference/masters` and **seeded on
startup** from these bundled copies if the datastore has none. Product/surgeon
master data — **no patient PHI**. See `docs/EXTRACTION_FIELD_GUIDE.md` §2.

| File | Key | Provides |
|------|-----|----------|
| `GTIN_Codes.csv` | `GTIN_14` (decoded `(01)`) | `SKU` (= Ref Number), `PRODUCT_DESCRIPTION`, `STATUS` |
| `part_info.csv` | `Part Number` (= Ref Number) | `Description`, `Part Type`, `Category` — row 1 is junk, header is row 2 |
| `surgeon_info.csv` | `<SurgeonLastName><DistCode>` | `Surgeon Full Name`, `Hospital`, `Region`, canonical `DistCode` — blank-key overflow rows are skipped |
| `output_columns.csv` | — | the exact 26-column output order (the deliverable) |

The Expiry Log (`Expiry_Log.xlsx`) is a separate upload (`POST /reference/log`)
and stays the authoritative lot→expiry validation source.
