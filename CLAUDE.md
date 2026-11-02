# Usage project — Claude notes

**Read `docs/WORK_LOG.md` first** — current production state, decisions not worth
re-litigating, and open threads.

## VPS deploy
- **Host:** `root@srv1373951` / IP `72.62.174.193`
- **Repo path:** `/root/usage`
- **Deploy command:** `cd ~/usage && git pull origin main && make deploy`
- **Live URL:** https://usage.90ten.life
- **`.env` beats the code.** `/root/usage/.env` pins `ANTHROPIC_MODEL`, so
  changing the default in `app/config.py` moves nothing on the VPS. A model
  change is a two-step deploy: edit `.env`, then pull and build.
- **After any deploy that touches the vision call:** hit `/health/vision` (or
  the "Test AI connection" button) before running a batch. A green test suite
  says nothing about whether the API will accept the request — see the 2.20.0
  entry in `docs/WORK_LOG.md`.

## Memory (2.29.0 — read before touching PDF rendering)
- The VPS is **7.9 GB with no swap**. On 2026-11-02 the host OOM killer took
  uvicorn out five times in twenty minutes at ~5.3 GB each, and every request in
  flight came back as **"Bad Gateway"** — including uploads that had nothing to
  do with the cause. An OOM kill leaves no traceback, so "the logs look fine" is
  the signature, not a counter-argument. Check `dmesg -T | grep -i "out of
  memory"` and `docker inspect usage-labels-api-1 --format '{{.State.ExitCode}}
  {{.State.OOMKilled}} {{.RestartCount}}'`.
- **`pdf.DEFAULT_DPI` is not the thing that controls memory.** `scale = dpi/72`
  multiplies the page size the PDF *declares*, which scanner apps set as high as
  23x30 inches. `MAX_RENDER_PIXELS` (30 MP) is the real guard; it leaves a
  letter/A4/legal page at the full 400 DPI that 2.24.0 measured as necessary for
  barcode decode. **Don't "fix" a memory problem by lowering the DPI** — that
  silently costs barcodes and doesn't address a declared-size blowup anyway.
- Render pages with `pdf.iter_page_images()` (streaming) or `pdf.render_page()`
  (one page). `pdf_to_page_images()` holds the whole document and exists for the
  tests. `_RENDER_LOCK` keeps it to one render at a time because `/images`
  ingests four uploads in parallel.
- **The cap applies where an image is READ as well as written (2.29.1).**
  Capping only the write side left every already-stored oversized ticket
  killing the box on each batch run — a batch the kernel kills leaves its
  tickets pending, so the next run repeats it. `ingest_image` and
  `process_ticket` both pass `max_px=preprocess.MAX_IMAGE_PIXELS`.
- **`cap_pixels(decode_image(x))` is NOT the fix** — it allocates the full
  array first, and the allocation is the cost (measured: a 6% saving). Pass
  `max_px` to `decode_image` so libjpeg scales during decoding
  (`IMREAD_REDUCED_COLOR_N`): 2143 MB -> 608 MB for three 115 MP tickets.
- `docker-compose.yml` sets `mem_limit: 3g`, so the container dies instead of
  the box and it shows as `OOMKilled=true`. Measured headroom: a ten-page
  400 DPI PDF peaks ~390 MB; four at once ~994 MB.

## Stack
- FastAPI + vanilla JS SPA
- Supabase (prod) / local JSON files (OFFLINE_MODE dev/test)
- Docker Compose + Traefik + Let's Encrypt
- Python 3.11 in `python:3.11-slim-bookworm`
- Container name: `usage-labels-api-1`, image: `usage-labels-api:latest`

## Critical standing rules
- **Never commit real ticket photos or scans** — .gitignore blocks *.jpeg, *.pdf,
  MH*.jpg, MO*.jpg, tests/fixtures/real/. This is now the *only* place the
  "don't let PHI escape" rule is enforced by code, so don't weaken it.
- **There is no patient mask.** Removed in 2.19.0 by Andy's decision: storage is
  HIPAA-compliant, and the mask was costing accuracy it wasn't buying back — it
  clipped Surgery Date and Surgeon out of the header on differently-framed
  photos, and its `located=False` path silently produced tickets with zero rows.
  The ticket image is sent and stored whole, patient sticker included.
- **The ticket image is sent unmasked, and one provider has no BAA.** The whole
  image — patient name, DOB, MRN, CSN — goes to whichever reader
  `VISION_PROVIDER` selects.
  - `anthropic`: the BAA is load-bearing and covers the main extraction call,
    not just a crop. Since 2.25.0 it is also the **escalation** reader, so a
    ticket with an unfilled cell goes to Claude even when the primary is
    OpenRouter. Since 2.28.0 that is a **ladder** — `VISION_ESCALATE_MODELS`,
    Sonnet then Opus, each rung only if the one before left a cell red — so an
    escalated ticket may be sent to Anthropic twice.
  - `openrouter`: **no BAA exists.** OpenRouter does not publish one, and it is
    a router, so the request is handed to an upstream provider as well. Andy
    was shown this and approved it on 2026-09-30 to cut cost (~$78/mo → ~$7/mo).
    Recorded so it reads as a decision somebody made rather than a default
    nobody saw. Two consequences: `OPENROUTER_PRIVACY` sends
    `data_collection: deny` on every request, and OpenRouter's **own** prompt
    logging is an account setting (Settings → Privacy) that no code here can
    touch — it has to be turned off in their console.
  - Re-masking is the standing alternative if that posture ever changes; the
    old geometry is in git history at `app/pipeline/redact.py` (removed 2.19.0).
- **Model names are family names, not versions (2.28.0).** `ANTHROPIC_MODEL`
  and the `VISION_ESCALATE_MODELS` rungs may say `sonnet` / `opus` / `haiku`,
  and `app/pipeline/modelnames.py` asks the Models API which model in that
  family is current (24h cache, `image_input` required, pinned fallback on any
  failure — a name lookup must never stop a batch). An exact id is still
  honoured as a deliberate pin, which is how the OpenRouter `vendor/model` ids
  pass through. **OpenRouter is deliberately not resolved this way:** no
  version ordering, and image support varies, so "the newest qwen" could be a
  model that cannot see a ticket.
  - The VPS `.env` currently pins `ANTHROPIC_MODEL=claude-sonnet-5-5`. That
    still wins over the code, so it stays on that exact model until the line is
    changed to `ANTHROPIC_MODEL=sonnet`.
- **`EXTRACT_PATIENT_INITIALS`** (currently **on** in prod) decides whether the
  patient's two initials are **kept**, not whether they are read — the model
  sees the sticker either way. Off means `assemble.py` discards them and nothing
  about the patient reaches the database or the workbook. `_clean_initials`
  rejects anything that isn't exactly two letters rather than truncating it.
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
