# Sewer Overflow / Bypass Event Integration

Implementation plan for the Tier-1 #4 add from
`EXTERNAL_DATA_STATUS.md` — sewer-overflow and bypass events from EPA's
NPDES eRule Phase 2 feed. This is the **only daily-cadence** signal in
the project; everything else carries the 30–90d reporting lag
documented on every viewer and run banner.

Schema and distribution numbers below are pinned against the live
`current_sewer_overflow_and_collection_systems_tables.zip` refresh of
2026-06-15 (915 KB compressed, 4,221 events, 608 distinct permits, 15
states/territories reporting). If you re-pick this work after a long
gap, re-verify by re-running the header dump in "First concrete step"
below — EPA renames columns occasionally (MEMORY.md trap #1).

---

## Goal

Add an active-compliance signal that fires on **recent sewer overflow
and bypass events** at POTW NPDES permits. Closes the 30–90 day
reporting-lag gap for ChemTreat's highest-revenue segment (POTWs with
biological treatment, disinfection, biosolids). The pre-violation
permit-limit / ATTAINS work shifted *which* leads got drilled; this
shifts *how fresh* the signal can be.

Bulk-only signal — the API path (`pipeline.run`) doesn't consume it,
same convention as `permit_has_*` / `top_exceedance_pct`.

---

## Datasets — two feeds, complementary

| # | Feed | URL | Cadence | Role |
|---|---|---|---|---|
| 1 | **Sewer Overflow & Bypass Events** | `https://echo.epa.gov/files/echodownloads/current_sewer_overflow_and_collection_systems_tables.zip` | **Daily** | Active-compliance + collection-system enrollment. The headline integration. |
| 2 | **National CSO Inventory** | `https://echo.epa.gov/files/echodownloads/ALL_CSO_downloads.zip` | Weekly | Supplemental: outfall-level CSO inventory covering 649 permits the eRule data hasn't onboarded yet. |

### Feed #1: nine CSVs in one 915 KB zip

The events zip ships with nine CSVs and an ERD PDF. We read three of
them in v1; the rest are deferred until sales asks.

| CSV | Used? | Why |
|---|---|---|
| `sewer_overflow_bypass_report_events.csv` | yes | One row per event — backbone. 39 columns including PK, permit, dates, volume, wet-weather. |
| `sewer_overflow_bypass_types.csv` | yes | Event-type join (event_key → CSO/SSO/BYP). One-to-many — an event can carry multiple type codes. |
| `collection_system_permits.csv` | yes | 4,036 permits' collection-system enrollment data (CSS percent, population). Lives in this zip even though it's static. |
| `sewer_overflow_bypass_causes.csv` | no (v2) | Per-event cause codes. Defer until sales asks "show me SSOs caused by pump-station failure." |
| `sewer_overflow_bypass_impacts.csv` | no (v2) | Per-event impact codes. Defer. |
| `sewer_overflow_bypass_corrective_actions.csv` | no | Per-event corrective actions. Defer indefinitely. |
| `sewer_overflow_bypass_receiving_waters.csv` | no | Waterbody names. Defer. |
| `sewer_overflow_treatment_codes.csv` | no | Per-event treatment-equipment codes. Defer. |
| `sewer_overflow_bypass_columns_metadata.csv` | no | EPA's own column-doc dump. Useful for re-verifying schema; not loaded at runtime. |

### Events CSV — the 8 columns we read

```
permit_identifier                                   # join key = NPDES_ID
sewer_overflow_bypass_event_key                     # stable PK (use as violation_id)
sewer_overflow_bypass_start_datetime                # event start (for window filter)
sewer_overflow_bypass_end_datetime                  # event end
sewer_overflow_bypass_discharge_volume_gallons      # volume (sparse — 70% populated)
wet_weather_occurance_indicator                     # Y/N — key tier dimension
sewer_overflow_structure_type_desc                  # human-readable (pump station / manhole / OTH)
collection_system_population                        # facility revenue proxy
```

### Types CSV — the join

