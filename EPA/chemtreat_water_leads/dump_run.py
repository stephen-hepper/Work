"""On-demand materialization of a run's CSVs from `snapshot.sqlite`.

The bulk_loader and pipeline runs no longer write `all_leads.csv` /
`violation_events.csv` / `new_facilities_*.csv` / `new_violations_*.csv`
per run — those are pure views of the DB, recoverable via the
membership tables. Only the irrecoverable artifacts (`run_health.json`,
`newly_snc_*.csv` — which needs the prior `snc_flag` the upsert
overwrites) stay inline. This module is the on-demand materializer
sales runs when they want viewer-ready files for a specific run.

Usage:
    python -m chemtreat_water_leads.dump_run --db ./snapshot.sqlite --latest --out ./materialized
    python -m chemtreat_water_leads.dump_run --db ./snapshot.sqlite --run-id 42 --out ./materialized
    python -m chemtreat_water_leads.dump_run --db ./snapshot.sqlite --list

`new_facilities.csv` / `new_violations.csv` are reconstructed by
intersecting the chosen run's membership with prior membership: a row's
first-ever appearance is the run where it was new. The query is
`MIN(run_id) FROM run_*_membership = ?`. This matches the original
`diff_and_upsert_*` "new" semantics exactly for facility keys and
violation_ids.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path

from . import snapshot

log = logging.getLogger("chemtreat.dump_run")


def resolve_run_id(conn: sqlite3.Connection,
                   run_id: int | None,
                   latest: bool) -> int:
    """Return the run_id the caller asked for, or the latest if `latest=True`.

    Raises ValueError if neither is supplied or the requested run doesn't
    exist — explicit failure beats silently materializing the wrong run.
    """
    if run_id is not None and latest:
        raise ValueError("pass --run-id OR --latest, not both")
    if latest:
        row = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()
        if not row or row[0] is None:
            raise ValueError("no runs in DB yet — run bulk_loader or pipeline first")
        return int(row[0])
    if run_id is None:
        raise ValueError("pass --run-id N or --latest")
    found = conn.execute(
        "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not found:
        raise ValueError(f"run_id {run_id} not found in runs table")
    return run_id


def list_runs(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    """Return recent runs (newest first) with touched-row counts joined in.

    The counts give the user a hint at which run they want: a nationwide
    bulk touches ~40K facilities, a targeted pipeline ~hundreds.
    """
    conn.row_factory = sqlite3.Row
    return list(conn.execute("""
        SELECT
            r.run_id,
            r.run_at,
            r.notes,
            (SELECT COUNT(*) FROM run_facility_membership m
              WHERE m.run_id = r.run_id) AS facilities,
            (SELECT COUNT(*) FROM run_violation_membership m
              WHERE m.run_id = r.run_id) AS violations
        FROM runs r
        ORDER BY r.run_id DESC
        LIMIT ?
    """, (limit,)))


def materialize_run(conn: sqlite3.Connection, run_id: int, out_dir: Path) -> dict:
    """Write the four reconstructable CSVs for `run_id` into `out_dir`.

    Returns a dict of row counts per file so the caller (CLI or scripted
    consumer) can confirm what landed.

    Reconstructs:
      - all_leads.csv         <- snapshot.facilities_in_run
      - violation_events.csv  <- snapshot.violations_in_run
      - new_facilities.csv    <- keys in this run with MIN(run_id) = run_id
      - new_violations.csv    <- same pattern on violation_membership

    Does NOT reconstruct `newly_snc.csv` — that compares current snc_flag
    against the *prior* value, which the upsert overwrites and is gone.
    The original run folder still has that file (kept inline at run time).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    # Standing-inventory dumps — same shape as snapshot.dump_*_csv.
    fac_rows = snapshot.facilities_in_run(conn, run_id)
    counts["all_leads.csv"] = _write_dump(
        out_dir / "all_leads.csv", snapshot.FAC_CSV_COLUMNS, fac_rows)
    viol_rows = snapshot.violations_in_run(conn, run_id)
    counts["violation_events.csv"] = _write_dump(
        out_dir / "violation_events.csv", snapshot.VIOL_CSV_COLUMNS, viol_rows)

    # Diff CSVs — keys whose MIN(run_id) in the membership table IS this
    # run, i.e. the run that first saw them. Matches `fac_diff["new"]` /
    # `viol_diff["new"]` semantics at original run time.
    cols_f = snapshot.FAC_CSV_COLUMNS
    new_fac = list(conn.execute(f"""
        SELECT {', '.join('f.' + c for c in cols_f)}
        FROM facilities f
        JOIN run_facility_membership m
          ON f.registry_id = m.registry_id AND f.program = m.program
        WHERE m.run_id = ?
          AND (
            SELECT MIN(run_id) FROM run_facility_membership mm
             WHERE mm.registry_id = f.registry_id
               AND mm.program = f.program
          ) = ?
        ORDER BY f.lead_score DESC, f.company
    """, (run_id, run_id)))
    counts["new_facilities.csv"] = _write_dump(
        out_dir / "new_facilities.csv", cols_f, new_fac)

    cols_v = snapshot.VIOL_CSV_COLUMNS
    new_viol = list(conn.execute(f"""
        SELECT {', '.join('v.' + c for c in cols_v)}
        FROM violations v
        JOIN run_violation_membership m ON v.violation_id = m.violation_id
        WHERE m.run_id = ?
          AND (
            SELECT MIN(run_id) FROM run_violation_membership mm
             WHERE mm.violation_id = v.violation_id
          ) = ?
        ORDER BY v.registry_id, v.period_end DESC
    """, (run_id, run_id)))
    counts["new_violations.csv"] = _write_dump(
        out_dir / "new_violations.csv", cols_v, new_viol)

    # run_health.json — mirrored into runs.run_health_json by
    # bulk_loader / pipeline at end of run (since 2026-06-16). Lets
    # users upload one folder to the viewer instead of grabbing the
    # JSON from out/<run-folder> separately. Legacy runs that pre-date
    # the column have NULL; skip cleanly with a log so the user knows
    # they'll need the original run folder for that one file.
    health_json = snapshot.get_run_health(conn, run_id)
    if health_json is not None:
        (out_dir / "run_health.json").write_text(health_json, encoding="utf-8")
        counts["run_health.json"] = 1
    else:
        log.info("run %d pre-dates run_health_json column; the file is "
                 "still available in the original out/<run-folder>/",
                 run_id)

    return counts


