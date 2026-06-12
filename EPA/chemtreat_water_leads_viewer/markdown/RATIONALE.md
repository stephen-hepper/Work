# RATIONALE.md

Design notes for `index.html` — what choices were made, what's still
loose, and why. Read before changing the viewer.

---

## What this is

A single self-contained HTML file (HTML + CSS + JS, no build step, no
dependencies) that lets the ChemTreat sales team browse the output of
the pipeline at `../chemtreat_water_leads/`. Opens directly in a
browser; works offline; ships with seed data so it doubles as a demo.

The goal is **not** a real web app. It's a sales-facing veneer over a
CSV that makes the things sales needs (score reasoning, outreach
posture, fresh signals) prominent and the things they should ignore
(stale Resolved violations, paperwork-only categories) easy to filter
out.

---

## Where the CSVs come from

`all_leads.csv` and `violation_events.csv` are materialized on demand
from `../snapshot.sqlite` via
`python -m chemtreat_water_leads.dump_run --latest --out <dir>` (or
`--run-id N`). The runs themselves no longer write these CSVs inline
— only `run_health.json` and `newly_snc_*.csv` (irrecoverable
artifacts) land in the per-run folder. See the parent project's
RATIONALE.md "Per-run output folders" for the full reasoning.

The DB is the single source of truth; the CSVs the viewer eats are
viewer-shaped views of it, materialized on demand. Implications for
this viewer:

- **Column shape is locked** to the explicit `FAC_CSV_COLUMNS` /
  `VIOL_CSV_COLUMNS` lists in `snapshot.py`, not to whatever Python
  dict any given run happened to assemble. So the viewer's column
  references (in `setLeads`, `renderDetail`, upload detection) are
  contractual against those lists.
- **`violation_events.csv` always carries the union of CWA + SDWA
  columns** even when only one program produced events this run.
  The viewer's `renderEvents()` already picks the right title via
  `parameter || violation_description || contaminant`, so the extra
  empty cells are harmless.
- **`tag_*` columns serialize as `"True"`/`"False"` strings** to
  match the legacy DictWriter output. The viewer doesn't read them
  today, but anyone wiring them up should expect strings, not
  booleans.
- **Violations without a `violation_id` are dropped** by the
  pipeline's dedupe — they cannot be diffed across runs and so they
  never reach the DB. If a future event source emits IDless rows
  and sales asks why they're missing, the fix lives in the
  pipeline's fetch step, not in the viewer.

### Both producers — API pipeline AND bulk loader — write the same shape

`pipeline.py` (per-state API path) and `bulk_loader.py` (nationwide
weekly CSV-download path) both write to the same `snapshot.sqlite`
schema, and `dump_run` is the single path that emits CSVs from it —
so the viewer renders the output of either one identically. The bulk
loader runs the same phase-2 augmentation (re-score with events,
compute `outreach_posture`, compute `tag_*`) the API pipeline runs,
and falls back to the API DFR for any high-value lead the bulk file
missed. Sales gets full per-event drill-down detail from a single
weekly `bulk_loader` run; no need to chase a follow-up API run.

If a `dump_run` materialization of a bulk-run looks different from
the same of an API-run, the divergence is upstream in the pipeline's
data flatten/normalize step, not in the viewer.

---

## Hard constraints honored from the parent project

These come from `../chemtreat_water_leads/MEMORY.md` and `README.md`:

1. **Reporting lag must be impossible to miss.** The parent project
   surfaces it in three places (console banner, `data_lag_note` column
   on every event row, README section). The viewer is the **fourth**:
   a sticky yellow banner at the top of the page that can't be
   dismissed.
2. **Scores must remain explainable.** No ML, no obfuscation. Every
   row's expanded view parses `score_reasons` (pipe-separated) back
   into per-rule point pills so a rep can audit any number.
3. **Resolved = do not call.** MEMORY.md and the README outreach
   posture table both say Resolved violations should pause outreach.
   The viewer surfaces this in three ways: a green tint on the row,
   an explicit "Do not call" pill next to the badge, and a "Resolved
   — do not call" summary tile up top. The status filter also
   defaults Resolved **off** so reps don't accidentally call on
   resolved cases.
