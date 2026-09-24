-- 11 — Hospital price list (reference master for step 5, price enrichment)
-- One row per priced (tab, item, hospital) intersection of the hospital
-- price-list workbook. Full-replaced on each upload, exactly like the other
-- reference masters in db/09. Read-only at enrichment time; nothing here ever
-- feeds the learning tables.
--
-- A component with no price anywhere simply has no rows: it cannot produce a
-- price, and the per-tab description catalogue is derived from these rows.
-- Safe to run more than once.

create table if not exists reference_hospital_prices (
  tab          text not null,
  item_code    text not null,
  description  text,
  hospital     text not null,
  unit_price   numeric not null,
  ingested_at  timestamptz not null default now(),
  primary key (tab, item_code, hospital)
);

alter table reference_hospital_prices enable row level security;

create index if not exists reference_hospital_prices_tab_idx
  on reference_hospital_prices (tab);

-- One row per step-5 enrichment run: the audit trail, and the pointer the UI
-- uses to re-download the last good output. A failed run still gets a row (with
-- output_path null), which is what makes "a failed run leaves the previous
-- output in place" true by construction rather than by cleanup.
create table if not exists pricing_runs (
  run_id           text primary key,
  created_at       timestamptz not null default now(),
  source_filename  text,
  output_path      text,
  status           text not null,          -- succeeded | failed
  failure_reason   text,
  tabs             text,                   -- comma-separated, in row order
  eligible         integer,
  direct           integer,
  estimates        integer,
  unresolved       integer,
  skipped_wasted   integer,
  zero_estimates   integer,
  summary          jsonb
);

alter table pricing_runs enable row level security;

create index if not exists pricing_runs_created_idx
  on pricing_runs (created_at desc);
