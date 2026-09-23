# Work Log

Running record of non-obvious decisions, current production state, and open
threads. Git history has the *what*; this file has the *why*, the things that
aren't reconstructable from a diff, and what's still unfinished.

The user-facing release notes live in `app/version.py` (`CHANGELOG`), which is
what the app's "What's New" panel reads. Root `CHANGELOG.md` is the formal
Keep-a-Changelog record and stopped being maintained after 2.0.0.

Last updated: 2026-09-23.

---

## Current state

| | |
|---|---|
| Version in `main` | **2.12.2** (PR #38 merged); **2.13.0** on `claude/clever-cerf-oaqc5g`, not yet merged |
| Deployed | **2.12.2** — confirmed live 2026-09-23 |
| `EXTRACT_PATIENT_INITIALS` | **true** in the VPS `.env` (verified via `docker compose exec labels-api printenv`) |
| Model | `claude-sonnet-5` (`ANTHROPIC_MODEL`, default in `app/config.py`) |
| Effort | `medium` extraction, `low` initials |
| Schema | current through `db/10`. **`db/11_hospital_prices.sql` is NOT applied yet** — it must run before 2.13.0 deploys |
| Tests | 252 passed, 2 skipped (219 + 33 for step 5) |
| Learning stores | 2,410 facts, intact through the 2.12.0 migration |

PRs this cycle, all merged: #33 (2.9.0), #34 (2.10.0), #35 (2.11.0),
#36 (2.11.1 + 2.12.0 — carried both), #37 (2.12.1), #38 (2.12.2).

---

## Open threads

**1. `detect_template()` still guesses, and the redaction gate trusts the guess.**
With no filename evidence, it returns `Maxx Orthopedics` as a *guess*
(`app/pipeline/template.py`), and `redact_patient_region` treats that as fact.
The spec says an unknown template must route to manual queue; that path is
currently bypassed. v2.12.2 fixed the *observed* case (MH/MO prefixes are now
recognised) so this should rarely fire, but an oddly-named upload still gets
masked on a coin flip. The honest fix routes those to manual queue, which costs
review volume — deliberately left as a decision, not made unilaterally.

**2. Already-stored Maxx Health images were never re-masked.**
Anything ingested *before* the 2.12.2 deploy (2026-09-23) under an `MH*` filename
has a *visible* patient sticker in the `redacted-images` bucket (see the 2.12.2
entry below). MH tickets ingested from that deploy onward mask correctly. Bounded
by the 14-day retention, so the last affected image ages out around **2026-10-07**;
re-uploading those tickets re-masks them sooner. No decision recorded either way.

**3. The price list covers about a quarter of the hospitals.**
Measured before building step 5: of the 473 hospitals in `reference/surgeon_info.csv`,
64 (14%) match a price-list hospital exactly after normalization, 58 (12%) match
fuzzily, 7 are ambiguous, and **344 (73%) don't match at all**. That may be
entirely expected — the list may only cover the accounts that use construct
pricing — but it means most rows on a first real run will be estimates or
unresolved, and that is not a bug. Sanity-check it against real ticket volume,
not against the full master. The run summary's `unresolved_causes` breakdown
exists precisely so this is legible rather than looking broken.

**4. Confidence baseline after the effort drop.
Extraction moved from effort `high` (the unset default) to `medium` in 2.12.1.
The regression signal is `pct_confident` in History → "Getting better over time",
which is a per-day series — so the pre-2026-09-20 days *are* the baseline and can
be read retroactively. If it sags over a week of real tickets, put `vision.py`
back to `high`; it's a one-line change.

**5. Verify `Inits` on a real Maxx Health ticket.** — *not yet done*
Before 2.12.2 this came back blank on every MH ticket (wrong region cropped).
2.12.2 is live, so it's unblocked: one Debug Console run against an MH ticket
confirms both halves at once — the right region masked in the stored image, and
the initials populated in column D.

---

## Decisions worth not re-litigating

### A fuzzy hospital match never produces a green price

The work instructions treat a single ≥75% fuzzy candidate as a match, full stop.
It is a match — but not a *certain* one: on the real data it produces
`Arroyo Grande Surgical Institute` → `River Surgical Institute`. Green tells the
reviewer "this came straight from the list, don't check it", which is exactly the
wrong thing to say about a guess. So green requires an **exact or aliased**
hospital (`HospitalMatch.confident`); a fuzzy hospital still prices the row, as a
rose estimate. Fuzzy still earns its keep — it correctly catches
`AdventHealth Carrolwood` → `AdventHealth Carrollwood`.

### `***` is not three characters

The obvious reading of `MTUUX***-GK` is "three of anything", and for that family
it is. But the same catalogue writes `UFCR***-GK` for `UFCRLA00-GK` and
`ACLMR***-UK` for `ACLMRL100-UK` — four-character fills. A `{3}` quantifier
silently drops both families with no error anywhere. The wildcard is a `+` run,
and intra-tier price agreement is what keeps it honest, not the quantifier.
Lowercase `xxx` is a wildcard and uppercase `XX` is not, because `DAXX00D-F` is a
real part number; no part number in the master contains an uppercase `XXX`.

### Component tiers short-circuit; they are never unioned

The bare code `MTUUX` is a prefix of `MTUUX100-GK`, which the pattern
`MTUUX***-GK` also matches — and at Lancaster those two catalogue rows hold
different prices. Unioning the tiers reads that as "conflicting prices, refuse to
price the row" and loses a price that was never ambiguous. Tiers are evaluated in
order and the first that yields anything wins, so the more specific rule takes it.

### Wasted (yellow) cells are skipped, not filled

`write.py` paints a wasted line's Price cell yellow *whether or not it has a
price*, so a wasted line with a low-confidence price is blank **and** yellow —
which collides head-on with "fill genuinely blank cells". What a wasted component
is worth is a business decision, not a lookup, so those cells are excluded from
eligibility entirely and counted as `skipped_wasted`.

### The distributor is resolved per row, not per run

`entity` is a per-ticket field and one workbook can hold both `MH17469.jpg` and
`MO083596.jpg`, so "this run uses the Maxx Health tab" is not a thing that can be
true of a whole file. Each row resolves its own tab, and the observations that
feed the estimate ladder are partitioned by tab — otherwise "same part type
elsewhere in this file" quietly imports an Orthopedics price into a Health row.

### Entity comes from the Tickets sheet, then the filename, then fails

`assemble` overwrites the template-detected entity with the vision read, and
`write.py` blanks any low-confidence header field — so `Tickets!Entity` is empty
on a large fraction of real tickets. The `MH`/`MO` filename prefix is a required
fallback, not a nicety, and it reuses `detect_template` so the two rules can't
drift apart. An unresolvable distributor fails the run rather than guessing from
another distributor's prices.

### Step 5 is read-only with respect to every learning table

It reads `reference_part_info` and `reference_surgeons`, writes
`reference_hospital_prices` and `pricing_runs`, and touches nothing else. In
particular `learning_price` is never written — an estimate is not a fact learned
from a human correction, and letting it into the learning store would launder a
guess into evidence.

### Patient initials are a deliberately narrow exception to "no patient data"

`LABEL_EXTRACTION_BUILD_SPEC` §9 is "we do not process patient data at all", and
`app/pipeline/redact.py` enforces it. The `Inits` column required breaking that,
and the user chose vision extraction after being shown the alternatives. It was
scoped as tightly as the requirement allows rather than by relaxing the gate:

- `app/pipeline/patient.py` is the **single** place patient data is read.
- It runs **after** the redaction gate confirms the patient region was located —
  so a ticket that fails the gate still sends its image nowhere.
- It sends **only the sticker crop**, never the full ticket.
- It asks for **only two letters**. A response carrying anything else (a full
  name, three letters, a number) is discarded, not stored — `_clean()` is the
  safety net, not the prompt.
- The image that gets **stored** is still the fully redacted one. `redact.py` is
  untouched.
- Flag-gated (`EXTRACT_PATIENT_INITIALS`), default off. With the flag off the
  app's behaviour is byte-identical to before.
- **The learning tables and `corrections_audit` never see it.** `harvest_ticket`
  and `diff_ticket` both work off explicit field lists that exclude
  `patient_initials`. Keep it that way — that's why it isn't in `diff_ticket`'s
  list despite being in `TICKET_FIELDS`.

Turning the flag on is the moment PHI starts flowing to the API; it's a separate,
deliberate step from deploying the code, and it assumes a HIPAA BAA on the
Anthropic account.

### Initials are read at ingest, not at batch time

They have to be. `assemble`/`process_ticket` only ever see the *redacted* image
pulled from storage — the raw bytes are gone by then. So extraction happens in
`ingest_image` while the pre-mask image is still in memory, is stored on the
ticket row, and `assemble` reads it back to emit the confidence row. That's why
`ticket_patch` deliberately omits `patient_initials`: `update_ticket` merges, so
leaving it out means reprocessing keeps the value instead of blanking it.

### Effort was never chosen, it was defaulted

Neither call set `output_config.effort`, so both ran at Sonnet 5's default of
`high`. 2.12.1 pinned them (`medium` extraction, `low` initials) and asserted both
in tests so they can't drift back silently. Effort is the main per-ticket cost
lever left — the image tokens are the larger share and can't be reduced.

### Prompt caching only helps so much

The static system prompt is cached (2.11.1), but the ticket image is unique per
call and can't be. Caching trims the prompt portion, not the dominant cost.
Whether it's actually landing is visible in the `vision_ai` trace step, which
surfaces `cache_read_input_tokens`.

---

## What shipped

### 2.13.0 — Step 5: surgery price enrichment
Blank Price cells are blank because the ticket quoted a *construct* total instead
of a price per component; the accountant has been typing them in by hand against
a hospital price list. Step 5 does that lookup: upload the generated workbook,
get the same workbook back with those cells filled — neon green `39FF14` straight
from the price list, rose `FFC7CE` for an inferred estimate, still red where there
was no evidence. New `app/pricing/` package (`tabs`, `normalize`, `match`,
`estimate`, `ingest`, `enrich`), a fifth Reference Data tile, and
`db/11_hospital_prices.sql`. See "Decisions" above for the five places this
deliberately departs from the written work instructions.

The run validates itself before publishing: the workbook is snapshotted before
and after and the diff must equal exactly the set of cells the run planned to
change, every one of which must have been blank, numeric, and correctly coloured.
Anything else fails the run — and because each run writes a new `run_id` object
and `/pricing/latest` only reads `status='succeeded'`, a failed run leaves the
previous output downloadable for free.

### 2.12.2 — Maxx Health template detection (PHI fix)
`detect_template()` matched only the literal words `health`/`ortho` anywhere in
the filename. The real convention is an entity prefix plus ticket number
(`MH17469.jpg`, `MO083596.jpg`), which matched neither — so **every Maxx Health
ticket was read as Maxx Orthopedics**. The layouts are mirror images (Health's
patient sticker at x=0.04, Ortho's at x=0.55), so an MH ticket had the empty
right-hand side masked while the sticker stayed in full view. The gate missed it
because `located` is `np.any(redacted != img)` — "did any pixel change", not "did
we cover the right region". Present since v1. Now matches `^m[ho]\d` on the
basename (anchored + digit-required so `monday-scans.jpg` can't match, basename'd
so `/home/mona/` can't either); PDF pages keep the prefix (`MH17469-p2`).

### 2.12.1 — Effort levels pinned
See "Decisions" above.

### 2.12.0 — Patient `Inits` column
New column D on Usage (contract A–N, Notes moved to O) and H on Tickets; parser
maps the header back by name. Requires `db/10_patient_initials.sql`. See
"Decisions" above for the architecture.

### 2.11.1 — Prompt caching on the vision call
`cache_control` ephemeral on the static system prompt; cache token counts
surfaced in the trace.

### 2.11.0 — History lists grouped by month
Batches, correction uploads and daily learning impact collapse into per-month
`<details>` sections, newest expanded. Also raised the data windows that feed
them — `/metrics/learning` days 14 → 400 and `/corrections/uploads` limit 50 →
1000 — since month-grouping a two-week window is pointless.

### 2.10.0 — Debug Console "Review & correct"
After a trace you can correct any field inline, or "Confirm all as correct", and
it feeds the same harvest/diff pipeline as re-uploading a corrected workbook.
Known limitation (shared with the .xlsx path): it does **not** rewrite
`tickets`/`line_items`, so the on-screen value doesn't visually correct itself —
only the learning stores, audit log and status change.

### 2.9.0 — Learning loop made real + line pairing fixed
Two independent root causes for "corrections don't improve anything": barcode↔
vision lines were paired positionally (prices landed on the wrong implant), and
the learning stores were write-only at extraction time. Added `align.py`
(pair by LOT, then REF, positional only as last resort), junk-barcode filtering
(patient wristbands), learned GTIN→REF / part-desc / surgeon-hospital lookups
applied during extraction, same-hospital price fill, and a price sanity ceiling.

---

## Ops gotchas

- **`db/11_hospital_prices.sql` must run before 2.13.0 deploys.** It creates
  `reference_hospital_prices` and `pricing_runs`. Without it the price-list tile
  and step 5 both fail with PGRST204 — the same class of failure as the
  `source_filename` and `patient_initials` incidents. The startup probe catches
  it at boot and on `/diag`.
- **Uploading a price list replaces every tab**, including tabs the new file
  doesn't contain. Same full-replace semantics as the other masters; the tile
  says so, but it's worth knowing before someone uploads a one-tab extract.
- **Supabase truncates unpaginated selects at 1000 rows.** The real price list is
  ~2,500 rows and one tab alone is 1,038, so `hospital_prices_for_tab` goes
  through `find_all_paged`. A truncation here doesn't error — it reads as "this
  hospital has no price", which is exactly the kind of silence that would take a
  week to notice. Don't switch it back to `select()`.
- **Run `db/*.sql` before deploying code that needs it.** `ingest_image` always
  passes `patient_initials` to `create_ticket` (as null when the flag is off), so
  deploying 2.12.0+ against a table without the column fails every upload with
  PGRST204 — the same class of failure as the original `source_filename`
  incident. The startup schema probe catches it at boot and on `/diag`.
- **`.env` changes need a container recreate, not a restart.** `docker-compose.yml`
  uses `env_file: .env`, which is read at create time. `make deploy`
  (`up -d --build`) handles it; `--force-recreate` if a value doesn't appear in
  `docker compose exec labels-api printenv`.
- **`make logs` blocks.** It's `docker compose logs -f`; anything you paste after
  it queues behind the tail until you Ctrl-C.
- **Branch `claude/clever-cerf-oaqc5g` is reused across PRs.** A merged PR can't
  take new commits — after a merge, the next push to the same branch needs a new
  PR (this bit us: #36 was left unmerged for two months and silently collected the
  2.12.0 commits on top of the 2.11.1 ones).
