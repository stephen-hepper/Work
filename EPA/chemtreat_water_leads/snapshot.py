"""
snapshot.py
===========
Persistent state so we can answer "what changed since yesterday?" — and,
as of the source-of-truth refactor, the authoritative store the CSVs
are generated from.

Two roles, one SQLite file:

  1. Diff engine. Each run compares current state to what's in the DB
     and emits change sets (new facilities, newly SNC, new violations,
     newly resolved). These feed the `new_*.csv` files sales opens
     each morning.

  2. Standing-inventory store. Every column the CSV publishes is
     persisted here. At end of run, `all_leads.csv` and
     `violation_events.csv` are produced by SELECTing from this DB,
     filtered to "rows touched by the current run" via `last_seen >=
     run_start_ts`. A facility that fell out of today's territory
     filter stays in the DB (history preserved) but is absent from
     today's CSV.

Two tables:

    facilities      one row per (registry_id, program) we've ever seen
    violations      one row per individual violation event

Schema lives in `FAC_COLUMNS` and `VIOL_COLUMNS` below — ordered dicts
that double as the migration source and the CSV column order. Adding
a column means appending to those dicts; `_migrate(conn)` does the
ALTER TABLE on every existing DB on next open.

**Do not delete `snapshot.sqlite` between runs.** Doing so resets the
diff baseline AND wipes the standing-inventory CSV until the next
write completes. See MEMORY.md for the do-not-delete warning.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------- schema
#
# Ordered dicts. Insertion order is the canonical CSV column order
# (matching what `_flatten_facility` in pipeline.py and the
# `out/all_leads.csv` header have used historically). Append-only.

FAC_COLUMNS: dict[str, str] = {
    # Identity + score (existing columns kept first so legacy DBs are
    # unchanged where possible).
    "lead_score":                 "INTEGER",
    "score_reasons":              "TEXT",
    "outreach_posture":           "TEXT",
    "program":                    "TEXT",
    "registry_id":                "TEXT",
    "company":                    "TEXT",
    # Address block.
    "address":                    "TEXT",
    "city":                       "TEXT",
    "state":                      "TEXT",
    "zip":                        "TEXT",
    "county":                     "TEXT",
    "naics":                      "TEXT",
    "sic":                        "TEXT",
    # SDWA-only context columns. PWS metadata comes back in the API's
    # SDW response (echo_client.SDW_WANTED_COLUMNS) and is populated by
    # pipeline._flatten_facility. ECHO Exporter doesn't carry these at
    # the facility level, so bulk-only SDWA leads leave them NULL —
    # same asymmetry as permit_has_* going the other direction.
    # population_served is also the input to scoring.rule_population_served.
    "population_served":          "INTEGER",
    "system_type":                "TEXT",
    "owner_type":                 "TEXT",
    "primary_source":             "TEXT",
    "permit_id":                  "TEXT",
    # Compliance snapshot.
    "snc_status":                 "TEXT",
    "snc_status_date":            "TEXT",
    "snc_event":                  "TEXT",
    "violation_status":           "TEXT",
    "quarters_in_violation":      "TEXT",
    "quarters_in_snc":            "TEXT",
    "compliance_history_13q":     "TEXT",
    "formal_actions_5yr":         "INTEGER",
    "informal_actions_5yr":       "INTEGER",
    "total_penalties_usd":        "REAL",
    "last_penalty_date":          "TEXT",
    "last_inspection_days_ago":   "INTEGER",
    "missing_dmr_quarters":       "INTEGER",
    "echo_url":                   "TEXT",
    # Tags (stored as 0/1; dumped back to "True"/"False" so the CSV
    # byte-matches the legacy DictWriter output).
    "tag_active_snc":             "INTEGER",
    "tag_treatment_technique":    "INTEGER",
    "tag_mcl_violation":          "INTEGER",
    "tag_lead_copper":            "INTEGER",
    "tag_major_facility":         "INTEGER",
    "tag_only_resolved_events":   "INTEGER",
    "tag_chemtreat_high_relevance": "INTEGER",
    # Permit-limit signals (rolled up from npdes_limits.zip). Each
    # permit_has_* is 0/1 — TRUE when at least one active permit limit
    # on this facility's permit matches the ChemTreat-treatable class.
    # Rolled up to facility level: one CWA permit can have many outfalls
    # and many parameters; we collapse to "does any of them carry a
    # treatable limit?".
    "permit_has_phosphorus":      "INTEGER",
    "permit_has_ammonia":         "INTEGER",
    "permit_has_tss":             "INTEGER",
    "permit_has_bod":             "INTEGER",
    "permit_has_oil_grease":      "INTEGER",
    # `metals` covers Cu/Pb/Zn/Ni/Cr/Cd plus Iron/Manganese — same
    # precipitation chemistry, same product family.
    "permit_has_metals":          "INTEGER",
    # Cyanide is its own bucket — oxidation chemistry, plating-shop
    # niche. See bulk_loader._TREATABLE_PARAM_PATTERNS.
    "permit_has_cyanide":         "INTEGER",
    "permit_has_chlorine_residual": "INTEGER",
    # Microbiological control (coliform, E. coli, enterococci, fecal
    # indicators). Treated by ChemTreat's biocide / disinfection
    # product line, so coliform exceedances belong in the treatable
    # bucket. Added 2026-06-11.
    "permit_has_microbiological": "INTEGER",
    "permitted_parameters_text":  "TEXT",  # pipe-joined sample (top ~10)
    # ATTAINS-NPDES catchment linkage (rolled up from
    # npdes_attains_downloads.zip / NPDES_ATTAINS_AU_SUMMARIES.csv).
    # discharges_to_impaired is 0/1 — TRUE when ANY assessment unit
    # touched by this facility carries an "Impaired*" WATER_CONDITION.
    # matching_impaired_parameters is the rarer/stronger signal: the
    # facility's E90-monitored parameters that match the waterbody's
    # impairment causes (≈1% of leads, but each is a high-confidence
    # tightening-permit lead).
    "discharges_to_impaired":     "INTEGER",
    "impairment_causes_text":     "TEXT",
    "matching_impaired_parameters": "TEXT",
    # DMR exceedance signals (rolled up from npdes_dmrs_fyYYYY.zip).
    # `recent_dmr_exceedances_count` is the count of rows in the
    # archive with EXCEEDENCE_PCT > 0 for this permit; the others
    # describe the worst-single-row exceedance and the union of
    # ChemTreat-treatable parameter classes seen exceeded. Together
    # they let rule_recent_dmr_exceedance and
    # rule_exceeds_treatable_parameter fire — the latter is the
    # composite that makes "permit covers ammonia AND they're
    # currently exceeding ammonia" a single rule.
    # Spelling note: EPA's column is EXCEEDENCE_PCT (sic). We use
    # the correctly-spelled `exceedance` on our side; the streamer
    # bridges the two.
    "recent_dmr_exceedances_count": "INTEGER",
    "top_exceeded_parameter":     "TEXT",
    "top_exceedance_pct":         "REAL",
    "exceeded_treatable_parameters_text": "TEXT",
    "tag_treatable_permit":       "INTEGER",
    "tag_discharges_to_impaired": "INTEGER",
    "tag_impairment_parameter_match": "INTEGER",
    "tag_recent_exceedance":      "INTEGER",
    "tag_exceeds_treatable_parameter": "INTEGER",
    # Drill-down state — per-row operational columns that drive
    # hands-off rerun decisioning. Pipeline writes these from
    # `_drill_cwa` / `_drill_sdwa` via `_record_drilldown_outcome`.
    # The Snowflake eligibility view filters on
    # `next_drilldown_eligible_at`; the pipeline reads
    # `drilldown_failure_streak` at run start for backoff math.
    # See chemtreat_water_leads/markdown/SNOWFLAKE_DESIGN.md for the cross-system
    # contract.
    #   * last_drilldown_attempt_at: ISO timestamp of last drill attempt
    #     for this (registry_id, program). NULL = never attempted.
    #   * last_drilldown_outcome: 'with_events' | 'no_data' |
    #     'lookup_failed'. NULL = never attempted. Matches the
    #     _health.summarize_drilldown vocabulary so the JSON view and
    #     DB view agree on terms.
    #   * last_drilldown_run_id: FK to runs.run_id. Joins the per-row
    #     outcome back to the run that produced it for audit /
    #     SLA queries.
    #   * drilldown_failure_streak: count of consecutive 'lookup_failed'
    #     outcomes; resets to 0 on 'with_events' or 'no_data'. Drives
    #     give-up logic ("escalate after N straight failures").
    #   * next_drilldown_eligible_at: ISO timestamp this row becomes
    #     eligible for re-drill. Computed as
    #     `last_drilldown_attempt_at + backoff`, where `backoff` comes
    #     from `DRILLDOWN_BACKOFF[outcome]` for with_events/no_data
    #     (flat 7d / 30d) and from `LOOKUP_FAILED_BACKOFF_TIERS` for
    #     lookup_failed (streak-tiered 6h → 24h → 7d). Both live in
    #     pipeline.py. NULL = never attempted; eligibility view treats
    #     NULL as "eligible whenever score warrants."
    "last_drilldown_attempt_at":   "TEXT",
    "last_drilldown_outcome":      "TEXT",
    "last_drilldown_run_id":       "INTEGER",
    "drilldown_failure_streak":    "INTEGER",
    "next_drilldown_eligible_at":  "TEXT",
    # Legacy flag kept for backwards-compat with old DBs that only had
    # this column. Always equals tag_active_snc going forward; nothing
    # reads it today, but dropping it would break legacy diffs.
    "snc_flag":                   "TEXT",
    # Bookkeeping.
    "first_seen":                 "TEXT",
    "last_seen":                  "TEXT",
}

VIOL_COLUMNS: dict[str, str] = {
    "violation_id":               "TEXT",
    "registry_id":                "TEXT",
    "program":                    "TEXT",
    # SDWA-shaped fields (most common today).
    "source_id":                  "TEXT",
    "violation_code":             "TEXT",
    "violation_category":         "TEXT",
    "violation_description":      "TEXT",
    "contaminant":                "TEXT",
    "rule_family":                "TEXT",
    "period_begin":               "TEXT",
    "period_end":                 "TEXT",
    "resolved_date":              "TEXT",
    "status":                     "TEXT",
    "state_mcl":                  "TEXT",
    "federal_mcl":                "TEXT",
    "measure":                    "TEXT",
    "enforcement_count":          "INTEGER",
    "company":                    "TEXT",
    "data_lag_note":              "TEXT",
    # CWA-shaped fields (present only on NPDES events).
    "parameter":                  "TEXT",
    "limit_value":                "TEXT",
    "limit_unit":                 "TEXT",
    "dmr_value":                  "TEXT",
    "dmr_unit":                   "TEXT",
    "exceedance_pct":             "TEXT",
    "npdes_id":                   "TEXT",
    "stat_basis":                 "TEXT",
    # Bookkeeping.
    "first_seen":                 "TEXT",
    "last_seen":                  "TEXT",
}

# Columns the CSV publishes, in viewer-stable order. Bookkeeping
# columns are intentionally excluded.
FAC_CSV_COLUMNS = [
    c for c in FAC_COLUMNS
    if c not in ("first_seen", "last_seen", "snc_flag")
]
VIOL_CSV_COLUMNS = [
    c for c in VIOL_COLUMNS
    if c not in ("first_seen", "last_seen")
]

# Columns the dump must emit as "True"/"False" strings (legacy
# DictWriter behavior; preserves the byte shape sales has been
# opening in Excel).
_BOOLEAN_CSV_COLUMNS = {c for c in FAC_CSV_COLUMNS if c.startswith("tag_")}


# --------------------------------------------------------------- create

def _create_sql() -> str:
    """Build the CREATE TABLE statements from the column dicts.

    Run on every open via `executescript`. Idempotent thanks to IF NOT
    EXISTS; on an existing DB, `_migrate` then fills in any columns
    added since the DB was created.
    """
    fac_cols = ",\n    ".join(f"{n:30s} {t}" for n, t in FAC_COLUMNS.items())
    viol_cols = ",\n    ".join(f"{n:24s} {t}" for n, t in VIOL_COLUMNS.items())
    return f"""
    CREATE TABLE IF NOT EXISTS facilities (
        {fac_cols},
        PRIMARY KEY (registry_id, program)
    );

    CREATE TABLE IF NOT EXISTS violations (
        {viol_cols},
        PRIMARY KEY (violation_id)
    );

    CREATE INDEX IF NOT EXISTS idx_violations_registry ON violations(registry_id);
    CREATE INDEX IF NOT EXISTS idx_facilities_score    ON facilities(lead_score);
    CREATE INDEX IF NOT EXISTS idx_facilities_lastseen ON facilities(last_seen);
    CREATE INDEX IF NOT EXISTS idx_violations_lastseen ON violations(last_seen);

    CREATE TABLE IF NOT EXISTS runs (
        run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at     TEXT,
        notes      TEXT
    );

    -- Per-row run membership. Records every (run_id, row-key) the upsert
    -- step touched, so "which runs did this facility appear in?" becomes
    -- a single JOIN instead of fuzzy timestamp matching against runs.run_at.
    -- "First seen in any run" is MIN(run_id) per key. The old
    -- first_seen/last_seen ISO timestamps stay (back-compat) — these
    -- tables are additive.
    CREATE TABLE IF NOT EXISTS run_facility_membership (
        run_id      INTEGER NOT NULL,
        registry_id TEXT,
        program     TEXT,
        PRIMARY KEY (run_id, registry_id, program),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_rfm_facility
        ON run_facility_membership(registry_id, program);

    CREATE TABLE IF NOT EXISTS run_violation_membership (
        run_id       INTEGER NOT NULL,
        violation_id TEXT NOT NULL,
        PRIMARY KEY (run_id, violation_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_rvm_violation
        ON run_violation_membership(violation_id);
    """


def _migrate(conn: sqlite3.Connection) -> None:
    """ALTER TABLE to add any columns missing from a legacy DB.

    Safe to run on a fresh DB (no missing columns), safe on a legacy
    DB (adds only what's absent), safe to run twice (PRAGMA reports
    truthfully).
    """
    for table, wanted in (("facilities", FAC_COLUMNS), ("violations", VIOL_COLUMNS)):
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in wanted.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


@contextmanager
def open_db(path: str | Path):
    """Context manager that yields an open connection with row factory set.

    Creates tables on first open (`_create_sql`) and adds any columns
    missing from a legacy DB (`_migrate`) before yielding.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_create_sql())
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------- diff & upsert

def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _coerce_for_db(col: str, value):
    """Convert Python values to what sqlite3 should bind for this column.

    The two non-obvious cases:
      * tag_* columns are stored as 0/1 integers (sqlite has no native
        bool); we coerce True/False/None accordingly so dumps come
        back consistent.
      * Other columns pass through. None is a valid bind value (writes
        NULL).
    """
    if col.startswith("tag_"):
        if value is None:
            return 0
        return 1 if value else 0
    return value


def _build_upsert(table: str, columns: list[str], pk_cols: list[str]) -> str:
    """Build INSERT ... ON CONFLICT(pk) DO UPDATE SET ... statement.

    Updates every non-PK, non-first_seen column on conflict — including
    `last_seen`, which is what the dump's `last_seen >= run_start_ts`
    filter keys off of.
    """
    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(columns)
    set_clause = ", ".join(
        f"{c} = excluded.{c}"
        for c in columns
        if c not in pk_cols and c != "first_seen"
    )
    pk_list = ", ".join(pk_cols)
    return (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk_list}) DO UPDATE SET {set_clause}"
    )