4. **No real-time pretense.** The lag banner uses past-tense framing
   ("got into trouble") and explicitly says "verify before outreach."
   No "live" or "now" language anywhere.

---

## Design decisions and reasoning

### Single file, no dependencies

Sales runs this on a laptop. They open a CSV every morning. The cost
of "first install a package manager and run `npm install`" is higher
than the value of any framework. A 1,000-line static HTML file with
inline CSS/JS loads instantly, works on an airplane, and is reviewable
in a single read.

### Seeded by default, file upload optional

The user asked for "Both — seeded by default, file upload optional."
That means:

- Demo data is hardcoded in `SEED_LEADS` and `SEED_EVENTS` so the page
  is immediately useful as a mockup.
- Clicking **Upload CSV** opens a file picker that accepts the actual
  pipeline output. The handler auto-detects which file is which by
  inspecting column names (`lead_score`/`score_reasons` ⇒ leads file;
  `violation_id`/`exceedance_pct`/`violation_description`/`parameter`
  ⇒ events file). Upload both at once or one at a time.
- **Reset to demo data** restores the seed so demos remain repeatable.

### Inline row expansion (not a modal, not a side drawer)

Modals hide context. Side drawers force horizontal scanning. Inline
expansion keeps the rep's place in the ranked list and lets them
expand multiple rows in sequence without losing scroll position.

Trade-off: only one row expanded at a time, to keep the page from
becoming a tall mess. If a rep wants to compare two facilities they
re-click each in sequence.

### Score color thresholds (≥80 red, 60–79 orange, 40–59 yellow, <40 gray)

The README explicitly says "78 vs 82 is noise; 78 vs 40 is signal." So
the colors bucket coarsely. Within a bucket the rep should treat the
ordering as tiebreaker, not law.

### 13-quarter compliance history as colored squares

The raw `compliance_history_13q` string ("VVVVSSSSCCCCC") is unreadable
in a table cell. Rendering it as 13 colored squares left-to-right
(oldest → newest) gives at-a-glance read on trajectory — is the
facility getting worse, recovering, or chronically flat? A legend
underneath explains V/S/C/N.

### Filter defaults (Resolved off, Archived off)

The default filter shows Unresolved + Addressed, which is what sales
should act on. Resolved and Archived are checkboxes they can turn on
if they want full territory awareness, but the defaults match the
README's outreach posture table.

### Color-coded program tag (CWA blue, SDWA teal)

Sales reps for industrial wastewater accounts (CWA) and drinking
water systems (SDWA) often work different territories. A color makes
mode-switching cheap when scanning.

### "Copy facility summary" button

Reps often paste lead details into CRM notes or Slack threads. Better
to give them a clean text dump than have them retype.

---

## Known gaps and intended behavior

### 1. Lead-level outreach status is not in the real CSV

**Status:** Resolved — closed by an upstream pipeline change plus a small
mapping at the viewer boundary.

The pipeline (`chemtreat_water_leads/pipeline.py`) now writes an
`outreach_posture` column on every row of `all_leads.csv`. It's
computed at pipeline time from the per-event statuses using the
aggregation logic this section originally proposed, with two extra
buckets the seed data didn't need:

| `outreach_posture` (pipeline) | Maps to (viewer) | Meaning |
|---|---|---|
| `active`               | `Unresolved` | at least one Unaddressed/Unresolved event |
| `enforcement_underway` | `Addressed`  | Addressed events but nothing Unaddressed |
| `verify_first`         | `Resolved`   | events exist, all Resolved — do not call |
| `historical`           | `Archived`   | all events Archived (>5 yr; stale) |
| `no_events`            | `NoEvents`   | no events drilled; rely on facility score |

`setLeads()` prefers `outreach_posture` over the legacy
`snc_status`-regex fallback, mapping through the `POSTURE_MAP` constant
defined near `postureText()`. This keeps every existing rendering path
unchanged (status pill, posture box CSS, "Do not call" pill on
`Resolved`, green row tint, filter chips for the original four
statuses) and just absorbs the rename at the data boundary.

