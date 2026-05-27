"""Drill-down outcome classification for the Run Health tab.

Pins the split between "lookup failed (re-run)" and "no records on file
(usually legitimate)" — both look like outreach_posture=no_events, but
mean different things to the user. See _health.summarize_drilldown and the
failed_out tracking in pipeline._drill_cwa / _drill_sdwa.
"""

import unittest
from unittest.mock import patch

from chemtreat_water_leads import _health, pipeline


class TestSummarizeDrilldown(unittest.TestCase):

    def _leads(self):
        # 3 CWA (one with events, one failed, one clean-empty) + 2 SDWA
        # (one with events, one failed). A CWA lead with no permit_id is
        # not "attempted" and must be excluded from the denominator.
        return [
            {"registry_id": "R1", "program": "CWA", "permit_id": "P1", "state": "WA"},
            {"registry_id": "R2", "program": "CWA", "permit_id": "P2", "state": "WA"},
            {"registry_id": "R3", "program": "CWA", "permit_id": "P3", "state": "VA"},
            {"registry_id": "R4", "program": "SDWA", "permit_id": "", "state": "GA"},
            {"registry_id": "R5", "program": "SDWA", "permit_id": "", "state": "GA"},
            {"registry_id": "R6", "program": "CWA", "permit_id": "", "state": "AL"},
        ]

    def test_buckets_and_state_breakdown(self):
        leads = self._leads()
        events = [
            {"registry_id": "R1", "program": "CWA"},   # R1 has events
            {"registry_id": "R4", "program": "SDWA"},  # R4 has events
        ]
        # R2 (CWA, WA) and R5 (SDWA, GA) raised; R3 (CWA, VA) came back empty.
        failed = {("R2", "CWA"), ("R5", "SDWA")}

        out = _health.summarize_drilldown(leads, events, failed, leads)

        # R6 has no permit_id -> not attempted. Attempted = R1..R5 = 5.
        self.assertEqual(out["attempted"], 5)
        self.assertEqual(out["with_events"], 2)      # R1, R4
        self.assertEqual(out["lookup_failed"], 2)    # R2, R5
        self.assertEqual(out["no_data"], 1)          # R3
        self.assertEqual(out["lookup_failed_by_state"], {"WA": 1, "GA": 1})
        self.assertEqual(out["lookup_failed_keys"], ["R2|CWA", "R5|SDWA"])

    def test_failed_key_only_counts_if_still_missing(self):
        # A lead that's in failed_keys BUT ended up with events (a later
        # pass recovered it) should be with_events, not lookup_failed.
        leads = [{"registry_id": "R1", "program": "CWA", "permit_id": "P1",
                  "state": "WA"}]
        events = [{"registry_id": "R1", "program": "CWA"}]
        out = _health.summarize_drilldown(leads, events, {("R1", "CWA")}, leads)
        self.assertEqual(out["with_events"], 1)
        self.assertEqual(out["lookup_failed"], 0)


class TestDrillFailedTracking(unittest.TestCase):
    """`failed_out` records a lead iff its FINAL drill attempt raised."""

    def setUp(self):
        # Keep the per-call sleeps from slowing the test.
        self._sleep = patch.object(pipeline.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def _cwa_lead(self):
        return {"registry_id": "R1", "program": "CWA", "permit_id": "P1",
                "company": "Acme"}

    def test_exception_marks_failed(self):
        failed = set()
        with patch.object(pipeline.echo_client, "fetch_npdes_violation_events",
                          side_effect=RuntimeError("Read timed out")):
            pipeline._drill_cwa([self._cwa_lead()], "s", "e", [],
                                inter_call_sleep=0, missed_out=[],
                                failed_out=failed)
        self.assertIn(("R1", "CWA"), failed)

    def test_clean_empty_does_not_mark_failed(self):
        failed = set()
        with patch.object(pipeline.echo_client, "fetch_npdes_violation_events",
                          return_value=[]):
            pipeline._drill_cwa([self._cwa_lead()], "s", "e", [],
                                inter_call_sleep=0, missed_out=[],
                                failed_out=failed)
        self.assertNotIn(("R1", "CWA"), failed)

    def test_later_success_clears_earlier_failure(self):
        failed = set()
        events = []
        lead = self._cwa_lead()
        # Pass 1 raises -> failed gets the key.
        with patch.object(pipeline.echo_client, "fetch_npdes_violation_events",
                          side_effect=RuntimeError("boom")):
            pipeline._drill_cwa([lead], "s", "e", events,
                                inter_call_sleep=0, missed_out=[], failed_out=failed)
        self.assertIn(("R1", "CWA"), failed)
        # Pass 2 succeeds -> key is discarded (final outcome wins).
        with patch.object(pipeline.echo_client, "fetch_npdes_violation_events",
                          return_value=[{"violation_id": "V1"}]):
            pipeline._drill_cwa([lead], "s", "e", events,
                                inter_call_sleep=0, missed_out=None, failed_out=failed)
        self.assertNotIn(("R1", "CWA"), failed)


if __name__ == "__main__":
    unittest.main()
