# Reference masters (read-only lookup sources)

Full-replaced on each upload via `POST /reference/masters` and **seeded on
startup** from these bundled copies if the datastore has none. Product/surgeon
master data — **no patient PHI**. See `docs/EXTRACTION_FIELD_GUIDE.md` §2.

> In production these are **Excel** workbooks (same as the Expiry Log). The upload
> endpoint and startup seed accept either `.xlsx` or `.csv`; the copies committed
> here are CSV exports (lighter for git/tests). Columns are matched by header
> **name**, and `GTIN_14` is re-padded to 14 digits if Excel dropped leading zeros.

**Snapshot date.** `MASTERS_VERSION` holds the effective date of the committed
copies (currently `2026-06-23`). The startup seed stamps the load with this date
so the freshness banner shows the data date, not the deploy time. When you refresh
these files, bump `MASTERS_VERSION` too — or just upload the new files via
`POST /reference/masters`, which records the upload date automatically.

| File | Key | Provides |
|------|-----|----------|
| `GTIN_Codes.csv` | `GTIN_14` (decoded `(01)`) | `SKU` (= Ref Number), `PRODUCT_DESCRIPTION`, `STATUS` |
| `part_info.csv` | `Part Number` (= Ref Number) | `Description`, `Part Type`, `Category` — row 1 is junk, header is row 2 |
| `surgeon_info.csv` | `<SurgeonLastName><DistCode>` | `Surgeon Full Name`, `Hospital`, `Region`, canonical `DistCode` — blank-key overflow rows are skipped |
| `output_columns.csv` | — | the exact 26-column output order (the deliverable) |

The Expiry Log (`Expiry_Log.xlsx`) is a separate upload (`POST /reference/log`)
and stays the authoritative lot→expiry validation source.
