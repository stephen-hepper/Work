# Basic Commands

Copy-paste reference for running the lead generator. Each section
includes a rough time estimate and notes on what's different between
the first run (cold) and subsequent runs (warm).

## TL;DR — which command should I use?

| Your situation | Use this | First run | Later runs |
|---|---|---|---|
| Just one or two states | `pipeline` (API) | 1–3 min | 30s–2 min |
| Regional sales territory (3–10 states) | `pipeline` (API) | 5–20 min | 2–8 min |
| Most of the lower 48 (20+ states) | `bulk_loader` | 10–20 min | 3–10 min |
| Nationwide | `bulk_loader` | 10–20 min | 3–10 min |
| Just one city / one permit-holder | `pipeline` with one-state filter, then grep | <1 min | <1 min |

**Rule of thumb:** if your territory is more than ~15 states, the bulk
loader is faster. Below that, the API is fine and gets you slightly
fresher data (weekly vs every-7-days cache).

---

## First-time setup

The project lives under the uv-managed workspace at `Work/`:

```bash
cd ~/PycharmProjects/Work          # workspace with pyproject.toml + uv.lock
uv sync                            # installs requests (only runtime dep)
cd EPA                             # all run commands below assume CWD=EPA
```

If you don't use uv, plain `pip install requests` works too — that's
still the only external dependency.

No API keys, no database setup, no config files. SQLite state lives in
a single file you pass on the command line.

---

## Single-state pull (API)

The fastest way to get a sense of the tool's output. Good for testing
or for a rep who only covers one state.

```bash
python -m chemtreat_water_leads.pipeline --states TX --out ./out
```

**What happens:** queries CWA across 13 NAICS prefixes (Texas has lots
of refining and chemical mfg, so this is the heavy state), then SDWA
once, then drills into individual violation events for any facility
scoring ≥ 50.

**First run:** **~1–3 min.** All facilities are flagged "new" since the
snapshot DB doesn't exist yet. The `new_facilities_*.csv` will equal
`all_leads.csv`.

**Later runs:** **~30 sec – 2 min.** Same network work, but only the
*deltas* end up in the `new_*.csv` files — usually 0–20 rows. This is
what you actually want sales looking at each day.

---

## Regional pull (API) — typical sales territory

```bash
python -m chemtreat_water_leads.pipeline \
    --states TX,LA,OK,AR,MS,AL \
    --out ./out \
    --db ./snapshot.sqlite
```

Six Gulf-region states is a reasonable example. Adjust to your actual
territory.

**First run:** **~5–15 min.** Most of this is `time.sleep(0.4)` between
API calls being polite to EPA. With 6 states × 13 NAICS = 78 CWA
queries + 6 SDWA queries + drill-down for high-value leads, you're
looking at ~250–400 HTTP calls total.

**Later runs:** **~2–8 min.** Same network volume, but the diff against
the snapshot DB filters output to actual new signals.

---

## Larger multi-region pull (API)

```bash
python -m chemtreat_water_leads.pipeline \
    --states TX,LA,OK,AR,MS,AL,GA,FL,SC,NC,VA,WV,OH,PA,KY,TN \
    --out ./out \
    --db ./snapshot.sqlite \
    -v
```

Sixteen states — most of the Southeast + Appalachia. Verbose mode (`-v`)
helps when you want to see API progress.

**First run:** **~20–45 min.** This is roughly the upper end of what
the API is practical for. If your run is regularly slower than this,
switch to `bulk_loader`.

**Later runs:** **~10–20 min.** Networking dominates regardless.

---

## Nationwide pull (bulk — recommended)

```bash
python -m chemtreat_water_leads.bulk_loader \
    --out ./out \
    --db ./snapshot.sqlite \
    --cache ./cache
```

This downloads three EPA bulk files (cached for 7 days, matching EPA's
weekly refresh cadence), stream-filters them, and produces the same
output shape as the API pipeline.

**First run:** **~10–20 min total.**

- Download `echo_exporter.zip` (~250 MB): 1–5 min depending on connection
- Stream-filter 1.5M facility rows: 1–3 min
- Download `npdes_downloads.zip` (~80 MB): 30s–2 min
- Read NPDES violation events: 1–3 min
- Download `SDWA_latest_downloads.zip` (~40 MB): 15s–1 min
- Read SDWA violation events: <1 min

**Later runs (within 7 days):** **~3–10 min.** Zips are cached and
re-used. Only the parse/filter/diff work runs.