_FAC_UPSERT_SQL = _build_upsert(
    "facilities", list(FAC_COLUMNS), ["registry_id", "program"]
)
_VIOL_UPSERT_SQL = _build_upsert(
    "violations", list(VIOL_COLUMNS), ["violation_id"]
)


def diff_and_upsert_facilities(conn: sqlite3.Connection,
                               current: list[dict],
                               run_id: int,
                               now: str | None = None) -> dict:
    """Compare `current` against DB, return diff dict, then upsert.

    `current` items are the dicts produced by `pipeline._flatten_facility`
    (after phase-2 re-scoring). Every key in `FAC_COLUMNS` is consulted
    via .get(); missing keys bind as NULL.

    `run_id` is the id returned by `record_run()` for the current run.
    Every facility this call touches gets a row in
    `run_facility_membership` so future queries can answer "which runs
    did this facility appear in?" via a single JOIN.

    Returns dict with keys: new, score_increased, newly_snc. The diff
    *logic* only inspects lead_score and snc/SNC fields — adding
    columns to the schema does not change these outputs, so the
    `new_*.csv` deltas remain byte-identical.

    Pass `now` to use a caller-supplied timestamp (the standard pattern
    so `last_seen >= run_start_ts` in the dump query catches every row
    this run touched). When `now` is None, the function falls back to
    a freshly-computed timestamp — for callers that don't care about
    dump filtering.
    """
    ts = now or _now_iso()
    existing = {
        (r["registry_id"], r["program"]): r
        for r in conn.execute(
            "SELECT registry_id, program, lead_score, snc_flag, "
            "tag_active_snc FROM facilities"
        )
    }

    new_rows: list[dict] = []
    score_increased: list[dict] = []
    newly_snc: list[dict] = []

    for f in current:
        key = (f.get("registry_id"), f.get("program"))
        prior = existing.get(key)

        # Derive snc_flag from tag_active_snc (or the legacy snc_status
        # text) so diff comparisons stay stable across the refactor.
        snc_flag_now = "Y" if f.get("tag_active_snc") else (
            "Y" if _looks_like_snc(f.get("snc_status")) else "N"
        )

        if prior is None:
            new_rows.append(f)
        else:
            prior_score = prior["lead_score"] or 0
            if (f.get("lead_score") or 0) > prior_score + 10:
                score_increased.append({**f, "prior_score": prior_score})
            prior_snc = str(prior["snc_flag"] or "").upper().startswith("Y") \
                or bool(prior["tag_active_snc"])
            if snc_flag_now == "Y" and not prior_snc:
                newly_snc.append(f)

        # Backfill last_drilldown_run_id from this run's id when the
        # lead carries a fresh drill-down outcome but no explicit
        # run_id (the drill helpers omit it so record_run can stay
        # atomic with this upsert block — see pipeline._record_drilldown_outcome).
        if f.get("last_drilldown_outcome") and not f.get("last_drilldown_run_id"):
            f["last_drilldown_run_id"] = run_id

        binds = []
        for col in FAC_COLUMNS:
            if col == "first_seen":
                binds.append(prior["first_seen"] if prior and "first_seen" in prior.keys() else ts)
            elif col == "last_seen":
                binds.append(ts)
            elif col == "snc_flag":
                binds.append(snc_flag_now)
            else:
                binds.append(_coerce_for_db(col, f.get(col)))
        conn.execute(_FAC_UPSERT_SQL, binds)
        # Membership row for this (run_id, facility-key). INSERT OR IGNORE
        # so a `current` list that contains the same key twice in a single
        # run doesn't trip the PK constraint.
        conn.execute(
            "INSERT OR IGNORE INTO run_facility_membership "
            "(run_id, registry_id, program) VALUES (?, ?, ?)",
            (run_id, key[0], key[1]),
        )

    return {
        "new": new_rows,
        "score_increased": score_increased,
        "newly_snc": newly_snc,
    }


