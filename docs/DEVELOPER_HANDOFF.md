# Developer Handoff — Distributor Label Extraction Pipeline

**For:** the coding agent(s) building this
**Read order:**
1. `LABEL_EXTRACTION_BUILD_SPEC.md` — *what* we're building and *why* (requirements, rules, confidence model, learning loop). Authoritative on behavior.
2. `supabase_schema.sql` — the database. Run it as-is against the managed Supabase project. Authoritative on data shape; **match these table/column names exactly.**
3. This file — *how* to build it: structure, contracts, deploy, task list.

**Still genuinely open (don't block Phase 1; assume the noted default):**
- Intake source (Smartsheet/email/folder) → **default: HTTP upload endpoint.** Any future adapter just POSTs to it.
- One ticket per image → **assumed yes.**
- Mfg date required in output? → captured if present, leave in.
- Patient-sticker anchor positions per template → needed for redaction; see Phase 1 task R.

---

## 1. Resolved decisions
- **Intake = HTTP multipart upload.** `POST /images`. Smartsheet/email integrations, if added, push to the same endpoint.
- **Raw images are never persisted.** The upload handler holds raw bytes in memory, runs redaction first, writes only the redacted image to Storage, discards the raw. PHI never lands on disk.
- **Account = hospital** (price-memory key).
- **Datastore = managed Supabase Cloud.** Backend uses the service-role key (bypasses RLS).
- **Deploy = Docker container behind Traefik on the VPS.**

---

## 2. Repo structure
The GitHub repo already exists, named **`usage`** — build into it. The tree below is the target layout inside that repo.
```
usage/
├── app/
│   ├── main.py                 # FastAPI app + routes
│   ├── config.py               # env-backed settings (pydantic-settings)
│   ├── db.py                   # Supabase client + query helpers
│   ├── storage.py              # bucket upload/download/delete
│   ├── jobs.py                 # APScheduler: daily batch run + (optional) purge
│   ├── pipeline/
│   │   ├── preprocess.py       # deskew, denoise, contrast (opencv)
│   │   ├── template.py         # detect Maxx Ortho vs Maxx Health; locate regions
│   │   ├── redact.py           # locate + mask patient sticker; manual-queue fallback
│   │   ├── barcode.py          # decode DataMatrix/linear; parse GS1 (biip)
│   │   ├── reference.py        # REF/lot lookups (part_resolved view + reference_lots)
│   │   ├── vision.py           # Claude API call + prompt + JSON parse
│   │   ├── confidence.py       # scoring rules + business validators
│   │   ├── assemble.py         # build Ticket/LineItem rows; persist + field_extractions
│   │   └── run.py              # orchestrate one image; batch runner
│   ├── sheets/
│   │   ├── write.py            # openpyxl workbook w/ color fills + legend
│   │   └── read.py             # parse corrected workbook back to rows
│   └── learning/
│       ├── harvest.py          # corrected rows -> learning stores
│       ├── diff.py             # corrected vs field_extractions -> corrections_audit
│       └── ingest_log.py       # Expiry Log -> full replace of reference tables
├── tests/
│   └── fixtures/               # 4 sample ticket JPEGs + Expiry_Log.xlsx
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 3. Dependencies (`requirements.txt`)
```
fastapi
uvicorn[standard]
python-multipart
pydantic-settings
python-dotenv
supabase
anthropic
openpyxl
pillow
numpy
opencv-python-headless
pylibdmtx
pyzbar
biip
apscheduler
```
System libs (in the image, see Dockerfile): `libdmtx0`, `libzbar0`, `libgl1`, `libglib2.0-0`.

---

## 4. Config / env (`.env.example`)
```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-5
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service-role-key>     # server-side only, never ship to a client
RETENTION_DAYS=14                            # mirrors app_settings; informational
SUM_TOLERANCE=0.01                           # grand-total reconciliation tolerance
VISION_CONF_THRESHOLD=medium                 # min model confidence to write a value
```

---

## 5. API contract
All routes server-side; Traefik terminates TLS in front.

| Method | Path | Body | Returns |
|--------|------|------|---------|
| `GET`  | `/health` | — | `200 {"status":"ok"}` (Traefik healthcheck) |
| `POST` | `/images` | multipart, 1..N image files | `202 {batch_id, tickets:[{ticket_id, status}]}` — redacts, stores redacted, creates ticket rows as `pending_review` (or `manual_queue` if redaction can't locate patient region) |
| `POST` | `/batches/run` | optional `{batch_id}` | `200 {batch_id, sheet_path, ticket_count}` — processes pending tickets, writes the colored workbook to `output-sheets`. Also invoked on schedule. |
| `GET`  | `/batches` | — | `200 [{batch_id, run_date, ticket_count, status}]` |
| `GET`  | `/batches/{id}/sheet` | — | `200` file stream of the workbook |
| `POST` | `/corrections/upload` | multipart, 1..N corrected `.xlsx` | `200 {processed, tickets_matched, tickets_unknown}` — harvest + diff per §7 of spec |
| `POST` | `/reference/log` | multipart, one `.xlsx` | `200 {row_count, unique_parts, unique_lots}` — full-replace reference tables |
| `GET`  | `/metrics/auto-resolve` | `?weeks=N` | `200 [{week, pct_confident}]` (Phase 3) |

Notes:
- `/images` and `/corrections/upload` must accept **multiple files** in one request (batched re-upload is a hard requirement).
- Matching corrected sheets is by `ticket_id` read from the sheet — order/timing irrelevant.

---

## 6. Module responsibilities (key signatures)

**`pipeline/redact.py`** — the PHI gate. Runs before anything else touches the image.
```python
def redact_patient_region(img: "np.ndarray", template: str) -> tuple["np.ndarray", bool]:
    """Mask the patient sticker for the detected template.
    Returns (redacted_image, located). If located is False, caller must
    route the ticket to manual_queue and NOT send the image anywhere."""
```

**`pipeline/barcode.py`**
```python
def decode_labels(label_crops: list["np.ndarray"]) -> list[dict]:
    """Per label: decode DataMatrix/linear, parse GS1 via biip.
    -> {gtin, lot, expiry, mfg, serial, raw, decoded: bool}"""
```

**`pipeline/reference.py`**
```python
def resolve_part(ref: str | None, gtin: str | None, lot: str | None) -> dict:
    """Look up description/size via part_resolved (learning overrides log);
    recover REF from lot or GTIN->REF crosswalk; validate against reference_lots.
    -> {ref, description, size, expiry_ref, in_log: bool, source}"""
```

**`pipeline/vision.py`**
```python
def extract_handwritten(redacted_img_bytes: bytes) -> dict:
    """Single Claude call. Header + prices + qty + totals. JSON-only output,
    per-field {value, confidence}, null when unreadable. See prompt in §7."""
```

**`pipeline/confidence.py`**
```python
def score_field(sources: dict) -> str:        # -> "high" | "medium" | "low"
def validate_ticket(ticket: dict, lines: list[dict]) -> list[str]:
    """Business rules: REF in log, lot/expiry agreement, date sanity,
    sum(line_total) == grand_total within SUM_TOLERANCE. Returns flag list."""
```

**`sheets/write.py`** — fills per cell: confident=no fill, medium=`FFF2CC`, low/blank=`F4CCCC`. Three sheets: Tickets, Line Items, Legend. Ticket/Line IDs uncolored.

**`learning/harvest.py`** — for each corrected row, upsert into `learning_part_desc` (REF→desc/size), `learning_rep_map` (code→rep), `learning_price` (REF+hospital→price), `learning_gtin_xref` (gtin→ref), `learning_surgeon_map` (surgeon+distcode→surgeon/hospital). Self-contained; runs regardless of retention. All of these are read back during extraction (`resolve_part`, `resolve_surgeon`, the price fill in `assemble.py`), so corrections directly improve re-runs.

**`learning/diff.py`** — if `field_extractions` still present for the ticket, compare and write `corrections_audit` (set `was_blank`/`was_low_conf`). Skip silently if purged.

---

## 7. Claude vision prompt (use verbatim as the system prompt)
```
You extract fields from a redacted orthopedic implant usage ticket (Maxx
Orthopedics or Maxx Health). The patient area has been masked; ignore any
masked region and never infer patient information.

Return ONLY a JSON object, no prose and no markdown fences. For every field
return {"value": <value or null>, "confidence": "high"|"medium"|"low"}.
Use null when you cannot read a field — do NOT guess. Confidence reflects how
clearly legible the source is.

Shape:
{
  "header": {
    "entity": {...}, "rep": {...}, "rep_code": {...}, "surgeon": {...},
    "hospital": {...}, "surgery_date": {...}, "po_number": {...}
  },
  "lines": [ {"index": <int>, "qty": {...}, "unit_price": {...}} ],
  "freight": {...},
  "grand_total": {...}
}

Dates as ISO YYYY-MM-DD. Money as numbers without symbols. "lines" is ordered
top-to-bottom; skip empty slots that read "Place Implant Label".
```
Parse defensively: strip any fences, `json.loads`, drop values below `VISION_CONF_THRESHOLD`. Model confidence is an input to `confidence.py`, not the final cell color.

---

## 8. Deployment

**`Dockerfile`**
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libdmtx0 libzbar0 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**`docker-compose.yml`** (assumes an existing external Traefik network named `traefik`)
```yaml
services:
  labels-api:
    build: .
    env_file: .env
    networks: [traefik]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.labels.rule=Host(`usage.90ten.life`)"
      - "traefik.http.routers.labels.entrypoints=websecure"
      - "traefik.http.routers.labels.tls.certresolver=letsencrypt"
      - "traefik.http.services.labels.loadbalancer.server.port=8000"
    restart: unless-stopped
networks:
  traefik:
    external: true
```
Confirm the certresolver name (`letsencrypt` above) matches the existing Traefik config, and point `usage.90ten.life` at the VPS in DNS. Secrets come from `.env`; never bake them into the image.

---

## 9. Build phases & acceptance criteria

### Phase 1 — Extraction MVP (no learning)
- **R. Redaction:** detect template, locate + mask patient sticker. *Done when:* all 4 fixtures redact correctly; an image where the region can't be found is marked `manual_queue` and no image is sent to the API.
- **B. Barcode + reference:** decode labels, resolve REF→desc/size, recover REF from lot. *Done when:* known fixture REFs (e.g. RAUUX400-RK, MO-MLHH-MF/36, the bone screws) resolve from the log; unknown REFs flag.
- **V. Vision fallback:** header + prices + qty + totals via the §7 prompt. *Done when:* valid JSON parsed, nulls handled, sub-threshold values dropped.
- **C. Confidence + validators:** three-state scoring + the sum-to-total check. *Done when:* a deliberately mismatched total flags the price cells.
- **S. Sheet:** colored 3-sheet workbook to `output-sheets`. *Done when:* confident cells uncolored, guesses amber, blanks red, IDs present and uncolored.
- *Phase exit:* run the 4 fixtures end-to-end; spot-check the workbook by eye.

### Phase 2 — Learning loop
- **P. Persistence:** write `tickets`, `line_items`, `field_extractions` with per-field confidence; trigger sets `expires_at`.
- **U. Re-upload:** `/corrections/upload` accepts N files, matches by ticket_id. *Done when:* out-of-order, batched, and post-expiry uploads all process (harvest always; diff only when snapshot present).
- **H. Harvest + diff:** learning stores upsert from corrected rows; `corrections_audit` records changes. *Done when:* a corrected price reappears as a suggestion on the next ticket for that REF+hospital; a corrected REF→desc overrides the log via `part_resolved`.
- **X. Purge:** pg_cron or APScheduler purges expired `field_extractions`; worker deletes expired redacted images from Storage. *Done when:* nothing past the window remains, learned facts persist.

### Phase 3 — Polish
- Web upload/download UI, `/metrics/auto-resolve` dashboard, GTIN→REF crosswalk maturity, duplicate detection.

---

## 10. Test fixtures
`tests/fixtures/` holds the 4 sample ticket photos and `Expiry_Log.xlsx`. Expected behavior:
- The Expiry Log loads to ~1,682 parts / ~51k lots.
- Blank slots ("Place Implant Label" on the knee-system fixture) produce no line.
- A mix of REFs resolve from the log and some don't — the misses should land as amber/flagged, never silent.
- Patient stickers must be masked in every stored image.