**Later runs (after 7 days):** back to first-run timing because the
cache invalidates.

---

## Nationwide bulk, no events — fastest possible scan

If sales only needs the facility list (not per-pollutant detail), skip
the event drill-downs:

```bash
python -m chemtreat_water_leads.bulk_loader \
    --out ./out --db ./snapshot.sqlite --cache ./cache \
    --no-events
```

**First run:** **~5–10 min** (just `echo_exporter.zip`).
**Later runs:** **~1–3 min.**

`--no-events` makes the run fully offline — **zero EPA API calls** and
zero event-zip downloads (no `npdes_downloads.zip`, no
`SDWA_latest_downloads.zip`, no API fine-comb fallback to
`echo.epa.gov`). Use this in air-gapped or rate-limit-sensitive
environments. The facility inventory still lands in
`all_leads.csv` and the snapshot DB; only the per-event detail is
absent.

---

## Nationwide bulk, filtered to specific states

The bulk loader also accepts `--states`. Useful when you already know
the nationwide cache is fresh and just want a regional cut without
re-running the API loop:

```bash
python -m chemtreat_water_leads.bulk_loader \
    --states CA,WA,OR,AZ,NV \
    --cache ./cache --db ./snapshot.sqlite --out ./out
```

**First run:** ~10–15 min (download + filter).
**Later runs:** **~2–4 min** with warm cache.

---

## Daily cron (nightly diffs to a sales mailbox)

```cron
# At 6 AM every morning, refresh the snapshot and copy new_*.csv
# somewhere humans look. We don't pipe to email here — that's a
# separate step you'd wire up to your SMTP server or Slack webhook.
0 6 * * *  cd /opt/chemtreat && \
           python -m chemtreat_water_leads.pipeline \
             --states TX,LA,OK,AR \
             --out /opt/chemtreat/daily/$(date +\%Y\%m\%d) \
             --db  /opt/chemtreat/state/snapshot.sqlite \
             >> /opt/chemtreat/logs/run.log 2>&1
```

**Each run:** 2–8 min (regional API pull, warm DB).

The cron writes one timestamped directory per run, so the
`new_facilities_YYYYMMDD.csv` files form a daily history you can keep
or rotate. The `snapshot.sqlite` is the source of truth across runs —
**don't delete it** unless you want every facility to look "new" again.

---

## Running tests

The project ships with a stdlib `unittest` suite covering the bulk
loader's discovery, scoring, event-join, and `--no-events` paths.

```bash
cd EPA
../.venv/bin/python -m unittest discover -s tests -t .
```

23 tests, runs in well under a second. No network access required —
all fixtures are in-memory zips. Run before any change to
`bulk_loader.py` so regressions show up immediately.

---

## Inspecting the snapshot DB directly

The SQLite file is the audit trail. Useful for ad-hoc questions sales
might ask:

```bash
sqlite3 ./snapshot.sqlite

# How many facilities are we tracking by state?
sqlite> SELECT state, COUNT(*) FROM facilities GROUP BY state ORDER BY 2 DESC;

# Top 20 unresolved leads right now
sqlite> SELECT company, city, state, lead_score
        FROM facilities
        WHERE lead_score >= 60
        ORDER BY lead_score DESC LIMIT 20;

# When was each facility first detected?
sqlite> SELECT company, first_seen FROM facilities
        WHERE state = 'TX' ORDER BY first_seen DESC LIMIT 10;

# Has the lead score for a specific company moved?
sqlite> SELECT company, lead_score, first_seen, last_seen
        FROM facilities WHERE company LIKE '%REFINING%';
```

---

## Resetting / starting over

If you want to wipe history and start fresh (e.g. you changed scoring
rules and want everything re-evaluated):

```bash
rm ./snapshot.sqlite     # next run rebuilds from scratch
```

If you want to keep history but force a re-download of the bulk zips:

```bash
rm -rf ./cache           # forces re-download on next bulk_loader run
```

---

## What output files mean

Each run writes into its own subfolder of the output directory, named
`<command>_<scope>_<YYYYMMDD-HHMMSS>` (e.g.
`out/bulk_nationwide_20260527-090000/` or
`out/pipeline_WA-AL-VA-LA-GA_20260527-121500/`). Runs never overwrite
each other — a targeted `pipeline` run leaves an earlier `bulk` run's
files untouched. The path is printed at the end of each run. Inside that
folder:

