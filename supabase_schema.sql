-- ============================================================================
-- Distributor Label Extraction Pipeline — Supabase schema
-- Target: managed Supabase Cloud (supabase.com)
-- Apply via: Supabase SQL Editor, or `supabase db push` / psql against the
--            project's connection string. Run top to bottom.
--
-- Design notes:
--   * Backend connects with the SERVICE-ROLE key, which bypasses RLS. RLS is
--     enabled with no public policies so nothing is exposed if anon/auth keys
--     ever touch these tables.
--   * Reference tables are a FULL REPLACE on each Expiry Log re-upload.
--   * Learning stores accumulate forever and are never purged.
--   * Per-field original snapshots (field_extractions) are purged after the
--     retention window so late diffs work but old PHI-adjacent data doesn't
--     linger. Learned facts survive the purge.
-- ============================================================================

create extension if not exists pgcrypto;   -- gen_random_uuid()
create extension if not exists pg_cron;     -- scheduled purge (enable in dashboard if needed)

-- ----------------------------------------------------------------------------
-- 0. Settings
-- ----------------------------------------------------------------------------
create table if not exists app_settings (
  key   text primary key,
  value text not null
);
insert into app_settings (key, value) values ('retention_days', '14')
  on conflict (key) do nothing;
-- Bump retention later with:
--   update app_settings set value = '21' where key = 'retention_days';
-- (affects tickets created after the change)

-- ----------------------------------------------------------------------------
-- 1. Reference data  (FULL REPLACE on every Expiry Log ingest)
-- ----------------------------------------------------------------------------

-- One row per lot, straight from the 'Expiry Log History' tab.
create table if not exists reference_lots (
  id                 bigserial primary key,
  part_no            text not null,
  description        text,
  lot                text,
  total_qty_released integer,
  lot_pallet         text,
  expiry_date        date,
  ingested_at        timestamptz not null default now()
);
create index if not exists idx_reflots_part on reference_lots (upper(part_no));
create index if not exists idx_reflots_lot  on reference_lots (upper(lot));

-- Deduped REF -> description/size, rebuilt from reference_lots on each ingest.
create table if not exists reference_parts (
  part_no     text primary key,
  description text,
  size        text,            -- parsed out of description if possible, else null
  last_seen   timestamptz not null default now()
);

-- Audit of each log upload.
create table if not exists log_ingests (
  id           uuid primary key default gen_random_uuid(),
  file_path    text,           -- storage path in 'reference-logs'
  ingested_at  timestamptz not null default now(),
  row_count    integer,
  unique_parts integer,
  unique_lots  integer
);

-- Reference masters (product + surgeon crosswalks). FULL REPLACE on each upload
-- via POST /reference/masters. Drive the 26-column output deliverable.
create table if not exists reference_gtin (
  gtin_14            text primary key,   -- decoded (01)
  gtin_12_upc        text,
  sku                text,               -- = Ref Number
  product_description text,
  status             text,               -- In Use | PreMarket | ...
  packaging_type     text,
  packaging_level    text,
  ingested_at        timestamptz not null default now()
);
create index if not exists idx_refgtin_sku on reference_gtin (upper(sku));

create table if not exists reference_part_info (
  part_number text primary key,          -- = Ref Number (exact, incl. +/- suffix)
  description text,
  part_type   text,
  category    text,
  ingested_at timestamptz not null default now()
);
create index if not exists idx_refpart_upper on reference_part_info (upper(part_number));

create table if not exists reference_surgeons (
  surgeon_distcode   text primary key,   -- upper(lastname || distcode)
  surgeon_last_name  text,
  dist_code          text,
  status             text,
  distributor        text,
  distributor_rep    text,
  sales_manager      text,
  maxx_ortho_manager text,
  surgeon_full_name  text,
  hospital           text,
  region             text,
  ingested_at        timestamptz not null default now()
);
create index if not exists idx_refsurg_dist on reference_surgeons (upper(dist_code));

