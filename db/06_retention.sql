-- 06 — Retention: expiry trigger + purge function + nightly schedule
-- Requires pg_cron (script 01). If the last line errors with
-- "schema cron does not exist", enable pg_cron then re-run just that line.

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

create or replace function purge_expired_extractions() returns void as $$
begin
  delete from field_extractions fe
  using tickets t
  where fe.ticket_id = t.ticket_id
    and t.expires_at < now();
end;
$$ language plpgsql;

-- Nightly at 03:00 UTC.
select cron.schedule('purge-expired-extractions', '0 3 * * *',
                     $$ select purge_expired_extractions(); $$);
