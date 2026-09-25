# Usage project — Claude notes

**Read `docs/WORK_LOG.md` first** — current production state, decisions not worth
re-litigating, and open threads.

## VPS deploy
- **Host:** `root@srv1373951` / IP `72.62.174.193`
- **Repo path:** `/root/usage`
- **Deploy command:** `cd ~/usage && git pull origin main && make deploy`
- **Live URL:** https://usage.90ten.life

## Stack
- FastAPI + vanilla JS SPA
- Supabase (prod) / local JSON files (OFFLINE_MODE dev/test)
- Docker Compose + Traefik + Let's Encrypt
- Python 3.11 in `python:3.11-slim-bookworm`
- Container name: `usage-labels-api-1`, image: `usage-labels-api:latest`

## Critical standing rules
- **Never commit real ticket photos** — .gitignore blocks *.jpeg, MH*.jpg, MO*.jpg, tests/fixtures/real/
- **PHI gate:** patient region masked before any storage; failure routes to manual queue
- **One deliberate exception:** `EXTRACT_PATIENT_INITIALS` (currently **on** in prod)
  reads the patient's two initials from the pre-mask image at ingest —
  `app/pipeline/patient.py` only. It sends just the sticker crop, keeps only two
  letters, and the stored image is still fully redacted. Keep it that narrow; see
  `docs/WORK_LOG.md` before changing anything in that path.
- **Anthropic account must run under a HIPAA BAA**
- **Learning tables** (`learning_price`, `learning_part_desc`, `learning_rep_map`, `learning_gtin_xref`, `learning_surgeon_map`, `corrections_audit`, `corrected_uploads`) — flag explicitly before any work that could risk these

## Branch
Active dev branch: `claude/clever-cerf-oaqc5g`

## How Andy wants things reported
- **Always give the link. Don't wait to be asked.** Every time one of these comes
  up, the URL goes in the message:
  - a PR — the full `https://github.com/phillyshah/usage/pull/N`, repeated in
    later messages that refer to it, not just the one that created it
  - a SQL migration to run — link the file on GitHub (`.../blob/main/db/NN_*.sql`)
    and the raw URL, since it gets pasted into the Supabase SQL Editor
  - anything deployed or viewable — the live URL
- A reference like "PR #41" or "db/11" on its own is not enough. Assume he is on
  a phone or in another window and cannot look it up.
