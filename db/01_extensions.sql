-- 01 — Extensions + settings
-- Run first. pgcrypto gives gen_random_uuid(); pg_cron runs the nightly purge.
-- (Enable pg_cron in Database > Extensions first if this errors.)

create extension if not exists pgcrypto;
create extension if not exists pg_cron;

create table if not exists app_settings (
  key   text primary key,
  value text not null
);
insert into app_settings (key, value) values ('retention_days', '14')
  on conflict (key) do nothing;