def _looks_like_snc(text) -> bool:
    """SNC heuristic for legacy text fields where no tag_active_snc was set."""
    s = str(text or "").upper()
    return any(t in s for t in ("SIGNIFICANT", "SNC", "CATEGORY I", "ENFORCEMENT PRIORITY"))


def diff_and_upsert_violations(conn: sqlite3.Connection,
                               current: list[dict],
                               run_id: int,
                               now: str | None = None) -> dict:
    """Same pattern for individual violation events.

    `run_id` plays the same role as in `diff_and_upsert_facilities`:
    every event we successfully upsert also gets a row in
    `run_violation_membership`. Events without a `violation_id` are
    skipped on both sides (no upsert, no membership).

    Returns dict with keys: new, newly_resolved.
    A violation is "newly resolved" if status went Unresolved → Resolved
    or Addressed between runs.

    Events without a `violation_id` are skipped — we can't dedupe them
    across runs and they'd churn the `new_violations_*.csv` diff. This
    was the behavior before the refactor as well; flagged in
    RATIONALE.md gap.
    """
    ts = now or _now_iso()
    existing = {
        r["violation_id"]: r
        for r in conn.execute("SELECT violation_id, status FROM violations")
    }

    new_rows: list[dict] = []
    newly_resolved: list[dict] = []

    for v in current:
        vid = v.get("violation_id")
        if not vid:
            continue  # can't dedupe without an ID
        prior = existing.get(vid)

        if prior is None:
            new_rows.append(v)
        else:
            prior_status = (prior["status"] or "").lower()
            new_status = (v.get("status") or "").lower()
            if prior_status == "unresolved" and new_status in ("resolved", "addressed"):
                newly_resolved.append(v)

        binds = []
        for col in VIOL_COLUMNS:
            if col == "first_seen":
                binds.append(prior["first_seen"] if prior and "first_seen" in prior.keys() else ts)
            elif col == "last_seen":
                binds.append(ts)
            else:
                binds.append(_coerce_for_db(col, v.get(col)))
        conn.execute(_VIOL_UPSERT_SQL, binds)
        conn.execute(
            "INSERT OR IGNORE INTO run_violation_membership "
            "(run_id, violation_id) VALUES (?, ?)",
            (run_id, vid),
        )

    return {"new": new_rows, "newly_resolved": newly_resolved}


