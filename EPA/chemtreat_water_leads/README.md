# ChemTreat Water-Violation Lead Generator

A small Python package that pulls EPA water-violation data, scores
facilities for ChemTreat sales relevance, and tells you what's new since
the last run. Designed to be auditable: every score comes with reasons,
every diff is reproducible from the SQLite snapshot.

---

## What this is (and isn't)

**It is:** a daily-cadence lead-discovery tool that surfaces industrial
facilities and public water systems that just got into trouble with EPA
over a water-related issue.

**It isn't:** a substitute for sales judgment. EPA data has known
reporting lags and quirks (see *Caveats* below). The score is a
prioritization aid, not a verdict.

---

## Data sources

All data comes from **EPA's ECHO REST API** at `echodata.epa.gov`. ECHO
is the public-facing front end to several internal EPA systems:

| Endpoint we call | Backed by | What we get |
|---|---|---|
| `cwa_rest_services.get_facilities` | ICIS-NPDES | Industrial wastewater dischargers, with current-quarter compliance flags |
| `sdw_rest_services.get_systems` | SDWIS-Fed | Public water systems with violations |
| `eff_rest_services.get_effluent_chart` | ICIS-NPDES DMRs | *Individual* effluent violation events: pollutant, limit, measured value, exceedance % |
| `dfr_rest_services.get_dfr` | ICIS / SDWIS / FRS composite | Facility detail used for SDWA event drill-down |

No API key required. Rate limit is informal — we sleep 0.3–0.5s
between calls. EPA does throttle the DFR endpoint under rapid-fire
load (manifesting as 200 responses with a stub `Results` dict);
`fetch_sdwa_violation_events` detects throttled stubs by response-key
density and retries once with a 2-second backoff. A small share of
state-wide CWA queries (LA, OH) occasionally return empty without a
`QueryID` under pressure too; that fallback is on the TODO list.

---

## Pipeline (`pipeline.run`)

```
┌─────────────────────┐    ┌───────────────────┐    ┌──────────────────────┐
│ 1. Find violators   │ -> │ 2. Score & rank   │ -> │ 3. Drill into events │
│    (per state +     │    │   (explainable     │    │   (high-score        │
│     NAICS prefix)   │    │    rule sum)      │    │    facilities only)  │
└─────────────────────┘    └───────────────────┘    └──────────────────────┘
                                                              │
                                                              v
┌─────────────────────┐    ┌───────────────────┐    ┌──────────────────────┐
│ 6. Write CSV output │ <- │ 5. Update DB      │ <- │ 4. Diff vs snapshot  │
└─────────────────────┘    └───────────────────┘    └──────────────────────┘
```

### Step 1 — Find violators

We loop over (state, NAICS-prefix) pairs and ask ECHO for active CWA
facilities matching `p_st=<state>`, `p_act=Y`, and the NAICS prefix
(`p_ncs=<naics>` — confusingly named, this is the **NAICS** filter, not
a non-compliance filter). For SDWA, we ask for *active systems with
violations* using `p_viola=Y`.

EPA returns the result set in two patterns depending on size: a
self-contained array for small queries, or a `QueryID` + paginated
`get_qid` workflow for state-sized queries. `echo_client._qid_workflow`
handles both transparently, requesting the compliance columns we need
via the `qcolumns` parameter (column-ID translation comes from each
service's `.metadata` endpoint, fetched once per process and cached).

After the response comes back, a client-side compliance gate
(`_has_cwa_compliance_signal`) keeps only the rows showing some open
non-compliance signal — SNC text, quarters in non-compliance, formal
action count, or a `V`/`S` in the 13-quarter compliance history string.
EPA's server-side compliance filters are individually narrow (one
violation type each, no OR-combinator); the client-side gate captures
the broader "in any kind of trouble" set sales actually wants.

Filtering by NAICS server-side keeps the result focused on ChemTreat's
target industries. Edit `TARGET_NAICS` in `pipeline.py` to adjust.

See `MEMORY.md` for the field-name traps in this layer — `p_ncs`,
`qcolumns` column-IDs, the `CWPViolStatus` Yes/No flag, the
`WaterSystems` SDWA pagination key, and the `ViolationsEnforcementActions.Sources`
DFR violation array all caused stacked silent failures during the
project. They're fixed; the doc captures the trail.

