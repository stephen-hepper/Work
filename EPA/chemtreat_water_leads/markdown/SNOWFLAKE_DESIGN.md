# Snowflake Design — Hands-off Rerun Loop

The state store for this project is moving from SQLite (`snapshot.sqlite`)
to Snowflake. The Python pipeline stays as the runner but reads
Snowflake to decide what to drill, then writes back when each attempt
finishes. This doc covers the target schema, the eligibility view that
drives the rerun loop, the `snowflake-connector-python` integration in
`pipeline.py`, and the SQLite → Snowflake ETL/CDC sketch.

For acronyms (CWA, SDWA, NPDES, DMR, DFR, ATTAINS, PWS, etc.) see
[`STARTING_GUIDE.md`](STARTING_GUIDE.md). For the per-row drill-down
state contract (column meanings, backoff policy) see
[`snapshot.py`](../snapshot.py) FAC_COLUMNS comments and `pipeline.DRILLDOWN_BACKOFF`.

---

## Architecture

```
┌─────────────────────────┐         ┌────────────────────────┐
│ Snowflake               │         │ Python pipeline        │
│                         │         │ (pipeline.py /         │
│  facilities             │ ◀ reads │  bulk_loader.py)       │
│  violations             │         │                        │
│  runs                   │         │  1. Read eligibility   │
│  run_facility_membership│         │     view → state list  │
│  run_violation_membership│        │  2. Drill (API or bulk)│
│                         │ writes ▶│  3. Write outcomes +   │
│  v_drilldown_eligible   │         │     events back        │
└─────────────────────────┘         └────────────────────────┘
                                              │
                                              ▼
                                    ┌────────────────────────┐
                                    │ Snowflake scheduled    │
                                    │ task (every N hours):  │
                                    │ COUNT(*) FROM eligible │
                                    │ → if > 0, enqueue run  │
                                    └────────────────────────┘
```

Decision plane: **the Python pipeline reads Snowflake state and
decides what to drill.** Snowflake is the state store, not the
scheduler. A separate Snowflake scheduled task (or Airflow / cron /
queue) watches `v_drilldown_eligible` and triggers pipeline runs
when the count crosses a threshold.

---

## Target schema

Direct port of `snapshot.py`'s `FAC_COLUMNS` / `VIOL_COLUMNS` /
`runs` / `run_facility_membership` / `run_violation_membership`. SQLite
type → Snowflake type mapping:

| SQLite | Snowflake | Notes |
|---|---|---|
| `INTEGER` | `NUMBER` (or `NUMBER(10)` for IDs) | |
| `TEXT` | `VARCHAR` (default 16 MB; size unnecessary) | |
| `REAL` | `FLOAT` | |
| ISO timestamp in TEXT | `TIMESTAMP_NTZ(0)` | Pipeline writes ISO strings; Snowflake auto-parses on INSERT |

### `facilities`

