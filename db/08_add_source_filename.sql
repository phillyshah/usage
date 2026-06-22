-- 08 — Add source_filename to tickets
-- Stores the original uploaded photo file name (e.g. "MO083596.jpg") so the
-- review sheet can show a "File" column the accountants use to trace each row
-- back to its source photo. Safe to run more than once.

alter table tickets add column if not exists source_filename text;
