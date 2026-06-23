-- 09 — Reference master tables (product + surgeon crosswalks)
-- Full-replace on each masters upload (POST /reference/masters), exactly like
-- the Expiry Log. These drive the 26-column output deliverable:
--   * reference_gtin     : decoded (01) GTIN-14 -> SKU (= Ref Number) + status
--   * reference_part_info: Ref Number -> Description / Part Type / Category
--   * reference_surgeons : <SurgeonLastName><DistCode> -> surgeon/hospital/region
-- All read-only reference; never edited in place.

-- GTIN_Codes.csv : product master (GTIN_14 -> SKU). ~5,413 rows.
create table if not exists reference_gtin (
  gtin_14           text primary key,
  gtin_12_upc       text,
  sku               text,            -- = Ref Number
  product_description text,
  status            text,            -- In Use | PreMarket | ...
  packaging_type    text,
  packaging_level   text,
  ingested_at       timestamptz not null default now()
);
create index if not exists idx_refgtin_sku on reference_gtin (upper(sku));

-- part_info.csv : Ref Number -> Description / Part Type / Category. ~1,719 rows.
create table if not exists reference_part_info (
  part_number text primary key,      -- = Ref Number (exact, incl. suffix +/-)
  description text,
  part_type   text,
  category    text,
  ingested_at timestamptz not null default now()
);
create index if not exists idx_refpart_upper on reference_part_info (upper(part_number));

-- surgeon_info.csv : <SurgeonLastName><DistCode> -> surgeon/hospital/region. ~564 records.
create table if not exists reference_surgeons (
  surgeon_distcode  text primary key, -- normalized key: upper(lastname || distcode)
  surgeon_last_name text,
  dist_code         text,
  status            text,
  distributor       text,
  distributor_rep   text,
  sales_manager     text,
  maxx_ortho_manager text,
  surgeon_full_name text,
  hospital          text,
  region            text,
  ingested_at       timestamptz not null default now()
);
create index if not exists idx_refsurg_dist on reference_surgeons (upper(dist_code));

-- Audit of each masters upload (mirrors log_ingests).
create table if not exists masters_ingests (
  id            uuid primary key default gen_random_uuid(),
  ingested_at   timestamptz not null default now(),
  gtin_rows     integer,
  part_rows     integer,
  surgeon_rows  integer
);

alter table reference_gtin       enable row level security;
alter table reference_part_info  enable row level security;
alter table reference_surgeons   enable row level security;
alter table masters_ingests      enable row level security;
