"""
snapshot.py
===========
Persistent state so we can answer "what changed since yesterday?"

Most lead-gen value is in *new* signals, not the standing inventory.
A facility that has been out of compliance for two years is probably
already in someone's CRM. A facility that *just* got hit with a new
violation is a fresh opportunity.

We store everything in a single SQLite file (stdlib, no extra deps).
Two tables:

    facilities      one row per (registry_id, program) we've ever seen
    violations      one row per individual violation event

On each run:
  1. We pull current state from EPA.
  2. We diff against the DB:
       - new facilities          (first appearance in the DB)
       - new violation events    (NPDES_VIOLATION_ID not in DB)
       - resolved violations     (was unresolved last run, now resolved)
       - score changes           (e.g. crossed into SNC since last run)
  3. We write the current snapshot back to the DB.

The diff output goes to a separate CSV the sales team reviews each morning.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS facilities (
    registry_id   TEXT,
    program       TEXT,                -- 'CWA' or 'SDWA'
    company       TEXT,
    city          TEXT,
    state         TEXT,
    naics         TEXT,
    permit_id     TEXT,
    lead_score    INTEGER,
    snc_flag      TEXT,
    first_seen    TEXT,                -- ISO8601
    last_seen     TEXT,
    PRIMARY KEY (registry_id, program)
);

CREATE TABLE IF NOT EXISTS violations (
    violation_id  TEXT PRIMARY KEY,    -- NPDES_VIOLATION_ID or SDWA VIOLATION_ID
    registry_id   TEXT,
    program       TEXT,
    parameter     TEXT,
    limit_value   TEXT,
    dmr_value     TEXT,
    exceedance_pct TEXT,
    period_end    TEXT,
    status        TEXT,                -- Unresolved / Resolved / Addressed / Archived
    first_seen    TEXT,
    last_seen     TEXT
);

CREATE INDEX IF NOT EXISTS idx_violations_registry ON violations(registry_id);
CREATE INDEX IF NOT EXISTS idx_facilities_score   ON facilities(lead_score);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at     TEXT,
    notes      TEXT
);
"""


@contextmanager
def open_db(path: str | Path):
    """Context manager that yields an open connection with row factory set."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------- diff & upsert

def diff_and_upsert_facilities(conn: sqlite3.Connection,
                               current: list[dict]) -> dict:
    """Compare `current` against DB, return diff, then upsert.

    `current` items must have: registry_id, program, company, city, state,
    naics, permit_id, lead_score, snc_flag.
    Returns dict with keys: new, score_increased, newly_snc.
    """
    now = datetime.utcnow().isoformat(timespec="seconds")
    existing = {
        (r["registry_id"], r["program"]): r
        for r in conn.execute(
            "SELECT registry_id, program, lead_score, snc_flag FROM facilities"
        )
    }

    new_rows: list[dict] = []
    score_increased: list[dict] = []
    newly_snc: list[dict] = []

    for f in current:
        key = (f["registry_id"], f["program"])
        prior = existing.get(key)

        if prior is None:
            new_rows.append(f)
        else:
            if (f["lead_score"] or 0) > (prior["lead_score"] or 0) + 10:
                # only flag meaningful jumps, not 1-2 point noise
                score_increased.append({**f, "prior_score": prior["lead_score"]})
            if str(f.get("snc_flag", "")).upper().startswith("Y") and \
               not str(prior["snc_flag"] or "").upper().startswith("Y"):
                newly_snc.append(f)

        conn.execute("""
            INSERT INTO facilities (registry_id, program, company, city, state,
                                    naics, permit_id, lead_score, snc_flag,
                                    first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(registry_id, program) DO UPDATE SET
                company = excluded.company,
                city = excluded.city,
                state = excluded.state,
                naics = excluded.naics,
                permit_id = excluded.permit_id,
                lead_score = excluded.lead_score,
                snc_flag = excluded.snc_flag,
                last_seen = excluded.last_seen
        """, (
            f["registry_id"], f["program"], f.get("company"), f.get("city"),
            f.get("state"), f.get("naics"), f.get("permit_id"),
            f.get("lead_score"), f.get("snc_flag"), now, now,
        ))

    return {
        "new": new_rows,
        "score_increased": score_increased,
        "newly_snc": newly_snc,
    }


def diff_and_upsert_violations(conn: sqlite3.Connection,
                               current: list[dict]) -> dict:
    """Same pattern for individual violation events.

    Returns dict with keys: new, newly_resolved.
    A violation is "newly resolved" if status went Unresolved -> Resolved
    or Addressed between runs.
    """
    now = datetime.utcnow().isoformat(timespec="seconds")
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

        conn.execute("""
            INSERT INTO violations (violation_id, registry_id, program, parameter,
                                    limit_value, dmr_value, exceedance_pct,
                                    period_end, status, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(violation_id) DO UPDATE SET
                status = excluded.status,
                last_seen = excluded.last_seen
        """, (
            vid, v.get("registry_id"), v.get("program"), v.get("parameter"),
            str(v.get("limit_value") or ""), str(v.get("dmr_value") or ""),
            str(v.get("exceedance_pct") or ""), v.get("period_end"),
            v.get("status"), now, now,
        ))

    return {"new": new_rows, "newly_resolved": newly_resolved}


def record_run(conn: sqlite3.Connection, notes: str = "") -> None:
    conn.execute("INSERT INTO runs (run_at, notes) VALUES (?, ?)",
                 (datetime.utcnow().isoformat(timespec="seconds"), notes))