def record_run(conn: sqlite3.Connection,
               notes: str = "",
               now: str | None = None) -> int:
    """Insert a row into `runs` and return its `run_id`.

    Callers (`pipeline._run_inner`, `bulk_loader._run_bulk_inner`) hold
    the returned id and pass it down to `diff_and_upsert_*` so each
    touched facility/violation row gets a (run_id, key) entry in the
    membership tables. Call this BEFORE the upserts so the foreign key
    in the membership tables resolves.
    """
    cur = conn.execute(
        "INSERT INTO runs (run_at, notes) VALUES (?, ?)",
        (now or _now_iso(), notes),
    )
    return cur.lastrowid


# ----------------------------------------------------- CSV dumps
#
# These produce the standing-inventory CSVs (`all_leads.csv`,
# `violation_events.csv`) by SELECTing from the DB rather than from
# in-memory pipeline state. Filter `last_seen >= run_start_ts` so the
# CSV reflects "rows touched by the current run" — facilities that
# fell out of today's territory are preserved in the DB but absent
# from the CSV.

def _coerce_for_csv(col: str, value):
    """Convert sqlite return value to the string the CSV expects.

    Empty string for NULL on TEXT/INTEGER/REAL — matches the legacy
    DictWriter behavior (None → ""). Booleans (tag_*) are coerced to
    "True"/"False" to preserve the byte shape sales has been seeing
    in Excel; a NULL tag (legacy row) becomes "False" so Excel
    filters stay sensible.
    """
    if col in _BOOLEAN_CSV_COLUMNS:
        if value is None:
            return "False"
        return "True" if int(value) else "False"
    if value is None:
        return ""
    return value