```sql
CREATE OR REPLACE TABLE facilities (
    registry_id                       VARCHAR        NOT NULL,
    program                           VARCHAR        NOT NULL,
    lead_score                        NUMBER,
    score_reasons                     VARCHAR,
    outreach_posture                  VARCHAR,
    company                           VARCHAR,
    address                           VARCHAR,
    city                              VARCHAR,
    state                             VARCHAR(4),
    zip                               VARCHAR(10),
    county                            VARCHAR,
    naics                             VARCHAR,
    sic                               VARCHAR,
    population_served                 NUMBER,
    system_type                       VARCHAR,
    owner_type                        VARCHAR,
    primary_source                    VARCHAR,
    permit_id                         VARCHAR,

    -- Compliance snapshot
    snc_status                        VARCHAR,
    snc_status_date                   VARCHAR,
    snc_event                         VARCHAR,
    violation_status                  VARCHAR,
    quarters_in_violation             VARCHAR,
    quarters_in_snc                   VARCHAR,
    compliance_history_13q            VARCHAR,
    formal_actions_5yr                NUMBER,
    informal_actions_5yr              NUMBER,
    total_penalties_usd               FLOAT,
    last_penalty_date                 VARCHAR,
    last_inspection_days_ago          NUMBER,
    missing_dmr_quarters              NUMBER,
    echo_url                          VARCHAR,

    -- Tags (0/1)
    tag_active_snc                    NUMBER(1),
    tag_treatment_technique           NUMBER(1),
    tag_mcl_violation                 NUMBER(1),
    tag_lead_copper                   NUMBER(1),
    tag_major_facility                NUMBER(1),
    tag_only_resolved_events          NUMBER(1),
    tag_chemtreat_high_relevance      NUMBER(1),

    -- Permit-limit pre-violation signals (CWA, bulk-only)
    permit_has_phosphorus             NUMBER(1),
    permit_has_ammonia                NUMBER(1),
    permit_has_tss                    NUMBER(1),
    permit_has_bod                    NUMBER(1),
    permit_has_oil_grease             NUMBER(1),
    permit_has_metals                 NUMBER(1),
    permit_has_cyanide                NUMBER(1),
    permit_has_chlorine_residual      NUMBER(1),
    permit_has_microbiological        NUMBER(1),
    permitted_parameters_text         VARCHAR,

    -- ATTAINS pre-violation signals
    discharges_to_impaired            NUMBER(1),
    impairment_causes_text            VARCHAR,
    matching_impaired_parameters      VARCHAR,

    -- DMR active-compliance signals
    recent_dmr_exceedances_count      NUMBER,
    top_exceeded_parameter            VARCHAR,
    top_exceedance_pct                FLOAT,
    exceeded_treatable_parameters_text VARCHAR,
    tag_treatable_permit              NUMBER(1),
    tag_discharges_to_impaired        NUMBER(1),
    tag_impairment_parameter_match    NUMBER(1),
    tag_recent_exceedance             NUMBER(1),
    tag_exceeds_treatable_parameter   NUMBER(1),

    -- Drill-down operational state (drives the rerun loop)
    last_drilldown_attempt_at         TIMESTAMP_NTZ(0),
    last_drilldown_outcome            VARCHAR,        -- 'with_events' | 'no_data' | 'lookup_failed'
    last_drilldown_run_id             NUMBER,
    drilldown_failure_streak          NUMBER,
    next_drilldown_eligible_at        TIMESTAMP_NTZ(0),

    -- Legacy
    snc_flag                          VARCHAR,

    -- Bookkeeping
    first_seen                        TIMESTAMP_NTZ(0),
    last_seen                         TIMESTAMP_NTZ(0),

    PRIMARY KEY (registry_id, program)
);

-- Filters the eligibility view + viewer / sales-side filters keys on these
CREATE INDEX idx_facilities_score        ON facilities(lead_score);
CREATE INDEX idx_facilities_eligible_at  ON facilities(next_drilldown_eligible_at);
CREATE INDEX idx_facilities_lastseen     ON facilities(last_seen);
```

### `violations`

Same direct port — column list mirrors `snapshot.VIOL_COLUMNS`.
Skipping the full DDL for brevity; pattern is identical.

### `runs` + membership tables

```sql
CREATE OR REPLACE TABLE runs (
    run_id      NUMBER AUTOINCREMENT PRIMARY KEY,
    run_at      TIMESTAMP_NTZ(0),
    notes       VARCHAR
);

CREATE OR REPLACE TABLE run_facility_membership (
    run_id       NUMBER NOT NULL,
    registry_id  VARCHAR NOT NULL,
    program      VARCHAR NOT NULL,
    PRIMARY KEY (run_id, registry_id, program),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE OR REPLACE TABLE run_violation_membership (
    run_id        NUMBER NOT NULL,
    violation_id  VARCHAR NOT NULL,
    PRIMARY KEY (run_id, violation_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
```

---

## The eligibility view

This is the heart of the hands-off rerun loop. The pipeline reads it
to decide what to drill; a Snowflake scheduled task watches its count
to decide when to trigger a run.

