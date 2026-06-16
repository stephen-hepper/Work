"""On-demand materialization of run CSVs from snapshot.sqlite.

Pins the contract the bulk_loader/pipeline runners depend on for their
end-of-run log message: that `dump_run --run-id N --out X` produces
identical-shape files (all_leads.csv, violation_events.csv,
new_facilities.csv, new_violations.csv) to what the old inline writes
produced. If this drifts, the viewer's CSV auto-detect breaks for
materialized runs.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from chemtreat_water_leads import dump_run, snapshot


def _seed_two_runs(db_path: Path) -> tuple[int, int]:
    """Two minimal runs: run 1 introduces two facilities + one violation;
    run 2 adds a third facility + a second violation and refreshes the
    originals. Returns the two run_ids."""
    leads_r1 = [
        {"registry_id": "R1", "program": "CWA", "permit_id": "P1",
         "lead_score": 80, "company": "Alpha", "state": "TX"},
        {"registry_id": "R2", "program": "CWA", "permit_id": "P2",
         "lead_score": 60, "company": "Beta", "state": "WA"},
    ]
    leads_r2 = leads_r1 + [
        {"registry_id": "R3", "program": "SDWA", "permit_id": "",
         "lead_score": 70, "company": "Gamma", "state": "FL"},
    ]
    events_r1 = [
        {"registry_id": "R1", "program": "CWA", "violation_id": "V1",
         "period_end": "2026-01-15", "parameter": "Phosphorus"},
    ]
    events_r2 = events_r1 + [
        {"registry_id": "R3", "program": "SDWA", "violation_id": "V2",
         "period_end": "2026-02-20", "parameter": "Coliform"},
    ]
    with snapshot.open_db(db_path) as conn:
        run1 = snapshot.record_run(conn, notes="seed_r1",
                                    now="2026-06-10T08:00:00")
        snapshot.diff_and_upsert_facilities(conn, leads_r1, run1,
                                             now="2026-06-10T08:00:00")
        snapshot.diff_and_upsert_violations(conn, events_r1, run1,
                                             now="2026-06-10T08:00:00")
        run2 = snapshot.record_run(conn, notes="seed_r2",
                                    now="2026-06-11T08:00:00")
        snapshot.diff_and_upsert_facilities(conn, leads_r2, run2,
                                             now="2026-06-11T08:00:00")
        snapshot.diff_and_upsert_violations(conn, events_r2, run2,
                                             now="2026-06-11T08:00:00")
    return run1, run2


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return reader.fieldnames or [], rows


class TestMaterializeRun(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.db = self.tmp / "snap.sqlite"
        self.run1, self.run2 = _seed_two_runs(self.db)
        self.out = self.tmp / "materialized"

    def test_all_four_csvs_written(self):
        with snapshot.open_db(self.db) as conn:
            counts = dump_run.materialize_run(conn, self.run2, self.out)
        for fn in ("all_leads.csv", "violation_events.csv",
                   "new_facilities.csv", "new_violations.csv"):
            self.assertTrue((self.out / fn).exists(), f"missing {fn}")
            self.assertIn(fn, counts)

    def test_all_leads_matches_facilities_in_run(self):
        """The materialized CSV must carry every facility row the run
        touched — not just newly-discovered ones — so the viewer's
        Inventory tab works against any run."""
        with snapshot.open_db(self.db) as conn:
            dump_run.materialize_run(conn, self.run2, self.out)
        _, rows = _read_csv(self.out / "all_leads.csv")
        keys = {(r["registry_id"], r["program"]) for r in rows}
        # Run 2 touched all three facilities.
        self.assertEqual(keys, {("R1", "CWA"), ("R2", "CWA"), ("R3", "SDWA")})

    def test_new_facilities_only_first_appearance(self):
        """`new_facilities.csv` for run 2 = facilities whose first-ever
        membership row is in run 2. R1/R2 appeared in run 1, so only R3
        is new in run 2."""
        with snapshot.open_db(self.db) as conn:
            dump_run.materialize_run(conn, self.run2, self.out)
        _, rows = _read_csv(self.out / "new_facilities.csv")
        keys = {(r["registry_id"], r["program"]) for r in rows}
        self.assertEqual(keys, {("R3", "SDWA")})

    def test_new_facilities_for_first_run_is_everything(self):
        """For the FIRST run in the DB, every touched key is new."""
        with snapshot.open_db(self.db) as conn:
            dump_run.materialize_run(conn, self.run1, self.out)
        _, rows = _read_csv(self.out / "new_facilities.csv")
        keys = {(r["registry_id"], r["program"]) for r in rows}
        self.assertEqual(keys, {("R1", "CWA"), ("R2", "CWA")})

    def test_new_violations_only_first_appearance(self):
        with snapshot.open_db(self.db) as conn:
            dump_run.materialize_run(conn, self.run2, self.out)
        _, rows = _read_csv(self.out / "new_violations.csv")
        self.assertEqual({r["violation_id"] for r in rows}, {"V2"})

    def test_column_shape_matches_snapshot_dump(self):
        """Materialized files must use the same column order as the old
        `snapshot.dump_*_csv` writes so the viewer's auto-detect (which
        keys on column names like `lead_score`, `score_reasons`,
        `exceedance_pct`, `violation_description`) doesn't regress."""
        with snapshot.open_db(self.db) as conn:
            dump_run.materialize_run(conn, self.run2, self.out)
        leads_hdr, _ = _read_csv(self.out / "all_leads.csv")
        viol_hdr, _ = _read_csv(self.out / "violation_events.csv")
        self.assertEqual(leads_hdr, snapshot.FAC_CSV_COLUMNS)
        self.assertEqual(viol_hdr, snapshot.VIOL_CSV_COLUMNS)

    def test_run_health_materialized_when_mirrored(self):
        """When `runs.run_health_json` is populated (as it is on any
        bulk_loader / pipeline run since 2026-06-16), dump_run writes
        it to `<out>/run_health.json` so the viewer-uploadable folder
        is self-contained."""
        sample_json = '{"schema_version": 2, "totals": {"leads": 2}}'
        with snapshot.open_db(self.db) as conn:
            snapshot.set_run_health(conn, self.run2, sample_json)
            counts = dump_run.materialize_run(conn, self.run2, self.out)
        health_file = self.out / "run_health.json"
        self.assertTrue(health_file.exists(),
            "dump_run did not write run_health.json")
        # Byte-identical round-trip — we're not re-serializing, just
        # mirroring the stored text.
        self.assertEqual(health_file.read_text(encoding="utf-8"), sample_json)
        self.assertEqual(counts.get("run_health.json"), 1)

    def test_run_health_skipped_on_legacy_run(self):
        """Runs created before the 2026-06-16 schema change have
        run_health_json = NULL. dump_run must NOT write a stub file
        or crash; it skips cleanly with a log line."""
        # `_seed_two_runs` doesn't populate run_health_json — both
        # runs are legacy by construction in this test.
        with snapshot.open_db(self.db) as conn:
            health = snapshot.get_run_health(conn, self.run2)
            self.assertIsNone(health, "fixture should pre-date the mirror")
            counts = dump_run.materialize_run(conn, self.run2, self.out)
        self.assertFalse((self.out / "run_health.json").exists(),
            "legacy runs should not emit a stub run_health.json")
        self.assertNotIn("run_health.json", counts)


