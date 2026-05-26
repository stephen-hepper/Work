"""Scoring assertions on bulk-shape raw dicts.

The pre-fix `_bulk_to_api_shape` packed CWA and SDWA aliases into one
dict; the scorer's `or` fallbacks treated CWA's `"0"` as truthy and
never fell through to the SDWA value, leaving SDWA-only leads 40+
points light. These tests pin the fix: with per-program shapes, the
SDWA rules fire correctly even when the CWA side is all "N"/"0".
"""

import unittest

from chemtreat_water_leads import scoring
from chemtreat_water_leads.bulk_loader import _bulk_to_program_shapes


def _sdwa_raw(row: dict) -> dict:
    """Helper: pull the SDWA shape from a one-row bulk fixture."""
    shapes = _bulk_to_program_shapes(row)
    for prog, raw in shapes:
        if prog == "SDWA":
            return raw
    raise AssertionError("Row did not produce an SDWA shape")


class TestScoringViaBulkSDWA(unittest.TestCase):

    def test_sdwa_snc_text_scores_when_cwa_clean(self):
        """rule_significant_violator must fire from SDWA_COMPLIANCE_STATUS
        text even when every CWA column is 'N'/'0'/'No Violation Identified'."""
        raw = _sdwa_raw({
            "REGISTRY_ID": "110000000001",
            "CWA_SNC_FLAG": "N",
            "CWA_FORMAL_ACTION_COUNT": "0",
            "CWA_QTRS_WITH_NC": "0",
            "CWA_COMPLIANCE_STATUS": "No Violation Identified",
            "SDWA_SNC_FLAG": "Y",
            "SDWA_COMPLIANCE_STATUS": "Significant/Category I Noncompliance",
        })
        score, reasons = scoring.score_facility(raw)
        # SNC rule contributes 40 points.
        self.assertGreaterEqual(score, 40,
            msg=f"Expected SDWA SNC to score >=40, got {score}: {reasons}")
        self.assertTrue(any("Significant" in r for r in reasons),
            msg=f"Expected SNC reason in {reasons}")

    def test_sdwa_formal_actions_score_when_cwa_zero(self):
        """rule_formal_action reads `f.get("CWPFormalEaCnt") or f.get("Feas")`.
        The per-program shape contains no CWPFormalEaCnt key at all in
        the SDWA dict, so the `or` falls through cleanly to Feas."""
        raw = _sdwa_raw({
            "REGISTRY_ID": "110000000002",
            "CWA_FORMAL_ACTION_COUNT": "0",
            "SDWA_SNC_FLAG": "N",   # SNC off — isolate the formal-action rule
            "SDWA_FORMAL_ACTION_COUNT": "2",
            "SDWA_COMPLIANCE_STATUS": "Enforcement Priority",
        })
        score, reasons = scoring.score_facility(raw)
        self.assertTrue(any("formal enforcement action" in r.lower()
                            for r in reasons),
            msg=f"Expected formal-action reason in {reasons}")
        # +15 for formal action; SNC also fires (+40 via "Enforcement Priority"
        # text). Either contribution alone proves the per-program path works.
        self.assertGreaterEqual(score, 15)

    def test_sdwa_chronic_documented_limitation(self):
        """Bulk SDWA has no quarters-with-vio column at the facility
        level — the chronic rule cannot fire from bulk SDWA data alone.
        This is a known limitation documented in RATIONALE.md, not a
        regression. Test pins the behavior so accidental re-introduction
        of a fabricated quarters column shows up as a failure here."""
        raw = _sdwa_raw({
            "REGISTRY_ID": "110000000003",
            "SDWA_SNC_FLAG": "N",
            "SDWA_COMPLIANCE_STATUS": "",
            "SDWA_FORMAL_ACTION_COUNT": "1",   # something to trigger the shape
        })
        self.assertNotIn("CWPQtrsWithNC", raw)
        self.assertNotIn("QtrsWithVio", raw)
        score, reasons = scoring.score_facility(raw)
        self.assertFalse(any("quarter" in r.lower() for r in reasons),
            msg=f"Chronic rule should not fire on bulk SDWA: {reasons}")


if __name__ == "__main__":
    unittest.main()