```
sewer_overflow_bypass_event_key                     # JOIN
sewer_overflow_bypass_type_code                     # CSO / SSO / BYP
sewer_overflow_bypass_type_code_sequence            # 1, 2, ... — for multi-type events
```

### Collection-system permits CSV — the CSS / population signal

```
permit_identifier
percent_collection_system_css                       # 0=sanitary-only, 100=fully combined
collection_system_population                        # POTW revenue proxy
collection_system_owner_type_desc                   # Municipality, Privately Owned, ...
```

### Feed #2 — National CSO Inventory

`ALL_CSO_downloads.zip` → single `ALL_CSO_DOWNLOADS.csv`, 300 KB. 40
columns; we want two:

```
NPDES_ID                                            # join key
FACILITY_TYPE_INDICATOR                             # POTW vs NON-POTW
```

Roll up: `{npdes_id: {has_combined_sewer_system: 1, is_potw: 0/1}}`.
Covers 896 permits, 649 of which aren't in the events-zip CSS data
(older permits, states still onboarding). Used purely to set
`tag_combined_sewer_system` for the wider universe.

---

## Live distribution (2026-06-15 refresh)

Pinned numbers to justify the tier cutoffs and to set
expectations on hit rates for the first nationwide run.

### Coverage — eRule rollout is sparse

- **15 states/territories** reporting events: IL, GA, PR, AZ, RI, SD,
  NH, MS, NE, DC, NN, AK, KY, WA, VI. Top three (IL, GA, PR) account
  for >65% of events.
- **608 distinct permits** with at least one event. Of ~80K NPDES
  permits, this is <1% — but every non-empty row is a high-confidence
  POTW-segment lead.
- Reporting began **March 2025** under the eRule Phase 2; states roll
  on as their primacy programs onboard. Expect this number to climb
  quarterly.

### Event-type mix

- **SSO: 83%** (3,522 of 4,221) — sanitary sewer overflows, broadly
  required to be reported.
- **CSO: 9%** (375) — combined sewer overflows. Permit-covered at older
  POTWs; reporting required when public health/environment endangered.
- **BYP: 8%** (324) — anticipated/unanticipated treatment bypasses.

### Wet-weather split

- **N (dry-weather): 70%** — treatment-process failure. The alarming
  kind. Strongest sales signal.
- **Y (wet-weather): 30%** — often designed CSO behavior at older
  POTWs. Useful but lower-tier unless volume is large.

### Volume distribution (where reported — 70% of events)

| Percentile | Gallons |
|---|---|
| p50 | 2,400 |
| p90 | 526,000 |
| p99 | 15.6M |
| max | 2.8B |

### Collection-system enrollment

