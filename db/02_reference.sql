-- 02 — Reference tables
-- Filled by the Expiry Log upload (full replace each time). Start empty.

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

create table if not exists reference_parts (
  part_no     text primary key,
  description text,
  size        text,
  last_seen   timestamptz not null default now()
);

create table if not exists log_ingests (
  id           uuid primary key default gen_random_uuid(),
  file_path    text,
  ingested_at  timestamptz not null default now(),
  row_count    integer,
  unique_parts integer,
  unique_lots  integer
);
