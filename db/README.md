# Database scripts

These are `supabase_schema.sql` split into ordered, individually-runnable blocks
for first-time setup in the Supabase **SQL Editor**. Run them **in numerical
order**:

| # | File | What it does |
|---|------|--------------|
| 01 | `01_extensions.sql` | pgcrypto + pg_cron, `app_settings` (retention) |
| 02 | `02_reference.sql`  | reference tables (filled by the Expiry Log upload) |
| 03 | `03_learning.sql`   | learning stores (never purged) |
| 04 | `04_operational.sql`| batches, tickets, line_items, snapshots, audit |
| 05 | `05_view.sql`       | `part_resolved` resolver view |
| 06 | `06_retention.sql`  | expiry trigger + purge function + nightly cron |
| 07 | `07_rls.sql`        | enable RLS on all tables (no policies) |

Prereq: enable **pg_cron** first (Database → Extensions) so script 06 succeeds.

After 07, verify you have **14 tables**:

```sql
select table_name from information_schema.tables
where table_schema = 'public' order by table_name;
```

Then create the 4 private Storage buckets (Storage → New bucket, Public OFF):
`redacted-images`, `output-sheets`, `corrected-uploads`, `reference-logs` —
or run `make buckets` once `.env` is filled in.

> `../supabase_schema.sql` is the same content as one file if you'd rather paste
> it all at once. Names match the application code exactly — don't rename.
