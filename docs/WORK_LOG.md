# Work Log

Running record of non-obvious decisions, current production state, and open
threads. Git history has the *what*; this file has the *why*, the things that
aren't reconstructable from a diff, and what's still unfinished.

The user-facing release notes live in `app/version.py` (`CHANGELOG`), which is
what the app's "What's New" panel reads. Root `CHANGELOG.md` is the formal
Keep-a-Changelog record and stopped being maintained after 2.0.0.

Last updated: 2026-09-30.

---

## Current state

| | |
|---|---|
| Version in `main` | **2.21.0** (PR #50); **2.22.0** on `claude/clever-cerf-oaqc5g`, not yet merged |
| Deployed | **2.19.0** — and every extraction under it was rejected; see the 2.20.0 entry |
| `EXTRACT_PATIENT_INITIALS` | **true** in the VPS `.env`. As of 2.19.0 this decides whether the two letters are KEPT, not whether they are read |
| Reader | `VISION_PROVIDER` — **`openrouter` from 2.25.0**, with Claude Opus as the escalation reader for tickets left with an empty Price/Surgeon/Hospital/Date |
| Model | `claude-sonnet-5-5` from 2.20.0 — **but the VPS `.env` pins `ANTHROPIC_MODEL` and wins**, so the move needs an `.env` edit as well as a pull |
| Effort | `high` extraction (5.5 recalibrated the levels; `medium` was tuned against Sonnet 5). The separate initials call is gone (2.19.0) |
| Schema | current through `db/11`. **`db/12_price_learning.sql` is NOT applied yet** — needed by 2.16.0 |
| Tests | 432 passed, 2 skipped |
| Learning stores | 2,410 facts, intact through the 2.12.0 migration |

PRs this cycle, all merged: #33 (2.9.0), #34 (2.10.0), #35 (2.11.0),
#36 (2.11.1 + 2.12.0 — carried both), #37 (2.12.1), #38 (2.12.2).

---

## Open threads

**1. ~~The patient mask does not cover every layout.~~ CLOSED 2026-09-30 — the
mask was removed entirely.** Andy's decision, on the grounds that storage is
HIPAA-compliant so the mask was not buying the protection it cost.

It cost a lot. The band was placed by fixed fractional coordinates, so on a
differently-framed photo it clipped `Surgery Date` and `Surgeon` out of the
header — the whole of `_rule_edge` existed to compensate — and when it could not
place itself the ticket went to `manual_queue`, where it produced **zero rows**
in Usage, Line Items and Raw Extraction while still appearing on the Tickets
sheet. A ticket that vanishes from the deliverable is worse than one that needs
review.

Measured before removal, on the four ticket photographs from the 2026-09-30
batch: the sticker sits at y≈0.31–0.38 while `patient_mask` stopped at 0.26, so
the stored images kept patient name, DOB and CSN. The coverage check that was
supposed to catch exactly this could never fire — it re-read the rectangle it
had just painted black, so `surviving == 0` was a tautology. Worth remembering
as a class of bug: a check that verifies its own output verifies nothing.

Consequences now carried in CLAUDE.md rather than here: the Anthropic BAA covers
the main extraction call (full ticket image, not a crop), and `.gitignore` is the
only place the "don't let PHI escape" rule is still enforced by code — which is
why `*.pdf` was added to it.

**2. ~~`detect_template()` still guesses, and the redaction gate trusts the
guess.~~ CLOSED 2026-09-30.** There is no redaction gate. `detect_template` still
guesses `Maxx Orthopedics` with no filename evidence, but the only thing riding
on it now is `grid_region` for the barcode crop, where a wrong guess costs
accuracy rather than exposure. The two layouts share the same `grid_region`, so
in practice it costs nothing.

**3. ~~Already-stored Maxx Health images were never re-masked.~~ MOOT
2026-09-30.** Nothing is masked any more. `scripts/purge_redacted_images.py`
remains if old images should be cleared out; it dry-runs by default.

**4. The two price-list tabs have opposite shapes, and neither is dense.**
Measured against the real `Hospital_Price_List-2.xlsx` (2,459 rows after the
aggregate column is dropped) and the 479 hospitals in `reference/surgeon_info.csv`:

| | hospitals matched | of which green-eligible | grid density for those | expected result |
|---|---|---|---|---|
| **Summary Price List** (Maxx Orthopedics) | 123 of 479 | 55 exact | **9%** (median 6 of 73 items) | broad reach, mostly **rose** |
| **MH for MO** (Maxx Health) | 23 of 479 | 10 exact | **78%** (median 55 of 66) | narrow reach, mostly **green** when it hits |

An earlier note here said "~26% of hospitals, expect a lot of rose". That was
measured against the two tabs pooled and it flattens the difference: it is right
for Orthopedics and actively misleading for Health, where a matched hospital
almost always yields a real looked-up price.

The binding constraint on the Summary tab is **density, not matching**. Only 4 of
176 accounts carry ≥20 prices and 18 carry ≤3; the tab reads as a set of
negotiated exceptions per account rather than a price schedule. Matching cannot
fix an account with four prices in it — a fuller schedule out of SAP is the only
thing that would.

One caveat before anyone acts on the 9%: it is a property of the *list*, not of
real tickets. The list is concentrated — `UPUUX` is priced at 140 of 176
hospitals, `MTUUX` 96, `MLUCX` 88, `UFCR` 74, and **8 items carry 47% of all
priced cells**. If real tickets are dominated by those, the effective hit rate is
far better than 9%. That is answerable from prod (`learning_price`, 2,410 facts,
plus `line_items`) and is worth measuring before investing anywhere else.

`HOSPITAL_ALIASES` is the only lever that raises *green* rather than rose. It is
deliberately still a code dict; the run summary lists every unmatched and
ambiguous hospital, so the loop is to run it and feed back the names that
actually appear on tickets.

**5. Confidence baseline after the effort drop.
Extraction moved from effort `high` (the unset default) to `medium` in 2.12.1.
The regression signal is `pct_confident` in History → "Getting better over time",
which is a per-day series — so the pre-2026-09-20 days *are* the baseline and can
be read retroactively. If it sags over a week of real tickets, put `vision.py`
back to `high`; it's a one-line change.

**6. Verify `Inits` on a real Maxx Health ticket.** — *not yet done*
Before 2.12.2 this came back blank on every MH ticket (wrong region cropped).
2.12.2 is live, so it's unblocked: one Debug Console run against an MH ticket
confirms both halves at once — the right region masked in the stored image, and
the initials populated in column D.

---

## Decisions worth not re-litigating

### Measure where accuracy is lost before spending on improving it

Asked how to dramatically improve accuracy, the honest answer was that nobody
knew where it was failing. Every defect diagnosed over this stretch — dropped
handwritten lines, five spellings of one entity, a clipped surgery date,
hospitals matching the wrong facility — was plumbing, prompt or reference data.
**None of them would have been fixed by a better model or a higher effort
level**, which is where the instinct points.

`corrections_audit` had recorded every human fix since 2.9.0 and nothing read it
but a per-day count. `metrics.correction_accuracy` now groups it by field, and
the split is the point:

| split | the tool... | what it implies |
|---|---|---|
| `blank` (conf `low`) | declined to guess; cell was red | a coverage problem |
| `amber` (conf `medium`) | guessed, flagged it, was wrong | calibration works, accuracy doesn't |
| `silent` (conf `high`) | **was confident and wrong** | nothing was coloured; nobody was asked |

The third column is the one to act on and the one that never existed before. It
falls out of `diff._audit_one` for free: that function sets `was_blank` for
`low` and `was_low_conf` for `medium`, so `high` is the case carrying neither
flag. Read the panel after a couple of weeks of real tickets before choosing
between the two bigger options below.

### The two bigger accuracy options, not yet built

Recorded so the reasoning isn't lost, and because both are more invasive than
anything shipped so far:

**Crop per label cell instead of sending the whole page.** The barcode path
crops `grid_region` (`run.py:162`) and scored 5/5 on a real ticket; the vision
call gets the entire page and must read the header, find every label, read every
handwritten price AND match each price to the right label in one pass.
`app/pipeline/align.py` exists solely to repair the alignment it gets wrong.
Sending each label cell with its own price box makes the alignment problem stop
existing rather than be corrected afterwards. Cost is N calls per ticket against
one; the static prompt is already cached, so this needs measuring, not assuming.

**Use the grand total as a checksum that corrects, not just flags.**
`assemble.py:488` already reconciles lines + freight against the handwritten
total and downgrades prices to amber on a mismatch — but never asks *which* line
is wrong, even when the difference names it exactly (a $175 gap was precisely
three missing pin lines). Re-reading only the implicated cell is targeted at the
highest-value, least reliable field. Anything it changes must land amber, never
high: a reconciliation that "fixes" its way to a wrong answer that happens to
sum is worse than the mismatch.

### A ticket has two kinds of line, and only one of them has a label

The right-hand column carries peel-off implant labels with barcodes. The left
carries pins, screws and instruments **written onto the form's own Ref #/
Description/Price blanks**, with no label at all. All three layers of the
pipeline assumed only the first kind existed:

1. the prompt said "for each *device label*" and never mentioned the blanks;
2. `align_vision_lines` returns one entry **per label**, so with five barcodes
   and eight vision lines the three handwritten ones were discarded before
   assembly saw them — the aligner's own docstring describes padding for exactly
   this case, it just never happened;
3. their part numbers are not in `reference_part_info` and never will be
   reliably, because disposables trail the implant master.

The ticket's own arithmetic is the giveaway: implants summed to $6,490 against a
circled $6,665, and the missing $175 was three pin lines. Labels are now padded
to the vision line count, a handwritten line keeps the description written on the
form, and an unknown part number is no longer fatal.

**The quantity convention differs too.** `(x2) 25ea` against a `Price: $50.00`
box means two items at $25 — the box holds the LINE total, not the unit price.
Reading 50 as the unit price doubles the line and breaks the reconciliation.

### What a reviewed step-5 workbook is allowed to teach

Closing the loop pays off because `db.price_suggestion` is consulted at
*extraction* time (`assemble.py:281`): a learned price fills a blank for the
same hospital, amber so it is still eyeballed, and never overrides a price read
off the ticket. So a blank filled once need never be blank again.

The risk is the mirror image, and it is why this is not simply "harvest the
file". `harvest_ticket` learns every non-blank line price unconditionally, so
pointing it at a step-5 output would teach all the estimates as facts, which
`price_suggestion` would then serve back — the tool citing its own arithmetic.
Three rules keep that from happening:

| in the returned file | learned? | as |
|---|---|---|
| value **changed** by the reviewer | yes | `correction` — authoritative, never overwritten |
| unchanged and **green** | yes | `price_list` — dropped on the next price-list upload |
| unchanged and **rose** | **no** | an estimate nobody touched is still a guess |
| blanked out again | no | rejecting a value is not asserting one |

`learning_price.source` is what makes this expressible, and it defaults to
`correction` so every pre-existing row — all of which came from the step-4
path — is marked truthfully and stays out of any purge.

**The price-list rows are cleared when a new price list is uploaded.** The list
is full-replaced monthly but `learning_price` is never purged, so without that
a learned price-list value would outlive the update meant to supersede it. Only
`source='price_list'` rows go; human corrections are untouched. This was the one
objection to learning green values at all, and clearing is what answers it.

### The run id rides in the workbook's custom properties

Step 4 has to know which run a returned file came from, and there is nowhere in
the grid to put that without the "only blank Price cells changed" validator
objecting — rightly. `docProps/custom.xml` is outside the cell grid entirely,
survives an openpyxl round-trip, and is invisible to the operator.

Note the join it avoids: `learning_price` is keyed on (part, hospital), both of
which the Usage sheet already carries, so no line id is needed. Just as well —
Usage and Line Items are **not** row-for-row (153 vs 159 on the first real
file), and `parse_corrected_workbook` reads Line Items, which step 5 never
touches. Without the stamp and the recorded cells, uploading a priced workbook
to step 4 would report success and silently learn nothing.

### The price-list tabs are where an account is listed, not a distributor's contract

The written work instructions scoped each distributor to one tab (acceptance
tests 10 and 11: "Maxx Health uses only MH for MO"). The first real workbook
disproved it. Of its 88 blank prices, **27 belonged to hospitals priced only on a
tab that rule forbade** — Blake Medical Center and Parkridge on `MH for MO`,
Comprehensive Outpatient Joint and Spine Institute on `STINSON ORTHO`, all from
Maxx Orthopedics tickets.

Confirmed with the user, and the rule is now **preference, not exclusivity**:

- every tab is searched, ranked by how well it matches the hospital
  (`exact` > `alias` > `core` > `fuzzy`), ties broken toward the ticket's own tab
- a price from the ticket's **own** tab, on an exact/aliased hospital, is green
- a price from **any other** tab is always rose, however good the match — it is
  another sales channel's number for that account, and the run summary lists
  every off-tab source under `off_tab`

**Ranking by quality before preference is load-bearing.** Preferring the home tab
unconditionally picks the wrong answer exactly where it hurts: `Methodist
Hospital HCA` matched its own tab only fuzzily, to `Methodist Hospital
Southlake` — a different facility — while another tab had it exactly. That was
21 rows of one file.

### Entity is a vision read; the filename is a convention

`Entity` is the model reading printed text off the ticket, so one batch from one
company arrives spelled several ways. The first real file had `Maxx Orthopedics`
(30), `Maxx Orthopedics, Inc` (6), `Maxx Orthopedics, Inc.` (1), `MAXX` (1) and
blank (9) — and the lookup was an exact string match, so four of those five
failed, and one failing row aborted all 153.

`detect_template` on the `MH`/`MO` filename prefix resolved **every** row. It is
already the authoritative signal for PHI redaction (2.12.2), so pricing now tries
it **first** and falls back to the entity text (normalized, legal suffixes
stripped). A disagreement is logged and the filename wins.

The corollary: an unresolvable row costs that row, not the run. A file where
*nothing* resolves still fails loudly, which is what the original rule was for.

### Stripping the health-system tag cuts both ways

`normalize_hospital` drops a trailing parenthetical so `Centerpoint Med Ctr
(HCA)` matches a master that writes `Centerpoint Medical Center`. But the usage
sheet also writes the tag **without** brackets, and then the strip destroys the
match:

```
'Methodist Hospital HCA' vs 'Methodist Hospital (HCA)'
    stripped -> 'methodist hospital hca' / 'methodist hospital'      0.900, fuzzy
    kept     -> 'methodist hospital hca' / 'methodist hospital hca'  EXACT
```

Both spellings are now indexed (`normalize.hospital_forms`) and either may carry
an exact match. Neither form alone is sufficient — keep both.

### The status email measures uploads, not batches, and uses the team's day

Two traps, both of which would have made it quietly wrong:

- **`daily_batch_job` runs unattended at 02:00 UTC** — which is 10pm Eastern the
  *evening before* — and turns whatever is pending into a batch. So "a batch
  exists today" can be true with nobody having touched the app. Uploading
  tickets is the part only a person can do, so that is what silence is measured
  against. Batches are still reported; they just aren't the alarm.
- **`batches.run_date` defaults to Postgres `current_date`, which is UTC** and
  rolls over at 8pm Eastern, so Thursday-evening work is stamped Friday. Every
  window in `notify.py` is computed in `NOTIFY_TIMEZONE` from `created_at`. Do
  not "simplify" it back to `run_date`.

Weekends are skipped rather than counted in the consecutive-miss tally, so a
Monday with nothing uploaded reads "1st weekday", not "3rd".

### The status email cannot carry PHI, by type rather than by template

`DayStatus` is a date and six integers. There is no field on it capable of
holding a hospital, a surgeon or a patient's initials, so the renderer cannot
leak one however the wording changes later. `tests/test_notify.py` asserts the
dataclass's own field types, so widening it fails the suite rather than quietly
opening a hole. The email also says so in its own footer, because the people
receiving it are the ones who would notice if it stopped being true.

### Email config mirrors the Maxx dashboard's contract, not its code

Same variable names (`EMAIL_PROVIDER`, `EMAIL_FROM`, `SMTP_HOST/PORT/USER/
PASSWORD`) and the same decision to return a *reason* instead of raising, so one
set of relay credentials copies between the projects and "not configured yet"
stays a state the UI can describe in words. What is deliberately **not** copied
is that project's hand-rolled SMTP client: it exists because Node has no SMTP in
its standard library. Python does — `smtplib.SMTP_SSL` plus `EmailMessage` is
the whole thing in about fifteen lines, including the dot-stuffing that file
warns about. Implicit TLS on 465 only, same reasoning: STARTTLS opens in
plaintext and upgrades, so a downgrade is possible and every failure mode
doubles.

`resend`/`postmark` are accepted in the config contract (the dashboard's `.env`
may name them) but refused with a sentence, because this app has no HTTP sender
and a silent no-op would be worse.

### Three defects that a green test suite could not have caught

Found by running the shipped code against the real `Hospital_Price_List-2.xlsx`
rather than against fixtures. Every one of them fails **silently** — no
exception, no wrong number on screen, just a price that never appears. Fixture
tests cannot find this class of bug, because the fixture encodes the same
assumption the code does. Run new parsers against the real file.

1. **Four-star wildcards never matched.** The tokenizer split on the literal
   `***`, so `ACLM****-UK` and `RFPS****-GK` compiled to patterns requiring a
   literal `*` in the REF. 17 price rows unreachable. Now matches *runs*
   (`\*+`), which is what the catalogue actually writes.
2. **`AVERAGE ITEM PRICE` was ingested as a hospital.** It is the Summary tab's
   last column — a spreadsheet aggregate (the values give it away:
   `2357.142857`). 74 rows, indistinguishable from a real account, and the
   estimate ladder medians *across* hospitals, so a derived average was folded
   back in as an independent observation: it moved the result for **37 of 58
   components**. `AGGREGATE_HEADERS` in `tabs.py` now excludes those columns and
   logs what it skipped.
3. **The fuzzy tiebreak picked one sibling facility out of five** — see below.

### The hospital matcher's margin rule was justified by a case that never reaches it

`CREDIBLE_MARGIN` was introduced to stop `Advanced Surg Ctr of North County` and
`… North County HIgh Demand` from tying. **That justification was wrong**: those
two normalize identically, so the case resolves at the *exact* tier and never
reaches the fuzzy matcher at all. The margin solved nothing and broke something:

```
'Baylor, Scott, & White'  ->  Baylor Scott & White Star     0.878  <- picked
                              Baylor Scott & White Frisco   0.837
                              ... three more siblings
```

A query naming **no facility** confidently picked one of five, on a margin that
is an artifact of suffix length. Across the full master the margin bought exactly
three matches: two correct Lehigh Valley typo cases and this one wrong Baylor.

The matcher is now four short-circuiting tiers — exact, alias, **core**, fuzzy.
`core` compares names with the generic words stripped and requires exactly one
column to reduce to that form; it catches `Baylor Scott & White Medical Center -
Sunnyvale` → `Baylor Scott & White Sunnyvale`, which fuzzy scored at only 0.79.
Fuzzy keeps the margin but now also requires the leader to be either near-exact
(≥0.95, the typo case) or picked out *by* the query — sharing a meaningful word
the runner-up lacks. Net: Summary 107 → 123 matched, MH 21 → 23, and both generic
Baylor names correctly ambiguous.

### A fuzzy or core hospital match never produces a green price

The work instructions treat a single ≥75% fuzzy candidate as a match, full stop.
It is a match — but not a *certain* one: on the real data it produces
`Arroyo Grande Surgical Institute` → `River Surgical Institute`. Green tells the
reviewer "this came straight from the list, don't check it", which is exactly the
wrong thing to say about a guess. So green requires an **exact or aliased**
hospital (`HospitalMatch.confident`); a fuzzy hospital still prices the row, as a
rose estimate. Fuzzy still earns its keep — it correctly catches
`AdventHealth Carrolwood` → `AdventHealth Carrollwood`.

The `core` tier is excluded from green for its own reason: `meaningful_hospital`
strips `hospital`, `surgery` and `center`, so `Boca Raton Hospital` and
`Boca Raton Surg Ctr` collapse to the same core — plausibly two different
accounts. Good enough to estimate from; not good enough to tell someone not to
check.

### `***` is neither three characters nor three glyphs

Two separate mistakes, made one after the other, both of which failed silently:

- The **fill** is not three characters. `UFCR***-GK` stands for `UFCRLA00-GK` and
  `ACLMR***-UK` for `ACLMRL100-UK` — four-character fills. A `{3}` quantifier
  matches neither. The fill is a `+` run, and intra-tier price agreement is what
  keeps it honest, not the quantifier.
- The **token** is not three glyphs either. `ACLM****-UK` and `RFPS****-GK` are
  real catalogue codes, and splitting on a literal `***` leaves the fourth star
  to be escaped into the pattern, which can then never match. The tokenizer
  matches runs: `re.compile(r"(\*+|x{3,})")`.

Lowercase `xxx` is a wildcard and uppercase `XX` is not, because `DAXX00D-F` is a
real part number; no part number in the master contains an uppercase `XXX`.

### Component tiers short-circuit for the price, but the losers are kept

The bare code `MTUUX` is a prefix of `MTUUX100-GK`, which the pattern
`MTUUX***-GK` also matches — and those two catalogue rows **disagree two times in
three** on the real list (of 32 hospitals pricing both, only 11 agree; for `RFPS`,
0 of 4). So unioning the tiers reads that as "conflicting prices, refuse to price
the row" and loses a price that was never ambiguous. Tiers are evaluated in order
and the first that yields anything wins the *direct* price.

But the losing tiers are not discarded — `ComponentMatch.fallbacks` keeps them,
because the winning tier having no price **at this hospital** is not the same as
there being no evidence. `MTUUX` is priced at 96 hospitals and `MTUUX***-GK` at
only 80, and **64 of those 96 have no variant price at all**; across the five
family/variant pairs that is 202 hospital×component intersections that would
otherwise come back blank. Estimate rung 1b uses the family row at the same
hospital, and writes it **rose** — that same 2-in-3 disagreement is the proof
that a family price is not a variant price. It ranks above every cross-hospital
rung, because the hospital is what sets the price.

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

### 2.22.0 — Two things volume was going to break

Both reported by Andy on the first good run through OpenRouter.

**Uploads were dropping photos.** Four of a batch came back as "couldn't be
uploaded — Server disconnected". That error is a transient HTTP/2 drop from the
shared storage client under concurrent writes, and `transient.is_transient()`
already classified it as retryable — but **only the batch path ever retried.**
`ingest_image` had none, so the per-file isolation written to stop one bad file
sinking a batch was instead discarding four good photos.

The retry loop is now one shared `transient.retry()` used by both paths rather
than a copy in each, which is what let them drift apart in the first place. The
backoff matters as much as the retry: under contention, spreading out beats
hammering through, and Andy's volume is rising every month.

**Surgery dates from prior years.** A ticket read "Sept. 28, 2024" while its own
patient sticker gave a DOS of 9/28/2026. These tickets are always from the
current year, so the year is corrected — and the corrected value is **amber with
a note naming the original reading**, because silently rewriting data the model
extracted is how a tool stops being trusted.

Deliberately not a blind "force the current year": a surgery on 28 December
processed on 3 January is genuinely from the prior year, and stamping this year
on it would push it eleven months into the future — turning a correct date into
a wrong one. The current year is used only when it does not land the date
implausibly ahead of today. That case has its own test, because it is the one
that will actually happen and the one a naive implementation gets wrong.

### 2.21.0 — A second reader, because the first one hit a spend cap

The 2.20.0 preflight paid for itself on its first run. Andy deployed, hit the
button, and got the answer in seconds:

> You have reached your specified API usage limits. You will regain access on
> 2026-10-01 at 00:00 UTC.

**Not a bug — a spend cap on the Anthropic account, on the last day of the
month.** Which almost certainly means the *original* Sep 30 outage was this too:
a monthly cap hitting at month-end produces exactly what was seen — every ticket
failing deterministically on a day that had worked five days earlier. The
`max_tokens` truncation theory recorded under 2.19.0 is therefore probably
**wrong**; it is left in place because the raise to 16000 is right regardless and
because a theory that was superseded is worth seeing next to the thing that
superseded it.

So: `VISION_PROVIDER` selects `anthropic` (default, so a deployment that sets
nothing is unchanged) or `openrouter`, for open-weight models at roughly a tenth
of the cost — ~$78/mo to ~$7/mo at this volume.

**The pattern is ported from maxxdash** (`apps/dashboard/lib/assistant-provider.ts`),
which had already solved this and reasoned it through: a provider setting whose
variable NAMES travel with the configuration, a `data_collection: deny` constant
rather than a setting somebody flips to make a failing model work, and a
router-side `models` array instead of a second retry loop in our code. Two
deliberate departures:

- **The fallback had to change.** maxxdash falls back to `deepseek/deepseek-v3.2`,
  which is text-only. Copied as-is it would have failed every ticket the moment
  the router walked to it. Ours is `qwen/qwen3-vl-32b-instruct`.
- **The model is chosen for reasoning, not OCR.** Specialised OCR models now beat
  large general VLMs on document benchmarks outright, but the hard part of these
  tickets is not character recognition — it is matching handwritten prices to the
  right labels, spotting wasted components, and reconciling to the grand total.
  That is reasoning over a page, so `qwen/qwen3-vl-235b-a22b-instruct`.

**Only the transport differs.** `_call_anthropic` and `_call_openrouter` build a
request and read an answer; everything that two outages bought — the error
marker, the truncation check, the refusal check, the retry classification, the
trace — is shared, and OpenAI-compatible `finish_reason` values are mapped onto
the vocabulary `_parse` already speaks. A second copy of that logic would be a
second place for a failure to go quiet, which is the whole lesson of 2.19.0 and
2.20.0.

**PHI, stated plainly because it is the cost of this change.** The ticket image
is sent whole, patient sticker included, and **OpenRouter publishes no BAA** —
nor does the upstream provider it routes to. Masking was offered and declined;
Andy approved sending unmasked on 2026-09-30 to cut cost. The standing rule now
lives in CLAUDE.md so it is a decision on the record. Two things do not follow
from the code and have to be done in their console: OpenRouter's own prompt
logging is an account setting, and the alternative — re-masking — would mean
restoring `app/pipeline/redact.py` from git history.

Worth noting for whoever reads this next: an automated guard blocked two
attempts to write this paragraph before it was approved. It was reading the
situation correctly.

### 2.20.0 — The fix that broke it a second way

2.19.0 shipped, merged, deployed. Andy re-ran the same nine tickets and got the
new banner: **"9 tickets couldn't be read."**

The banner was 2.19.0's error reporting working exactly as intended. The failure
it reported was mine.

```python
thinking={"type": "enabled", "budget_tokens": 4000},   # 2.19.0
```

**`budget_tokens` is rejected with a 400 on this model family.** Before 2.19.0
the call sent `thinking={"type": "adaptive"}` — the correct, and only, on-mode.
Fixing the token-budget problem I replaced a working parameter with a rejected
one, and every call 400'd from the moment it deployed.

So there were two faults, and 2.19.0 fixed one and introduced the other:

| | |
|---|---|
| Original outage (Sep 30, silent) | Best-supported cause: `max_tokens=8000` covering thinking *and* output, truncating the JSON. 2.19.0's raise to 16000 stands. |
| Second outage (loud) | `budget_tokens` on a model that rejects it. |

**The lesson, and it is the one worth keeping.** 393 tests were green while every
single API call was malformed. Every vision test patches `anthropic.Anthropic`,
so `messages.create` is a `MagicMock` that accepts any keyword argument ever
invented. **A mocked client cannot validate an API contract.** This code has now
failed twice at the boundary between our process and the API, and until 2.20.0
nothing in the repo had ever exercised that boundary.

What that bought:

- `vision.check_connection()` — one small live call that **sends the same
  parameter shape as a real extraction**, via a shared `_request_params()` so
  the two cannot drift. Exposed as `GET /health/vision` and a "Test AI
  connection" button. Answers in seconds; the previous way to learn this was to
  run nine tickets and read the result.
- `run_batch` preflights and **raises `VisionUnavailable` rather than
  processing**. A batch that can read nothing should not consume the tickets and
  hand back a barcode-only spreadsheet that looks finished. Tickets stay
  `pending_review`.
- Request-shape guards in `tests/test_vision_caching.py`: thinking is adaptive,
  `budget_tokens` appears nowhere in the request, effort is `high`,
  `max_tokens >= 16000`. They still cannot validate the contract — only the live
  check does — but they pin the shape we mean to send, and they would have
  caught this.

Also moved to `claude-sonnet-5-5` (current Sonnet, same $2/$10) and effort to
`high`: 5.5 recalibrated the levels, so the `medium` tuned against Sonnet 5 no
longer means what it did, and this is careful reading of handwriting at ~100
tickets a day where accuracy is the whole complaint.

A refusal (`stop_reason == "refusal"`, HTTP 200, no text) is now reported as a
refusal with its category, rather than falling through to the parser and being
called "unparseable response" — true, but it would send the next person hunting
in the wrong place.

**Three defects in the same change, all found by reading the diff rather than by
the suite, all in UI code nothing covers.** Recorded because the pattern is the
point: the 503 carried a dict `detail`, and `api.js` only unwraps a string one,
so the browser saw a bare "503 Service Unavailable" and the UI offered "try
again" — the one piece of advice that cannot help with a misconfiguration. The
branch that was supposed to catch it matched on **message text**, which is the
same class of mistake as the one that caused the release; it now branches on
`err.status`. And it rendered the error through a CSS class I had invented,
while `renderNotice` already takes a `detail` argument that builds a collapsible
technical block. `tests/test_vision_failure.py` now exercises the route.

Deliberately not bundled: the server-side `fallbacks` parameter, which retries a
declined request on another model. It needs the beta messages endpoint and a
beta header, and adding a beta endpoint to a call that has broken twice was the
wrong trade. Worth doing once the pipeline is confirmed healthy.

### 2.19.0 — The batch that came back 54% red

**What the user saw.** Nine real tickets, 24 rows, and 193 of 360 cells in the
Usage sheet red. "This is an absolute disaster… WAY WAY WAY too many reds."

**What it actually was.** Not a confidence-tuning problem. The vision call
returned nothing on all nine tickets and the run reported success.

The workbook proves it on its own. `Raw Extraction` had `Vision Ref`, `Vision
Lot` and `Vision Price` **null on all 24 lines** while `Barcode Decoded?` was
"Yes" on all 24. The red columns were exactly the vision-fed ones — Surgeon,
Inits, DistCode, Date, Month, Year, Hospital, Price, each red on 24/24 rows, 192
of the 193. The clean columns were exactly the barcode-fed ones. The control is
decisive: the Sep 25 batch, same code path five days earlier, had 157/159 vision
refs and populated headers.

**Why nobody was told.** `vision.py` ended in `except Exception: return _EMPTY`
— unbound, unlogged, unflagged, unretried. Three more silent paths sat beside
it: `_parse` turned truncated JSON into the same empty result *after a billed
call*; `tracer.record` is a no-op outside `/debug/trace`, so a normal batch
persisted nothing about the call at all; and `_safe_process` already retried
`RateLimitError` / `529` / "overloaded" four times with backoff — none of which
could ever reach it, because this handler swallowed them first. The three empty
results (no key, API error, unparseable response) were byte-identical to each
other and to a genuinely blank ticket.

**The likely trigger, stated honestly.** 2.17.0 grew `SYSTEM_PROMPT` from ~4.4k
to ~6.0k chars and asked the model for materially more work, while the call ran
`thinking={"type": "adaptive"}` with `max_tokens=8000` covering thinking *and*
output. Longer thinking truncates the JSON, `_parse` makes that an empty result.
A plain API error is the alternative. **The two cannot be separated from the
evidence, because the failure left no record anywhere** — which is the bug, not
a footnote. Both are now removed (fixed 4000-token thinking budget inside
16000), and any recurrence identifies itself.

**The lesson worth keeping.** A deterministic path that still works is the most
dangerous thing to have next to a failing one. The barcodes decoded, the
spreadsheet came out full-looking, and the failure wore the exact costume of a
hard day's tickets. Any "never let X sink the batch" handler needs to answer
"and how will anyone find out?" before it is allowed to return a default.

**The second, independent problem.** Even with vision healthy, red was the wrong
answer far too often, because `low` confidence meant *delete the value*. A
sub-threshold read was thrown away; the hospital written on the ticket never
reached the Usage column (fed only by the surgeon lookup); the grand-total
residual was computed for a log message and discarded. In every case the
reviewer was asked to find something the tool was holding. Red now means one
thing: nothing to suggest. `confidence.at_least_amber` and `write._shown`
enforce both halves — an amber cell always carries a value, an empty cell is
always red.

**Backtested** against the four tickets whose photographs we have, read
correctly: **54% red → 10%**, and every remaining red is genuine (three tickets
have no per-line prices written at all; MO18777-A has surgeon, date and rep
blank on the form; the handwritten pin lines have no lot or expiry). The Cox
ticket reconciles exactly — 1300 + 1150 + 900 + 250 + 175 of pins = the circled
3775 — exercising per-label prices, unlabelled handwritten lines and the
`(x2) 25ea` convention in one case.

**Two judgement calls made against a design proposal.** Worth recording because
both look like improvements until you check them:

- *Do not* let step 5 overwrite an amber Price cell. The proposal was that amber
  marks a tool suggestion and a price-list hit is better evidence. It doesn't:
  amber is the confidence colour for **any** single-source read, so most prices
  a human wrote on a ticket arrive amber too, and the change would have let a
  price list silently overwrite the figure on the ticket. Verified by
  `test_at7_an_existing_zero_price_is_left_alone`, which caught it immediately.
- *Do* guard the learning harvest. `learn_price` treated any non-empty price on
  a returned workbook as a human decision, so a suggestion nobody touched came
  back and was learned as ground truth — the tool citing its own arithmetic.
  Step 5 already refuses this for its rose estimates; extraction-time
  suggestions needed the same refusal the moment they started filling cells
  that used to arrive blank. `harvest._untouched_suggestions`.

A GS1-decoded `(240)` REF was also being scored as if someone had read it by
eye, so a barcode-certain part number rendered blank and red whenever the part
master hadn't caught up with it — routine for hip components and disposables.

### 2.18.0 — Per-field accuracy report
A History panel grouping every correction by field, worst first, split into
blank / amber / **wrong-without-warning**. No migration; the data has been in
`corrections_audit` since 2.9.0 and nothing had ever read it. See "Decisions"
above for how to read it and what it is meant to decide.

### 2.17.0 — Handwritten lines, and a mask that ate the header
Three defects found on the first real ticket photographs. Handwritten line items
were dropped by all three layers of the pipeline (see "Decisions"); `(x2) 25ea`
was being read as a unit price rather than a line total; and part numbers absent
from the master killed the line instead of falling back to the written
description.

Separately, the patient mask's fixed left edge at 0.55w was cutting through
`Surgery Date` and `Surgeon` — on one layout it clipped the year off `9/28/26`,
on another it took half the surgeon's name. The mask edge now snaps to the form's
own printed gutter rule (0.58w–0.65w depending on layout), which recovers both
fields and still covers the sticker. That is an extraction fix; the wider mask
coverage problem is open thread 1.

### 2.16.0 — A reviewed step-5 workbook teaches the next extraction
Send a priced spreadsheet back through step 4 and the prices are learned against
(part, hospital), so the next batch arrives with those cells already filled.
Changed values are learned as human decisions; price-list values are learned but
expire with the list they came from; untouched estimates are never learned. See
"Decisions" above for why each of those is the way it is. Requires
`db/12_price_learning.sql`.

### 2.15.1 — Step 5 download button
`el()` reads only class/text/html/attrs, so an `href` passed at the top level was
silently dropped and the download button had no destination. Also wired up
`api.pricingLatest()`, which was defined and never called, so a finished workbook
became unreachable after a page reload. Added `tests/test_static_ui.py`, since
this broke the whole deliverable of step 5 while every Python test passed.

### 2.15.0 — Step 5 against the first real workbook
The first real usage workbook (153 rows, 88 blank prices) failed outright on row
one. Three defects, all visible in that single file, all covered under
"Decisions" above: entity spelling variants breaking an exact-string lookup, the
one-tab-per-distributor rule hiding 27 prices that existed, and parenthetical
stripping turning an exact hospital match into a wrong fuzzy one.

After: the run completes, 28 green / 55 rose / 5 unresolved, the counts add up,
every green value was verified verbatim against the price list, and `Methodist
Hospital HCA` resolves to `Methodist Hospital (HCA)` instead of Southlake.

### 2.14.0 — Weekday status email
Every weekday at 5pm Eastern the app emails a short summary of the day, or says
plainly that nothing was run. The problem is a silent one: a day nobody uses the
tool produces no error and no empty report, so it stays invisible until month
end. The subject carries the state (`Usage Fri 25 Sep: 3 batches, 47 tickets` /
`... NOTHING RUN TODAY (3rd weekday)`) so it is readable from a phone
notification. Recipients live in `app_settings`, editable in the UI without a
deploy. No migration.

It is sent **every** weekday rather than only on a miss, deliberately: an
alert-only job that dies looks exactly like a good day, which is the same
invisible-failure problem one level up. For the same reason the last send
attempt — including the relay's own words on failure — is recorded and surfaced
on the card and in `/diag`.

### 2.13.0 — Step 5: surgery price enrichment
Blank Price cells are blank because the ticket quoted a *construct* total instead
of a price per component; the accountant has been typing them in by hand against
a hospital price list. Step 5 does that lookup: upload the generated workbook,
get the same workbook back with those cells filled — neon green `39FF14` straight
from the price list, rose `FFC7CE` for an inferred estimate, still red where there
was no evidence. New `app/pricing/` package (`tabs`, `normalize`, `match`,
`estimate`, `ingest`, `enrich`), a fifth Reference Data tile, and
`db/11_hospital_prices.sql`. See "Decisions" above for the five places this
deliberately departs from the written work instructions, and for the three
defects that running it against the real price list turned up afterwards.

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

- **`scripts/purge_redacted_images.py` deletes stored ticket images.** Images
  masked by the pre-2.17.0 region may show patient details the mask missed;
  re-masking cannot help, because the stored copy is already the masked one.
  Dry-runs by default, `--delete` to act, `--before YYYY-MM-DD` to scope it.
  Extraction results and learned facts are untouched.
- **`db/12_price_learning.sql` must run before 2.16.0 deploys.** It adds
  `learning_price.source` and `pricing_runs.cells`, both additive with no data
  change. Without it the price harvest fails and step 4 loses the new behaviour.
- **Uploading a price list now deletes learned rows** — but only those with
  `source='price_list'`, never a human correction. They are rebuilt on the next
  step-4 round trip with the new numbers.
- **The status email needs SMTP credentials in `.env`, and nothing else.** With
  them blank the app behaves exactly as before and the card says which value is
  missing. There is no migration: recipients live in `app_settings`, which has
  existed since `db/01`.
- **The relay and the mailbox are shared with the Maxx dashboard** —
  `smtp.hostinger.com`, sending as the dashboard's own address. Read the live
  values with `docker compose exec maxxdash-dashboard printenv EMAIL_FROM
  SMTP_HOST SMTP_USER SMTP_PASSWORD`; they are runtime-only and committed in
  neither repo. The consequence worth writing down: **rotating that mailbox's
  password breaks two apps, not one.** Hostinger also authenticates the envelope
  sender, so `EMAIL_FROM` must equal `SMTP_USER` or the relay refuses outright.
- **`tzdata` is in requirements.txt on purpose.** `python:*-slim` images ship
  without the OS tz database, so `ZoneInfo("America/New_York")` would raise at
  scheduler start and the 5pm job would never register.
- **The 5pm job is pinned to a zone name, not an offset.** `NOTIFY_TIMEZONE`
  handles EST/EDT, so 5pm stays 5pm. Verified across the 1 Nov 2026 changeover:
  21:00 UTC before, 22:00 UTC after.
- **Weekday-only still includes public holidays.** Thanksgiving will send a
  "nothing run" alert. Known and accepted for now; a skip-list in `app_settings`
  is the cheap fix if it becomes annoying.
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