- **4,036 distinct permits** in `collection_system_permits.csv` (broader
  than the 608 with events; the rest have registered the system but
  haven't reported events yet).
- **267 of those** with `percent_collection_system_css > 0` (combined
  sewer systems).
- **Owner-type mix**: Municipality 77%, Privately Owned 9%, State 5%,
  Mixed Public/Private 4%, Tribal 2%, the rest <1% each.
- **Population**: p50 = 1,000 served; p90 = 15,379; max = 2.29M (NYC
  size).

### CSO inventory overlap

- 896 distinct permits in `ALL_CSO_downloads.zip`.
- 247 intersect with `percent_collection_system_css > 0` from feed #1.
- **649 only in inventory** (eRule data hasn't caught them yet) — this
  is the slice feed #2 buys us.

---

## Schema additions (`snapshot.py FAC_COLUMNS`)

Append (auto-migrated via `_migrate`):

```python
# --- Sewer overflow & bypass event signals (current_sewer_overflow_
# and_collection_systems_tables.zip, daily refresh). CWA/POTW-only —
# bulk-only. Empty on SDWA leads and on CWA leads in states not yet
# reporting under the 2025 eRule Phase 2.
"recent_sewer_overflow_count":       "INTEGER",   # events in window (default 365d)
"recent_sewer_overflow_volume_gal":  "REAL",      # sum of populated volumes
"most_recent_sewer_overflow_at":     "TEXT",      # ISO datetime of newest event
"recent_sewer_overflow_types":       "TEXT",      # pipe-joined union (e.g. "SSO" or "CSO | BYP")
"has_dry_weather_overflow":          "INTEGER",   # 1 if any event has wet_weather_occurance_indicator='N'
"tag_recent_sewer_overflow":         "INTEGER",
"tag_recent_sso":                    "INTEGER",
"tag_dry_weather_overflow":          "INTEGER",
# --- Collection-system enrollment (collection_system_permits.csv in
# the events zip + ALL_CSO_downloads.zip). Static; cheap.
"percent_collection_system_css":     "INTEGER",   # 0-100
"collection_system_population":      "INTEGER",
"has_combined_sewer_system":         "INTEGER",   # 1 if css_pct > 0 OR present in CSO inventory
"tag_combined_sewer_system":         "INTEGER",
```

No new VIOL_COLUMNS. The streamer reuses the existing CWA-event shape:

```python
{
    "violation_id": <sewer_overflow_bypass_event_key>,
    "registry_id": <backfilled via permit_to_registry>,
    "permit_id": permit, "npdes_id": permit,
    "program": "CWA",
    "violation_category": "Sewer Overflow / Bypass Event",
    "parameter": "Sewer Overflow" | "Sanitary Sewer Overflow" | "Bypass",
    "dmr_value": <volume_gallons as text>,
    "dmr_unit": "gallons",
    "period_begin": <start_datetime>, "period_end": <end_datetime>,
    "violation_description": <structure_type_desc + wet/dry tag>,
    "status": "Unresolved",
    "data_lag_note": SEWER_LAG_NOTE,
}
```

New constant in `pipeline.py` alongside `CWA_LAG_NOTE` / `SDWA_LAG_NOTE`:

```python
SEWER_LAG_NOTE = (
    "Sewer Overflow / Bypass event reporting began 2025-03 under the "
    "NPDES eRule Phase 2. Coverage varies by state. A facility with "
    "no events here may simply be in a state that hasn't onboarded yet."
)
```

Add `tag_recent_sewer_overflow` and `tag_combined_sewer_system` to the
`tag_chemtreat_high_relevance` composite (OR side, keep
`not tag_only_resolved_events` on the AND side).

---

## Scoring (`scoring.py`)

### New `WEIGHTS` keys

```python
# ---- sewer overflow event tiers (facility rules, bulk-only) -----
"sewer_overflow_severe":           15,    # dry SSO ≥100K gal OR any ≥1M gal
"sewer_overflow_high":             12,    # any SSO OR dry-weather ≥100K gal
"sewer_overflow_moderate":          8,    # any ≥10K gal OR any dry-weather
"sewer_overflow_minor":             5,    # any event in window
"sewer_overflow_window_days":     365,
"sewer_overflow_volume_severe":   1_000_000,
"sewer_overflow_volume_high":       100_000,
"sewer_overflow_volume_moderate":    10_000,
"combined_sewer_system":            5,    # static CSS hit
"collection_system_pop_large":     10,    # ≥50K
"collection_system_pop_medium":     7,    # ≥10K
"collection_system_pop_small":      4,    # ≥ 3K
"collection_system_pop_large_threshold":   50_000,
"collection_system_pop_medium_threshold":  10_000,
"collection_system_pop_small_threshold":    3_000,
```

### Three new facility rules

```python
def rule_recent_sewer_overflow(f: dict):
    """Tiered by worst-event signature in last `sewer_overflow_window_days`.

    The tier ladder bakes in two findings from the live data:
      1. SSO is treatment failure by definition (raw sewage where it
         shouldn't be); CSO is often permit-designed wet-weather
         behavior. So an SSO ranks above a CSO of the same volume.
      2. 70% of events are dry-weather. Dry-weather overflow at any
         volume is more diagnostic than a 1M-gal wet-weather CSO at an
         older POTW.

    Reads `recent_sewer_overflow_count`,
    `recent_sewer_overflow_volume_gal`, `recent_sewer_overflow_types`,
    `has_dry_weather_overflow` — all populated by
    `bulk_loader.stream_sewer_overflow_events`. Bulk-only; pipeline.run
    (API path) doesn't pull this data today."""
    # tier logic — see "Tier ladder" table below for the exact
    # boolean precedence.

def rule_combined_sewer_system(f: dict):
    """Flat +5 for permits operating a combined sewer system. Long-term
    POTW lead identity, independent of recent events. Stacks with
    rule_recent_sewer_overflow when both fire (CSS POTWs are more
    likely to overflow during weather events)."""

def rule_collection_system_population(f: dict):
    """CWA-side revenue proxy for POTW permits — mirrors
    `rule_population_served` (SDWA-only) using
    `collection_system_population` from the eRule collection-system
    rollup. Tiered ≥3K / ≥10K / ≥50K → +4 / +7 / +10."""
```

### Tier ladder for `rule_recent_sewer_overflow`

```
SEVERE  (+15):  has_dry_weather_overflow AND 'SSO' in types AND volume ≥ 100K
                OR any event with volume ≥ 1M
HIGH    (+12):  'SSO' in types
                OR has_dry_weather_overflow AND volume ≥ 100K
MODERATE (+8):  total volume ≥ 10K
                OR has_dry_weather_overflow
MINOR    (+5):  any event in window (CSO/BYP, wet-weather, volume not reported)
```

The fall-through is intentional: a wet-weather CSO with no volume
reported still scores +5 because it's a non-zero signal that the
collection system is overflowing. EPA's volume field is sparse (70%
populated); we don't want unpopulated-volume to silently zero out the
signal.

### Tags

```python
"tag_recent_sewer_overflow":  bool(facility.get("recent_sewer_overflow_count")),
"tag_recent_sso":             "SSO" in str(facility.get("recent_sewer_overflow_types") or ""),
"tag_dry_weather_overflow":   bool(facility.get("has_dry_weather_overflow")),
"tag_combined_sewer_system":  bool(facility.get("has_combined_sewer_system")),
```

Composite update — add `tag_recent_sewer_overflow` to the
`tag_chemtreat_high_relevance` OR-side. Don't add the CSS tag (it's
common enough that adding it would inflate the composite past the
"pare 7K rows to 50" goal).

