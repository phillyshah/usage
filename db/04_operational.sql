-- 04 — Operational / extraction records
-- batches, tickets, line_items, field_extractions, corrections_audit, corrected_uploads

create table if not exists batches (
  id                uuid primary key default gen_random_uuid(),
  run_date          date not null default current_date,
  created_at        timestamptz not null default now(),
  output_sheet_path text,
  ticket_count      integer,
  status            text not null default 'generated'
);

create table if not exists tickets (
  ticket_id         uuid primary key default gen_random_uuid(),
  batch_id          uuid references batches(id) on delete set null,
  source_image_path text,
  entity            text,
  surgery_date      date,
  rep               text,
  rep_code          text,
  surgeon           text,
  hospital          text,
  po_number         text,
  freight           numeric(12,2),
  grand_total       numeric(12,2),
  sum_line_totals   numeric(12,2),
  flags             jsonb default '[]'::jsonb,
  status            text not null default 'pending_review',
  created_at        timestamptz not null default now(),
  expires_at        timestamptz,
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

create table if not exists field_extractions (
  id          bigserial primary key,
  ticket_id   uuid not null references tickets(ticket_id) on delete cascade,
  line_id     uuid references line_items(line_id) on delete cascade,
  field_name  text not null,
  orig_value  text,
  confidence  text not null,
  source      text,
  created_at  timestamptz not null default now()
);
create index if not exists idx_fieldext_ticket on field_extractions (ticket_id);

create table if not exists corrections_audit (
  id                 bigserial primary key,
  ticket_id          uuid not null,
  line_id            uuid,
  field_name         text not null,
  orig_value         text,
  orig_confidence    text,
  corrected_value    text,
  was_blank          boolean not null default false,
  was_low_conf       boolean not null default false,
  corrected_at       timestamptz not null default now()
);
create index if not exists idx_corr_ticket on corrections_audit (ticket_id);

create table if not exists corrected_uploads (
  id               uuid primary key default gen_random_uuid(),
  file_path        text,
  uploaded_at      timestamptz not null default now(),
  sheets_processed integer default 0,
  tickets_matched  integer default 0,
  tickets_unknown  integer default 0,
  status           text not null default 'processed'
);
