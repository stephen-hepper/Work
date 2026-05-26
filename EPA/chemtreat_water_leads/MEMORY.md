# MEMORY.md

For future Claude (or any developer) picking up this project. Things I
wish I'd known on day one. Read this before touching `echo_client.py`.

---

## Project at a glance

**What:** Lead-generation tool for ChemTreat's sales team. Pulls EPA
water-violation data, scores facilities for sales relevance, and produces
daily diffs of new violators.

**Who:** ChemTreat is a water-treatment chemicals company. Their
customers are industrial facilities with cooling towers, boilers, process
water, wastewater systems — power gen, refining, chemical mfg, food &
beverage, paper, primary metals, etc. They sell coagulants, biocides,
corrosion inhibitors, scale control, etc. The sales team uses violation
data to prioritize accounts — *not* to cold-call with "we saw you got
fined." Outreach posture matters; ambulance-chasing reads badly.

**The user's setup:** project at `Work/EPA/chemtreat_water_leads/`,
Python 3, running on mobile/laptop. I don't have network access to
echo.epa.gov from my sandbox, so I've never been able to verify the
final API code myself — every fix has been mediated through the user
running commands and pasting output. That's been most of the friction in
this project.

---

## Architecture

```
chemtreat_water_leads/
├── echo_client.py    HTTP layer for EPA's ECHO REST API
├── scoring.py        Explainable lead-scoring rules
├── sdwa_codes.py     Reference code translations (SDWA codes → human names)
├── snapshot.py       SQLite diff/state (so we can emit "new" violations daily)
├── pipeline.py       Per-state/regional orchestration via API
├── bulk_loader.py    Nationwide orchestration via EPA's bulk CSV downloads
├── README.md         Methodology + caveats
└── COMMANDS.md       Practical "how to run" reference with time estimates
```

The split between `pipeline.py` (API) and `bulk_loader.py` (CSV download)
exists because the per-state API loop is slow for nationwide runs.
Sub-15-state territories → use the API. Bigger → use bulk download
(~10–20 min for nationwide vs. 1–3 hours via API).

---

## EPA's ECHO API — the things that will bite you

ECHO's docs are technically thorough but the parameter names are
confusingly close, the error behavior is silent, and the response shapes
vary across endpoints in undocumented ways. **Assume nothing.** Verify
empirically by hitting the API and dumping raw responses (the
`inspect_cwa_response` helper in `echo_client.py` exists for this).

### Trap 1: `p_ncs` is the NAICS filter, not a non-compliance filter

This one cost us multiple iterations. The naming is brutal:
- `p_ncs` → **NaicS** filter. Pass a NAICS prefix like `"325"`.
- `p_naics` → **does not exist.** Silently ignored.
- There is no single parameter that means "currently in non-compliance."

I assumed `p_ncs=Y` was a non-compliance filter (it looks like
"Non-Compliance Status"). The API silently accepted it and returned every
active CWA facility in the state, ignoring both my `p_ncs=Y` and
`p_naics=325`. The bug looked like a working filter for a long time.

**Lesson:** EPA silently ignores unrecognized parameters and unrecognized
parameter values. There's no validation error. The only way to detect
this is sanity-checking result counts and adding client-side safety
filters.

### Trap 2: The two-step QID workflow is required for state-sized queries

ECHO has two query patterns:

1. **Self-contained:** `get_facility_info` — one HTTP call, returns the
   facility array inline. Only works for small queries (bounded by
   geography, etc.).
2. **Two-step QID:** `get_facilities` → `get_qid` — first call returns
   a QueryID + row count; second call paginates the actual facilities.

For state-sized queries (TX, NAICS 325, currently in violation), EPA
returns the QID pattern because the result set is too big for one
response. If you only do step 1, you get a "Success" response with a
`QueryID` field and **no facility array** — which looks like an empty
result if you're not expecting it.

**`echo_client._qid_workflow` handles this.** It accepts both inline
arrays (small queries) and QID responses (large queries). Don't change
this unless you understand why both branches are there.

