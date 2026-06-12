"""Each run writes into its own out/<command>_<scope>_<stamp>/ folder.

Pins the per-run-folder behavior so a future refactor can't silently
go back to overwriting a fixed all_leads.csv / violation_events.csv /
run_health.json in out/ root (which let a targeted `pipeline` run clobber
a prior nationwide `bulk` run). See RATIONALE.md "Per-run output folders".
"""

import tempfile
import unittest
from datetime import datetime as real_datetime
from pathlib import Path
from unittest.mock import patch

from chemtreat_water_leads import bulk_loader
from chemtreat_water_leads.pipeline import _run_output_dir
from tests._fixtures import make_exporter_zip


# One ECHO-Exporter row that trips a CWA SNC signal (enough to produce a
# lead, no events needed). Mirrors tests/test_no_events_flag.py.
_TX_SNC_ROW = {
    "REGISTRY_ID": "110000000001",
    "FAC_NAME": "Acme Chemical",
    "FAC_STATE": "TX",
    "FAC_NAICS_CODES": "325",
    "NPDES_IDS": "TX0000001",
    "CWA_SNC_FLAG": "Y",
    "CWA_COMPLIANCE_STATUS": "Significant Violator",
}


class TestRunOutputDirNaming(unittest.TestCase):
    """`_run_output_dir` is the single source of the folder name shape."""

    def test_nationwide_scope(self):
        with tempfile.TemporaryDirectory() as d:
            rd = _run_output_dir(Path(d), "bulk", None, "2026-05-27T09:00:00")
            self.assertEqual(rd.name, "bulk_nationwide_20260527-090000")
            self.assertTrue(rd.is_dir())

    def test_state_list_scope(self):
        with tempfile.TemporaryDirectory() as d:
            rd = _run_output_dir(Path(d), "pipeline",
                                 ["WA", "AL", "VA", "LA", "GA"],
                                 "2026-05-27T12:15:00")
            self.assertEqual(rd.name, "pipeline_WA-AL-VA-LA-GA_20260527-121500")

    def test_long_state_list_collapses_to_count(self):
        # 20 states joined would be an unwieldy folder name (>40 chars).
        states = [f"S{i}" for i in range(20)]
        with tempfile.TemporaryDirectory() as d:
            rd = _run_output_dir(Path(d), "pipeline", states,
                                 "2026-05-27T12:15:00")
            self.assertEqual(rd.name, "pipeline_20states_20260527-121500")

    def test_distinct_timestamps_yield_distinct_folders(self):
        with tempfile.TemporaryDirectory() as d:
            a = _run_output_dir(Path(d), "bulk", ["TX"], "2026-05-27T09:00:00")
            b = _run_output_dir(Path(d), "bulk", ["TX"], "2026-05-27T09:00:01")
            self.assertNotEqual(a.name, b.name)


class TestBulkRunWritesToSubfolder(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.exporter_zip = make_exporter_zip(self.tmp_path, [_TX_SNC_ROW])
        self.out_dir = self.tmp_path / "out"
        self.db_path = self.tmp_path / "snap.sqlite"
        self.cache_dir = self.tmp_path / "cache"

    def _run(self):
        with patch.object(bulk_loader, "_download_cached",
                          side_effect=lambda url, c, name: self.exporter_zip):
            bulk_loader.run_bulk(
                out_dir=self.out_dir, db_path=self.db_path,
                cache_dir=self.cache_dir, states=["TX"], include_events=False,
            )

    def test_outputs_land_in_run_folder_not_root(self):
        self._run()

        # Nothing is written directly to out/ root — that was the old
        # overwrite target.
        root_files = sorted(p.name for p in self.out_dir.iterdir() if p.is_file())
        self.assertEqual(root_files, [],
                         f"unexpected files in out/ root: {root_files}")

        run_dirs = [p for p in self.out_dir.iterdir() if p.is_dir()]
        self.assertEqual(len(run_dirs), 1)
        rd = run_dirs[0]
        self.assertRegex(rd.name, r"^bulk_TX_\d{8}-\d{6}$")
        # Inline artifacts only — both irrecoverable from the DB:
        #   run_health.json: captured run-time warnings + drilldown stats
        #   newly_snc_*.csv: compares prior snc_flag which upsert overwrites
        # (the file is skipped when the diff list is empty, so a first
        # run from a clean DB legitimately produces zero of them — only
        # assert presence-or-absence is correct, not exact-one.)
        # The big CSVs (all_leads / violation_events / new_facilities /
        # new_violations) are materialized on demand from snapshot.sqlite
        # via the dump_run module — see RATIONALE.md.
        self.assertTrue((rd / "run_health.json").exists())
        # Things that USED to be written but no longer are.
        for fn in ("all_leads.csv", "violation_events.csv",
                   "READ_ME_FIRST.txt"):
            self.assertFalse(
                (rd / fn).exists(),
                f"{fn} should NOT be written inline — materialize via dump_run",
            )

    def test_second_run_does_not_overwrite_first(self):
        # Control the run-start clock so two back-to-back runs land in
        # distinct, second-resolution folders without a real sleep.
        with patch.object(bulk_loader, "_download_cached",
                          side_effect=lambda url, c, name: self.exporter_zip), \
             patch.object(bulk_loader, "datetime") as mock_dt:
            mock_dt.utcnow.return_value = real_datetime(2026, 5, 27, 9, 0, 0)
            bulk_loader.run_bulk(out_dir=self.out_dir, db_path=self.db_path,
                                 cache_dir=self.cache_dir, states=["TX"],
                                 include_events=False)
            mock_dt.utcnow.return_value = real_datetime(2026, 5, 27, 9, 0, 1)
            bulk_loader.run_bulk(out_dir=self.out_dir, db_path=self.db_path,
                                 cache_dir=self.cache_dir, states=["TX"],
                                 include_events=False)

        run_dirs = sorted(p.name for p in self.out_dir.iterdir() if p.is_dir())
        self.assertEqual(
            run_dirs,
            ["bulk_TX_20260527-090000", "bulk_TX_20260527-090001"],
        )
        # Both folders kept their own run_health.json — first run not clobbered.
        for name in run_dirs:
            self.assertTrue((self.out_dir / name / "run_health.json").exists())


if __name__ == "__main__":
    unittest.main()