```sql
CREATE OR REPLACE VIEW v_drilldown_eligible AS
SELECT
    registry_id,
    program,
    state,
    lead_score,
    last_drilldown_outcome,
    last_drilldown_attempt_at,
    drilldown_failure_streak,
    next_drilldown_eligible_at,
    -- Reason a row appears in the view — drives observability and
    -- lets the pipeline log "drilling 47 high-value, 12 retry, 30 never-attempted"
    CASE
        WHEN last_drilldown_attempt_at IS NULL              THEN 'never_attempted'
        WHEN last_drilldown_outcome = 'lookup_failed'       THEN 'retry_failed'
        WHEN last_drilldown_outcome = 'no_data'             THEN 'refresh_no_data'
        WHEN last_drilldown_outcome = 'with_events'         THEN 'refresh_stale'
        ELSE 'other'
    END AS eligibility_reason
FROM facilities
WHERE lead_score >= 50                       -- EVENT_DRILLDOWN_MIN_SCORE
  AND (
    -- Never attempted — always eligible if score warrants
    next_drilldown_eligible_at IS NULL
    -- Backoff elapsed
    OR next_drilldown_eligible_at <= CURRENT_TIMESTAMP()
  )
  -- Optional escalation cap: give up after N straight failures. The
  -- Python writer already escalates the backoff window itself (6h →
  -- 24h → 7d at streak 1-2 / 3-4 / 5+), so this is a hard "drop off
  -- entirely" cap if you want one. Comment in to enable.
  -- AND COALESCE(drilldown_failure_streak, 0) < 10
;
```

**Why a view, not a stored column.** Backoff policy lives in
`pipeline.DRILLDOWN_BACKOFF` + `LOOKUP_FAILED_BACKOFF_TIERS` and gets
written into `next_drilldown_eligible_at` at write time. The view's
job is just to apply the score gate + the time comparison. Policy
change → re-write the constants in `pipeline.py` and let new runs
propagate the new timestamps; the view doesn't need to know.

### Companion view for monitoring

```sql
CREATE OR REPLACE VIEW v_drilldown_health AS
SELECT
    last_drilldown_outcome,
    COUNT(*)                                       AS rows,
    SUM(CASE WHEN lead_score >= 50 THEN 1 ELSE 0 END) AS rows_high_value,
    AVG(drilldown_failure_streak)                  AS avg_streak,
    MAX(drilldown_failure_streak)                  AS max_streak,
    MIN(last_drilldown_attempt_at)                 AS oldest_attempt,
    MAX(last_drilldown_attempt_at)                 AS newest_attempt
FROM facilities
GROUP BY last_drilldown_outcome
ORDER BY rows DESC;
```

Useful as a Snowflake dashboard / alert source — "max_streak > 5 on
> 100 rows" is a sign EPA is throttling sustainably and a human
should look.

### Companion view for the scheduled task

```sql
CREATE OR REPLACE VIEW v_drilldown_queue_depth AS
SELECT
    eligibility_reason,
    state,
    COUNT(*) AS rows
FROM v_drilldown_eligible
GROUP BY eligibility_reason, state
ORDER BY rows DESC;
```

The scheduled task does `SELECT SUM(rows) FROM v_drilldown_queue_depth`
and triggers a run when the depth crosses a threshold (e.g. > 100).

---

## `snowflake-connector-python` integration in `pipeline.py`

Replace the SQLite-specific calls in `snapshot.py` with a Snowflake
backend. The signatures of the public helpers
(`open_db`, `record_run`, `diff_and_upsert_facilities`,
`diff_and_upsert_violations`, `load_prior_drilldown_state`) stay the
same; the implementation swaps `sqlite3` for `snowflake.connector`.

### Connection

```python
# chemtreat_water_leads/snapshot.py (new branch)
import snowflake.connector
from contextlib import contextmanager

@contextmanager
def open_db(_unused):
    """Snowflake replacement for the SQLite open_db. Returns a Cursor
    via context manager. `_unused` kept positional for back-compat
    with the SQLite path's `db_path` argument."""
    conn = snowflake.connector.connect(
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"],
        schema=os.environ["SNOWFLAKE_SCHEMA"],
        role=os.environ.get("SNOWFLAKE_ROLE"),
    )
    try:
        cur = conn.cursor(snowflake.connector.DictCursor)
        yield cur
        conn.commit()
    finally:
        cur.close()
        conn.close()
```

Use **key-pair auth** for cron / scheduled runs (not password):

```python
import snowflake.connector
from cryptography.hazmat.primitives import serialization
with open(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"], "rb") as f:
    pkey = serialization.load_pem_private_key(f.read(), password=None)
conn = snowflake.connector.connect(
    user=..., account=..., warehouse=..., database=..., schema=...,
    private_key=pkey.private_bytes(...),
)
```

### Reading prior state

