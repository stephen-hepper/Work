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

## Drill-down trigger: three positive gates + one backoff gate

**Decision.** `_drilldown_candidates` selects leads for API fine-comb
drill-down using three independent OR'd POSITIVE triggers, then one
NEGATIVE gate (the backoff window):

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
4. **Backoff gate (added 2026-06-09):** `next_drilldown_eligible_at`
   in the past (or NULL — never drilled). A lead that would otherwise
   be a candidate gets SKIPPED if we drilled it recently and the
   per-outcome backoff hasn't elapsed. See "Per-row drill-down state"
   below for the policy. Closes the rerun loop locally so weekly bulk
   reruns don't re-trip EPA's throttle on yesterday's failed leads.

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
- Each run's CSV snapshot is preserved in its own output folder (see
  below), so a prior run's view is never lost — even when a later run
  covers different territory. The DB holds the full cross-run history;
  each folder holds one run's slice of it.
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

## Per-run output folders (runs don't overwrite each other)

**Decision.** Each run writes its CSV snapshot, `run_health.json`, and
`READ_ME_FIRST.txt` into a fresh subfolder of `--out`, named
`<command>_<scope>_<YYYYMMDD-HHMMSS>` (e.g.
`bulk_nationwide_20260527-090000`, `pipeline_WA-AL-VA-LA-GA_20260527-121500`).
`scope` is the joined state list or `nationwide`. Nothing is written to
the `out/` root. Helper: `pipeline._run_output_dir`, called by both
entry points just after `run_start_ts` is captured.

**Why.** The CSVs are per-run snapshots dumped from the DB filtered to
`last_seen >= run_start_ts`, i.e. only the territory that run touched.
Writing them to fixed filenames in `out/` root meant a targeted
`pipeline --states WA,AL,VA,LA,GA` run (used to add per-DMR depth to a
handful of states) would overwrite the `all_leads.csv` from a prior
nationwide `bulk` run — collapsing a 50-state inventory down to 5 states
in the file the viewer loads. Per-run folders let both coexist: the
nationwide baseline and the deep-dive sit side by side, and the user
chooses which to upload.

**Why not keep a `latest/` copy in `out/` root too.** Considered and
declined — it reintroduces an overwrite target (the whole problem) and
adds a second write path to keep consistent. The end-of-run log line
prints the exact folder, so "where did my files go" is answered without
a stable alias. The DB remains the single source of truth across runs;
the folders are disposable views, now just namespaced by run.

**Scope-name guard.** A state list long enough to make an unwieldy
folder name (>40 chars, i.e. ~13+ states) collapses to `<N>states`.
Nationwide bulk (no `--states`) uses `nationwide`. Seconds-resolution
timestamps make same-scope reruns distinct; two runs within the same
second would collide, which doesn't happen in practice (runs take
minutes).

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
- **Did not change the rule weights** at the time of those refactors.
  Externalization shipped later as TODO.md item D — every numeric
  value now lives in `scoring.WEIGHTS`.

---

## Three signal classes (added 2026-06)

**Decision.** The scoring rules are now organized into three
distinct signal classes — enforcement-status, pre-violation, and
active-compliance — and the viewer mirrors them as separate
filter-chip groups.

**Why this matters.** Pre-2026-06 the only signals we surfaced were
*enforcement-status* — SNC listings, formal actions, paid penalties,
chronic non-compliance. Every score reflected what EPA had already
done. Sales conversations sounded like "we saw you got fined"
(ambulance-chasing, bad).

The three external-data integrations shipped in 2026-06 added two
new signal classes:

- **Pre-violation signals.** Fire on structural state that exists
  independent of current compliance. `rule_treatable_permit_parameter`
  reads what the NPDES permit *covers* (regardless of whether the
  facility is in compliance); `rule_discharges_to_impaired` reads
  whether the receiving water is on 303(d) (regardless of whether
  the facility caused it). Sources: `npdes_limits.zip`,
  `npdes_attains_downloads.zip`.