### Trap 3: `get_qid` returns a minimal column set by default

This was the *third* fix to the same query. After the QID workflow
worked, we were getting facility names and locations but NO compliance
fields — no `CWASNC`, no `CWAQtrsWithNC`, no `CWAFormalActionCount`.

The default column set from `get_qid` is about 20 fields, mostly
identity/location. To get the compliance fields you need for scoring,
you must pass `qcolumns` — a comma-separated list of column **ID
numbers** (not names).

The mapping from name → ID comes from `<service>.metadata`. ECHO doesn't
publish a stable ID list anywhere I could find, so we discover it at
runtime, cache it, and translate our wanted column names to IDs before
calling `get_qid`. Implementation is in `_get_service_columns` and
`_build_qcolumns` at the top of `echo_client.py`.

**Lesson:** the "default" response from EPA's APIs is whatever minimal
subset their UI happened to need. Anything beyond identity/location
requires explicit `qcolumns`.

### Trap 4: Field-name prefixes vary by endpoint and program

Same logical field has different names depending on which response it's
coming from:

| Concept       | CWA `get_qid` fields | SDWA fields | Generic |
|---|---|---|---|
| Facility name | `CWPName`            | `PWSName`   | `FacName` |
| City          | `CWPCity`            | `PWSCity`   | `FacCity` |
| State         | `CWPState`           | `PWSState`  | `FacState` |
| ZIP           | `CWPZip`             | `PWSZip`    | `FacZip` |
| NAICS         | `CWPNAICSCodes`      | (n/a)       | `FacNAICSCodes` |
| SNC flag      | `CWASNC` / `CWASNCFlag` | `SDWASNC` | — |

CWP = "Clean Water Permit." PWS = "Public Water System." Fac = generic.

The `_flatten_facility` function in `pipeline.py` uses a `pick(...)`
helper that checks all three naming conventions for each logical field.
If you add a new field, follow the same pattern.

### Trap 5: Response shape varies — defensive parsing is required

Across endpoints we've seen the facility array under any of:
- `Results.Facilities`
- `Results.FacilityInfo`
- `Results.Systems`
- `Results.DFRSections[type=SDWA].Violations`
- `Results.DrinkingWaterViolations`

The unwrap logic in `_qid_workflow` and `fetch_sdwa_violation_events`
tries each in order. **Never assume a single shape.** When you see an
unexpected empty result, the first thing to check is whether the array
is under a different key than you're looking at.

### Trap 6: The reporting lag is real and not a bug

EPA data is NOT real-time:
- **SDWA: ~90 days.** Quarterly federal reporting, with one extra
  quarter for state QA. A violation from this week won't appear for ~3
  months. EPA's own wording: "violation and enforcement data are
  reported quarterly to the data system of record no later than the
  quarter following the quarter in which the events occur."
- **CWA DMR: ~30–45 days.** Monthly DMRs filed after the monitoring
  period closes.

This is the single most important thing for sales to understand —
otherwise reps will cold-call about "fresh" violations that resolved
months ago. We surface the lag in **four places** (CLI banner,
`data_lag_note` column on every event row, `READ_ME_FIRST.txt` in the
output dir, README section). If you ever simplify this, keep at least
two of the four. The duplication is on purpose.

---

## Sales relevance details (don't forget these)

### Target NAICS for ChemTreat (`TARGET_NAICS` in `pipeline.py`):

Power gen (2211), food (311), beverage (312), paper (322), petroleum/coal
products (324), chemical mfg (325), nonmetallic minerals (327), primary
metals (331), fabricated metals (332), machinery (333), transportation
equipment (336), hospitals (622), oil & gas extraction (2111), mining
(212). These are prefixes — EPA does prefix-match server-side.

Edit `TARGET_NAICS` if sales gives you feedback. It's a marketing
decision, not a technical one.

### Scoring rules (`scoring.RULES` + `scoring.EVENT_RULES`):

Hand-curated, intentionally simple, fully explainable. Each rule returns
`(points, reason_string)` or `None`. The total score lands in the CSV
along with a `score_reasons` column — sales can audit any score.

