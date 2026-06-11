"""Briefings state store — separate sqlite from snapshot.sqlite.

The pipeline owns `snapshot.sqlite`. This package opens that file
strictly read-only. The state of "which leads have we featured in a
briefing, and when" lives here, in its own file, so the briefings
package can stay a pure read-consumer of the pipeline's output.

Cross-DB joins for the candidate query use `ATTACH DATABASE` — open
the snapshot read-only as the primary connection, attach the state
DB read-only alongside, and let SQLite do the join.

Two tables:

  briefing_runs    one row per (region, run) — audit log
  lead_briefings   one row per (registry_id, program) — last-featured state
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


# Score-drift threshold for the "score_changed" candidate predicate.
# A lead the LLM already briefed re-enters the candidate set when its
# current lead_score moves more than this many points away from the
# score we recorded at briefing time. 5 is sensitive enough to surface
# meaningful shifts without flapping on noise.
SCORE_DRIFT_THRESHOLD = 5


_SCHEMA = """
CREATE TABLE IF NOT EXISTS briefing_runs (
    briefing_run_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at           TEXT NOT NULL,
    region           TEXT NOT NULL,
    mode             TEXT NOT NULL,         -- 'dry_run' or 'send'
    lead_count       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_briefings (
    registry_id           TEXT NOT NULL,
    program               TEXT NOT NULL,
    last_featured_at      TEXT NOT NULL,
    last_featured_run_id  INTEGER,
    lead_score_at_brief   INTEGER,
    last_seen_at_brief    TEXT,
    region                TEXT,
    PRIMARY KEY (registry_id, program),
    FOREIGN KEY (last_featured_run_id) REFERENCES briefing_runs(briefing_run_id)
);

CREATE INDEX IF NOT EXISTS idx_lead_briefings_region
    ON lead_briefings(region);
CREATE INDEX IF NOT EXISTS idx_lead_briefings_last_featured
    ON lead_briefings(last_featured_at);
"""


def init_state_db(state_path: Path) -> None:
    """Create the schema if it doesn't exist. Idempotent — safe to
    call at every script start; existing data is preserved."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(state_path)) as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def _open_write(state_path: Path):
    """Read-write connection for marking briefings."""
    conn = sqlite3.connect(str(state_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns we project from `facilities` into the candidate result. Same
# shape as the legacy `_LEAD_LIST_COLUMNS` minus the long score_reasons
# string (left in for the LLM's framing, kept short to control payload).
_CANDIDATE_FAC_COLS = (
    "f.registry_id", "f.program", "f.company", "f.city", "f.state",
    "f.naics", "f.lead_score", "f.outreach_posture", "f.score_reasons",
    "f.last_seen",
    "f.tag_active_snc", "f.tag_treatment_technique", "f.tag_mcl_violation",
    "f.tag_lead_copper", "f.tag_chemtreat_high_relevance",
    "f.tag_exceeds_treatable_parameter", "f.tag_treatable_permit",
    "f.tag_discharges_to_impaired", "f.tag_recent_exceedance",
)
_CANDIDATE_TAG_COLS = tuple(
    c.split(".", 1)[1] for c in _CANDIDATE_FAC_COLS if ".tag_" in c
)


def candidates_for_states(
    snapshot_path: Path,
    state_path: Path,
    states: list[str],
    limit: int,
    *,
    min_score: int = 50,
    score_drift_threshold: int = SCORE_DRIFT_THRESHOLD,
    force_rebrief: bool = False,
) -> list[dict]:
    """Return up to `limit` leads in the given states that warrant a
    fresh briefing.

    Without `force_rebrief`, a lead qualifies when ANY of:
      * never_briefed   — no row in lead_briefings
      * score_changed   — |current score - lead_score_at_brief| > threshold
      * new_activity    — facilities.last_seen > lead_briefings.last_featured_at

    With `force_rebrief=True`, all qualifying-by-score leads are
    returned regardless of prior briefing state — for testing the
    gating logic without burning through the candidate pool.

    Each row carries a `briefing_status` field set to the predicate
    that made it a candidate (never_briefed / score_changed /
    new_activity). `score_changed` rows additionally carry
    `prior_lead_score` so the LLM's prose can frame the delta.

    Ensures `init_state_db` has been called so the lead_briefings
    table exists for the LEFT JOIN.
    """
    init_state_db(state_path)

    placeholders = ",".join("?" * len(states))
    select_cols = ", ".join(_CANDIDATE_FAC_COLS) + (
        ", lb.last_featured_at, lb.lead_score_at_brief"
    )
    base = (
        f"FROM facilities f "
        f"LEFT JOIN briefings_state.lead_briefings lb "
        f"  ON lb.registry_id = f.registry_id AND lb.program = f.program "
        f"WHERE f.state IN ({placeholders}) "
        f"  AND f.lead_score >= ? "
    )

    if force_rebrief:
        sql = (
            f"SELECT {select_cols} {base}"
            f"ORDER BY f.lead_score DESC LIMIT ?"
        )
        binds = [*states, min_score, limit]
    else:
        sql = (
            f"SELECT {select_cols} {base}"
            f"  AND ( "
            f"       lb.last_featured_at IS NULL "
            f"    OR ABS(f.lead_score - lb.lead_score_at_brief) "
            f"       > ? "
            f"    OR f.last_seen > lb.last_featured_at "
            f"  ) "
            f"ORDER BY f.lead_score DESC LIMIT ?"
        )
        binds = [*states, min_score, score_drift_threshold, limit]

    # Open snapshot read-only; attach state DB read-only for the JOIN.
    # ATTACH on a read-only primary works in SQLite.
    snap_uri = f"file:{snapshot_path}?mode=ro"
    state_uri = f"file:{state_path}?mode=ro"
    conn = sqlite3.connect(snap_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"ATTACH DATABASE '{state_uri}' AS briefings_state")
        rows = conn.execute(sql, binds).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    tag_set = set(_CANDIDATE_TAG_COLS)
    for r in rows:
        d = {k: r[k] for k in r.keys() if r[k] is not None}
        for k in list(d):
            if k in tag_set:
                d[k] = bool(d[k])
        d["briefing_status"] = _classify(d, score_drift_threshold, force_rebrief)
        # Rename the briefing-source field so it reads naturally in the
        # LLM context. last_featured_at stays for new_activity context.
        if "lead_score_at_brief" in d:
            d["prior_lead_score"] = d.pop("lead_score_at_brief")
        out.append(d)
    return out


def _classify(d: dict, score_drift_threshold: int,
              force_rebrief: bool) -> str:
    """Determine which predicate fired for this candidate row.

    For `force_rebrief` runs that pulled a lead with no prior brief,
    `never_briefed` is still correct. For ones with a prior brief but
    no actual change, return `forced` so the LLM knows this surfaced
    via the bypass rather than fresh signal."""
    if d.get("last_featured_at") is None:
        return "never_briefed"
    prior = d.get("lead_score_at_brief")
    current = d.get("lead_score")
    if (prior is not None and current is not None
            and abs(current - prior) > score_drift_threshold):
        return "score_changed"
    last_seen = d.get("last_seen")
    last_featured = d.get("last_featured_at")
    if last_seen and last_featured and last_seen > last_featured:
        return "new_activity"
    return "forced" if force_rebrief else "stale"


def record_briefing_run(
    state_path: Path,
    region: str,
    mode: str,
    featured: list[dict],
    now: datetime | None = None,
) -> int:
    """Append a `briefing_runs` row and UPSERT one `lead_briefings`
    row per featured lead. `mode` is 'dry_run' or 'send'.

    Returns the new briefing_run_id."""
    if mode not in ("dry_run", "send"):
        raise ValueError(f"unknown mode: {mode!r}")
    init_state_db(state_path)
    ts = (now or datetime.utcnow()).isoformat(timespec="seconds")
    with _open_write(state_path) as conn:
        cur = conn.execute(
            "INSERT INTO briefing_runs (run_at, region, mode, lead_count) "
            "VALUES (?, ?, ?, ?)",
            (ts, region, mode, len(featured)),
        )
        run_id = cur.lastrowid
        for lead in featured:
            reg = lead.get("registry_id")
            prog = lead.get("program")
            if not reg or not prog:
                continue
            conn.execute(
                "INSERT INTO lead_briefings ("
                "  registry_id, program, last_featured_at, "
                "  last_featured_run_id, lead_score_at_brief, "
                "  last_seen_at_brief, region) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(registry_id, program) DO UPDATE SET "
                "  last_featured_at = excluded.last_featured_at, "
                "  last_featured_run_id = excluded.last_featured_run_id, "
                "  lead_score_at_brief = excluded.lead_score_at_brief, "
                "  last_seen_at_brief = excluded.last_seen_at_brief, "
                "  region = excluded.region",
                (reg, prog, ts, run_id,
                 lead.get("lead_score"), lead.get("last_seen"), region),
            )
        return run_id
