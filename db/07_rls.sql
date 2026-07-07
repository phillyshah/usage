-- 07 — Lock down RLS
-- Enable RLS on every table with NO policies, so only the server-side
-- service-role key (which bypasses RLS) can read/write. The publishable/anon
-- key gets nothing.

do $$
declare t text;
begin
  foreach t in array array[
    'app_settings','reference_lots','reference_parts','log_ingests',
    'learning_part_desc','learning_rep_map','learning_price','learning_gtin_xref',
    'learning_surgeon_map',
    'batches','tickets','line_items','field_extractions',
    'corrections_audit','corrected_uploads'
  ] loop
    execute format('alter table %I enable row level security;', t);
  end loop;
end $$;

-- Verify: should return 15 rows.
-- select table_name from information_schema.tables
-- where table_schema = 'public' order by table_name;
