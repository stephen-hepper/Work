"""Verify `_bulk_to_program_shapes` emits the right per-program raw dicts."""

import unittest

from chemtreat_water_leads.bulk_loader import _bulk_to_program_shapes


class TestProgramShapes(unittest.TestCase):

    def test_cwa_only_when_only_cwa_signal_fires(self):
        row = {
            "REGISTRY_ID": "110000000001",
            "FAC_NAME": "Acme Refinery",
            "FAC_NAICS_CODES": "325211",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant/Category I Noncompliance",
            "SDWA_SNC_FLAG": "N",
            "SDWA_FORMAL_ACTION_COUNT": "0",
            "SDWA_COMPLIANCE_STATUS": "No Violation Identified",
        }
        shapes = _bulk_to_program_shapes(row)
        self.assertEqual(len(shapes), 1)
        prog, raw = shapes[0]
        self.assertEqual(prog, "CWA")
        self.assertEqual(raw["SNCFlag"], "Y")
        # SDWA-only fields must not appear in the CWA dict.
        self.assertNotIn("Feas", raw)
        self.assertNotIn("SNC", raw)

    def test_sdwa_only_when_only_sdwa_signal_fires(self):
        row = {
            "REGISTRY_ID": "110000000002",
            "FAC_NAME": "City Water Authority",
            "FAC_NAICS_CODES": "",
            "CWA_SNC_FLAG": "N",
            "CWA_FORMAL_ACTION_COUNT": "0",
            "CWA_COMPLIANCE_STATUS": "No Violation Identified",
            "SDWA_SNC_FLAG": "Y",
            "SDWA_COMPLIANCE_STATUS": "Significant Violator",
        }
        shapes = _bulk_to_program_shapes(row)
        self.assertEqual(len(shapes), 1)
        prog, raw = shapes[0]
        self.assertEqual(prog, "SDWA")
        self.assertEqual(raw["SNCFlag"], "Y")
        self.assertEqual(raw["SNC"], "Significant Violator")
        # CWA-only fields must not appear in the SDWA dict.
        self.assertNotIn("CWPFormalEaCnt", raw)
        self.assertNotIn("CWPSNCStatus", raw)

    def test_both_when_both_signals_fire(self):
        row = {
            "REGISTRY_ID": "110000000003",
            "FAC_NAME": "Dual Program Facility",
            "FAC_NAICS_CODES": "325",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant Violator",
            "SDWA_SNC_FLAG": "Y",
            "SDWA_COMPLIANCE_STATUS": "Significant Violator",
        }
        shapes = _bulk_to_program_shapes(row)
        progs = {p for p, _ in shapes}
        self.assertEqual(progs, {"CWA", "SDWA"})

    def test_empty_when_no_signal(self):
        row = {
            "REGISTRY_ID": "110000000004",
            "CWA_SNC_FLAG": "N",
            "CWA_FORMAL_ACTION_COUNT": "0",
            "CWA_QTRS_WITH_NC": "0",
            "CWA_CURRENT_VIOL": "N",
            "CWA_COMPLIANCE_STATUS": "No Violation Identified",
            "SDWA_SNC_FLAG": "N",
            "SDWA_FORMAL_ACTION_COUNT": "0",
            "SDWA_COMPLIANCE_STATUS": "No Violation Identified",
        }
        self.assertEqual(_bulk_to_program_shapes(row), [])

    def test_cwa_zero_does_not_mask_sdwa_value(self):
        """The whole point of the per-program shapes: CWA="0" can no
        longer mask SDWA="2" via Python `or` in the scorer."""
        row = {
            "REGISTRY_ID": "110000000005",
            "CWA_SNC_FLAG": "N",
            "CWA_FORMAL_ACTION_COUNT": "0",
            "SDWA_SNC_FLAG": "Y",
            "SDWA_FORMAL_ACTION_COUNT": "2",
            "SDWA_COMPLIANCE_STATUS": "Significant Violator",
        }
        shapes = _bulk_to_program_shapes(row)
        self.assertEqual(len(shapes), 1)
        prog, raw = shapes[0]
        self.assertEqual(prog, "SDWA")
        self.assertEqual(raw["Feas"], "2")
        # No CWA fields present to mask the SDWA-side `or` fallback.
        self.assertNotIn("CWPFormalEaCnt", raw)


if __name__ == "__main__":
    unittest.main()
