-- 03 — Learning stores
-- Accumulate from corrected re-uploads. Never purged.

create table if not exists learning_part_desc (
  part_no     text primary key,
  description text,
  size        text,
  updated_at  timestamptz not null default now()
);

create table if not exists learning_rep_map (
  rep_code   text primary key,
  rep_name   text not null,
  updated_at timestamptz not null default now()
);

create table if not exists learning_price (
  part_no    text not null,
  hospital   text not null,
  unit_price numeric(12,2) not null,
  last_seen  timestamptz not null default now(),
  primary key (part_no, hospital)
);

create table if not exists learning_gtin_xref (
  gtin          text primary key,
  part_no       text not null,
  confirmations integer not null default 1,
  updated_at    timestamptz not null default now()
);

-- <SurgeonLastName><DistCode> -> surgeon/hospital/dist code learned from
-- corrected sheets. Fallback only — the reference_surgeons master always wins.
create table if not exists learning_surgeon_map (
  surgeon_key       text primary key,
  surgeon_full_name text,
  hospital          text,
  dist_code         text,
  updated_at        timestamptz not null default now()
);
