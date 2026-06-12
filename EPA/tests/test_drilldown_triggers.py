"""Drill-down candidate selection: threshold + newly-discovered + score-jumped."""

import unittest

from chemtreat_water_leads.bulk_loader import (
    _drilldown_candidates,
    _is_unactionable_for_drilldown,
)


def _lead(reg_id: str, program: str, score: int,
          posture: str = "no_events",
          snc_status: str | None = None) -> dict:
    return {
        "registry_id": reg_id,
        "program": program,
        "lead_score": score,
        "outreach_posture": posture,
        "snc_status": snc_status,
    }


class TestDrilldownTriggers(unittest.TestCase):

    def test_threshold_trigger(self):
        leads = [_lead("A", "CWA", 60)]
        prior = {("A", "CWA"): 60}  # not newly-discovered, score didn't jump
        cands = _drilldown_candidates(leads, prior)
        self.assertEqual({L["registry_id"] for L in cands}, {"A"})

    def test_newly_discovered_below_threshold(self):
        """A previously-unseen facility at score 30 still earns a drill —
        the diff-driven 'what's new' view depends on this."""
        leads = [_lead("B", "CWA", 30)]
        prior = {}   # B not in DB
        cands = _drilldown_candidates(leads, prior)
        self.assertEqual({L["registry_id"] for L in cands}, {"B"})

    def test_newly_discovered_below_secondary_floor_skipped(self):
        """Floor protects the from-scratch first-run case: a brand-new
        facility at score 5 should NOT trigger a drill. Without this,
        a nationwide first-run would queue every facility for the API."""
        leads = [_lead("B-low", "CWA", 5)]
        prior = {}   # newly discovered
        cands = _drilldown_candidates(leads, prior)
        self.assertEqual(cands, [])

    def test_score_jumped(self):
        leads = [_lead("C", "CWA", 35)]
        prior = {("C", "CWA"): 20}   # +15 jump
        cands = _drilldown_candidates(leads, prior)
        self.assertEqual({L["registry_id"] for L in cands}, {"C"})

    def test_score_jumped_below_10_does_not_trigger(self):
        leads = [_lead("D", "CWA", 28)]
        prior = {("D", "CWA"): 20}   # +8 jump, under threshold
        cands = _drilldown_candidates(leads, prior)
        self.assertEqual(cands, [])

    def test_already_has_events_excluded(self):
        """If bulk gave us per-event detail, no need to API-drill."""
        leads = [_lead("E", "CWA", 90, posture="active")]
        prior = {}   # newly-discovered, but already has events
        cands = _drilldown_candidates(leads, prior)
        self.assertEqual(cands, [])

    def test_three_triggers_picked_together(self):
        leads = [
            _lead("A", "CWA", 60),   # threshold
            _lead("B", "SDWA", 30),  # newly discovered
            _lead("C", "CWA", 35),   # score jumped
            _lead("D", "CWA", 20),   # nothing fires
        ]
        prior = {
            ("A", "CWA"): 60,
            ("C", "CWA"): 20,
            ("D", "CWA"): 18,
        }
        cands = _drilldown_candidates(leads, prior)
        self.assertEqual({L["registry_id"] for L in cands}, {"A", "B", "C"})


class TestUnactionableGate(unittest.TestCase):
    """RNC-only / terminated / no-violation leads are gated out of
    fine-comb regardless of score. They scored high on enforcement-status
    rules but EPA's per-event endpoints have nothing chemistry-relevant
    to return for them. Verified empirically against the 2026-06-12
    nationwide run: 724 of 993 high-value fine-comb candidates matched
    one of these patterns and returned zero events on attempted drill."""

    def test_rnc_failure_to_report_gated(self):
        leads = [_lead("R", "CWA", 117,
                       snc_status="Failure to Report DMR - Not Received")]
        cands = _drilldown_candidates(leads, prior_scores={})
        self.assertEqual(cands, [])

    def test_terminated_permit_gated(self):
        leads = [_lead("T", "CWA", 82, snc_status="Terminated Permit")]
        cands = _drilldown_candidates(leads, prior_scores={})
        self.assertEqual(cands, [])

    def test_no_violation_identified_gated(self):
        leads = [_lead("N", "CWA", 65, snc_status="No Violation Identified")]
        cands = _drilldown_candidates(leads, prior_scores={})
        self.assertEqual(cands, [])

    def test_violation_identified_still_drills(self):
        """Substring match is on the un-actionable patterns specifically —
        'Violation Identified' must NOT match 'No Violation Identified'
        and must still drill."""
        leads = [_lead("V", "CWA", 65, snc_status="Violation Identified")]
        cands = _drilldown_candidates(leads, prior_scores={})
        self.assertEqual({L["registry_id"] for L in cands}, {"V"})

    def test_effluent_snc_still_drills(self):
        """Real effluent-driven SNC — actionable, must still drill."""
        leads = [_lead("E", "CWA", 102,
                       snc_status="Effluent - Monthly Average Limit")]
        cands = _drilldown_candidates(leads, prior_scores={})
        self.assertEqual({L["registry_id"] for L in cands}, {"E"})

    def test_empty_snc_status_does_not_gate(self):
        """A lead with no snc_status text still drills — the gate only
        fires on a positive un-actionable signal, never on absence."""
        leads = [
            _lead("X", "CWA", 60, snc_status=None),
            _lead("Y", "CWA", 60, snc_status=""),
        ]
        cands = _drilldown_candidates(leads, prior_scores={})
        self.assertEqual({L["registry_id"] for L in cands}, {"X", "Y"})

    def test_predicate_case_insensitive(self):
        for txt in ("FAILURE TO REPORT DMR", "failure to report",
                    "Failure to Report", "TERMINATED PERMIT"):
            self.assertTrue(
                _is_unactionable_for_drilldown({"snc_status": txt}),
                f"expected match on {txt!r}")
        for txt in (None, "", "Violation Identified",
                    "Effluent - Monthly Average Limit"):
            self.assertFalse(
                _is_unactionable_for_drilldown({"snc_status": txt}),
                f"expected NO match on {txt!r}")


if __name__ == "__main__":
    unittest.main()
