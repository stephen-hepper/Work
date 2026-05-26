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
                               now: str | None = None) -> dict:
    """Compare `current` against DB, return diff dict, then upsert.

    `current` items are the dicts produced by `pipeline._flatten_facility`
    (after phase-2 re-scoring). Every key in `FAC_COLUMNS` is consulted
    via .get(); missing keys bind as NULL.

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
                               now: str | None = None) -> dict:
    """Same pattern for individual violation events.

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

    return {"new": new_rows, "newly_resolved": newly_resolved}


def record_run(conn: sqlite3.Connection,
               notes: str = "",
               now: str | None = None) -> None:
    conn.execute(
        "INSERT INTO runs (run_at, notes) VALUES (?, ?)",
        (now or _now_iso(), notes),
    )


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