### Step 2 — Score

`scoring.score_facility(raw, events=None)` returns `(total, reasons)`.
The total is the sum of contributions from individual rules; the
reasons list is a human-readable breakdown like:

```
Score 142:
  +40: Significant Non-Complier (SNC)
  +32: 6 quarter(s) in non-compliance
  +15: 1 formal enforcement action(s) in last 5 yr
  +25: 5 open violation event(s)
  +20: 1 active Treatment Technique violation(s)
  +10: 1 active Lead/Copper Rule violation(s)
```

The breakdown lands in the `score_reasons` CSV column so a rep can
look at any row and see *why* it ranks where it does. Negative
contributions are allowed and rendered with the sign — the
`rule_only_resolved_demote` rule subtracts 30 when every drilled event
is Resolved/Archived (the "do not call" case), so resolved-only
facilities sort below genuinely-open leads instead of mixing in at the
top.

Scoring runs in two phases:

1. **Facility-only pass**, during `_flatten_facility`, reads only the
   summary columns ECHO returned (SNC text, quarter counts, formal
   action counts, …). This score decides which facilities get drilled
   for individual violation events.
2. **Event-aware re-score**, after drill-down, re-runs `score_facility`
   with the events list and replaces the initial score and reasons.
   The event rules contribute Treatment Technique / MCL / Lead-Copper
   boosts and the do-not-call demote.

There's no `MAX_SCORE` cap — flattening the top of the distribution
hid real differentiation (an earlier version had 99 leads tied at 87).
Sales would rather see "this one is a 142" than have a third of the
inventory collapsed onto the same number.

#### Facility rules (`scoring.RULES`)

| Rule | Max points | Rationale |
|---|---|---|
| `rule_significant_violator` | 40 | EPA's "SNC" designation is the strongest single signal — it doesn't trigger on isolated incidents. Matches descriptive text in CWPSNCStatus / SNC, plus the `SNCFlag` / `SeriousViolator` booleans for SDWA. |
| `rule_chronic_violation` | 32 | Quarters-in-non-compliance × 8 (capped). Captures duration. Reads CWPQtrsWithNC / QtrsWithVio. |
| `rule_formal_action` | 15 | Formal enforcement = legal obligation to remediate. Real budget exists. Reads CWPFormalEaCnt / Feas. |
| `rule_major_facility` | 10 | "Major" NPDES permits = high flow / high pollutant load = larger ChemTreat opportunity. CWA-only signal. |
| `rule_recent_penalty` | 5–8 | Tiered by penalty size. CWA-only (SDW has no per-system penalty amount). |
| `rule_recent_inspection` | 5 | EPA actively watching = facility under time pressure. |

#### Event rules (`scoring.EVENT_RULES`)

These run only after the drill-down and only when individual events
are available. They turn the score into something status-aware instead
of just a sum of summary flags.

| Rule | Points | Rationale |
|---|---|---|
| `rule_active_open_events` | 5/event, cap 25 | Each Unaddressed/Unresolved/Open event is a current opportunity. |
| `rule_treatment_technique_active` | 20 | Per MEMORY.md, Treatment Technique violations are the single highest-relevance category for ChemTreat — what their chemistry products fix. Only counts events with status not in {Resolved, Archived}. |
| `rule_health_based_mcl_active` | 15 | Active MCL violations are health-based, high urgency, ChemTreat-treatable. |
| `rule_lead_copper_active` | 10 (or 5) | Lead-and-Copper Rule = specific corrosion-control opportunity. Falls back to the facility-level PbViol/CuViol/LeadAndCopperViol flags at 5 points when no events match. |
| `rule_only_resolved_demote` | **−30** | If every drilled event is Resolved/Archived, demote so the row sorts below genuinely-open leads. |

To tune: edit the rule functions or add new ones to `RULES` /
`EVENT_RULES`. There's intentionally no ML here — interpretability
matters more than a few points of AUC on a marketing dataset.

#### Tag columns and outreach posture

In addition to the score, every row gets two view-builders worth of
output for sales-side filtering:

- **Tags** (`scoring.compute_tags`): seven booleans — `tag_active_snc`,
  `tag_treatment_technique`, `tag_mcl_violation`, `tag_lead_copper`,
  `tag_major_facility`, `tag_only_resolved_events`, and the composite
  `tag_chemtreat_high_relevance`. Sales filters in Excel on these to
  pare 7,000 rows into the 50 they want without parsing reason strings.
