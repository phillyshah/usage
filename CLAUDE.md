# Usage project — Claude notes

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
- **Anthropic account must run under a HIPAA BAA**
- **Learning tables** (`learning_price`, `learning_part_desc`, `learning_rep_map`, `learning_gtin_xref`, `corrections_audit`, `corrected_uploads`) — flag explicitly before any work that could risk these

## Branch
Active dev branch: `claude/clever-cerf-oaqc5g`