---

## Pipeline wiring (`bulk_loader.py`)

### `BULK_URLS` additions

```python
"sewer_overflow": "https://echo.epa.gov/files/echodownloads/current_sewer_overflow_and_collection_systems_tables.zip",
"cso_inventory":  "https://echo.epa.gov/files/echodownloads/ALL_CSO_downloads.zip",
```

### New stream readers

```python
def stream_sewer_overflow_events(
    zip_path: Path,
    kept_npdes_permits: set[str],
    permit_to_registry: dict[str, str] | None = None,
    window_days: int = 365,
) -> tuple[dict[str, dict], list[dict]]:
    """Mirrors stream_dmr_exceedances return shape exactly."""

def stream_collection_system_permits(
    zip_path: Path,
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Reads collection_system_permits.csv from the same events zip.
    Returns {permit: {percent_collection_system_css, collection_system_
    population, has_combined_sewer_system}}."""

def stream_cso_inventory(
    zip_path: Path,
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Reads ALL_CSO_DOWNLOADS.csv. Supplements
    stream_collection_system_permits — fills in 649 permits the eRule
    data hasn't onboarded yet. Returns {permit:
    {has_combined_sewer_system: 1}}."""
```

### `run_bulk` insertion point

Drop into the same `if include_events:` block that already runs
permit-limits / ATTAINS / DMR, after the DMR block and **before** the
re-score (`_augment_leads(leads, events=[])`). Same exception isolation
(`try/except log.warning`) so a malformed daily refresh degrades to a
warning instead of killing the run. The events join into the same
`events` list that gets persisted by `snapshot.diff_and_upsert_violations`.

### Per-feed cache age — non-negotiable

The events zip refreshes **daily** but our global `CACHE_MAX_AGE_DAYS=7`
would burn 6 of those days. Lift `_download_cached` to take an optional
`max_age_days` arg (default = `CACHE_MAX_AGE_DAYS`); call with
`max_age_days=1` for `sewer_overflow`. Three-line change.

### `--no-events` gate

