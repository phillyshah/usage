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