- **Active-compliance signals.** Fire on per-DMR exceedance detail
  from EPA's fiscal-year DMR archive. `rule_recent_dmr_exceedance`
  tiers by severity (50%/100%/200%/1000% thresholds);
  `rule_exceeds_treatable_parameter` is the composite that requires
  BOTH the permit to cover AND the facility to be exceeding the
  same treatable class. Source: `npdes_dmrs_fy<YEAR>.zip`.

**Conversation tones map to classes.** Each class enables a
different sales-call opener:

| Class | Opener |
|---|---|
| Enforcement-status | "We see you've got a Significant Non-Complier designation — what's the remediation plan?" (after-the-fact) |
| Pre-violation | "We noticed your permit covers phosphorus and the receiving water is on 303(d) for nutrients — your limits will tighten at next renewal" (account research) |
| Active-compliance | "We see you exceeded TSS by 175% last quarter and your permit covers TSS — we make the chemistry that fixes that" (current opportunity) |

The composite `tag_chemtreat_high_relevance` OR-includes signals
from all three classes on the positive side; the do-not-call
guardrail (`tag_only_resolved_events`) still gates it.

---

## DMR archive: per-DMR depth gap closed

**Decision.** `bulk_loader.stream_dmr_exceedances` reads
`npdes_dmrs_fy<YEAR>.zip` and emits BOTH per-permit signal dicts
AND per-row event payloads. The events go into the same `events`
list as `stream_npdes_violations` + `stream_sdwa_violations`,
appended LAST so snapshot's per-`violation_id` upsert lets the
DMR-archive emission overwrite the NPDES_SE emission's empty
parameter fields.

**Why.** Pre-2026-06 the bulk NPDES violation files
(`NPDES_SE/PS/CS_VIOLATIONS.csv`) carried violation codes + dates
but no per-DMR detail (parameter / limit / measured / exceedance %
were all None on bulk-loaded events). The viewer's Run Health card
called this the "depth gap" and offered a per-state API re-run
command as a workaround. Shipping the DMR archive integration
closes the gap nationally in a single bulk pass — no API fine-comb
needed for CWA depth.

**Ordering matters.** DMR signals apply BEFORE the pre-rescore so
the two new rules contribute to drill-down candidate selection.
DMR events append AFTER the existing bulk event feeds so dedup-on-
violation_id resolves correctly. See `_run_bulk_inner` in
`bulk_loader.py` for the exact sequencing.

**EPA spelling typo.** The exceedance column is `EXCEEDENCE_PCT`
(misspelled). EPA's own docs say `EXCEEDANCE_PCT`. The streamer,
the fixtures, and the test all pin the typo deliberately. See
MEMORY.md Trap 12.

**INT32_MAX sentinel.** EPA reports `2,147,483,647` when the
permit's `LIMIT_VALUE` is 0 (zero-discharge parameters). Clamped at
99,999% for display; raw value preserved in event payload for
downstream audit. See MEMORY.md Trap 13.

---

## Fine-comb 429 short-circuit (added 2026-06-02)

**Decision.** `_drill_cwa` and `_drill_sdwa` in `pipeline.py` track
consecutive HTTP 429 responses and break the loop after
`THROTTLE_STREAK_THRESHOLD = 20`. Remaining eligible candidates are
marked `lookup_failed` via `_short_circuit_remaining`, populated in
`run_health.json`, and surfaced in the viewer.

**Why.** The 2026-06-02 nationwide run wedged for ~2 hours when
EPA throttled our IP — every fine-comb call after a ~20-call burst
returned 429, but the per-permit `try/except` kept the loop running
at 1–2 s/call producing zero events while sitting on 177k
bulk-derived events that hadn't yet been persisted. The
short-circuit drops wedge time to ~40 seconds while preserving
correct accounting (failed candidates show up for re-run, not as
"no records on file").

**Why narrow.** Only HTTP 429 increments the streak. Per-facility
network drops, 5xx errors, and `EpaBotBlocked` (which has its own
retry path) all reset the streak. A 429 streak is *EPA's IP-level
throttle is on*; everything else is a single-facility issue and
shouldn't take down the whole drill.

