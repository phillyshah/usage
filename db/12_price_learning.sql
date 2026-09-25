-- 12 — Learned-price provenance, and what step 5 wrote
--
-- Step 5 fills blank prices; step 4 sends the reviewed file back so the system
-- learns from it. Two things are needed to do that without laundering a guess
-- into a fact. Both are additive; no existing row changes. Safe to re-run.

-- WHERE A LEARNED PRICE CAME FROM.
--
--   'correction'  a human typed or changed it. Authoritative, never purged.
--   'price_list'  step 5 took it verbatim from the hospital price list and a
--                 human left it alone.
--
-- The distinction earns its keep on the next price-list upload: the price list
-- is FULL-REPLACED monthly, but learning_price is never purged, so a learned
-- price-list value would otherwise outlive the update meant to supersede it.
-- db.clear_price_list_learning() drops those rows and only those rows.
--
-- The default is 'correction' on purpose: every row that exists before this
-- migration came from the step-4 corrections path, so defaulting that way
-- marks the existing store truthfully and keeps it out of any future purge.
alter table learning_price
  add column if not exists source text not null default 'correction';

create index if not exists learning_price_source_idx on learning_price (source);

-- WHAT STEP 5 WROTE, cell by cell: [{row, ref, hospital, value, kind}].
--
-- Without this, a returned workbook is just a column of numbers and there is no
-- way to tell which ones a person decided and which ones the tool guessed. The
-- run id is stamped into the workbook's custom document properties (outside the
-- cell grid, so it disturbs nothing) and points back at this row.
alter table pricing_runs
  add column if not exists cells jsonb;
