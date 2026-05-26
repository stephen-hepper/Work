"""SDWA leads pass bulk discovery without NAICS; CWA leads need NAICS."""

import tempfile
import unittest
from pathlib import Path

from chemtreat_water_leads.bulk_loader import stream_echo_exporter
from tests._fixtures import make_exporter_zip


class TestDiscovery(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_sdwa_emitted_without_naics_match(self):
        """A public water system with an SDWA SNC flag should be kept
        even though FAC_NAICS_CODES is blank."""
        rows = [{
            "REGISTRY_ID": "110000000001",
            "FAC_NAME": "Smalltown Water Authority",
            "FAC_STATE": "TX",
            "FAC_NAICS_CODES": "",
            "SDWA_IDS": "TX0010001",
            "SDWA_SNC_FLAG": "Y",
            "SDWA_COMPLIANCE_STATUS": "Significant Violator",
        }]
        zip_path = make_exporter_zip(self.tmp_path, rows)
        yielded = list(stream_echo_exporter(zip_path, naics_prefixes=["325"]))
        self.assertEqual(len(yielded), 1)
        _, program, _ = yielded[0]
        self.assertEqual(program, "SDWA")

    def test_cwa_dropped_when_naics_does_not_match(self):
        """A CWA facility whose NAICS code is outside TARGET_NAICS should
        be dropped (the existing industrial-NAICS gate)."""
        rows = [{
            "REGISTRY_ID": "110000000002",
            "FAC_NAME": "Off-target CWA Facility",
            "FAC_STATE": "TX",
            "FAC_NAICS_CODES": "541110",   # legal services, not industrial
            "NPDES_IDS": "TX0000123",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant Violator",
        }]
        zip_path = make_exporter_zip(self.tmp_path, rows)
        yielded = list(stream_echo_exporter(zip_path, naics_prefixes=["325"]))
        self.assertEqual(yielded, [])

    def test_cwa_kept_when_naics_matches(self):
        rows = [{
            "REGISTRY_ID": "110000000003",
            "FAC_NAME": "Chemical Mfg Plant",
            "FAC_STATE": "TX",
            "FAC_NAICS_CODES": "325211",
            "NPDES_IDS": "TX0000124",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant Violator",
        }]
        zip_path = make_exporter_zip(self.tmp_path, rows)
        yielded = list(stream_echo_exporter(zip_path, naics_prefixes=["325"]))
        self.assertEqual(len(yielded), 1)
        _, program, _ = yielded[0]
        self.assertEqual(program, "CWA")

    def test_dual_program_facility_emits_both_when_naics_matches(self):
        rows = [{
            "REGISTRY_ID": "110000000004",
            "FAC_NAME": "Industrial + Drinking Water Combo",
            "FAC_STATE": "TX",
            "FAC_NAICS_CODES": "325",
            "NPDES_IDS": "TX0000125",
            "SDWA_IDS": "TX0010005",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant Violator",
            "SDWA_SNC_FLAG": "Y",
            "SDWA_COMPLIANCE_STATUS": "Significant Violator",
        }]
        zip_path = make_exporter_zip(self.tmp_path, rows)
        yielded = list(stream_echo_exporter(zip_path, naics_prefixes=["325"]))
        progs = {p for _, p, _ in yielded}
        self.assertEqual(progs, {"CWA", "SDWA"})


if __name__ == "__main__":
    unittest.main()