```python
# pipeline.run / bulk_loader.run_bulk — top of the run
with snapshot.open_db(None) as cur:
    cur.execute("""
        SELECT registry_id, program, drilldown_failure_streak
        FROM facilities
        WHERE drilldown_failure_streak IS NOT NULL
    """)
    prior_streaks = {
        (r["REGISTRY_ID"], r["PROGRAM"]): r["DRILLDOWN_FAILURE_STREAK"] or 0
        for r in cur
    }
```

Snowflake returns column names UPPERCASE by default — wrap with
`_normalize_row_keys()` helper if the pipeline expects lowercase, or
add `cur.execute('ALTER SESSION SET QUOTED_IDENTIFIERS_IGNORE_CASE = TRUE')`
at connection time.

### Discovering work — the rerun loop

```python
# A new entry point: chemtreat_water_leads.pipeline.run_eligible
def run_eligible(out_dir: Path) -> None:
    """Read v_drilldown_eligible, group by state, fire one targeted
    `pipeline.run` per state with the eligible registry_ids."""
    with snapshot.open_db(None) as cur:
        cur.execute("""
            SELECT state, ARRAY_AGG(registry_id) AS reg_ids
            FROM v_drilldown_eligible
            GROUP BY state
            ORDER BY COUNT(*) DESC
        """)
        by_state = {r["STATE"]: r["REG_IDS"] for r in cur}
    log.info("Eligibility view returned %d states / %d total leads",
             len(by_state), sum(len(v) for v in by_state.values()))
    for state, reg_ids in by_state.items():
        # Hand off to the per-state drill path. Could batch into one
        # call per state, or chunk further if N is huge.
        run([state], out_dir, _db_path_unused=None,
            restrict_to_registry_ids=set(reg_ids))
```

The drill helpers in `pipeline.py` already accept a leads list — no
internal changes needed. Add a `restrict_to_registry_ids` parameter to
`run()` that filters the candidate set after discovery (or, more
efficiently, pass the registry_ids straight to a new targeted
discovery path that hits the API per-registry-id rather than per-state).

### Bulk upsert (rather than per-row)

SQLite uses `INSERT ... ON CONFLICT DO UPDATE`. Snowflake uses `MERGE`:

```python
# snapshot.diff_and_upsert_facilities (Snowflake branch)
def diff_and_upsert_facilities(cur, current, run_id, now=None):
    # Stage rows in a temp table, then MERGE — much faster than
    # row-by-row INSERTs against Snowflake.
    cur.execute("CREATE OR REPLACE TEMPORARY TABLE _stage_facilities LIKE facilities")
    cur.executemany("INSERT INTO _stage_facilities VALUES (...)", rows)
    cur.execute("""
        MERGE INTO facilities f
        USING _stage_facilities s
          ON f.registry_id = s.registry_id AND f.program = s.program
        WHEN MATCHED THEN UPDATE SET <every column except first_seen>
        WHEN NOT MATCHED THEN INSERT (<every column>) VALUES (<every column>)
    """)
    cur.execute("""
        INSERT INTO run_facility_membership (run_id, registry_id, program)
        SELECT %s, registry_id, program FROM _stage_facilities
    """, (run_id,))
```

`executemany()` against Snowflake is fine up to ~10K rows; beyond
that, prefer **PUT + COPY INTO** with a parquet stage for performance.
A typical bulk run touches ~40K facility rows + ~185K violation rows —
that's PUT+COPY territory.

---

## ETL / CDC sketch — SQLite → Snowflake

Two options depending on how long both stores live in parallel.

### Option A — One-shot migration, retire SQLite

```bash
# 1. Export each table from SQLite to TSV (Snowflake's friendliest format)
sqlite3 -header -separator $'\t' snapshot.sqlite \
    "SELECT * FROM facilities" > /tmp/facilities.tsv
sqlite3 -header -separator $'\t' snapshot.sqlite \
    "SELECT * FROM violations" > /tmp/violations.tsv
sqlite3 -header -separator $'\t' snapshot.sqlite \
    "SELECT * FROM runs" > /tmp/runs.tsv
sqlite3 -header -separator $'\t' snapshot.sqlite \
    "SELECT * FROM run_facility_membership" > /tmp/rfm.tsv
sqlite3 -header -separator $'\t' snapshot.sqlite \
    "SELECT * FROM run_violation_membership" > /tmp/rvm.tsv

# 2. PUT into Snowflake stage + COPY into target tables
snowsql -q "PUT file:///tmp/facilities.tsv @~/load/;
            COPY INTO facilities FROM @~/load/facilities.tsv
              FILE_FORMAT = (TYPE='CSV' FIELD_DELIMITER='\t' SKIP_HEADER=1);"
# ...repeat for the other 4 tables...

# 3. Switch the pipeline's `snapshot` module to the Snowflake backend
# 4. Decommission snapshot.sqlite
```

