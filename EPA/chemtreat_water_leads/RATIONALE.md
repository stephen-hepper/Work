# RATIONALE.md

Design notes for the lead-generator package. Read alongside MEMORY.md
(field-name traps) and README.md (methodology).

This file focuses on architectural choices that aren't obvious from
reading the code: why bulk emits one row per (facility, program) rather
than one per facility, why the drill-down trigger has three independent
gates, and why `snapshot.sqlite` is structurally the source of truth.

---

## Bulk loader: per-program raw shapes (not one shared dict)

**Decision.** `_bulk_to_program_shapes(row) -> list[(program, raw)]`
returns 0, 1, or 2 program-specific dicts, each carrying *only* that
program's canonical field names.

**Why this matters.** The scoring rules use Python `or` chains for
program-name fallback: `rule_formal_action` reads
`f.get("CWPFormalEaCnt") or f.get("Feas")`. The pre-refactor
`_bulk_to_api_shape` packed both program aliases into one dict:
`{"CWPFormalEaCnt": "0", "Feas": "5"}`. Python's `or` evaluates `"0"
or "5"` as `"0"` (truthy string!), so the SDWA value is never reached.
Every bulk SDWA lead with a clean CWA side was 40+ points light.

Splitting into per-program dicts eliminates the masking class entirely:
the CWA dict has no `Feas` key, so the `or` always falls through to it;
the SDWA dict has no `CWPFormalEaCnt`, same effect in the other
direction.

**Cost.** One ECHO Exporter row can produce 0–2 lead rows now. A
facility tripping both CWA and SDWA signals (rare but real — e.g. a
chemical plant that also operates an on-site water system) emits one
row per program. Downstream dedupe via `(registry_id, program)`
already handled this — that's why the snapshot PK is composite.

---

## Drill-down trigger: three independent gates

**Decision.** `_drilldown_candidates` selects leads for API fine-comb
drill-down using three independent OR'd triggers:

1. `lead_score >= EVENT_DRILLDOWN_MIN_SCORE` (50) — the absolute
   threshold; high-value leads always get per-event detail.
2. `(registry_id, program) not in prior_scores` — newly-discovered
   facilities. The "today's diff" view sales opens each morning
   depends on knowing what each new lead looks like at the event
   level, even if the absolute score is mid-range.
3. `lead_score > prior_scores[key] + 10` — score jumped by more than
   10 points since the prior run. Captures enforcement-trajectory
   changes (a chronic violator just got slapped with a formal action,
   etc.) that matter regardless of absolute score.

Leads with `outreach_posture != "no_events"` (i.e. bulk gave us per-
event detail already) are excluded — no point re-drilling them.

**Why not just the score threshold.** Score-only meant fresh
violations on previously-clean facilities (the diff signal sales
actually opens each morning) had to wait until they accumulated enough
flag points to clear 50 — usually weeks after the violation appeared.
The newly-discovered trigger guarantees one drill per new facility per
run, regardless of how low the initial score is.

**Why not all six handoff triggers literally.** The handoff listed
"active SNC tag, formal action count > 0, current violation flag" as
separate triggers. Each of those contributes ≥15 points to the score
via existing rules — so any facility tripping them is already ≥50
under normal conditions. Adding them as parallel triggers would be
redundant logic with no behavior change. Score-threshold +
newly-discovered + score-jumped is the minimal set that captures every
case the handoff cared about.

**Why the secondary floor (`SECONDARY_DRILLDOWN_MIN_SCORE = 20`).** The
newly-discovered and score-jumped triggers each fire on relative,
diff-driven motivation — they don't care about absolute score. Without
a floor, the very first run against an empty DB would treat every
facility as "newly discovered" and queue 100K+ API drill-down calls,
guaranteeing an EPA bot-block. The floor of 20 captures any lead with
at least one substantive scoring rule fire (1 quarter NC = 8, formal
action = 15, etc.) while excluding the long tail of barely-flagged
facilities sales wouldn't act on regardless of freshness. The
score≥50 threshold is unchanged — high-value leads always drill.

---

## `snapshot.sqlite` as source of truth

**Decision.** Both `pipeline.run` and `bulk_loader.run_bulk` write
through `snapshot.diff_and_upsert_*` and then dump `all_leads.csv` /
`violation_events.csv` from the DB via `snapshot.dump_*_csv`. The
CSVs are disposable views of "what this run touched"
(`last_seen >= run_start_ts`). The DB never deletes — historical rows
persist when they fall out of a run's territory.

**Why this matters.**
- Diff baseline is durable across runs. Deleting the DB resets the
  "what's new today" view to "everything looks new again."
- A user opening `all_leads.csv` between runs sees the prior run's
  view, not an empty file. The CSV is regenerated atomically at end
  of run.
- The schema is single-sourced in `FAC_COLUMNS` / `VIOL_COLUMNS`
  ordered dicts — appending to one of those dicts adds the column to
  the DB schema, the CSV header, and the dump in one edit. No
  parallel definitions to drift.

**Implications for the bulk path:**
- Bulk doesn't have all the columns the API path produces (no
  per-DMR `parameter` / `limit_value` / `dmr_value` / `exceedance_pct`
  in the bulk NPDES violation CSVs — they're left None and render as
  empty cells).
- Bulk SDWA has no quarters-with-vio at the facility level. The
  chronic rule can't fire for SDWA from bulk-only data; it needs
  event data (which the API fine-comb fallback supplies for
  high-value or newly-discovered SDWA leads).
- The CSV dump filter (`last_seen >= run_start_ts`) means a
  `--no-events` run still produces a complete CSV — the facilities
  are written, just without event-level columns.

---

## `--no-events`: truly offline

**Decision.** `--no-events` makes zero EPA API calls and zero
event-zip downloads. Both the bulk event load AND the API fine-comb
fallback are wrapped in a single `if include_events:` block in
`run_bulk`. Asserted by `tests/test_no_events_flag.py`.

**Why it matters.** The use case is air-gapped or rate-limit-sensitive
environments where the operator wants the facility inventory only.
The pre-refactor code accidentally still hit EPA for the fine-comb
fallback even when the user passed `--no-events`. The test pins the
fix so a future refactor can't silently re-introduce the leak.

---

## Bulk NPDES violation file selection

**Decision.** `stream_npdes_violations` reads three sibling files —
`NPDES_SE_VIOLATIONS.csv` (single-event effluent), `NPDES_PS_VIOLATIONS.csv`
(permit-schedule milestones), `NPDES_CS_VIOLATIONS.csv` (compliance-schedule
events) — rather than `NPDES_VIOLATION_ENFORCEMENTS.csv`.

**Why.** The previous selector picked `NPDES_VIOLATION_ENFORCEMENTS.csv`
because it was the first file in the zip matching `"VIOLATION"`. But
that file is a join table between violations and enforcement actions —
its columns are `NPDES_VIOLATION_ID, VIOLATION_CODE, VIOLATION_DESC,
ACTIVITY_ID, ACTIVITY_TYPE_CODE, ACTIVITY_TYPE_DESC, ENF_IDENTIFIER`.
No NPDES_ID, no parameter, no dates. Joining via that file produces no
matches because there's no per-row permit identifier.

The substantive per-event data lives in the three `*_VIOLATIONS.csv`
files. All three share the same column shape for our purposes
(`NPDES_ID`, `NPDES_VIOLATION_ID`, `VIOLATION_CODE`, `VIOLATION_DESC`,
`RNC_DETECTION_CODE`, date columns), so they can be processed in a
single loop and concatenated.

---

## Event joins: REGISTRY_ID with permit/PWSID fallback

**Decision.** Both `stream_npdes_violations` and `stream_sdwa_violations`
match an event when `REGISTRY_ID ∈ kept_registry_ids` OR when the
program's natural identifier (`NPDES_ID` for CWA, `PWSID` for SDWA) is
in the corresponding lookup set. On a fallback match, the lead's
RegistryID is backfilled onto the event before persistence.

**Why.** Bulk violation rows in practice never carry REGISTRY_ID —
verified empirically against `NPDES_SE/PS/CS_VIOLATIONS.csv` and
`SDWA_VIOLATIONS_ENFORCEMENT.csv` headers. Without the permit/PWSID
fallback, the REGISTRY_ID-only filter dropped every bulk event,
silently. The backfill is required because snapshot's `registry_id`
column drives the `events_by_key` join used in phase-2 augmentation.

---

## Run health as a first-class output

**Decision.** Every run writes `out/run_health.json` alongside the
existing CSVs. The viewer's "Run Health" tab consumes it to surface
signals (terminal warnings, per-state coverage gaps, suggested
follow-up commands) to non-technical sales users.

**Why a separate file.** The signals that matter most for "should I
trust this run / what should I run next?" live in the run's terminal
log: API bot-blocks, DFR throttle exhaustion, drill-down miss counts,
the "still no events after retries" warning. Sales doesn't read
terminal logs. The two paths to fix that are:

1. Embed the signals into the existing CSVs as columns (every row
   carries the run's overall warnings).
2. Emit a small structured file the viewer can render as a UI panel.

Option 1 pollutes the per-row schema with run-global metadata and
fights with the diff/dump logic in `snapshot.py`. Option 2 keeps the
shapes clean and gives the viewer something easy to parse. We chose
option 2.

**Why a separate `_health.py` module.** Both `bulk_loader.run_bulk`
and `pipeline.run` need to emit the file. If the helpers lived in
`bulk_loader.py`, `pipeline.py`'s import of them would create an
import cycle (`bulk_loader` already imports constants from
`pipeline`). A small dedicated module breaks the cycle and keeps the
shared logic in one place.

**Schema versioning.** The JSON's top-level `schema_version` is
checked by the viewer's `setHealth()`; unknown versions are refused
rather than silently mis-rendered. When you change the schema
shape, bump `_health.SCHEMA_VERSION` and update the viewer's
`renderHealth()` together.

**What the viewer recomputes vs. trusts.** Two signals (per-state
coverage gap, all-resolved cluster) are recomputed from the currently
loaded `all_leads.csv` so the numbers stay in sync with what the user
filters in the Inventory tab. The rest (drilldown stats, warnings,
totals, depth ratio) are read straight from the JSON — those reflect
the moment-in-time state at end of run and can't be reconstructed
from CSV alone.

---

## What we explicitly did NOT do

- **Did not add quarters-with-vio / Pb-Cu signals to bulk SDWA
  discovery.** Verified empirically — those columns don't exist in
  ECHO Exporter. Adding fictional column reads would silently fail.
- **Did not add a `--no-api-fallback` flag separate from
  `--no-events`.** The handoff suggested it as a possibility; we
  chose the simpler one-flag model. If a future user wants bulk
  events but not API fallback, that's a five-line addition.
- **Did not change the rule weights.** Externalizing weights is on
  TODO.md item D, separate from this refactor.
