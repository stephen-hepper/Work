"""Pin behavior of stream_cso_inventory.

The National CSO Inventory ships as one row per CSO outfall (40
columns; we read only NPDES_ID). Many rows per permit is the norm —
the live 2026-06-15 refresh has 896 distinct permits with everything
from 1 outfall to many dozens. Streamer collapses to a single boolean
per permit.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from chemtreat_water_leads import bulk_loader
from tests._fixtures import make_cso_inventory_zip


class TestCSOInventoryStreamer(unittest.TestCase):

    def test_empty_filter_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            zp = make_cso_inventory_zip(Path(td), [])
            sigs = bulk_loader.stream_cso_inventory(
                zp, kept_npdes_permits=set())
        self.assertEqual(sigs, {})

    def test_filter_actually_filters(self):
        rows = [
            {"NPDES_ID": "IL0001", "FACILITY_TYPE_INDICATOR": "POTW"},
            {"NPDES_ID": "TX9999", "FACILITY_TYPE_INDICATOR": "POTW"},
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = make_cso_inventory_zip(Path(td), rows)
            sigs = bulk_loader.stream_cso_inventory(
                zp, kept_npdes_permits={"IL0001"})
        self.assertEqual(set(sigs), {"IL0001"})

    def test_dedup_many_outfalls_per_permit(self):
        """One permit with 5 CSO outfalls collapses to a single signal
        with has_combined_sewer_system=1. The live data has permits
        with tens of outfalls — without dedup the rollup would still
        be correct (set/dict semantics) but the test pins the contract."""
        rows = [
            {"NPDES_ID": "IL0001", "PERM_FEATURE_NMBR": str(i),
             "FACILITY_TYPE_INDICATOR": "POTW"}
            for i in range(1, 6)
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = make_cso_inventory_zip(Path(td), rows)
            sigs = bulk_loader.stream_cso_inventory(
                zp, kept_npdes_permits={"IL0001"})
        self.assertEqual(set(sigs), {"IL0001"})
        self.assertEqual(sigs["IL0001"], {"has_combined_sewer_system": 1})

    def test_signal_is_binary(self):
        """Every row in this file documents a CSO outfall by
        definition, so any matching row → has_combined_sewer_system=1.
        We don't care about facility type or outfall character columns."""
        rows = [
            {"NPDES_ID": "IL0001",
             "FACILITY_TYPE_INDICATOR": "NON-POTW",   # rare but it happens
             "PF_CHARACTER": "CSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = make_cso_inventory_zip(Path(td), rows)
            sigs = bulk_loader.stream_cso_inventory(
                zp, kept_npdes_permits={"IL0001"})
        self.assertEqual(sigs["IL0001"]["has_combined_sewer_system"], 1)

    def test_blank_npdes_id_skipped(self):
        rows = [
            {"NPDES_ID": "", "FACILITY_TYPE_INDICATOR": "POTW"},
            {"NPDES_ID": "IL0001", "FACILITY_TYPE_INDICATOR": "POTW"},
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = make_cso_inventory_zip(Path(td), rows)
            sigs = bulk_loader.stream_cso_inventory(
                zp, kept_npdes_permits={"IL0001"})
        self.assertEqual(set(sigs), {"IL0001"})

    def test_missing_csv_raises_loud(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = tmp / "broken.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("renamed.csv", "NPDES_ID\nIL0001\n")
            with self.assertRaises(RuntimeError) as ctx:
                bulk_loader.stream_cso_inventory(
                    zp, kept_npdes_permits={"IL0001"})
            self.assertIn("data-downloads", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
