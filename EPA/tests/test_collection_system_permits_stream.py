"""Pin behavior of stream_collection_system_permits.

The CSV is one row per (permit, collection-system identifier). Real
EPA data has 99% single-row permits but a small tail (one with 6 sub-
systems in the 2026-06-15 refresh) — that tail is exactly where the
aggregation contract matters.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from chemtreat_water_leads import bulk_loader
from tests._fixtures import make_sewer_overflow_zip


class TestCollectionSystemPermitsStreamer(unittest.TestCase):

    def _build(self, tmp: Path, permits: list[dict]) -> Path:
        # Events/types empty — this streamer reads only the permits CSV
        return make_sewer_overflow_zip(tmp, events=[], types=[],
                                       permits=permits)

    def test_empty_filter_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            zp = self._build(Path(td), [])
            sigs = bulk_loader.stream_collection_system_permits(
                zp, kept_npdes_permits=set())
        self.assertEqual(sigs, {})

    def test_filter_actually_filters(self):
        permits = [
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "001",
             "collection_system_population": "5000",
             "percent_collection_system_css": "0"},
            {"permit_identifier": "TX9999",   # NOT in scope
             "collection_system_identifier": "001",
             "collection_system_population": "5000",
             "percent_collection_system_css": "0"},
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = self._build(Path(td), permits)
            sigs = bulk_loader.stream_collection_system_permits(
                zp, kept_npdes_permits={"IL0001"})
        self.assertEqual(set(sigs), {"IL0001"})

    def test_single_system_permit(self):
        """The common case: one row per permit. Signal mirrors the row."""
        permits = [
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "001",
             "collection_system_population": "12345",
             "percent_collection_system_css": "0"},
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = self._build(Path(td), permits)
            sigs = bulk_loader.stream_collection_system_permits(
                zp, kept_npdes_permits={"IL0001"})
        sig = sigs["IL0001"]
        self.assertEqual(sig["collection_system_population"], 12345)
        self.assertEqual(sig["percent_collection_system_css"], 0)
        self.assertEqual(sig["has_combined_sewer_system"], 0)

    def test_multi_system_aggregation(self):
        """A permit covering 3 sub-systems: population SUMS, css_pct
        takes MAX, has_combined flips to 1 because at least one
        system is combined.

        Mirrors the live-data shape of MS0061743 (6 sub-systems
        aggregating to 94,517 population) — except this fixture also
        stresses the max-css branch which the live data didn't exhibit.
        """
        permits = [
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "001",
             "collection_system_population": "5000",
             "percent_collection_system_css": "0"},     # sanitary-only
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "002",
             "collection_system_population": "10000",
             "percent_collection_system_css": "75"},    # mostly combined
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "003",
             "collection_system_population": "20000",
             "percent_collection_system_css": "20"},    # partially combined
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = self._build(Path(td), permits)
            sigs = bulk_loader.stream_collection_system_permits(
                zp, kept_npdes_permits={"IL0001"})
        sig = sigs["IL0001"]
        self.assertEqual(sig["collection_system_population"], 35000)  # 5+10+20K
        self.assertEqual(sig["percent_collection_system_css"], 75)    # max
        self.assertEqual(sig["has_combined_sewer_system"], 1)

    def test_blank_and_unparseable_treated_as_zero(self):
        """EPA occasionally leaves population/css blank or "NA".
        Treat as 0 — defensive, same convention as the DMR streamer's
        `_safe_pct` helper."""
        permits = [
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "001",
             "collection_system_population": "",
             "percent_collection_system_css": "NA"},
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "002",
             "collection_system_population": "1000",
             "percent_collection_system_css": ""},
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = self._build(Path(td), permits)
            sigs = bulk_loader.stream_collection_system_permits(
                zp, kept_npdes_permits={"IL0001"})
        sig = sigs["IL0001"]
        self.assertEqual(sig["collection_system_population"], 1000)
        self.assertEqual(sig["percent_collection_system_css"], 0)
        self.assertEqual(sig["has_combined_sewer_system"], 0)

    def test_css_exactly_zero_does_not_set_combined(self):
        """css_pct=0 is the sanitary-only case — keep
        has_combined_sewer_system at 0. Off-by-one regression guard."""
        permits = [
            {"permit_identifier": "IL0001",
             "collection_system_identifier": "001",
             "collection_system_population": "1000",
             "percent_collection_system_css": "0"},
        ]
        with tempfile.TemporaryDirectory() as td:
            zp = self._build(Path(td), permits)
            sigs = bulk_loader.stream_collection_system_permits(
                zp, kept_npdes_permits={"IL0001"})
        self.assertEqual(sigs["IL0001"]["has_combined_sewer_system"], 0)

    def test_missing_csv_raises_loud(self):
        """If EPA renames the file in the zip, fail loudly — same
        convention as the events streamer."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = tmp / "broken.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("renamed_permits.csv",
                            "permit_identifier\nIL0001\n")
            with self.assertRaises(RuntimeError) as ctx:
                bulk_loader.stream_collection_system_permits(
                    zp, kept_npdes_permits={"IL0001"})
            self.assertIn("data-downloads", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