class TestSetGetRunHealth(unittest.TestCase):
    """Direct unit on the snapshot helpers — covers the round-trip and
    the legacy-NULL contract independent of dump_run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.db = self.tmp / "snap.sqlite"
        # Create a fresh DB + one run row
        with snapshot.open_db(self.db) as conn:
            self.run_id = snapshot.record_run(conn, notes="t",
                                               now="2026-06-16T08:00:00")

    def test_get_returns_none_before_set(self):
        with snapshot.open_db(self.db) as conn:
            self.assertIsNone(snapshot.get_run_health(conn, self.run_id))

    def test_round_trip_preserves_bytes(self):
        """JSON gets stored and read back exactly — no re-serialization
        round-trip changes whitespace, key order, or unicode escapes."""
        text = '{"schema_version": 2, "warnings": ["é \\u00e9"]}'
        with snapshot.open_db(self.db) as conn:
            snapshot.set_run_health(conn, self.run_id, text)
            got = snapshot.get_run_health(conn, self.run_id)
        self.assertEqual(got, text)

    def test_set_is_idempotent(self):
        """Last write wins — re-calling set_run_health overwrites
        without raising. Lets a retry / re-run land cleanly."""
        with snapshot.open_db(self.db) as conn:
            snapshot.set_run_health(conn, self.run_id, '{"v": 1}')
            snapshot.set_run_health(conn, self.run_id, '{"v": 2}')
            self.assertEqual(snapshot.get_run_health(conn, self.run_id),
                             '{"v": 2}')

    def test_get_returns_none_for_unknown_run(self):
        with snapshot.open_db(self.db) as conn:
            self.assertIsNone(snapshot.get_run_health(conn, 9999))


class TestResolveRunId(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.db = self.tmp / "snap.sqlite"
        self.run1, self.run2 = _seed_two_runs(self.db)

    def test_latest_picks_max_run_id(self):
        with snapshot.open_db(self.db) as conn:
            self.assertEqual(
                dump_run.resolve_run_id(conn, run_id=None, latest=True),
                self.run2,
            )

    def test_explicit_run_id_passes_through(self):
        with snapshot.open_db(self.db) as conn:
            self.assertEqual(
                dump_run.resolve_run_id(conn, run_id=self.run1, latest=False),
                self.run1,
            )

    def test_both_args_rejected(self):
        with snapshot.open_db(self.db) as conn:
            with self.assertRaises(ValueError):
                dump_run.resolve_run_id(conn, run_id=self.run1, latest=True)

    def test_neither_arg_rejected(self):
        with snapshot.open_db(self.db) as conn:
            with self.assertRaises(ValueError):
                dump_run.resolve_run_id(conn, run_id=None, latest=False)

    def test_unknown_run_id_rejected(self):
        with snapshot.open_db(self.db) as conn:
            with self.assertRaises(ValueError):
                dump_run.resolve_run_id(conn, run_id=9999, latest=False)


class TestListRuns(unittest.TestCase):

    def test_list_runs_newest_first_with_counts(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "snap.sqlite"
            r1, r2 = _seed_two_runs(db)
            with snapshot.open_db(db) as conn:
                runs = dump_run.list_runs(conn)
            self.assertEqual([r["run_id"] for r in runs], [r2, r1])
            # Run 2 touched 3 facilities, 2 violations. Run 1: 2 + 1.
            counts_r2 = next(r for r in runs if r["run_id"] == r2)
            self.assertEqual(counts_r2["facilities"], 3)
            self.assertEqual(counts_r2["violations"], 2)


if __name__ == "__main__":
    unittest.main()