One-shot takes ~5 minutes for our current data volumes (~40K facilities,
~185K violations). Snowflake costs ~$0.01 for the load operation.

### Option B — Dual-write window, gradual cutover

Useful if anyone is actively querying SQLite while the migration ships.

1. Pipeline writes to BOTH backends for N runs (`snapshot.dual_write = True`).
2. Verify Snowflake row counts match SQLite each run.
3. Move sales / Snowflake-side consumers off SQLite.
4. Flip pipeline to Snowflake-only.
5. Keep SQLite around as a cold backup for one more cycle.

Cost: ~2× write load during the dual-write window. ~1 day of runs is
plenty to validate.

### Going forward — incremental sync

Once Snowflake is primary, no CDC needed: the Python pipeline is the
only writer. Membership tables grow ~one row per `(run, touched_row)`
forever; consider a retention policy:

```sql
-- Drop membership rows older than 1 year — the `runs` table itself
-- stays (it's tiny), and last_seen on facilities/violations gives
-- you per-row recency without the per-run grain.
DELETE FROM run_facility_membership
WHERE run_id IN (SELECT run_id FROM runs WHERE run_at < DATEADD('year', -1, CURRENT_TIMESTAMP()));
```

---

## Operational checklist

Before the cutover:

- [ ] Snowflake account + warehouse provisioned (XS or S is plenty for our volumes)
- [ ] Database + schema created; role + grants set
- [ ] Key-pair auth set up for the pipeline runner; private key in the secret store
- [ ] All 5 tables created from the DDL above
- [ ] Both views created (`v_drilldown_eligible`, `v_drilldown_health`, `v_drilldown_queue_depth`)
- [ ] Initial bulk load completed (one-shot or first dual-write)
- [ ] `snowflake-connector-python` added to `pyproject.toml`
- [ ] Connection env vars (or `~/.snowflake/config.toml`) set on the runner
- [ ] First run completes end-to-end; row counts match SQLite within tolerance

After the cutover:

- [ ] Snowflake scheduled task watches `v_drilldown_queue_depth` and
      triggers a pipeline run when total depth > 100 (or whatever
      threshold sales/ops agree on)
- [ ] Snowflake alert on `v_drilldown_health.max_streak > 5` (sustained
      EPA throttle)
- [ ] Dashboard for sales: top-N facilities by `lead_score` filtered to
      `outreach_posture IN ('active', 'enforcement_underway')`
- [ ] Optional: read replica / Snowflake-side webhook to push CSVs into
      a sales tool (replacement for today's `all_leads.csv` download flow)

---

# ▶ OPTIONAL — Raw EPA bulk-file staging

> **This section is opt-in.** The rest of the doc above describes the
> minimum viable Snowflake state (derived `facilities` / `violations` /
> `runs` / membership tables only). Everything below describes an
> *additional* layer: landing the raw EPA bulk-download files in
> Snowflake as-is, alongside the derived layer. Skip this section
> entirely if your use case is satisfied by the rolled-up signals the
> scoring layer produces.

## Why you might add it

- **Audit / provenance.** "Which exact row in this week's `npdes_limits.csv`
  caused `permit_has_phosphorus=1` on facility X?" Today the answer
  requires re-downloading the zip and grepping; landed, it's a JOIN.
- **Replay.** Re-score a 4-month-old snapshot against the current
  scoring rules without re-downloading from EPA (which they may have
  refreshed or renamed since). Today the bulk_loader cache holds only
  the most recent 7-day window.
- **Cross-team consumption.** Other teams / tools (compliance, finance,
  product) can query the raw EPA data in SQL without standing up
  their own ingestion of the same files.
- **Permit-trajectory analytics.** Questions like "which facilities
  had a permit tighten in the last year?" need the full `npdes_limits`
  history, not just the current rolled-up `permit_has_*` flags.

## Why you might skip it

- **Storage cost.** ~13 GB unzipped per weekly snapshot. At 26 weeks
  of history that's ~340 GB raw / ~80 GB compressed in Snowflake.
  Low single-digit dollars per month at Snowflake's storage rates, but
  non-zero.
- **Loading time.** ~3–10 minutes per file, ~6 files per weekly run.
  Adds ~30–60 min to the wall-clock for a full nationwide bulk run.
- **ETL surface area.** Six more tables + a retention task to keep
  current. Each EPA schema change becomes a small maintenance burden
  on the raw side (the derived side is shielded by the bulk-loader
  streamers).
- **Sufficient-as-is.** For pure lead-generation, the derived layer
  already exposes everything sales needs. Raw is for the analytics +
  audit use cases above.

---

## Tables — direct port of the EPA bulk file shapes

One Snowflake table per EPA bulk CSV. Column shape comes from the
file's own header — defined via `INFER_SCHEMA` on first load (see
loader pattern below), so we don't enumerate 130+ columns by hand.