- **`outreach_posture`** (`scoring.compute_outreach_posture`): one word
  per facility — `active`, `enforcement_underway`, `verify_first`,
  `historical`, or `no_events`. A one-glance "should I call?"
  indicator computed from event statuses.

Both columns are populated in the phase-2 augmentation step after
drill-down, alongside the event-aware re-score.

### Step 3 — Drill into individual events

For facilities scoring ≥ 50 (`EVENT_DRILLDOWN_MIN_SCORE`), we pull
specific violation events. Each program uses a different endpoint and
returns different data shapes.

**CWA — Discharge Monitoring Report exceedances** (`get_effluent_chart`)

Per NPDES permit, last 365 days. Each event includes:

- `parameter` — the pollutant (e.g. "BOD, 5-day", "Total Suspended Solids")
- `limit_value` / `dmr_value` — permitted vs measured
- `exceedance_pct` — how badly over the limit
- `period_end` — when the violation was recorded
- `violation_id` — stable unique ID (used for dedupe & diffing)

CWA events are numeric and time-stamped. "We see you had three BOD
exceedances on outfall 001 between June and August" is a *very*
different sales opener than "we saw you got fined."

**SDWA — Drinking water violations** (`get_dfr`)

Per public water system. The actual violation list lives under
`Results.ViolationsEnforcementActions.Sources[*].Violations` in the DFR
response — and that path was not in any of EPA's documented examples;
the project's stacked silent-failure history (see MEMORY.md) includes
several earlier guesses at where the array lived. Each event already
carries text fields (`FederalRule`, `ContaminantName`,
`ViolationCategoryDesc`, `Status`) so no code-lookup is needed in this
path. The `sdwa_codes.py` module is still used by the bulk loader,
which deals with the coded CSV download instead.

Categories you'll see (from `ViolationCategoryDesc`):

| Category | What it means | ChemTreat relevance |
|---|---|---|
| Maximum Contaminant Level Violation | MCL exceeded | High — actual water-quality issue |
| Treatment Technique Violation | Required treatment process failed | Highest — this is what treatment chemistry fixes |
| Monitoring and Reporting / Monitoring Violation | Failed to sample/test on schedule | Medium — often a process problem leading indicator |
| Reporting Violation | Failed to file results with state | Low — paperwork |
| Other Violation | Catch-all (public-notice rule, etc.) | Low — paperwork |

SDWA events also carry a `Status` field that matters for outreach:

| Status | Meaning | Outreach posture |
|---|---|---|
| `Unaddressed` | Still open | Active opportunity |
| `Addressed` | Formal enforcement underway | Opportunity but constrained — they may already have a vendor |
| `Resolved` | System returned to compliance | **Do not cold-call** — they fixed it |
| `Archived` | Closed by EPA; no longer counted against the system. **Not** a synonym for "old" — EPA archives recently-resolved violations too, so dates here are frequently recent. | Treat like `Resolved` — verify, don't cold-call |

The pipeline aggregates these into the `outreach_posture` column on
each lead row (see Step 2). EPA's DFR endpoint sometimes returns a
stub response under rate-pressure (a `Results` dict with only a few
keys); `fetch_sdwa_violation_events` detects this by key density and
retries once with a 2-second backoff.

### Step 4 — Diff against snapshot

`snapshot.py` stores everything in a SQLite file. On each run we
compare current state to the DB and emit four change sets:

| Diff | Meaning |
|---|---|
| `new_facilities_*.csv` | Facility appeared in violator results for the first time |
| `newly_snc_*.csv` | Existing facility just crossed into Significant Non-Complier |
| `new_violations_*.csv` | New individual DMR violation event |
| (in-DB) newly_resolved | Violation moved from Unresolved → Resolved (sales should pause outreach) |

Score increases of >10 points are also tagged on the all-leads output.

### Step 5–6 — Update DB, write CSVs

