# Go-live runbook

Ordered steps to take this from the repo to `https://usage.90ten.life`. Each step
is marked **[you]** (needs your accounts/secrets/DNS/VPS access) or **[code]**
(already handled in this repo).

---

## 1. Supabase — database + storage

1. **[you]** Create a managed project at supabase.com. Note the **Project URL** and
   the **service-role key** (Settings → API). The service-role key bypasses RLS and
   is **server-side only** — never ship it to a browser.
2. **[you]** Open **SQL Editor**, paste `supabase_schema.sql`, run it top to bottom.
   This creates all 14 tables, the `part_resolved` view, the expiry trigger, and the
   nightly `purge_expired_extractions` pg_cron job.
   - If pg_cron isn't on: **Database → Extensions → enable `pg_cron`**, then re-run the
     final `cron.schedule(...)` line.
3. **[code]** Create the four **private** Storage buckets (the SQL deliberately leaves
   this to the API). With `.env` filled in:
   ```bash
   make buckets        # python scripts/bootstrap_supabase.py
   ```
   Idempotent; it also verifies every schema table is reachable. Creates:
   `redacted-images`, `output-sheets`, `corrected-uploads`, `reference-logs`.

> The tables are the *schema*; the buckets are *object storage*. You need both.
> Reference tables (`reference_lots`, `reference_parts`) start empty — they fill the
> first time you upload the Expiry Log through the UI (full-replace each upload).

## 2. Anthropic — vision + BAA

1. **[you]** Get an API key. Set `ANTHROPIC_MODEL=claude-sonnet-4-6`.
2. **[you] (compliance)** Request a **HIPAA BAA** from Anthropic before processing real
   tickets. Redaction removes PHI at ingest, but the BAA is the defense-in-depth net
   the spec (§9) requires in case redaction ever misses.

## 3. Secrets — `.env` on the VPS

**[you]** On the VPS, copy `.env.example` to `.env` and fill in:
```
OFFLINE_MODE=false
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service-role-key>
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6
RETENTION_DAYS=14
```
`.env` is gitignored — keep it off GitHub. Secrets reach the container via `env_file`.

## 4. DNS + TLS

1. **[you]** In the DNS zone for `90ten.life`, add an **A record**:
   `usage` → the VPS public IP (AAAA too if the VPS has IPv6). TTL ~300 while testing.
2. **[code]** TLS is automatic: `docker-compose.yml` carries the Traefik labels that
   request a Let's Encrypt cert for `usage.90ten.life` on the `websecure` entrypoint.
3. **[you] (confirm)** Two things must match your existing Traefik install:
   - the external Docker network is named **`traefik`** (edit the compose `networks:`
     block if yours differs), and
   - the certresolver is named **`letsencrypt`** (the label `...tls.certresolver=letsencrypt`).
     If your Traefik static config names it e.g. `myresolver`, change the label.
4. Verify DNS before deploying:
   ```bash
   dig +short usage.90ten.life      # should return the VPS IP
   ```

## 5. Deploy

**[you]** On the VPS, from the repo root:
```bash
make deploy        # docker compose up -d --build
make logs          # watch it come up
```
The image installs the native barcode/OpenCV libs (`libdmtx0`, `libzbar0`, `libgl1`,
`libglib2.0-0`). First boot also fetches the TLS cert (can take ~30s).

## 6. Verify

```bash
make smoke URL=https://usage.90ten.life
```
Checks `/health`, that the UI is served, and that the TLS handshake succeeds. Then in a
browser open `https://usage.90ten.life` and:
1. **Update the reference log** with `Expiry_Log.xlsx` → confirm it reports ~1,682
   parts / ~51k lots.
2. Upload a couple of real ticket photos → **Extract data** → download the workbook and
   eyeball the colors.
3. Fix a flagged cell, save, **Send corrections** → confirm "1 matched".

---

## Operational notes

- **Daily run / purge** are scheduled in-process (APScheduler: batch 02:00 UTC, purge
  03:00 UTC) *and* in pg_cron (the SQL purge). The app's purge worker also deletes the
  expired redacted **image objects** from Storage (SQL can't). Both are safe to keep.
- **Retention** is `RETENTION_DAYS` (default 14) and is also stored in `app_settings`.
  Bump it if corrected sheets come back later than ~2 weeks:
  `update app_settings set value='21' where key='retention_days';` (affects new tickets).
- **Backups:** Supabase manages Postgres backups; the only on-disk PHI-adjacent data is
  the redacted images, which age out on the retention schedule.
- **Scaling:** single container is fine at ~100 tickets/day. If volume grows, add a
  Celery/Redis worker before scaling the web tier — not needed now.

## What still needs you (summary)

| Item | Why it can't be automated here |
|------|--------------------------------|
| Supabase project + service-role key | your account / secret |
| Run `supabase_schema.sql` | one paste in your SQL editor (or share a connection string) |
| Anthropic key + HIPAA BAA | your account / legal |
| DNS A record for `usage.90ten.life` | your DNS provider |
| VPS access + `docker compose up` | your server |
| Confirm Traefik network + certresolver names | depends on your existing Traefik config |