| EPA file → Snowflake table | Approx rows/snapshot | Source zip |
|---|---|---|
| `ECHO_EXPORTER.csv` → `raw_echo_exporter` | ~1.5M | `echo_exporter.zip` |
| `NPDES_SE/PS/CS_VIOLATIONS.csv` → `raw_npdes_violations` | ~5M (3 files unioned at load) | `npdes_downloads.zip` |
| `SDWA_VIOLATIONS_ENFORCEMENT.csv` → `raw_sdwa_violations` | ~5M | `SDWA_latest_downloads.zip` |
| `NPDES_LIMITS.csv` → `raw_npdes_limits` | ~10M | `npdes_limits.zip` |
| `NPDES_ATTAINS_AU_SUMMARIES.csv` → `raw_npdes_attains` | ~1M | `npdes_attains_downloads.zip` |
| `NPDES_DMRS_FY2026.csv` → `raw_npdes_dmrs` (one table; partitioned by FY) | ~5–10M per FY | `npdes_dmrs_fy2026.zip` |

Every raw table carries two bookkeeping columns appended at load time
(not from EPA's header):

| Column | Type | Purpose |
|---|---|---|
| `loaded_at` | `TIMESTAMP_NTZ` | When this row entered Snowflake. Drives the 26-week retention DELETE. |
| `snapshot_tag` | `VARCHAR` | Caller-supplied label (`'2026-wk24'`, `'2026-06-08'`). Lets a query pin a specific weekly snapshot for replay without timestamp math. |

## Loader pattern — direct PUT + COPY from the runner

Runs at the bottom of `bulk_loader._run_bulk_inner`, immediately
after each `_download_cached` call returns the local zip path. Add
a new helper `chemtreat_water_leads/snowflake_raw_loader.py`:

```python
# chemtreat_water_leads/snowflake_raw_loader.py
import io, zipfile
from datetime import datetime
from pathlib import Path
import snowflake.connector

# File-inside-zip name → target Snowflake table.
RAW_FILE_TARGETS = {
    "ECHO_EXPORTER.csv":              "raw_echo_exporter",
    "NPDES_SE_VIOLATIONS.csv":        "raw_npdes_violations",
    "NPDES_PS_VIOLATIONS.csv":        "raw_npdes_violations",
    "NPDES_CS_VIOLATIONS.csv":        "raw_npdes_violations",
    "SDWA_VIOLATIONS_ENFORCEMENT.csv": "raw_sdwa_violations",
    "NPDES_LIMITS.csv":               "raw_npdes_limits",
    "NPDES_ATTAINS_AU_SUMMARIES.csv": "raw_npdes_attains",
    # DMR file is named with the FY: NPDES_DMRS_FY2026.csv etc.
    # Handled separately so we capture the FY into snapshot_tag.
}


def load_zip_to_snowflake(zip_path: Path, snapshot_tag: str,
                          conn: snowflake.connector.SnowflakeConnection) -> None:
    """Extract each known CSV from the zip and PUT+COPY into its
    target raw_ table. Idempotent within a snapshot_tag: re-running
    the same tag is a no-op (the bookkeeping update is the only
    cross-row state). Files not in RAW_FILE_TARGETS are skipped."""
    cur = conn.cursor()
    with zipfile.ZipFile(zip_path) as zf:
        for inner_name in zf.namelist():
            target = RAW_FILE_TARGETS.get(Path(inner_name).name)
            if target is None and not inner_name.upper().startswith("NPDES_DMRS_FY"):
                continue
            # Special-case the DMR file — table name is shared but
            # the snapshot_tag should embed the FY for cross-FY queries.
            if target is None:
                target = "raw_npdes_dmrs"
            _stage_and_copy(cur, zf, inner_name, target, snapshot_tag)


def _stage_and_copy(cur, zf, inner_name, target, snapshot_tag):
    # 1. Extract to a tmpfile (PUT needs a real filesystem path)
    tmp = Path(f"/tmp/{Path(inner_name).name}")
    with zf.open(inner_name) as src, tmp.open("wb") as dst:
        dst.write(src.read())   # streaming-safe via shutil.copyfileobj if needed

    try:
        # 2. PUT to the named internal stage (one stage per table).
        cur.execute(f"PUT file://{tmp} @raw_stage_{target} "
                    f"AUTO_COMPRESS=TRUE OVERWRITE=TRUE")

        # 3. CREATE TABLE IF NOT EXISTS via INFER_SCHEMA — runs only
        #    on the first-ever load for this target; idempotent.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {target}
            USING TEMPLATE (
                SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*))
                FROM TABLE(INFER_SCHEMA(
                    LOCATION => '@raw_stage_{target}/{tmp.name}.gz',
                    FILE_FORMAT => 'raw_csv_fmt'
                ))
            )
        """)
        # 3a. Ensure bookkeeping columns exist (safe on existing tables).
        cur.execute(f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS "
                    f"loaded_at TIMESTAMP_NTZ")
        cur.execute(f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS "
                    f"snapshot_tag VARCHAR")

        # 4. COPY — column-name match handles EPA's case quirks.
        cur.execute(f"""
            COPY INTO {target}
            FROM @raw_stage_{target}/{tmp.name}.gz
            FILE_FORMAT = (FORMAT_NAME = 'raw_csv_fmt')
            MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
            ON_ERROR = 'CONTINUE'
        """)

        # 5. Backfill the two bookkeeping columns for just-loaded rows.
        cur.execute(f"""
            UPDATE {target}
               SET loaded_at = CURRENT_TIMESTAMP(),
                   snapshot_tag = %s
             WHERE loaded_at IS NULL
        """, (snapshot_tag,))
    finally:
        tmp.unlink(missing_ok=True)
```

Wire it into `bulk_loader._run_bulk_inner` after each download:

```python
# bulk_loader._run_bulk_inner (sketch)
from . import snowflake_raw_loader

snapshot_tag = datetime.utcnow().strftime("%Y-wk%V")   # ISO week tag

with snowflake_raw_loader.snowflake_connection() as conn:
    exporter_zip = _download_cached(BULK_URLS["echo_exporter"], cache_dir, "echo_exporter")
    snowflake_raw_loader.load_zip_to_snowflake(exporter_zip, snapshot_tag, conn)
    # ...same for the other 5 zips...
```

This adds the raw layer **as a side effect of the existing
bulk_loader run** — no separate ETL job to schedule, no separate
runner to manage. If the Snowflake write fails, the local bulk_loader
keeps working unchanged (raw staging is wrapped in a try / log.warning
in production).

## File format + named stages (one-time setup)

```sql
-- Single shared file format for all raw CSVs. EPA's bulk files are
-- comma-separated with a header row, double-quoted strings, empty
-- = NULL. NULL_IF list keeps EPA's sentinels out of typed columns.
CREATE OR REPLACE FILE FORMAT raw_csv_fmt
    TYPE = 'CSV'
    PARSE_HEADER = TRUE
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    NULL_IF = ('', 'NULL', 'N/A')
    EMPTY_FIELD_AS_NULL = TRUE
    ERROR_ON_COLUMN_COUNT_MISMATCH = FALSE
    SKIP_BLANK_LINES = TRUE;

-- One internal stage per raw_ table — keeps stage-side cleanup
-- localized when a table is dropped or rebuilt.
CREATE OR REPLACE STAGE raw_stage_raw_echo_exporter
    FILE_FORMAT = raw_csv_fmt;
CREATE OR REPLACE STAGE raw_stage_raw_npdes_violations
    FILE_FORMAT = raw_csv_fmt;
CREATE OR REPLACE STAGE raw_stage_raw_sdwa_violations
    FILE_FORMAT = raw_csv_fmt;
CREATE OR REPLACE STAGE raw_stage_raw_npdes_limits
    FILE_FORMAT = raw_csv_fmt;
CREATE OR REPLACE STAGE raw_stage_raw_npdes_attains
    FILE_FORMAT = raw_csv_fmt;
CREATE OR REPLACE STAGE raw_stage_raw_npdes_dmrs
    FILE_FORMAT = raw_csv_fmt;
```

## 26-week retention — one weekly Snowflake task

```sql
CREATE OR REPLACE TASK t_raw_retention_prune
    WAREHOUSE = wh_small
    SCHEDULE = 'USING CRON 0 4 * * SUN UTC'   -- Sundays 4 AM UTC
AS
    BEGIN
        DELETE FROM raw_echo_exporter
            WHERE loaded_at < DATEADD('week', -26, CURRENT_TIMESTAMP());
        DELETE FROM raw_npdes_violations
            WHERE loaded_at < DATEADD('week', -26, CURRENT_TIMESTAMP());
        DELETE FROM raw_sdwa_violations
            WHERE loaded_at < DATEADD('week', -26, CURRENT_TIMESTAMP());
        DELETE FROM raw_npdes_limits
            WHERE loaded_at < DATEADD('week', -26, CURRENT_TIMESTAMP());
        DELETE FROM raw_npdes_attains
            WHERE loaded_at < DATEADD('week', -26, CURRENT_TIMESTAMP());
        DELETE FROM raw_npdes_dmrs
            WHERE loaded_at < DATEADD('week', -26, CURRENT_TIMESTAMP());
    END;

ALTER TASK t_raw_retention_prune RESUME;
```

26 weeks = ~6 months — long enough to support quarter-over-quarter
analytics and EPA's quarterly SDWA refresh cycle, short enough to
keep storage costs predictable. To change the window, edit the
constant in one place. To pause retention temporarily (e.g. during a
historical analysis), `ALTER TASK t_raw_retention_prune SUSPEND`.

## Trade-offs — what gets harder

- **Schema drift.** If EPA renames a column in `npdes_limits.csv`,
  the INFER_SCHEMA on first load picks up the new name, but rows
  loaded under the old name persist under the old column. Cross-
  snapshot queries that reference the column either need a COALESCE
  or a schema-evolution migration. Same trap MEMORY.md flags for
  the streamers, just one layer down.
- **Cost visibility.** Raw layer storage cost is steady-state; query
  cost depends on what people run. A naive `SELECT *` against
  `raw_npdes_dmrs` (~5 GB) costs more than a derived-table query.
  Worth setting a Snowflake resource monitor on the raw warehouse.
- **Coupled write surface.** The bulk_loader run now has *two*
  failure modes (local SQLite write, Snowflake raw write). Wrap the
  raw load in try/log.warning so a Snowflake outage doesn't break
  the local lead-generation flow.
- **Re-load idempotency.** The current loader pattern appends on
  every COPY (no PK / no dedupe on raw tables — that's intentional,
  raw is meant to preserve EPA's row-level shape). Re-running the
  same snapshot_tag twice will double-load. Either guard the loader
  ("skip if rows with this snapshot_tag already exist") or accept
  that re-runs append and dedupe at query time via `MAX(loaded_at)`
  per natural key.

## Decision checklist — enable raw staging if any of these are true

- [ ] You want to re-score historic snapshots against current rules
- [ ] You have downstream teams (compliance / finance / product) who
      would query the raw EPA data directly
- [ ] You need permit-trend analytics that the rolled-up flags can't answer
- [ ] You want full audit trail from rolled-up signal back to source row

Skip raw staging if none of the above apply — the derived layer is
sufficient for lead-generation, and the extra surface area isn't free.

---

## What stays unchanged

The contract between the runner and the state store is what was already
factored — `open_db`, `record_run`, `diff_and_upsert_*`, the new
`load_prior_drilldown_state`, the per-row outcome columns the drill
helpers write. Every piece of code outside `snapshot.py` keeps working;
the migration is a single-module swap from `sqlite3` to
`snowflake.connector`. The scoring rules, the bulk-loader streamers,
the API client, the viewer, and the existing tests are all untouched
by the Snowflake transition.
