-- 05 — Resolver view
-- REF -> description/size, preferring a learned correction over the log.

create or replace view part_resolved as
select
  coalesce(l.part_no, r.part_no)               as part_no,
  coalesce(l.description, r.description)        as description,
  coalesce(l.size, r.size)                      as size,
  (l.part_no is not null)                       as from_correction
from reference_parts r
full outer join learning_part_desc l on upper(l.part_no) = upper(r.part_no);
