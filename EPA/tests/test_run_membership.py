"""Per-row run-membership history in snapshot.sqlite.

Verifies that record_run + diff_and_upsert_* populate the two
run_*_membership tables so "which runs touched this row?" is answerable
via a single JOIN, with "first seen in any run" = MIN(run_id) per key.
"""

import tempfile
import unittest
from pathlib import Path

from chemtreat_water_leads import snapshot


def _make_facility(reg, program, score=42, snc=None):
    return {
        "lead_score": score,
        "score_reasons": "",
        "outreach_posture": "no_events",
        "program": program,
        "registry_id": reg,
        "company": f"Co {reg}",
        "state": "TX",
        "snc_status": snc,
        "tag_active_snc": bool(snc),
    }


def _make_violation(vid, reg, status="Unresolved"):
    return {
        "violation_id": vid,
        "registry_id": reg,
        "program": "CWA",
        "status": status,
    }


class TestRunMembership(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "snap.sqlite"
        self.addCleanup(self._tmp.cleanup)

    def test_record_run_returns_id(self):
        with snapshot.open_db(self.db_path) as conn:
            first = snapshot.record_run(conn, notes="run-1")
            second = snapshot.record_run(conn, notes="run-2")
        self.assertIsInstance(first, int)
        self.assertEqual(second, first + 1)

    def test_membership_rows_written_on_upsert(self):
        leads = [_make_facility("R1", "CWA"), _make_facility("R2", "SDWA")]
        events = [_make_violation("V1", "R1"), _make_violation("V2", "R1")]
        with snapshot.open_db(self.db_path) as conn:
            run_id = snapshot.record_run(conn, notes="bulk")
            snapshot.diff_and_upsert_facilities(conn, leads, run_id)
            snapshot.diff_and_upsert_violations(conn, events, run_id)
            fac_keys = sorted(
                tuple(r) for r in conn.execute(
                    "SELECT registry_id, program FROM run_facility_membership "
                    "WHERE run_id = ? ORDER BY registry_id", (run_id,))
            )
            viol_keys = sorted(
                r[0] for r in conn.execute(
                    "SELECT violation_id FROM run_violation_membership "
                    "WHERE run_id = ?", (run_id,))
            )
        self.assertEqual(fac_keys, [("R1", "CWA"), ("R2", "SDWA")])
        self.assertEqual(viol_keys, ["V1", "V2"])

    def test_min_run_id_per_key_equals_first_run(self):
        """A facility touched by runs 1 and 2 has MIN(run_id)=1.
        That's how callers should derive 'first seen in any run'."""
        with snapshot.open_db(self.db_path) as conn:
            r1 = snapshot.record_run(conn, notes="first")
            snapshot.diff_and_upsert_facilities(
                conn, [_make_facility("R1", "CWA")], r1)
            r2 = snapshot.record_run(conn, notes="second")
            snapshot.diff_and_upsert_facilities(
                conn, [_make_facility("R1", "CWA", score=99),
                       _make_facility("R3", "CWA")], r2)
            rows = sorted(tuple(r) for r in conn.execute(
                "SELECT registry_id, program, MIN(run_id) "
                "FROM run_facility_membership GROUP BY 1, 2"
            ))
        self.assertEqual(rows, [("R1", "CWA", r1), ("R3", "CWA", r2)])

    def test_duplicate_upsert_in_one_run_is_idempotent(self):
        """Same (registry_id, program) shows up twice in a run's `current`
        list — the INSERT OR IGNORE keeps the membership PK clean."""
        leads = [_make_facility("R1", "CWA"), _make_facility("R1", "CWA")]
        with snapshot.open_db(self.db_path) as conn:
            run_id = snapshot.record_run(conn, notes="dup")
            snapshot.diff_and_upsert_facilities(conn, leads, run_id)
            count = conn.execute(
                "SELECT COUNT(*) FROM run_facility_membership "
                "WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_violation_without_id_does_not_write_membership(self):
        """Events without violation_id are skipped on upsert (existing
        behavior); they must also be absent from the membership table."""
        events = [
            _make_violation("V1", "R1"),
            _make_violation(None, "R1"),   # dropped
            {"violation_id": "", "registry_id": "R1", "program": "CWA"},  # dropped
        ]
        with snapshot.open_db(self.db_path) as conn:
            run_id = snapshot.record_run(conn, notes="partial")
            snapshot.diff_and_upsert_violations(conn, events, run_id)
            ids = [r[0] for r in conn.execute(
                "SELECT violation_id FROM run_violation_membership "
                "WHERE run_id = ?", (run_id,))]
        self.assertEqual(ids, ["V1"])

    def test_facilities_in_run_helper(self):
        with snapshot.open_db(self.db_path) as conn:
            r1 = snapshot.record_run(conn, notes="A")
            snapshot.diff_and_upsert_facilities(
                conn, [_make_facility("R1", "CWA", score=10),
                       _make_facility("R2", "CWA", score=80)], r1)
            r2 = snapshot.record_run(conn, notes="B")
            snapshot.diff_and_upsert_facilities(
                conn, [_make_facility("R3", "CWA", score=50)], r2)
            run1 = snapshot.facilities_in_run(conn, r1)
            run2 = snapshot.facilities_in_run(conn, r2)
        self.assertEqual([row["registry_id"] for row in run1], ["R2", "R1"])
        self.assertEqual([row["registry_id"] for row in run2], ["R3"])

    def test_violations_in_run_helper(self):
        with snapshot.open_db(self.db_path) as conn:
            r1 = snapshot.record_run(conn, notes="A")
            snapshot.diff_and_upsert_violations(
                conn, [_make_violation("V1", "R1"),
                       _make_violation("V2", "R1")], r1)
            r2 = snapshot.record_run(conn, notes="B")
            snapshot.diff_and_upsert_violations(
                conn, [_make_violation("V3", "R2")], r2)
            run1_ids = {row["violation_id"]
                        for row in snapshot.violations_in_run(conn, r1)}
            run2_ids = {row["violation_id"]
                        for row in snapshot.violations_in_run(conn, r2)}
        self.assertEqual(run1_ids, {"V1", "V2"})
        self.assertEqual(run2_ids, {"V3"})


if __name__ == "__main__":
    unittest.main()