def dump_facilities_csv(conn: sqlite3.Connection,
                        path: str | Path,
                        run_start_ts: str) -> int:
    """Write all_leads.csv from the facilities table.

    Only rows with `last_seen >= run_start_ts` — i.e. rows touched by
    the current run — are included, matching today's "CSV = current
    run" semantics. Returns the row count written.
    """
    cols = FAC_CSV_COLUMNS
    rows = list(conn.execute(
        f"SELECT {', '.join(cols)} FROM facilities "
        "WHERE last_seen >= ? "
        "ORDER BY lead_score DESC, company",
        (run_start_ts,),
    ))
    return _write_dump(path, cols, rows)


def dump_violations_csv(conn: sqlite3.Connection,
                        path: str | Path,
                        run_start_ts: str) -> int:
    """Write violation_events.csv from the violations table.

    Same `last_seen` filter as the facilities dump.
    """
    cols = VIOL_CSV_COLUMNS
    rows = list(conn.execute(
        f"SELECT {', '.join(cols)} FROM violations "
        "WHERE last_seen >= ? "
        "ORDER BY registry_id, period_end DESC",
        (run_start_ts,),
    ))
    return _write_dump(path, cols, rows)


def _write_dump(path: str | Path, cols: list[str], rows) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        n = 0
        for r in rows:
            w.writerow([_coerce_for_csv(c, r[c]) for c in cols])
            n += 1
    return n


