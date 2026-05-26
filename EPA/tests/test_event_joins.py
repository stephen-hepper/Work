"""Event joins fall back to permit/PWSID when REGISTRY_ID is blank."""

import tempfile
import unittest
from pathlib import Path

from chemtreat_water_leads.bulk_loader import (
    stream_npdes_violations, stream_sdwa_violations,
)
from tests._fixtures import make_npdes_zip, make_sdwa_zip


class TestNPDESEventJoin(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_npdes_join_via_permit_id_when_registry_blank(self):
        """Bulk NPDES violation files have no REGISTRY_ID column. The
        join must fall back to NPDES_ID and backfill registry_id from
        the lead's permit map."""
        rows = [{
            "NPDES_ID": "TX0000123",
            "NPDES_VIOLATION_ID": "V001",
            "VIOLATION_CODE": "B0019",
            "VIOLATION_DESC": "BMP Deficiency",
            "RNC_DETECTION_CODE": "N",
        }]
        zip_path = make_npdes_zip(self.tmp_path, rows)
        events = stream_npdes_violations(
            zip_path,
            registry_id_set=set(),   # empty — force permit fallback
            permit_id_set={"TX0000123"},
            permit_to_registry={"TX0000123": "110000ABC"},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["registry_id"], "110000ABC")
        # _normalize_bulk_npdes_event renames permit_id → npdes_id
        # (the schema column name in snapshot.VIOL_COLUMNS).
        self.assertEqual(events[0]["npdes_id"], "TX0000123")
        self.assertEqual(events[0]["program"], "CWA")

    def test_npdes_event_dropped_when_no_match(self):
        rows = [{
            "NPDES_ID": "OH9999999",
            "NPDES_VIOLATION_ID": "V002",
            "VIOLATION_CODE": "X0001",
        }]
        zip_path = make_npdes_zip(self.tmp_path, rows)
        events = stream_npdes_violations(
            zip_path,
            registry_id_set={"110000XYZ"},
            permit_id_set={"TX0000999"},
            permit_to_registry={"TX0000999": "110000XYZ"},
        )
        self.assertEqual(events, [])


class TestSDWAEventJoin(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_sdwa_join_via_pwsid_when_registry_blank(self):
        """SDWA bulk violations carry PWSID, never REGISTRY_ID — PWSID
        fallback is the only working join path."""
        rows = [{
            "PWSID": "TX0010001",
            "VIOLATION_ID": "SDV001",
            "VIOLATION_CODE": "07",   # Treatment Technique
            "VIOLATION_STATUS": "Unaddressed",
            "NON_COMPL_PER_BEGIN_DATE": "01/01/2026",
            "NON_COMPL_PER_END_DATE": "",
        }]
        zip_path = make_sdwa_zip(self.tmp_path, rows)
        events = stream_sdwa_violations(
            zip_path,
            registry_id_set=set(),
            pwsid_set={"TX0010001"},
            pwsid_to_registry={"TX0010001": "110000PWS"},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["registry_id"], "110000PWS")
        # _normalize_bulk_sdwa_event renames pwsid → source_id
        # (schema column name).
        self.assertEqual(events[0]["source_id"], "TX0010001")
        self.assertEqual(events[0]["violation_category"],
                         "Treatment Technique Violation")

    def test_sdwa_event_dropped_when_pwsid_not_in_set(self):
        rows = [{
            "PWSID": "CA9999999",
            "VIOLATION_ID": "SDV002",
            "VIOLATION_CODE": "01",
            "VIOLATION_STATUS": "Resolved",
        }]
        zip_path = make_sdwa_zip(self.tmp_path, rows)
        events = stream_sdwa_violations(
            zip_path,
            registry_id_set=set(),
            pwsid_set={"TX0010001"},
            pwsid_to_registry={"TX0010001": "110000PWS"},
        )
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