create table if not exists masters_ingests (
  id           uuid primary key default gen_random_uuid(),
  ingested_at  timestamptz not null default now(),
  gtin_rows    integer,
  part_rows    integer,
  surgeon_rows integer
);

-- ----------------------------------------------------------------------------
-- 2. Learning stores  (accumulate from corrected re-uploads; never purged)
-- ----------------------------------------------------------------------------

-- REF -> description/size, learned from corrections. Overrides reference_parts.
create table if not exists learning_part_desc (
  part_no     text primary key,
  description text,
  size        text,
  updated_at  timestamptz not null default now()
);

-- Rep/Distributor Code -> Rep name.
create table if not exists learning_rep_map (
  rep_code   text primary key,
  rep_name   text not null,
  updated_at timestamptz not null default now()
);

-- (REF + Hospital) -> price. Account = hospital. Suggestion only, never override.
create table if not exists learning_price (
  part_no    text not null,
  hospital   text not null,
  unit_price numeric(12,2) not null,
  last_seen  timestamptz not null default now(),
  primary key (part_no, hospital)
);

-- GTIN -> REF crosswalk, built whenever a label yields both a decoded GTIN
-- and a confirmed REF. 'confirmations' lets you weight repeat agreement.
create table if not exists learning_gtin_xref (
  gtin          text primary key,
  part_no       text not null,
  confirmations integer not null default 1,
  updated_at    timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 3. Operational / extraction records
-- ----------------------------------------------------------------------------

create table if not exists batches (
  id                uuid primary key default gen_random_uuid(),
  run_date          date not null default current_date,
  created_at        timestamptz not null default now(),
  output_sheet_path text,                 -- storage path in 'output-sheets'
  ticket_count      integer,
  status            text not null default 'generated'  -- generated | delivered
);

create table if not exists tickets (
  ticket_id         uuid primary key default gen_random_uuid(),  -- printed in the sheet; the re-upload join key
  batch_id          uuid references batches(id) on delete set null,
  source_image_path text,                 -- REDACTED image, storage path in 'redacted-images'
  entity            text,                 -- Maxx Orthopedics | Maxx Health
  surgery_date      date,
  rep               text,
  rep_code          text,
  surgeon           text,
  hospital          text,                 -- also the price-memory account key
  po_number         text,
  freight           numeric(12,2),
  grand_total       numeric(12,2),
  sum_line_totals   numeric(12,2),
  flags             jsonb default '[]'::jsonb,
  status            text not null default 'pending_review',
                    -- pending_review | verified | manual_queue | unknown
  created_at        timestamptz not null default now(),
  expires_at        timestamptz,          -- set by trigger = created_at + retention_days
  verified_at       timestamptz
);
create index if not exists idx_tickets_status  on tickets (status);
create index if not exists idx_tickets_expires on tickets (expires_at);
create index if not exists idx_tickets_batch   on tickets (batch_id);

create table if not exists line_items (
  line_id     uuid primary key default gen_random_uuid(),
  ticket_id   uuid not null references tickets(ticket_id) on delete cascade,
  ref         text,
  gtin        text,
  description text,
  size        text,
  lot         text,
  qty         integer,
  mfg_date    date,
  expiry_date date,
  unit_price  numeric(12,2),
  line_total  numeric(12,2),
  flags       jsonb default '[]'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists idx_lineitems_ticket on line_items (ticket_id);

-- Per-field original snapshot + confidence. This is what the diff compares
-- against on re-upload, and what the retention purge deletes.
-- line_id NULL  => ticket-level field (rep, hospital, grand_total, ...)
create table if not exists field_extractions (
  id          bigserial primary key,
  ticket_id   uuid not null references tickets(ticket_id) on delete cascade,
  line_id     uuid references line_items(line_id) on delete cascade,
  field_name  text not null,
  orig_value  text,                       -- stringified original extraction
  confidence  text not null,              -- high | medium | low
  source      text,                       -- barcode | log | vision | computed
  created_at  timestamptz not null default now()
);
create index if not exists idx_fieldext_ticket on field_extractions (ticket_id);

-- What humans changed on re-upload — the calibration record.
create table if not exists corrections_audit (
  id                 bigserial primary key,
  ticket_id          uuid not null,
  line_id            uuid,
  field_name         text not null,
  orig_value         text,
  orig_confidence    text,
  corrected_value    text,
  was_blank          boolean not null default false,  -- red cell -> filled
  was_low_conf       boolean not null default false,  -- amber cell -> changed
  corrected_at       timestamptz not null default now()
);
create index if not exists idx_corr_ticket on corrections_audit (ticket_id);

-- Log of corrected-sheet uploads (any number, any time).
create table if not exists corrected_uploads (
  id               uuid primary key default gen_random_uuid(),
  file_path        text,                  -- storage path in 'corrected-uploads'
  uploaded_at      timestamptz not null default now(),
  sheets_processed integer default 0,
  tickets_matched  integer default 0,
  tickets_unknown  integer default 0,
  status           text not null default 'processed'
);

-- ----------------------------------------------------------------------------
-- 4. Resolver view: prefer learned correction, fall back to log
--    Use this for REF -> description/size lookups during extraction.
-- ----------------------------------------------------------------------------
create or replace view part_resolved as
select
  coalesce(l.part_no, r.part_no)               as part_no,
  coalesce(l.description, r.description)        as description,
  coalesce(l.size, r.size)                      as size,
  (l.part_no is not null)                       as from_correction
from reference_parts r
full outer join learning_part_desc l on upper(l.part_no) = upper(r.part_no);

-- ----------------------------------------------------------------------------
-- 5. Retention: set expiry on insert, purge expired snapshots nightly
-- ----------------------------------------------------------------------------
create or replace function set_ticket_expiry() returns trigger as $$
declare ret integer;
begin
  select coalesce(value::int, 14) into ret from app_settings where key = 'retention_days';
  new.expires_at := coalesce(new.created_at, now()) + make_interval(days => ret);
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_ticket_expiry on tickets;
create trigger trg_ticket_expiry
  before insert on tickets
  for each row execute function set_ticket_expiry();

-- Deletes only the per-field original snapshots for tickets past their window.
-- Tickets, line_items, learning stores, and corrections_audit are untouched.
-- NOTE: deleting the redacted image objects from Storage is done by the worker
-- (query expired tickets, delete their source_image_path objects via the
-- Supabase client), since object deletion isn't done from SQL here.
create or replace function purge_expired_extractions() returns void as $$
begin
  delete from field_extractions fe
  using tickets t
  where fe.ticket_id = t.ticket_id
    and t.expires_at < now();
end;
$$ language plpgsql;

-- Schedule nightly at 03:00. (If pg_cron isn't enabled, turn it on in
-- Supabase: Database > Extensions > pg_cron, then re-run this line.)
select cron.schedule('purge-expired-extractions', '0 3 * * *',
                     $$ select purge_expired_extractions(); $$);

-- ----------------------------------------------------------------------------
-- 6. Lock down RLS (backend uses service-role key, which bypasses RLS)
-- ----------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array[
    'app_settings','reference_lots','reference_parts','log_ingests',
    'reference_gtin','reference_part_info','reference_surgeons','masters_ingests',
    'learning_part_desc','learning_rep_map','learning_price','learning_gtin_xref',
    'batches','tickets','line_items','field_extractions',
    'corrections_audit','corrected_uploads'
  ] loop
    execute format('alter table %I enable row level security;', t);
  end loop;
end $$;
-- Intentionally no policies created => no access via anon/auth keys.
-- All access is server-side via the service-role key.

-- ============================================================================
-- 7. Storage buckets — create in Supabase Storage (Dashboard or API), all PRIVATE:
--     redacted-images    : redacted ticket images
--     output-sheets      : generated review workbooks (per batch)
--     corrected-uploads  : incoming corrected workbooks
--     reference-logs     : uploaded Expiry Log snapshots (optional history)
--   Leave all private; the backend reads/writes with the service-role key.
-- ============================================================================
