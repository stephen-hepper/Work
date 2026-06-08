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
[`snapshot.py`](snapshot.py) FAC_COLUMNS comments and `pipeline.DRILLDOWN_BACKOFF`.

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
  -- Optional escalation cap: give up after N straight failures.
  -- Comment in if you want failed leads to drop off after some N.
  -- AND COALESCE(drilldown_failure_streak, 0) < 10
;
```

**Why a view, not a stored column.** Backoff policy lives in
`pipeline.DRILLDOWN_BACKOFF` and gets written into
`next_drilldown_eligible_at` at write time. The view's job is just
to apply the score gate + the time comparison. Policy change → re-write
the constant in `pipeline.py` and let new runs propagate the new
timestamps; the view doesn't need to know.

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

## What stays unchanged

The contract between the runner and the state store is what was already
factored — `open_db`, `record_run`, `diff_and_upsert_*`, the new
`load_prior_drilldown_state`, the per-row outcome columns the drill
helpers write. Every piece of code outside `snapshot.py` keeps working;
the migration is a single-module swap from `sqlite3` to
`snowflake.connector`. The scoring rules, the bulk-loader streamers,
the API client, the viewer, and the existing tests are all untouched
by the Snowflake transition.