| File | Purpose | Updates |
|---|---|---|
| `READ_ME_FIRST.txt` | Lag warning. Open first. | One per run folder |
| `all_leads.csv` | Full ranked inventory of current violators | One per run folder |
| `violation_events.csv` | Underlying individual DMR / SDWA events | One per run folder |
| `run_health.json` | Run metadata + warnings + coverage/depth signals for the viewer's Run Health tab | One per run folder |
| `new_facilities_YYYYMMDD.csv` | Facilities first seen this run | One per run folder |
| `newly_snc_YYYYMMDD.csv` | Facilities that crossed into Significant Non-Complier since last run | One per run folder |
| `new_violations_YYYYMMDD.csv` | Individual new violation events since last run | One per run folder |

The `new_*` files are what sales actually opens each morning. The
`all_leads.csv` is the standing inventory for territory planning.

### Columns sales filters on

`all_leads.csv` carries several columns specifically for slicing in
Excel without parsing the score-reasons string:

| Column | Type | Use |
|---|---|---|
| `lead_score` | int | Total of facility + event rule contributions (uncapped). Sort descending. |
| `score_reasons` | string | Pipe-separated breakdown like `+40: SNC \| +32: 6 quarter(s) ... \| -30: All drilled events Resolved/Archived`. Negative entries demote do-not-call facilities. |
| `outreach_posture` | enum | One word per facility — `active`, `enforcement_underway`, `verify_first`, `historical`, `no_events`. The one-glance "should I call?" indicator. |
| `tag_active_snc` | bool | Facility currently flagged Significant Non-Complier. |
| `tag_treatment_technique` | bool | Active Treatment Technique violation in events (highest ChemTreat-relevance event category). |
| `tag_mcl_violation` | bool | Active MCL violation (health-based). |
| `tag_lead_copper` | bool | Active Lead/Copper Rule violation or facility-level Pb/Cu flag. |
| `tag_major_facility` | bool | NPDES "Major" permit (high flow / high load — CWA only). |
| `tag_only_resolved_events` | bool | Has drilled events, all Resolved/Archived. Sales should verify or skip. |
| `tag_treatable_permit` | bool | NPDES permit covers a ChemTreat-treatable parameter class (phosphorus / ammonia / TSS / BOD / oil-grease / metals / cyanide / chlorine residual / microbiological). Pre-violation signal — bulk-only. |
| `tag_discharges_to_impaired` | bool | Outfall is upstream of a 303(d)-listed impaired waterbody. Bulk-only. |
| `tag_impairment_parameter_match` | bool | Facility's monitored effluent parameter matches a documented cause of the downstream impairment. Strongest pre-violation signal. Bulk-only. |
| `tag_recent_exceedance` | bool | Has at least one DMR exceedance in the loaded fiscal-year archive. Bulk-only. |
| `tag_exceeds_treatable_parameter` | bool | Composite — permit covers AND facility is currently exceeding the same treatable class. **Strongest single signal in the system.** Bulk-only. |
| `tag_chemtreat_high_relevance` | bool | Composite — fires when any of the above positive signals fire AND `tag_only_resolved_events` is False. The "if a rep had one filter, this is it" column. |

Active-compliance columns (populated by the DMR-archive integration,
bulk-only):

| Column | Type | Use |
|---|---|---|
| `top_exceeded_parameter` | string | Parameter name with the worst single exceedance in the loaded FY archive. |
| `top_exceedance_pct` | float | The exceedance % itself, clamped at 99,999 when EPA reports the INT32_MAX sentinel for zero-limit parameters. |
| `exceeded_treatable_parameters_text` | string | Pipe-joined union of ChemTreat-treatable classes seen exceeded for the facility. |
| `recent_dmr_exceedances_count` | int | Total exceedance rows in the FY archive for this permit. |

Pre-violation columns (bulk-only):

| Column | Type | Use |
|---|---|---|
| `permitted_parameters_text` | string | Pipe-joined sample of treatable parameter descriptions on the permit. |
| `permit_has_*` | 0/1 | Per-class boolean (phosphorus / ammonia / TSS / BOD / oil-grease / metals / cyanide / chlorine residual / microbiological). |
| `impairment_causes_text` | string | Pipe-joined union of impairment causes for waterbodies the outfall touches. |
| `matching_impaired_parameters` | string | Monitored effluent parameters that match an impairment cause — populates the strongest pre-violation tag. |

SDWA context columns (API path only — ECHO Exporter doesn't carry PWS
metadata, so bulk SDWA leads leave these empty):