Both feeds are network downloads — gate behind `include_events` exactly
like the existing three. Extend `tests/test_no_events_flag.py` to assert
neither URL is hit when `include_events=False`.

### `_health.py` warning

Add a coverage check: if `kept_npdes_permits` has rows in state X but
zero sewer-overflow events nationwide hit a permit in state X, emit a
soft warning under "coverage gaps." Sales-side reads as "your state
isn't reporting yet, not your integration is broken."

---

## Tests

Mirror `tests/test_dmr_exceedance_stream.py` structure. The DMR test
file is the closest sibling — same streamer shape, same fixture
pattern.

| File | Coverage |
|---|---|
| `tests/_fixtures.py` extension | `make_sewer_overflow_zip(tmp_path, events, types, css_permits)` building all 3 CSVs |
| `tests/test_sewer_overflow_stream.py` | filter, type-join, tier math, empty-input short-circuit, missing-CSV-raises, multi-type events, registry-id backfill, sparse-volume handling |
| `tests/test_sewer_overflow_scoring.py` | rule fires per tier, composite tag flips correctly, CSS population rule fires |
| `tests/test_cso_inventory_stream.py` | join filter, POTW indicator, dedup across many outfalls per permit |
| extend `tests/test_no_events_flag.py` | both URLs absent in offline mode |
| extend `tests/test_scoring_via_bulk.py` | end-to-end: facility row + sewer signal → score reflects all of it |

Streamer-test failure modes to pin (lessons from the DMR test file):
1. **Type-code join is one-to-many**. A test event with two type rows
   (SSO + CSO) must show up as `"CSO | SSO"` in the rolled-up types
   field. Sorting matters for stable diffs.
2. **Sparse volume**. A test with `volume = ""` must not crash and
   must not pollute the sum; the count still increments.
3. **EPA column naming**. Pin the actual lowercase column names from
   the live file — `permit_identifier`, not `PERMIT_IDENTIFIER`.
   Test fixtures lock the contract.
4. **`window_days` filter**. Events older than the window are filtered
   out; pass an explicit `window_days` and verify cutoff.
5. **`permit_to_registry` backfill**. An event row whose permit IS in
   the map gets `registry_id`; one whose permit ISN'T gets `None` (NOT
   dropped — snapshot upsert handles missing registry on event rows
   via the npdes_id fallback).

---

## Viewer (`chemtreat_water_leads_viewer/index.html`)

Per `feedback_viewer_testing` — `node --check` only, no jsdom harness.

- **Filter chips**: `Recent sewer overflow`, `Recent SSO`, `Dry-weather overflow`, `Combined sewer system`.
- **Detail panel** for the lead row: surface `most_recent_sewer_overflow_at`, `recent_sewer_overflow_types`, total volume, `percent_collection_system_css`, `collection_system_population`.
- **Lag banner**: add a third sub-bullet under the existing CWA/SDWA notes:
  "Sewer Overflow events: state-by-state rollout since 2025-03 — absent state ≠ no problem."
- Re-run `python -m chemtreat_water_leads_viewer.bake_docs` after editing the embedded markdown.
- Re-tier `scoreClass` thresholds after the first nationwide run. Top
  score post-DMR was 187; adding up to +30 here pushes outliers past
  220. Open TODO.md item "re-tier viewer color thresholds" applies.

---

## Risks & open questions

1. **State-rollout sparseness.** First nationwide run will hit ~1% of
   CWA leads. Verify on first run; if hit rate is genuinely zero,
   investigate the join key (permit-id normalization — leading-zero
   trap from ATTAINS).
2. **Tier cutoffs are derived from one refresh.** Re-check distribution
   after 6 months of accumulated state-rollout to confirm the volume
   percentiles haven't shifted.
3. **Stable event PK.** `sewer_overflow_bypass_event_key` is documented
   as system-generated. Verify it's stable across refreshes (i.e. that
   EPA doesn't renumber on weekly rebuilds). If unstable, synthesize
   `f"SO_{permit}_{start}_{type}"` like the SDWA bulk path does.
