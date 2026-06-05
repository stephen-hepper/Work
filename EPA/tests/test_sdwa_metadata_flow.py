"""Pin that the 4 new SDWA metadata columns flow through both paths.

`echo_client.SDW_WANTED_COLUMNS` has requested
PopulationServedCount / PWSTypeDesc / OwnerDesc / PrimarySourceDesc
since before this change, but pipeline._flatten_facility used to drop
them on the floor — the data was in the raw ECHO dict and never
reached the CSV. This test pins the flatten so a refactor can't
silently re-introduce the drop.

Bulk side: ECHO Exporter doesn't carry these columns, so bulk leads
must initialize the keys to None (not omit them — snapshot's upsert
uses .get() which would bind NULL anyway, but the explicit init keeps
the lead-row schema parallel across paths and makes the asymmetry
visible in the source).
"""

from __future__ import annotations

import unittest

from chemtreat_water_leads import bulk_loader, pipeline


SDWA_METADATA_KEYS = (
    "population_served",
    "system_type",
    "owner_type",
    "primary_source",
)


class TestPipelineFlattenExtractsSdwaMetadata(unittest.TestCase):
    """The API path's `_flatten_facility` must extract the 4 SDWA
    metadata fields from the raw ECHO response. echo_client.py already
    requests them via SDW_WANTED_COLUMNS."""

    def test_sdwa_raw_with_all_four_fields(self):
        raw = {
            "RegistryID": "110000999001",
            "PWSName": "EXAMPLE WATER AUTHORITY",
            "PWSId": "ST1234567",
            "PopulationServedCount": 12500,
            "PWSTypeDesc": "Community Water System",
            "OwnerDesc": "Local Government",
            "PrimarySourceDesc": "Surface Water",
            "SNCFlag": "N",
        }
        out = pipeline._flatten_facility(raw, "SDWA")
        self.assertEqual(out["population_served"], 12500)
        self.assertEqual(out["system_type"], "Community Water System")
        self.assertEqual(out["owner_type"], "Local Government")
        self.assertEqual(out["primary_source"], "Surface Water")

    def test_sdwa_raw_missing_fields_picks_none(self):
        """`pick()` returns None when no source key is populated.
        Older SDW responses (or partial qcolumns) shouldn't crash the
        flatten step."""
        raw = {"RegistryID": "R", "PWSName": "X", "SNCFlag": "N"}
        out = pipeline._flatten_facility(raw, "SDWA")
        for k in SDWA_METADATA_KEYS:
            with self.subTest(key=k):
                self.assertIsNone(out[k])

    def test_cwa_raw_has_empty_sdwa_metadata(self):
        """CWA raws don't carry PWS* fields. The 4 keys must still
        appear on the lead row (so the CSV header is stable) and be
        None (no false signal)."""
        raw = {"RegistryID": "R", "CWPName": "X", "SNCFlag": "N",
               "CWPSNCStatus": "No Violation Identified"}
        out = pipeline._flatten_facility(raw, "CWA")
        for k in SDWA_METADATA_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, out)
                self.assertIsNone(out[k])


class TestBulkLoaderBuildLeadRowInitializesKeys(unittest.TestCase):
    """ECHO Exporter doesn't carry PWS metadata at the facility level
    (verified empirically — see MEMORY.md "SDWA bulk has limited
    facility-level signals"). The bulk row builder still must
    initialize the keys to None on every program branch so the
    snapshot upsert / CSV dump column shape matches across paths."""

    def test_sdwa_bulk_row_carries_none_keys(self):
        prog_raw = {
            "RegistryID": "R",
            "FacName": "X",
            "SNCFlag": "Y",
            "SNC": "Significant/Category I Noncompliance",
            "Feas": "0",
        }
        row = bulk_loader._build_lead_row(prog_raw, "SDWA", 40, ["+40: SNC"])
        for k in SDWA_METADATA_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, row)
                self.assertIsNone(row[k])

    def test_cwa_bulk_row_carries_none_keys(self):
        prog_raw = {
            "RegistryID": "R",
            "FacName": "X",
            "SNCFlag": "Y",
            "CWPSNCStatus": "Significant/Category I Noncompliance",
            "CWPFormalEaCnt": "1",
        }
        row = bulk_loader._build_lead_row(prog_raw, "CWA", 55, ["+40", "+15"])
        for k in SDWA_METADATA_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, row)
                self.assertIsNone(row[k])


class TestSnapshotSchemaIncludesNewColumns(unittest.TestCase):
    """Snapshot's FAC_COLUMNS is the single source of truth for the
    CSV header — adding to it auto-flows through dump_facilities_csv.
    Pin the four new keys so a column-rename can't silently drop them
    from the CSV."""

    def test_fac_columns_have_sdwa_metadata(self):
        from chemtreat_water_leads import snapshot
        for k in SDWA_METADATA_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, snapshot.FAC_COLUMNS)
                self.assertIn(k, snapshot.FAC_CSV_COLUMNS)
        # Type sanity: population is integer, the rest text.
        self.assertEqual(snapshot.FAC_COLUMNS["population_served"], "INTEGER")
        self.assertEqual(snapshot.FAC_COLUMNS["system_type"], "TEXT")
        self.assertEqual(snapshot.FAC_COLUMNS["owner_type"], "TEXT")
        self.assertEqual(snapshot.FAC_COLUMNS["primary_source"], "TEXT")


if __name__ == "__main__":
    unittest.main()