def _write_dump(path: Path, cols: list[str], rows) -> int:
    """Write `rows` as a CSV with headers `cols`. Returns row count.

    Mirrors `snapshot._write_dump` but here for the diff queries that
    don't go through the existing `dump_*_csv` helpers. Uses the same
    `snapshot._coerce_for_csv` so the serialized shape stays identical
    (booleans as "True"/"False", None as empty string, etc.) — the
    viewer's CSV detection logic depends on this.
    """
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        n = 0
        for r in rows:
            w.writerow([snapshot._coerce_for_csv(c, r[c]) for c in cols])
            n += 1
    return n


def _cli() -> None:
    p = argparse.ArgumentParser(
        description="Materialize a run's CSVs from snapshot.sqlite on demand.",
    )
    p.add_argument("--db", default="./snapshot.sqlite",
                   help="Snapshot DB path (default: ./snapshot.sqlite)")
    p.add_argument("--out", default=None,
                   help="Output directory (default: ./materialized/run_<id>)")
    p.add_argument("--run-id", type=int, default=None,
                   help="Specific run_id to materialize")
    p.add_argument("--latest", action="store_true",
                   help="Materialize the most recent run")
    p.add_argument("--list", action="store_true",
                   help="List recent runs and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    with snapshot.open_db(db_path) as conn:
        if args.list:
            runs = list_runs(conn)
            if not runs:
                print("(no runs recorded yet)")
                return
            print(f"{'run_id':>6}  {'run_at':<19}  {'facilities':>10}  "
                  f"{'violations':>10}  notes")
            for r in runs:
                print(f"{r['run_id']:>6}  {r['run_at']:<19}  "
                      f"{r['facilities']:>10}  {r['violations']:>10}  "
                      f"{r['notes'] or ''}")
            return

        try:
            run_id = resolve_run_id(conn, args.run_id, args.latest)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)

        out_dir = Path(args.out) if args.out else Path(f"./materialized/run_{run_id}")
        counts = materialize_run(conn, run_id, out_dir)

    print(f"Materialized run {run_id} into {out_dir}/")
    for name, n in counts.items():
        # run_health.json is a single doc, not a row count — show
        # "(present)" instead of "1 rows".
        suffix = "(present)" if name == "run_health.json" else f"{n:>8} rows"
        print(f"  {name:25} {suffix}")
    print()
    has_health = (out_dir / "run_health.json").exists()
    if has_health:
        print("Upload all_leads.csv, violation_events.csv, and run_health.json "
              "to the viewer — all from this folder.")
    else:
        print("Upload all_leads.csv and violation_events.csv to the viewer. "
              "This run pre-dates the run_health.json mirroring — grab the "
              "JSON from the original out/<run-folder>/ if you want the "
              "Run Health tab.")


if __name__ == "__main__":
    _cli()