# ----------------------------------------------------- run-membership reads
#
# Stable handles for ad-hoc "which rows did run N touch?" queries. Not
# wired to any caller today; exist so consumers (CLI, viewer, future
# reports) don't have to embed the membership-table schema themselves.

def facilities_in_run(conn: sqlite3.Connection,
                      run_id: int) -> list[sqlite3.Row]:
    """Return every facility row touched by `run_id`, ranked by score."""
    cols = FAC_CSV_COLUMNS
    return list(conn.execute(
        f"SELECT {', '.join('f.' + c for c in cols)} FROM facilities f "
        "JOIN run_facility_membership m "
        "  ON f.registry_id = m.registry_id AND f.program = m.program "
        "WHERE m.run_id = ? "
        "ORDER BY f.lead_score DESC, f.company",
        (run_id,),
    ))


def violations_in_run(conn: sqlite3.Connection,
                      run_id: int) -> list[sqlite3.Row]:
    """Return every violation row touched by `run_id`."""
    cols = VIOL_CSV_COLUMNS
    return list(conn.execute(
        f"SELECT {', '.join('v.' + c for c in cols)} FROM violations v "
        "JOIN run_violation_membership m "
        "  ON v.violation_id = m.violation_id "
        "WHERE m.run_id = ? "
        "ORDER BY v.registry_id, v.period_end DESC",
        (run_id,),
    ))