Each run writes into its own subfolder of `--out`, named
`<command>_<scope>_<YYYYMMDD-HHMMSS>` (e.g.
`out/bulk_nationwide_20260527-090000/`), so runs never overwrite each
other — a targeted `pipeline` run leaves a prior nationwide `bulk` run's
files intact. The folder path is logged at the end of the run. See
`RATIONALE.md` ("Per-run output folders") for why. Three primary outputs
per run, inside that folder:

- `all_leads.csv` — full ranked inventory. Columns include the score
  and breakdown, the `outreach_posture` string, the seven `tag_*`
  booleans, normalized facility identity / location / NAICS, the SNC
  text and dates, quarter counts, formal action counts, total
  penalties, the 13-quarter compliance history, and an `echo_url`
  pointing back at EPA's facility page.
- `violation_events.csv` — the underlying DMR exceedances (CWA) and
  per-violation rows from the SDWA DFR. Includes status, period dates,
  the resolved date, federal/state MCL where applicable, and a
  `data_lag_note` so a row read in isolation still carries the caveat.
- `new_*.csv` — the daily changeset (`new_facilities`, `newly_snc`,
  `new_violations`). This is what the sales team should actually look
  at each morning — the standing inventory has dozens of inches of
  scroll, the changeset is the few rows that matter today.
- `run_health.json` — structured snapshot of the run (totals, drill-down
  stats, per-state coverage gaps, warnings). The viewer's Run Health
  tab consumes this to surface signals (terminal warnings, coverage
  gaps, suggested follow-up commands) to non-technical readers who
  never look at the log. One per run folder.

A separate viewer at `../chemtreat_water_leads_viewer/index.html` is a
single-page HTML app for browsing these CSVs. It reads
`outreach_posture` directly and renders the status pills, "do not
call" affordance, and filter chips off it. See
`chemtreat_water_leads_viewer/RATIONALE.md` for design notes.

---

## ⚠️ Reporting lag — read this carefully

**EPA water data is not real-time.** This is the single most important
thing for sales to understand before using these outputs.

| Program | Typical lag | Cause |
|---|---|---|
| **SDWA** (drinking water) | **~90 days** | EPA: violation and enforcement data are reported quarterly to the federal system *no later than the quarter following the quarter in which events occur*. States and EPA use this extra quarter to verify data accuracy. |
| **CWA** (wastewater) | **~30–45 days** | Facilities file Discharge Monitoring Reports monthly. Very recent activity (last 30 days) is incomplete. |

What this means in practice:

- A "newly seen" SDWA violation in today's diff may actually have happened
  4 months ago. The facility may have already returned to compliance.
- A rep cold-calling about an SDWA violation should verify current status
  *before* the call — the `status` field on each event row (Resolved /
  Addressed / Unresolved / Archived) is the most important column.
- CWA data is timelier but still backward-looking. Treat the pipeline as
  a *prioritization* tool, not a breaking-news feed.

To make this hard to miss, the lag is surfaced in **four places**:

1. **Console banner** printed at the start and end of every run.
2. **`data_lag_note` column** on every event row in the violation CSV.
3. **`READ_ME_FIRST.txt`** written to the output directory each run.
4. **This README section.**

## Other caveats — things to tell your team

1. **"Primacy" creates inconsistencies.** Most states implement SDWA
   themselves and report to EPA. State-by-state data completeness
   varies; EPA itself acknowledges underreporting in some states.

2. **The SNC flag is a quarterly recomputation.** A facility can be
   tagged SNC, fix things, and drop the flag within a quarter. The
   `newly_snc` diff is the cleanest "fresh signal" we have.

3. **The score is heuristic.** It is intentionally simple and
   inspectable. Don't treat it as more precise than it is — a 78 vs an
   82 is noise; a 78 vs a 40 is signal.

4. **Outreach posture matters.** This data is fully public, but a cold
   "we saw you got fined" email reads as ambulance-chasing. The intended
   use is *territory prioritization* and *account research* — the rep
   walks into existing meetings already knowing the facility has a
   chronic cooling-tower exceedance, not blasting violation notices at
   strangers.

---

## Running it

Quick example:

```bash
pip install requests
python -m chemtreat_water_leads.pipeline \
    --states TX,LA,OH,PA,WV --out ./out --db ./snapshot.sqlite
```

For nationwide / large-territory runs use the bulk loader instead:

```bash
python -m chemtreat_water_leads.bulk_loader \
    --out ./out --db ./snapshot.sqlite --cache ./cache
```