Two rule families. Facility rules run on every lead (cheap, summary
fields from `get_qid`). Event rules run after the high-score drill-down
and inspect the actual violation events.

**Facility rules** (`RULES`):
- SNC flag: 40 (strongest single signal)
- Quarters in non-compliance: 8 each, capped at 32
- Formal enforcement action: 15
- Major-permit facility: 10
- Recent penalty: 5–8
- Recent inspection: 5

**Event rules** (`EVENT_RULES`, applied to facilities scoring ≥50
after they've been drilled):
- Open events (Unaddressed/Unresolved): 5 each, capped at 25
- Active Treatment Technique violation: 20 (single highest-relevance
  category for ChemTreat — what their chemistry fixes)
- Active MCL violation: 15
- Active Lead/Copper Rule: 10 (event-level) or 5 (facility-flag fallback)
- All-resolved demote: **−30** (if a facility has events but every one is
  Resolved/Archived, push it well below actively-open peers so sales
  doesn't accidentally cold-call about a fixed issue)

**No `MAX_SCORE` cap as of 2026-05-21.** The previous 100-point ceiling
collapsed the top of the distribution (99 facilities tied at 87 in a TX
run). Removing it lets genuine outliers stand out — top score on a
fresh TX+VA+LA run was 142, with only 5 leads above 100. Theoretical
max ≈ 180 if every facility + event rule fires. Viewer's color tiers
(`scoreClass` in `index.html`) reflect this: ≥110 = outlier (star
badge), ≥80 = red, ≥60 = orange, ≥40 = yellow.

**Don't replace these with ML.** Sales needs to be able to look at a row
and say "this is a 142 because…." Interpretability matters more than
marginal AUC improvement.

### SDWA violation categories (in `sdwa_codes.py`)

| Category | Sales relevance |
|---|---|
| MCL (Maximum Contaminant Level) | High — health-based |
| TreatmentTechnique | **Highest** — what ChemTreat chemistry fixes |
| Monitoring | Medium — often leads to process problems |
| Reporting | Low — paperwork |
| PublicNotification | Low — paperwork |

### SDWA status field (don't skip this)

| Status | Outreach posture |
|---|---|
| Unresolved | Active opportunity |
| Addressed | Formal action underway; constrained but valid |
| Resolved | **Do not call.** They fixed it. |
| Archived | Stale (>5 yr); ignore |

---

## Verification: is it actually working?

After any change to `echo_client.py`, run this in order:

```bash
# 1. Syntax check
cd /path/to/Work/EPA
python3 -c "import ast, pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('chemtreat_water_leads').rglob('*.py')]"

# 2. Quick TX query - should return ~30-80 facilities with real names
python3 -c "
from chemtreat_water_leads import echo_client
results = echo_client.find_cwa_violators('TX', '325')
print(f'Got {len(results)} facilities')
for r in results[:5]:
    name = r.get('FacName') or r.get('CWPName')
    snc = r.get('CWASNC') or '-'
    qiv = r.get('CWAQtrsWithNC') or '-'
    print(f'  {name:40} snc={snc} qiv={qiv}')
"

# 3. If something looks off, dump a full record
python3 -c "
from chemtreat_water_leads.echo_client import inspect_cwa_response
inspect_cwa_response('TX', '325')
"
```

**Sniff test on counts:** for TX + NAICS 325 (chemical mfg):
- 0–5 → plausible, but verify by dumping a record (current observed: 1)
- 100–200 with all SNC fields empty → CWPViolStatus="No" is being mistreated
  as free text (silent-failure layer #7) — re-check `_has_cwa_compliance_signal`
- Thousands → server-side NAICS filter is being ignored again
- 70,000+ → no filtering at all (this was our zero-day state)

For broader sanity, **TX with no NAICS filter** should return ~3,000–5,000
facilities (currently 3,950), with `CWPSNCStatus` showing descriptive
strings like `"Effluent - Monthly Average Limit"` for the populated ones.

**Critical:** Always sanity-check that the SNC/QtrsWithNC fields actually
have values. If they're all `-` or `None`, the column metadata
discovery isn't working and you'll be filtering on fail-open logic,
which produces meaningless results.

---

## Anti-patterns I tried and abandoned

1. **Using `get_facility_info` as self-contained.** Works for tiny
   queries; returns QID for anything state-sized. Don't.
2. **Filtering by `p_ncs=Y` for non-compliance.** That's the NAICS filter
   set to NAICS code "Y" (nothing).
3. **Filtering by `p_e90_count=1 + p_e90_years=3` server-side.** Too
   narrow — only catches effluent exceedances, misses everything else.
   The `severe_only=True` kwarg in `find_cwa_violators` is the optional
   way to apply this if a user really wants only the strictest set.
4. **Fail-open client-side filter** ("if no CWA fields, keep the row").
   This let 166 chemical-mfg facilities pass through with zero compliance
   data because the qcolumns issue meant nothing arrived in the response.
   We still have the fail-open clause but it's no longer the dominant
   path now that qcolumns is wired up. **If you change the column
   discovery code, re-verify this isn't quietly back to fail-open.**
5. **Trusting EPA docs over empirical testing.** I built the first
   version from documented API patterns without testing. The docs are
   close-enough but not exact; field names and response shapes vary.

---

## Bulk loader: the nationwide path

`bulk_loader.py` downloads three EPA files (cached locally for 7 days
since that matches EPA's weekly refresh cadence):

| File | Size | Contains |
|---|---|---|
| `echo_exporter.zip` | ~250 MB | 1.5M facilities × 130+ columns |
| `npdes_downloads.zip` | ~80 MB | Individual NPDES violations |
| `SDWA_latest_downloads.zip` | ~40 MB | Individual SDWA violations |

URLs hardcoded at top of `bulk_loader.py`. **Verify against
<https://echo.epa.gov/tools/data-downloads> if a download fails** — EPA
occasionally renames these.

The bulk path uses different column names than the API (UNDERSCORE_CASE
instead of CamelCase). The `_bulk_to_api_shape()` function maps them so
the same `scoring.score_facility()` function works for both. Don't break
this mapping — the scorer is downstream of both paths.

Stream-parsing with `csv.DictReader` is intentional. The ECHO Exporter
is 250 MB unzipped; loading into pandas takes ~2 GB RAM. Stream-parse
keeps us under 100 MB and runs in similar time.

---

## SQLite snapshot — what it's for and what NOT to do

`snapshot.sqlite` is **the source of truth** for everything the CSVs
publish. Two roles, one file:

1. **Diff engine.** Each run compares current state to the DB and
   emits `new_facilities_*.csv`, `newly_snc_*.csv`,
   `new_violations_*.csv` — the deltas sales opens each morning.
2. **Standing inventory.** Every column in `all_leads.csv` and
   `violation_events.csv` lives in the DB. At end of run, those two
   CSVs are produced by SELECTing from the DB (filtered to rows whose
   `last_seen` matches the current run's start timestamp), not from
   in-memory pipeline state.

**Critical rule: do not delete `snapshot.sqlite` between runs.** Two
things break if you do:
- Diff baseline resets — every facility looks "new" again.
- The standing-inventory CSVs are empty until the next pipeline run
  completes a full territory scan. Anyone who opens `all_leads.csv`
  in the interim will see nothing.

The cron pattern in `COMMANDS.md` preserves the DB path across runs.

**Schema** lives in `snapshot.py` as two ordered dicts
(`FAC_COLUMNS`, `VIOL_COLUMNS`) that double as the migration source
and the CSV column order. To add a column: append to the relevant
dict. On next `open_db()`, `_migrate(conn)` runs `ALTER TABLE … ADD
COLUMN` for any column not already present in the live DB.
Idempotent on fresh and legacy DBs.

Tables:
- `facilities` PK `(registry_id, program)` — ~38 columns, every CSV
  field + `first_seen`/`last_seen` bookkeeping + legacy `snc_flag`
  (retained for diff comparisons).
- `violations` PK `violation_id` — ~29 columns, union of CWA-shaped
  (parameter, limit/dmr values, exceedance_pct, npdes_id, stat_basis)
  and SDWA-shaped (violation_code, contaminant, rule_family, etc.).
- `runs` — run history for auditing.

**Behavioral note.** Violations without a `violation_id` are silently
dropped (cannot dedupe across runs without an ID). This was true
before the refactor too; flagged for visibility. If sales reports
missing rows, the fix is to synthesize an ID upstream in the
event-fetch step, not paper over it in the dump.

**Concurrency.** Two runs against the same DB at the same time would
interleave `last_seen` updates and corrupt the dump filter. Runs must
be serial. Cron serializes by default; ad-hoc users should not run
two pipelines side-by-side.

---

## User communication notes

- They asked for explainability in code and methodology. Honor that.
  Comments explain *why*, not what.
- They're on mobile sometimes. Keep responses focused, with a clear
  "what to do next" at the end.
- When I've made mistakes (which has been a lot), they appreciate
  direct admission and concrete next steps, not over-apologizing.
- They run commands themselves and paste output back. Give them
  copy-paste-ready commands, and assume each iteration is one round
  trip — so each round needs to produce useful diagnostic output, not
  just a fix that might or might not work.
- Diagnostic helpers (like `inspect_cwa_response`) earn their keep
  ten times over. **Add diagnostic affordances early.** This is the
  biggest meta-lesson of the project.

---

## Open issues and TODOs

1. **Bulk loader URLs are dated.** ECHO renames files occasionally. If
   bulk fails with 404, check `https://echo.epa.gov/tools/data-downloads`
   for current names and update `BULK_URLS`.

2. **SDWA event drill-down via DFR is per-facility-slow.** For nationwide
   territories, the bulk SDWA download is better. The API path stays
   for regional runs but consider thresholds.

3. **No email digest yet.** Sales ops asked about this; `new_*.csv` ->
   SMTP is ~30 lines, but skipped to focus on data accuracy first.

4. **No HubSpot/Salesforce integration.** User explicitly said don't
   build this. Keep it that way unless they ask.

5. **Full SDWA code reference is incomplete.** `sdwa_codes.py` bundles
   ~30 common codes; for exhaustive coverage download
   `SDWA_REF_CODE_VALUES.csv` and either replace the inline dicts with
   a CSV reader or extend them with the missing codes.

6. **Run cadence is not yet wired to anything.** `COMMANDS.md` has a
   cron pattern but nothing is scheduled. That's the user's call.

## Resolved during build

**Metadata endpoint key.** The metadata response array lives under
`Results.ResultColumns`, not the keys I originally guessed
(`ColumnData`, `ColumnSummary`, etc). Each entry has `ObjectName` for
the field name and `ColumnID` for the numeric ID. The CWA service uses
`cwa_rest_services.metadata` (NOT `get_metadata` — that 500s). The All
Data service is at `echo_rest_services.metadata` and uses Fac-prefixed
field names.

**Actual CWA compliance field names** (verified against live metadata).
The prefix is CWP (Clean Water Permit), not CWA. These are the names
to use in `CWA_WANTED_COLUMNS` and downstream:

| What we score | EPA's ObjectName |
|---|---|
| SNC flag (descriptive text, not Y/N) | `CWPSNCStatus` |
| SNC status date | `CWPSNCStatusDate` |
| SNC event description | `CWPSNCEventDesc` |
| Quarters in non-compliance | `CWPQtrsWithNC` |
| Quarters in SNC | `CWPQtrsWithSNC` |
| 13-quarter compliance string | `CWP13qtrsComplHistory` |
| Formal enforcement actions | `CWPFormalEaCnt` |
| Informal enforcement | `CWPInformalEnfActCount` |
| Total penalties ($) | `CWPTotalPenalties` |
| Date of last penalty | `CWPDateLastPenalty` |
| Days since last inspection | `CWPDaysLastInspection` |
| Missing DMR quarters | `MissDMRQtrs` |
| Violation status | `CWPViolStatus` |

**CWPSNCStatus is not a flag.** It carries descriptive text like
"Significant/Category I Noncompliance" or "No Violation Identified".
Match on substring, not equality. The scoring rule
`rule_significant_violator` does this.

**CWPViolStatus IS a flag — Yes/No, not free text.** Looks similar to
CWPSNCStatus and lives one row above it in the wanted-columns list, but
the values are literal `"Yes"` / `"No"`. Treating it as descriptive text
(applying a clean-list like `("NO VIOLATION IDENTIFIED", ...)`) keeps
every `"No"` facility because `"NO"` isn't in the list — which is how
the TX/325 sanity check returned 166 clean facilities even after the
qcolumns work was done. Filter by `viol in ("Y","YES",...)` instead.
This was silent-failure layer #7 on top of the six in this doc.

### SDWA path: three more layers (#8, #9, #10)

The SDWA path was structurally broken end-to-end. Three independent bugs
stacked, each invisible because EPA returned HTTP 200 for every call.

**Layer #8 — `get_qid` returns SDW results under `WaterSystems`, not
`Systems`.** `_qid_workflow`'s unwrap fallback list checked
`Facilities`/`FacilityInfo`/`Systems` — none matched, so every page of
SDW results yielded an empty array. Init reported 7,388 TX rows; pagination
returned 0. Fix is one line: add `WaterSystems` to the fallback list.

**Layer #9 — SDW metadata uses different field names than the CWA
analogue.** SDW's metadata exposes `PWSId` (not `PWSID`), `SNC` (not
`SDWASNC`), `Feas` (not `SDWAFormalActionCount`), `QtrsWithVio`,
`CitiesServed`/`CountiesServed`/`ZipCodesServed` (because a public water
system can span multiple municipalities), and so on. The old
`SDW_WANTED_COLUMNS` list mostly used SDWA-prefixed guesses; of ~14
names, only 2 matched metadata (qcolumns was literally `'1,8'`). The
verified field set is in `SDW_WANTED_COLUMNS` in `echo_client.py` and the
`pick()` chains in `pipeline._flatten_facility`; the scorer falls back to
the SDW names after the CWA names.

**Layer #10 — SDWA DFR violations live under
`Results.ViolationsEnforcementActions.Sources[*].Violations`.**
`fetch_sdwa_violation_events` was looking under
`DrinkingWaterViolations` / `SDWAViolations` / `DFRSDWAViolations` / a
`DFRSections[type=SDWA]` array — none of those keys exist. The actual
list is two levels deep under `ViolationsEnforcementActions.Sources[*]`,
and each violation already carries text fields (`FederalRule`,
`ContaminantName`, `ViolationCategoryDesc`, `Status`) so the
`sdwa_codes` lookup tables aren't needed in this path. Field names:
`ViolationID`, `ViolationCategoryCode`/`ViolationCategoryDesc`,
`FederalRule`, `ContaminantName`,
`NonCompliancePeriodBeginDate`/`EndDate` (an `--->` sentinel means
ongoing), `Status` (Unaddressed / Addressed / Resolved / Archived —
matches the outreach posture table earlier in this doc),
`EnforcementActions` (nested list).

---

## The most important meta-lesson

**EPA's API does not validate inputs and does not error on bad ones.**
A typo in a parameter name silently returns unfiltered data that looks
plausibly right. The only defense is:

1. **Sanity-check counts** against your expectations after every change.
2. **Add client-side safety nets** that re-check the filter logic.
3. **Add diagnostic helpers** that dump raw responses so you can see
   ground truth, not assumed shape.
4. **Test before trusting docs.** The docs are 80% right. The other
   20% is what bites you.

Anything that returns a plausible-looking dataset without verifying the
shape and content is dangerous. We had three layers of silent
failure (`p_ncs=Y`, `p_naics`, missing qcolumns) stacked on top of each
other. Each one alone would have been a 30-minute fix. Stacked, they
took the bulk of this project. Future me: build the diagnostic helper
*first*.