def load_prior_drilldown_eligibility(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    """Snapshot per-row drill-down eligibility timestamps from the DB.

    Returns ``{(registry_id, program): next_drilldown_eligible_at_iso}``
    for rows where the column is populated. Used by
    `bulk_loader._drilldown_candidates` to skip leads that drilled
    recently and are still in their backoff window — closes the
    rerun loop locally without waiting for the Snowflake migration.

    Rows with NULL `next_drilldown_eligible_at` (never drilled) are
    omitted; the caller treats absence as "eligible whenever the
    score warrants" (same default the Snowflake eligibility view
    uses).

    Caller opens its own connection; mirrors `load_prior_drilldown_state`
    so a runner can load both with one DB open.
    """
    out: dict[tuple[str, str], str] = {}
    for r in conn.execute(
        "SELECT registry_id, program, next_drilldown_eligible_at "
        "FROM facilities WHERE next_drilldown_eligible_at IS NOT NULL"
    ):
        if r["registry_id"]:
            out[(r["registry_id"], r["program"])] = (
                r["next_drilldown_eligible_at"]
            )
    return out


def load_prior_drilldown_state(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], int]:
    """Snapshot prior drill-down failure streaks from the DB before a run.

    Returns ``{(registry_id, program): drilldown_failure_streak}``. Used
    by `pipeline._record_drilldown_outcome` so a fresh `lookup_failed`
    can increment the right base, and a `with_events` / `no_data` can
    reset cleanly. Rows with NULL streak (never drilled before) are
    omitted; the caller treats absence as streak == 0.

    Opens nothing — caller passes its own connection so this can fit
    inside the run's main `with snapshot.open_db(...)` block OR a
    pre-run helper context, mirroring the bulk_loader._load_prior_scores
    pattern.
    """
    out: dict[tuple[str, str], int] = {}
    for r in conn.execute(
        "SELECT registry_id, program, drilldown_failure_streak "
        "FROM facilities WHERE drilldown_failure_streak IS NOT NULL"
    ):
        if r["registry_id"]:
            out[(r["registry_id"], r["program"])] = (
                r["drilldown_failure_streak"] or 0
            )
    return out