**Threshold of 20.** Empirical: in the 2026-06-02 wedge, 1,700+
consecutive 429s came back with zero successes between them — the
throttle is on or off, not flaky. 20 is comfortably past
"intermittent" without making the user wait too long when the
throttle is real.

---

## Per-row drill-down state (added 2026-06-08/09)

**Decision.** Every facility row in `snapshot.facilities` carries five
operational columns recording the *last* drill attempt against EPA
for that lead: `last_drilldown_attempt_at`, `last_drilldown_outcome`,
`last_drilldown_run_id`, `drilldown_failure_streak`, and
`next_drilldown_eligible_at`. The pipeline writes them from
`_drill_cwa` / `_drill_sdwa` via `_record_drilldown_outcome`; the
backoff math (`pipeline.DRILLDOWN_BACKOFF`) sets the eligibility
timestamp per outcome.

**Why per-row, not just per-run.** Sales-side audit + the rerun loop
both need answers like "when did we last try this facility, and what
did EPA say?" The `runs` + membership tables answer "which runs
touched this row," but not "what was the per-row attempt outcome."
The pre-2026-06-09 design surfaced failed-vs-no-data only in
`run_health.json` (per-run JSON file), which is fine for the viewer's
"verify on ECHO" affordance but useless for a Snowflake task or a
local rerun loop that needs to make decisions row-by-row.

### Backoff policy (`pipeline.DRILLDOWN_BACKOFF` + `LOOKUP_FAILED_BACKOFF_TIERS`)

| Outcome | Backoff | Why this window |
|---|---|---|
| `with_events` | 7 days | Matches EPA's weekly bulk refresh — drilling more often returns the same data |
| `no_data` | 30 days | EPA already confirmed no events on file; re-asking burns quota without earning anything until something *new* lands |
| `lookup_failed`, streak 1-2 | 6 hours | First transient throttle. EPA's bot-block typically clears in minutes; 6h is comfortable margin |
| `lookup_failed`, streak 3-4 | 24 hours | Sustained throttle. Skip the day rather than burn another daily run into an in-progress block |
| `lookup_failed`, streak 5+ | 7 days | Persistent block. Verified 2026-06-10: EPA returns no `Retry-After` and blocks persist across endpoints for >24h. Aligns failed leads with the weekly bulk refresh |

`with_events` and `no_data` come from EPA with a definite answer, so a
flat per-outcome window is right. `lookup_failed` means EPA didn't
answer at all — the right window depends on whether this is a one-off
or sustained, so the policy escalates by `drilldown_failure_streak`.
Both tables live in `pipeline.py`; policy changes are a one-line edit
and propagate naturally to every subsequent drill's
`next_drilldown_eligible_at` write. No DB backfill required.

### Local eligibility gate (`_drilldown_candidates`)

The bulk path's candidate selector reads `next_drilldown_eligible_at`
via `snapshot.load_prior_drilldown_eligibility` and skips any lead
whose backoff hasn't elapsed. Closes the rerun loop **locally** —
weekly bulk reruns no longer re-attempt yesterday's failed leads.
Same logic the Snowflake-side `v_drilldown_eligible` view will run
(`WHERE next_drilldown_eligible_at <= CURRENT_TIMESTAMP()`); both
sides agree on the policy because both read the same column.

**Pipeline (API path) intentionally NOT gated.** `pipeline --states X`
is a targeted tool — the user invoked it expecting to drill
everything in that territory. The backoff gate would silently filter
rows back out and surprise the user. Bulk is the auto-loop primary;
pipeline is the manual override.

### Atomicity of `last_drilldown_run_id`

The drill helpers write only four of the five state columns; the
fifth (`last_drilldown_run_id`) is backfilled by
`snapshot.diff_and_upsert_facilities` from the run_id it already
has. Keeps `record_run` atomic with the upsert block — no need for
the runner to open the DB twice just to mint a run_id before
drilling.

See `SNOWFLAKE_DESIGN.md` for the cross-system contract (eligibility
view, target schema, connector pattern) the Snowflake migration will
inherit from this local design.
