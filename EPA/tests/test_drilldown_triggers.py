"""Drill-down candidate selection: threshold + newly-discovered + score-jumped."""

import unittest

from chemtreat_water_leads.bulk_loader import _drilldown_candidates


def _lead(reg_id: str, program: str, score: int,
          posture: str = "no_events") -> dict:
    return {
        "registry_id": reg_id,
        "program": program,
        "lead_score": score,
        "outreach_posture": posture,
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


if __name__ == "__main__":
    unittest.main()