4. **Multi-type events.** An event with both SSO + CSO codes should
   probably be classified by the highest-tier code (SSO > CSO). Tier
   logic uses `"SSO" in types` substring check — correct as long as
   we always pipe-join.
5. **Volume sparsity.** 30% of events have no volume reported. The
   tier ladder's minor tier (+5) catches volume-null events so they're
   not silently zeroed — but it does mean a 50M-gal event with `volume
   = ""` scores only +5. Mitigation: structure-type-desc hints at
   "WWTP" / "treatment plant" overflow are usually populated; could
   add a +3 bump for those, but probably overcomplicating.
6. **Score inflation.** Adding up to +30 (severe + CSS + pop_large)
   will shift the distribution. Re-baseline viewer colour tiers
   (already a TODO.md item).
7. **`collection_system_permits.csv` overlap with the inventory feed.**
   Two sources of truth for "is this a CSS permit?". Prefer the
   eRule data when both are available; fall back to the inventory
   otherwise. Simple precedence: events-zip wins.
8. **Briefings package.** The `chemtreat_sales_briefings` candidate
   query picks up score deltas automatically via `score_changed`. No
   new tool needed for v1. A dedicated `recent_sewer_overflows` tool
   would be a nice future LLM-callable, but defer.
9. **POTW NAICS not in `TARGET_NAICS`** (uncovered by the integration
   test). Real POTWs sit under NAICS `2213x` ("Water, Sewage and Other
   Systems") but the current `pipeline.TARGET_NAICS` list focuses on
   industrial customers (`2211`, `311`, `312`, `322`, `324`, `325`,
   `327`, …). So the bulk loader's exporter filter drops POTW leads
   today. Two options: (a) widen TARGET_NAICS to include `2213` —
   straightforward but materially changes the inventory shape since
   POTWs are the biggest CWA permit class; (b) keep the current scope
   and accept that sewer-overflow signal only attaches to industrial
   permits that happen to have a collection system (rare). **Decision
   needed before the first nationwide run** — without (a), the
   integration's headline POTW-segment value isn't realized. Worth
   asking sales whether ChemTreat actively prospects POTWs as
   chemistry customers; if yes, widen.

---

## Out of scope (intentional)

- Per-cause / per-impact filters (`sewer_overflow_bypass_causes.csv`,
  `_impacts.csv`).
- Corrective-action history.
- Receiving-waterbody join with ATTAINS (interesting — SSO into a
  303(d) water = double signal — but a separate cross-feed effort).
- API-path parity. Bulk-only signal; document in `SCORING_GUIDE.md`.
- Dedicated briefings tool. The `score_changed` predicate already
  surfaces shifted leads to the LLM.
- Per-outfall geo display (lat/lon are in the events table; viewer
  doesn't render maps).

---

## Estimated effort

| Piece | Estimate |
|---|---|
| `stream_sewer_overflow_events` + fixture + tests | 1 day |
| `stream_collection_system_permits` + tests | 0.25 day |
| `stream_cso_inventory` + tests | 0.25 day |
| Schema + scoring + tags + WEIGHTS | 0.5 day |
| Run-bulk wiring + per-feed cache age | 0.25 day |
| Viewer chips + banner + bake_docs | 0.5 day |
| Full nationwide run + tier re-baseline + viewer threshold tweak | 0.5 day |

**Total: ~3 days end-to-end.** Bulk of the uncertainty in the first
row — pinning EPA's column-naming quirks against the live file.

---

## First concrete step (done — pins below are live as of 2026-06-15)

Header dump for re-verification:

```bash
cd ~/PycharmProjects/Work/EPA
python3 -c "
import zipfile, csv, io
zf = zipfile.ZipFile('cache/sewer_overflow.zip')
for name in zf.namelist():
    if not name.endswith('.csv'): continue
    with zf.open(name) as fh:
        text = io.TextIOWrapper(fh, encoding='utf-8', errors='replace')
        rdr = csv.reader(text)
        print(f'=== {name} ===')
        print(f'  columns: {next(rdr)}')
"
```

If column names have shifted from what's pinned in this doc, update
both this doc and the streamer constants before any code changes.