| Column | Type | Use |
|---|---|---|
| `population_served` | int | Number of people the PWS serves. Drives `rule_population_served` (+4 / +7 / +10 at ≥3K / ≥10K / ≥50K) — revenue proxy for SDWA. |
| `system_type` | string | "Community Water System", "Non-Transient Non-Community", etc. |
| `owner_type` | string | "Local Government", "Private", "Federal", etc. |
| `primary_source` | string | "Surface Water", "Ground Water", "Purchased", etc. |

Drill-down operational columns (don't filter sales on these — they
drive the auto-rerun loop, not lead intel):

| Column | Type | Use |
|---|---|---|
| `last_drilldown_attempt_at` | ISO ts | When the API drill last ran for this lead. NULL = never drilled. |
| `last_drilldown_outcome` | enum | `with_events` / `no_data` / `lookup_failed` / NULL. |
| `last_drilldown_run_id` | int | FK to the `runs` table for the attempt — joins back to `runs.run_at` for audit. |
| `drilldown_failure_streak` | int | Consecutive `lookup_failed` count; resets on success/no_data. |
| `next_drilldown_eligible_at` | ISO ts | When this lead is next eligible for re-drill (= attempt_at + the backoff for this outcome). `with_events`/`no_data` are flat (7d / 30d); `lookup_failed` escalates by streak (6h at 1-2, 24h at 3-4, 7d at 5+). `bulk_loader` skips rows whose backoff hasn't elapsed; `pipeline --states X` ignores it (manual override). |

**Rerun cadence implications.** The local bulk_loader is now
self-throttling — you can rerun it as often as you want, and the
backoff gate skips leads whose retry window hasn't elapsed. Sensible
schedules:

- **Weekly nationwide bulk** picks up EPA's weekly refresh and
  naturally triggers ~7-day retries for previously-drilled leads.
- **Daily bulk reruns** catch failed lookups after the throttle window
  elapses. First-failure `lookup_failed` rows clear in 6h (so they're
  back in play the next day); rows that fail repeatedly escalate to 24h
  and then 7d, so a daily cron stops grinding into a sustained block.
  Each daily run is mostly no-op except for the eligible subset.
- **`pipeline --states X` for targeted depth** ignores the backoff
  gate by design — use when you explicitly want to drill everything
  in a territory (e.g. a Run Health "lookup_failed" gap).

The HTML viewer in `../chemtreat_water_leads_viewer/index.html` reads
these columns directly — open it in a browser and use the Upload CSV
button to load `all_leads.csv` (and optionally `violation_events.csv`)
from the run folder you want to view. The viewer shows one run at a
time; to compare a nationwide run against a targeted one, upload each
in turn.

---

## Sanity-check times if yours are way off

If a regional run is taking **much** longer than the estimates above:

- **API rate-limiting:** EPA doesn't publish a hard limit, but very
  large runs sometimes slow down. The 0.4s sleep in `pipeline.py` is
  meant to keep us under the threshold; increase it if you see HTTP
  errors in the log.
- **DFR throttle stubs:** the per-facility DFR endpoint is the
  pickiest. EPA sometimes returns a 200 with a stub `Results` dict
  (only a few keys) instead of the usual ~50; the client detects this
  by key density and retries once with a 2-second backoff
  (see `DFR_RETRY_BACKOFF_SEC` in `echo_client.py`). If you see many
  retries in `-v` output, raise `EVENT_DRILLDOWN_MIN_SCORE` to cut the
  drill-down count or extend the inter-drill sleep.
- **Drill-down volume:** if you have hundreds of high-scoring leads,
  the per-permit `get_effluent_chart` and per-system DFR calls
  dominate. Tune `EVENT_DRILLDOWN_MIN_SCORE` in `pipeline.py` upward
  to limit the count.
- **Network egress:** the bulk loader downloads ~2.2 GB total
  across six files on a first run. Behind a slow corporate firewall
  this can be the bottleneck.
- **HTTP 429 throttle on fine-comb:** EPA's `eff_rest_services` /
  `dfr_rest_services` sometimes rate-limit our IP. The drill loops
  short-circuit after a 20-streak of 429s, marking unattempted
  candidates as `lookup_failed` in `run_health.json`. The viewer's
  Run Health card surfaces a re-run command for the affected
  states. Wait at least 30 minutes before re-running to give the
  rolling window a chance to clear.

If a bulk run is failing entirely with a 404, EPA has probably
renamed a file — check the URLs at
<https://echo.epa.gov/tools/data-downloads> and update `BULK_URLS` in
`bulk_loader.py`.