The bulk path emits one row per (facility, program) — a facility that
trips both CWA and SDWA signals appears as two rows with the same
`registry_id` but different `program` values. Event joins fall back
to NPDES_ID (CWA) or PWSID (SDWA) when REGISTRY_ID is blank on bulk
violation rows (which is the common case). `--no-events` makes the
run fully offline — zero EPA API calls, zero event-zip downloads.

See `RATIONALE.md` for the design choices behind per-program shapes,
the three-trigger drill-down candidate set, and the SQLite-as-source-
of-truth contract.

See **`COMMANDS.md`** for the full command reference with realistic
time estimates, first-run vs later-run differences, daily-cron patterns,
and ad-hoc SQLite queries for inspecting the snapshot.

First run will write everything as "new" (no prior snapshot exists).
Subsequent runs produce useful diffs.

---

## File map

```
chemtreat_water_leads/
├── __init__.py
├── echo_client.py    # ECHO REST API client — one place to change endpoints
├── scoring.py        # Rules, tags, outreach_posture — edit here to tune
├── sdwa_codes.py     # SDWA reference code lookups (bulk-loader path)
├── snapshot.py       # SQLite diff/state — extend schema as new fields land
├── pipeline.py       # API-based orchestration (regional / state pulls)
├── bulk_loader.py    # CSV-based orchestration (nationwide pulls)
├── _health.py        # Run-health JSON writer + WarningCollector log handler
├── README.md         # Methodology — you are here
├── STARTING_GUIDE.md # First-time guide for sales-facing users
├── COMMANDS.md       # Practical run patterns & time estimates
├── DIAGRAM.md        # State map of bulk vs API depths
├── MEMORY.md         # Field-name traps & the silent-failure trail
├── RATIONALE.md      # Design decisions behind the bulk path
└── TODO.md           # Scoring/output follow-ups (D–G from the assessment)

../chemtreat_water_leads_viewer/
├── index.html        # Single-page CSV viewer
└── RATIONALE.md      # Viewer design notes & gap list
```

Tests live at `EPA/tests/`. Run with:

```bash
cd EPA && ../.venv/bin/python -m unittest discover -s tests -t .
```

**Read MEMORY.md before editing `echo_client.py`.** It documents ten
silent-failure layers EPA's API led us through (NAICS-filter naming,
qcolumns mechanics, response-shape variation, the `CWPViolStatus`
Yes/No flag, the SDW `WaterSystems` pagination key, the DFR
`ViolationsEnforcementActions.Sources` path, etc.). Every one of them
looked like a working filter for a while.

---

## Extending it

`TODO.md` tracks the concrete scoring/output follow-ups from the
methodology assessment:

- **D. Externalize rule weights** — replace inline `return 40, ...`
  literals with a `WEIGHTS` dict (or YAML). Sales feedback like
  "weight SNC less, TT more" becomes a config change instead of a code
  review.
- **E. Expose dropped facility metadata** — `PopulationServedCount`,
  `PWSTypeDesc`, `OwnerDesc`, `PrimarySourceDesc` come back in the SDWA
  response but aren't on the CSV. Population served is a direct revenue
  proxy. Add the columns and a `rule_population_served` rule.
- **F. Per-rule strength bands** — `(points, reason, strength ∈ {HIGH,
  MEDIUM, LOW})` and a `signal_strength_breakdown` column.
- **G. Persist score components** — one column per rule
  (`score_snc`, `score_chronic`, …) so sales can pivot on individual
  contributions instead of parsing the reason string.

Plus three open issues outside the scoring layer:

- **Retry-on-empty for state-wide queries** — LA/OH CWA queries
  sometimes return empty without a QID under rapid-fire pacing. The
  DFR-retry pattern in `fetch_sdwa_violation_events` could be lifted
  to `_qid_workflow`.
- **`EVENT_DRILLDOWN_MIN_SCORE` tuning** — currently 50, with no CWA
  leads reaching it (top CWA score in the latest TX run was 47). Either
  drop the threshold for CWA or split per-program thresholds.
- **Email digest of `new_*.csv`** — pandas + SMTP, ~30 lines.

For a longer running list (scaffolding, ideas not scored above), open
`TODO.md`. For the EPA-API gotcha history, see `MEMORY.md`.