`NoEvents` is a new first-class status because in practice most rows
fall into it — only high-scoring leads get drilled via the per-facility
DFR endpoint, so the bulk of the inventory has no drilled events. It
gets its own filter chip ("No drill-down", checked by default), its
own posture-box explainer ("score reflects facility-level flags;
verify on ECHO before outreach"), and neutral gray styling.

Backwards compatibility: if a CSV has no `outreach_posture` column
(older pipeline output) or a literal `status` column (seed data), the
old code path still runs. Unknown values map to `NoEvents` as the safe
default.

The "Resolved — do not call" tile, the green tint, and the `Do not
call` pill all now populate correctly on real CSVs.

### 2. The events file in `out/` is SDWA-shaped only

**Status:** Acknowledged, no action needed.

The current `out/violation_events.csv` header has SDWA columns only
(`violation_id`, `violation_category`, `violation_description`,
`contaminant`, `rule_family`, `period_begin`, `period_end`,
`resolved_date`, `status`, …) — no CWA-specific columns like
`parameter`, `limit_value`, `dmr_value`, `exceedance_pct`.

The renderer already handles this: `renderEvents()` picks
`parameter || violation_description || contaminant` for the title and
only shows the limit/measured/exceedance line when CWA fields are
present. So an SDWA-only or mixed-shape CSV both render cleanly.

If the pipeline later writes CWA events into the same CSV, the
renderer will display them correctly without changes.

### 3. CSV parsing is naive but adequate

**Status:** Acknowledged, no action needed.

The CSV parser handles quoted fields with embedded commas and escaped
quotes. It does not handle BOMs, ragged rows, or pathological
Unicode. The input is the user's own pipeline output, not adversarial
data — if EPA outputs ever break our parser, we add a fix when it
happens, not preemptively.

### 4. No persistence

**Status:** By design.

Filters, sort order, and the expanded row reset on every page reload.
Reps run this once per morning; persistence would add storage and
privacy considerations for very little benefit.

### 5z. Pre-violation + active-compliance chip groups (added 2026-06)

The filter bar now carries three chip groups instead of one:

1. **Status** (original) — Unresolved / Addressed / Resolved /
   Archived / NoEvents. AND-semantics across checked chips.
2. **Pre-violation signals** (added with the permit-limits + ATTAINS
   integration). Three chips:
   - Permit covers our chemistry (`tag_treatable_permit`)
   - Discharges to impaired water (`tag_discharges_to_impaired`)
   - Effluent matches impairment cause
     (`tag_impairment_parameter_match`)
3. **Active compliance signals** (added with the DMR archive
   integration). Two chips:
   - Currently exceeding a permit limit (`tag_recent_exceedance`)
   - Exceeding our chemistry parameter
     (`tag_exceeds_treatable_parameter`)

All three groups use the same AND-semantics within group and AND
across groups. None checked in a group = no filter from that
group (additive opt-in, not restrictive). The shared `tagTrue()`
helper accepts both real booleans (seed data) and CSV
`"True"`/`"False"` strings, so the chips work regardless of source.

The detail panel gains three new blocks rendered conditionally:
- `renderPreViolationBlock(r)` — shows treatable permitted
  parameters, matching impairment parameters (bolded if present),
  and downstream impairment causes. Disappears for older CSVs
  without these columns.
- `renderActiveComplianceBlock(r)` — shows worst-single-row
  exceedance (parameter + %), count of exceedance rows, and the
  set of treatable classes exceeded. Bolded + red header when
  `tag_exceeds_treatable_parameter` is True (the strongest
  composite signal). Disappears for rows with no exceedance data.
- `renderSdwaContextBlock(r)` (added 2026-06-08) — SDWA-only.
  Shows the four PWS metadata fields the API path now exposes:
  `population_served` (locale-formatted, e.g. "60,000 people"),
  `system_type`, `owner_type`, `primary_source`. Disappears for
  CWA leads and for bulk-only SDWA leads where ECHO Exporter
  didn't supply the fields. Sits between the compliance snapshot
  and the pre-violation block in the expanded detail panel.

**INT32_MAX special-case render.** When `top_exceedance_pct >=
99,999`, the block renders "≥ 99,999% (limit may be 0)" rather
than the bare number. EPA reports the INT32_MAX sentinel
(2,147,483,647) when a permit's `LIMIT_VALUE` is 0 — the +15
severity tier still applies correctly, but the raw value would
look broken to a sales reader. See parent project's MEMORY.md
Trap 13 for the empirical source.

### 5a. Run Health tab (added 2026-05-26)

The viewer has two tabs now: **Inventory** (the original table view) and
**Run Health** (new). The Health tab surfaces signals a non-technical
sales user would otherwise miss because they live in the terminal log:

- **Coverage gap** — high-score leads (score ≥ 50) where the bulk feed
  didn't supply event detail and the API fine-comb couldn't fill it in.
  Grouped by state, with a copy-pasteable
  `pipeline --states X,Y,Z` command for the top concentrations.
- **Depth gap** — CWA events without per-DMR detail (parameter,
  limit_value, dmr_value). Bulk NPDES files don't carry these; only the
  API path produces them.
- **SDWA gate breadth note** — explains why bulk SDWA inventory is
  small (tight gate) and offers the API command for a richer territory
  cut.
- **All-resolved cluster** — leads with `tag_only_resolved_events=True`.
- **API fine-comb stats** — candidates queued, events recovered, still
  missing after retries.
- **Run warnings** — every WARNING the pipeline emitted (bot-blocks,
  throttle persistence, drill-down miss summaries).

Data sources:
- `out/run_health.json` (new file written by both `bulk_loader.run_bulk`
  and `pipeline.run` via `chemtreat_water_leads/_health.py`). Schema is
  versioned (`schema_version: 1`); the viewer refuses to render
  unknown versions to avoid silent staleness.
- The currently-loaded `all_leads.csv` and `violation_events.csv` —
  several signals (per-state coverage gap, only-resolved cluster) are
  derived directly from the leads array so they stay in sync with what
  the user filters in the Inventory tab.

A red badge on the Run Health tab counts "things worth attention"
(high-score no-events leads + warnings). A green checkmark badge
appears when health is loaded but nothing's flagged.

### 5b. No diff highlighting from `new_*.csv`

**Status:** Open, future work.

`dump_run` produces `new_facilities.csv` and `new_violations.csv`
alongside the inventory CSVs, and the runs themselves emit
`newly_snc_*.csv` inline. Today the viewer just loads `all_leads.csv`
and shows everything; it doesn't visually distinguish new-since-last-run
rows. The `newly_snc` flag in the seed data demonstrates how a "Newly
SNC" badge would look.

**Intended fix:** accept a `new_facilities.csv` upload alongside
`all_leads.csv`, build a set of `registry_id` values from it, and
flag matching rows in the table with a "NEW" badge plus a quick
filter chip "Only new since last run."

### 6. ECHO links are not verified at load

**Status:** By design.

The `echo_url` field is taken verbatim from the CSV and rendered as a
link. We don't prefetch it or check that the facility page exists. If
EPA ever changes their URL scheme, the pipeline regenerates the
column on the next run, and the viewer follows automatically.

### 7. NAICS / industry context is shown as a code

**Status:** Open, low priority.

Rows show NAICS as a numeric code (e.g., `325211`) rather than the
human-readable industry ("Plastics Material and Resin Mfg"). The
mapping isn't bundled with the viewer because (a) the relevant codes
are already filtered server-side by the pipeline's `TARGET_NAICS`, and
(b) reps tend to know their territory's NAICS by code. If sales asks
for industry names, a small lookup table (~30 entries covering
`TARGET_NAICS`) could be added inline.

### 8. Upstream data inconsistency: score reasons can disagree with the snapshot columns

**Status:** Resolved — closed by the SDW field-name alignment in
`pipeline._flatten_facility`.

Originally observed on a live SDWA row (A & S Water Services, score
87): the `score_reasons` string included `+15: 2 formal enforcement
action(s) in last 5 yr`, but the corresponding compliance-snapshot
column `formal_actions_5yr` was `0`. Same row, same CSV.

Root cause was in the pipeline: `scoring.rule_formal_action` read
`f.get("CWPFormalEaCnt") or f.get("Feas")` (the SDW-program-specific
field name `Feas`), but `pipeline._flatten_facility` was writing
`formal_actions_5yr` from `CWPFormalEaCnt` only — so the scorer and
the CSV writer disagreed on the same logical value for SDWA rows.

The pipeline's flatten step now uses the same `pick("CWPFormalEaCnt",
"Feas")` chain the scorer uses, plus matching alignment on
`quarters_in_violation` (`CWPQtrsWithNC` / `QtrsWithVio`),
`quarters_in_snc` (`CWPQtrsWithSNC` / `QtrsWithSNC`),
`informal_actions_5yr` (`CWPInformalEnfActCount` / `Ifea`), and
`snc_status` (`CWPSNCStatus` / `SNC`). Several latent versions of the
same bug went with it.

Viewer is unchanged — it was rendering the CSV faithfully all along.

### 9. Seed data is fictional; ECHO links are disabled on demo rows

**Status:** Acknowledged, no action needed.

The 18 facilities in `SEED_LEADS` are entirely made up — names like
"Permian Basin Chemical Works" and registry IDs like `110000412345`
follow EPA's formatting conventions but don't correspond to real
facilities. The `echo_url` values are valid ECHO URLs structurally,
but the IDs aren't in EPA's database, so the page loads and shows
"facility not found."

Rather than swap in real facilities (which would invite reps to
treat the demo as actionable), the viewer takes a simpler path:

- A purple `demo-banner` sits below the lag banner whenever seed
  data is loaded, stating "Demo data — not real facilities" and
  pointing to the Upload CSV button.
- The **Open in EPA ECHO** button in each row's detail panel is
  disabled with a tooltip when demo data is active.
- The banner and the disabled state are both controlled by an
  `isDemoData` flag in `setLeads(...)`. Real CSV uploads pass
  `isDemo=false`, hide the banner, and re-enable the links.

This keeps demos honest without inventing real-looking leads sales
might accidentally act on.

### 10. No print stylesheet, no mobile layout

**Status:** Acknowledged, partial fix in place.

There's a coarse breakpoint at 1100px that stacks tiles and the detail
panel. Below ~700px the table is unusable; sales is expected to view
this on a laptop. Print would dump the full table without expanded
detail panels — not ideal but not broken.

---

## What we explicitly did NOT build

These came up during planning and were declined to keep the viewer
focused:

- **A backend.** Adding Flask/FastAPI to serve CSVs and persist
  filters would 5x the code and add deployment complexity. The
  pipeline already writes CSVs to disk; the viewer reads them
  client-side.
- **ML scoring or "smart" sort.** README.md is explicit: don't replace
  the rule-based scorer with ML. Same applies to the viewer — no
  re-ranking, no clustering, no "you might also like."
- **HubSpot/Salesforce sync.** MEMORY.md says the user explicitly
  declined this. Keep it that way.
- **Email digest from this viewer.** That's the pipeline's job, not
  the viewer's. The viewer is for ad-hoc browsing.
- **Editing.** The viewer is read-only. There's no "mark as
  contacted" or "add note" because that belongs in CRM, not in a
  static HTML file.

---

## Maintenance notes

- The viewer maps columns by **exact** name from the pipeline's CSV
  output. If `pipeline.py` adds or renames a column, update the
  references in `renderDetail()`, `setLeads()`, and the upload
  detection in the `csvUpload` change handler. A quick way to find
  them is `grep -n "lead_score\|score_reasons\|registry_id" index.html`.
- The seed data should stay structurally identical to the real CSV
  shape so swap-in remains seamless. If a column is added to the
  pipeline output, add it to every entry in `SEED_LEADS` as well.
- CSS uses CSS variables at the top of `<style>` for all colors. To
  rebrand or adjust contrast, edit the `:root` block, not individual
  rules.
