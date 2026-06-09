# Scoring guide

How a lead's `lead_score` is built, why the same facility can have two
different scores during one run, and which command produces which pass.

Companion doc to `README.md` (which lists the rules) and `COMMANDS.md`
(which lists the runs). This file connects them.

---

## TL;DR

Every facility is scored **twice**:

| Pass | Reads | Picks | Output |
|---|---|---|---|
| **1. Facility-only** | EPA's summary flags (SNC text, quarter counts, formal-action counts, …) | Who is worth the slow per-facility drill-down call | A first `lead_score` |
| **2. Event-aware** | The same flags **plus** the individual drilled violation events | The final ranking sales sees | A second `lead_score` that overwrites pass 1 |

Both passes call the same function — `scoring.score_facility(facility,
events=None)` — just with or without `events`. The two-pass design exists
because the drill-down is expensive (one API call per facility); cheap
flag-based scoring decides who's worth that call.

---

## Pass 1 — Facility-only

Computed for **every** lead, from the summary columns EPA returns in the
facility listing (`get_qid` for the API path, ECHO Exporter columns for
bulk). No per-violation detail; just counts and flags.

`scoring.RULES` (11 rules):

| Rule | Points | Fires on |
|---|---|---|
| `rule_significant_violator` | 40 | SNC text on `CWPSNCStatus` / `SNC`, or `SNCFlag`=`Y` |
| `rule_chronic_violation` | 8 × quarters, cap 32 | `CWPQtrsWithNC` / `QtrsWithVio` |
| `rule_formal_action` | 15 | `CWPFormalEaCnt` / `Feas` ≥ 1 |
| `rule_major_facility` | 10 | `CWPPermitTypes` contains "Major" (CWA only) |
| `rule_recent_penalty` | 5 or 8 | `CWPTotalPenalties` ≥ $10K or $100K |
| `rule_recent_inspection` | 5 | `CWPDaysLastInspection` between 0 and 180 |
| `rule_treatable_permit_parameter` | 5/hit, cap 15 | Any `permit_has_*` column populated by `bulk_loader.stream_permit_limits` (npdes_limits.zip). **Bulk-only** — pipeline.run leaves these columns empty so the rule returns None. |
| `rule_discharges_to_impaired` | 10 or 15 | `discharges_to_impaired=1` (any AU impaired) → +10; `matching_impaired_parameters` populated (effluent matches impairment cause) → +15 instead (no double-counting). Bulk-only, from `npdes_attains_downloads.zip`. |
| `rule_recent_dmr_exceedance` | 5/8/10/12/15 | Tiered by `top_exceedance_pct` at thresholds 50 / 100 / 200 / 1000%. Bulk-only, from `npdes_dmrs_fy<YEAR>.zip`. |
| `rule_exceeds_treatable_parameter` | 15 | Composite: any class in `exceeded_treatable_parameters_text` is also in `permit_has_*`. The strongest single signal in the system — "permit covers it AND they're exceeding it." Bulk-only. |
| `rule_population_served` | 4 / 7 / 10 | Tiered by `PopulationServedCount` at 3K / 10K / 50K. Revenue proxy for SDWA — a major utility is a much bigger account than a 200-person mobile-home park. **API-only** (ECHO Exporter doesn't carry PWS metadata; the rule returns None on bulk SDWA rows). |

All weights and tier thresholds live in the `WEIGHTS` dict at the top
of `scoring.py`. Sales asks like "weight SNC less, treatment technique
more" are a one-line edit; no need to touch rule bodies.

The pass-1 score decides who clears `EVENT_DRILLDOWN_MIN_SCORE` (`50`,
defined in `pipeline.py`) and gets drilled. Anyone below the threshold
keeps this score as their final score — they were never drilled.

### How to produce pass 1

Both top-level commands compute pass 1 on every lead they touch. The
difference is **where the summary flags come from**:

```bash
# Cheapest nationwide pass 1 — zero EPA API calls.
# Streams the weekly ECHO Exporter zip, scores every facility, writes
# all_leads.csv. No events drilled.
python -m chemtreat_water_leads.bulk_loader \
    --out ./out --db ./snapshot.sqlite --cache ./cache \
    --no-events

# Pass 1 on a specific territory via the API. Slower per state because
# every (state, NAICS prefix) combo is its own ECHO query, but produces
# the same pass-1 score shape.
python -m chemtreat_water_leads.pipeline \
    --states WA,VA --out ./out --db ./snapshot.sqlite
```

Internally both call `scoring.score_facility(raw)` with `events=None` —
only `RULES` run; `EVENT_RULES` are skipped.

---

## Pass 2 — Event-aware

Runs **only on facilities that cleared the drill-down threshold and got
their violation events fetched**. Adds five rules from `scoring.EVENT_RULES`
(`scoring.py:236`) that read the drilled events themselves, not just the
summary counts:

| Event rule | Points | Fires on |
|---|---|---|
| `rule_active_open_events` | +5 each, cap 25 | events with status Unaddressed / Unresolved / Open |
| `rule_treatment_technique_active` | +20 | event `violation_category` contains "TREATMENT TECHNIQUE" and status is not closed |
| `rule_health_based_mcl_active` | +15 | event `violation_category` contains "MAXIMUM CONTAMINANT" and status is not closed |
| `rule_lead_copper_active` | +10 (or +5 fallback) | event `rule_family` contains "LEAD AND COPPER" (event-level) or `PbViol`/`CuViol`/`LeadAndCopperViol` flags (facility-level fallback) |
| **`rule_only_resolved_demote`** | **−30** | the facility has events **and every one** is Resolved/Archived |

That last rule is the one that makes pass 1 and pass 2 diverge most
sharply. A facility can look bad in pass 1 (`+32 chronic +15 formal +5
inspect = 52`, drilled) but turn out to have every violation closed,
landing at `52 + (−30) = 22` in pass 2 — well below the drill threshold
and well below where sales looks.

The score sales sees in `all_leads.csv` is the pass-2 score. Pass 1
exists only inside the run; it's not persisted as a separate column.
The `score_reasons` column itemizes every rule that fired, with the sign,
so any score is auditable.

### How to produce pass 2

Two sources of events feed pass 2. Both commands invoke pass 2; what
differs is **where the events come from**:

```bash
# Full bulk run. Pass 1 from the ECHO Exporter, then bulk event zips
# (NPDES_SE/PS/CS, SDWA_VIOLATIONS_ENFORCEMENT) load events for matched
# leads. Pass 2 re-scores. Finally, the API fine-comb fallback drills
# leads that scored >=50 but still had no events from the bulk feed
# (or that are newly-discovered / score-jumped), and pass 2 re-runs on
# those. ~15-30 min, 3 downloads (~830 MB).
python -m chemtreat_water_leads.bulk_loader \
    --out ./out --db ./snapshot.sqlite --cache ./cache

# Targeted API run. Pass 1 from the per-state ECHO API queries, then
# pipeline._drill_cwa + pipeline._drill_sdwa drill EVERY high-value lead
# in those states via:
#   eff_rest_services.get_effluent_chart  -> CWA per-DMR events
#   dfr_rest_services.get_dfr             -> SDWA per-violation events
# Pass 2 re-scores. Use this to deepen states where the bulk path left
# leads as outreach_posture=no_events (visible in Run Health).
python -m chemtreat_water_leads.pipeline \
    --states WA,AL,VA,LA,GA --out ./out --db ./snapshot.sqlite
```

Internally, both commands re-call `scoring.score_facility(raw, events)`
on each drilled lead. Pass 2 also recomputes the `outreach_posture`
string and the seven `tag_*` boolean columns; see
`scoring.compute_outreach_posture` and `scoring.compute_tags`.

---

## When the two scores diverge in your output

The Run Health "Drill-down coverage" card and the Inventory tile at the
same threshold can show different counts. They're answering different
questions:

- **Run Health "X of Y high-value leads have event detail"**: `Y` is the
  pass-1 ≥ 50 count — the leads that were **selected for drilling**.
- **Inventory tile at `Min score 50`**: the pass-2 ≥ 50 count — leads
  that are **still worth a rep's attention after the event-aware
  re-scoring**.

The gap = leads that scored ≥ 50 on facility flags, were drilled, and
then got demoted below 50 by `rule_only_resolved_demote`. In a recent
pipeline run on WA/AL/VA/LA/GA: 297 drilled, 11 demoted, 286 final ≥ 50
— and after applying the default Inventory STATUS chips (which hide
`Resolved` + `Archived` postures by default) the visible row count
dropped further to 238. None of this is a bug; each number measures a
different slice. The card subtitle now says "facility-only" to make this
explicit.

---

## Which command to run when

Both commands do both passes. The picking criterion is **where do the
events come from** and **how deep do you need per-event detail**:

| Goal | Run |
|---|---|
| Nationwide facility-only inventory, no event detail | `bulk_loader --no-events` |
| Nationwide standard run (bulk events + API fine-comb for top no-event leads) | `bulk_loader` |
| Deep per-DMR detail for CWA leads on chosen states | `pipeline --states X,Y` |
| Fill a Run Health "lookup failed" gap from a prior run | `pipeline --states <the failed states>` |

Typical cadence: a weekly `bulk_loader` keeps the standing inventory
fresh; targeted `pipeline --states X` runs deepen specific territories
when the Run Health tab flags coverage gaps.

For practical run timing, EPA's throttling behavior, and cron patterns,
see `COMMANDS.md`. For the bulk vs. API design trade-offs (per-program
shapes, drill-down triggers, source-of-truth contract), see
`RATIONALE.md`. For the silent-failure history that shaped the API
client and discovery logic, see `MEMORY.md`.
