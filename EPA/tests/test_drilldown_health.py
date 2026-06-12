"""Drill-down outcome classification for the Run Health tab.

Pins the split between "lookup failed (re-run)" and "no records on file
(usually legitimate)" — both look like outreach_posture=no_events, but
mean different things to the user. See _health.summarize_drilldown and the
failed_out tracking in pipeline._drill_cwa / _drill_sdwa.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chemtreat_water_leads import _health, pipeline, bulk_loader
from tests._fixtures import make_exporter_zip, make_npdes_zip, make_sdwa_zip


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
        self.assertEqual(out["gated_unactionable"], 0)  # no gate provided
        self.assertEqual(out["no_data"], 1)          # R3
        self.assertEqual(out["lookup_failed_by_state"], {"WA": 1, "GA": 1})
        self.assertEqual(out["lookup_failed_keys"], ["R2|CWA", "R5|SDWA"])
        self.assertEqual(out["gated_unactionable_keys"], [])

    def test_failed_key_only_counts_if_still_missing(self):
        # A lead that's in failed_keys BUT ended up with events (a later
        # pass recovered it) should be with_events, not lookup_failed.
        leads = [{"registry_id": "R1", "program": "CWA", "permit_id": "P1",
                  "state": "WA"}]
        events = [{"registry_id": "R1", "program": "CWA"}]
        out = _health.summarize_drilldown(leads, events, {("R1", "CWA")}, leads)
        self.assertEqual(out["with_events"], 1)
        self.assertEqual(out["lookup_failed"], 0)

    def test_gated_split_out_of_no_data(self):
        # R1 has events, R2 raised (failed), R3 came back empty (no_data),
        # R4 and R5 were gated (un-actionable per the gate predicate). The
        # gated set is a SUBSET of no_event; gated leads must land in the
        # gated bucket, NOT in no_data — that's the whole point of the
        # split (don't tell the user "no records on file" when we
        # intentionally didn't ask).
        leads = [
            {"registry_id": "R1", "program": "CWA", "permit_id": "P1", "state": "WA"},
            {"registry_id": "R2", "program": "CWA", "permit_id": "P2", "state": "WA"},
            {"registry_id": "R3", "program": "CWA", "permit_id": "P3", "state": "VA"},
            {"registry_id": "R4", "program": "CWA", "permit_id": "P4", "state": "KS"},
            {"registry_id": "R5", "program": "CWA", "permit_id": "P5", "state": "NJ"},
        ]
        events = [{"registry_id": "R1", "program": "CWA"}]
        failed = {("R2", "CWA")}
        gated = {("R4", "CWA"), ("R5", "CWA")}

        out = _health.summarize_drilldown(leads, events, failed, leads,
                                          gated_keys=gated)

        self.assertEqual(out["attempted"], 5)
        self.assertEqual(out["with_events"], 1)         # R1
        self.assertEqual(out["lookup_failed"], 1)       # R2
        self.assertEqual(out["gated_unactionable"], 2)  # R4, R5
        self.assertEqual(out["no_data"], 1)             # R3 only — not 3
        self.assertEqual(out["gated_unactionable_keys"], ["R4|CWA", "R5|CWA"])

    def test_failed_wins_over_gated(self):
        # A key in BOTH failed_keys and gated_keys should land in
        # lookup_failed — we asked EPA, EPA didn't answer, so the user
        # still has actionable re-run information. The gate gets credit
        # only for leads we successfully avoided asking.
        leads = [
            {"registry_id": "R1", "program": "CWA", "permit_id": "P1", "state": "WA"},
        ]
        out = _health.summarize_drilldown(
            leads, [], {("R1", "CWA")}, leads,
            gated_keys={("R1", "CWA")},
        )
        self.assertEqual(out["lookup_failed"], 1)
        self.assertEqual(out["gated_unactionable"], 0)


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


class TestBulkEmitsBreakdown(unittest.TestCase):
    """A bulk run with events writes the failed/no-data breakdown into
    run_health.json (so the viewer's refined coverage card works for bulk
    too), without losing the bulk-specific fine-comb stats."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_bulk_run_health_has_breakdown(self):
        # One CWA lead that clears the drill threshold (SNC 40 + 2 formal 15
        # = 55), so it's a fine-comb candidate.
        exporter = make_exporter_zip(self.tmp, [{
            "REGISTRY_ID": "110000000001", "FAC_NAME": "Acme Chemical",
            "FAC_STATE": "TX", "FAC_NAICS_CODES": "325", "NPDES_IDS": "TX0000001",
            "CWA_SNC_FLAG": "Y", "CWA_FORMAL_ACTION_COUNT": "2",
            "CWA_COMPLIANCE_STATUS": "Significant Violator",
        }])
        npdes = make_npdes_zip(self.tmp, [])   # no bulk events
        sdwa = make_sdwa_zip(self.tmp, [])

        zips = {"echo_exporter": exporter, "npdes": npdes, "sdwa": sdwa}
        out_dir = self.tmp / "out"

        with patch.object(bulk_loader, "_download_cached",
                          side_effect=lambda url, c, name: zips[name]), \
             patch.object(bulk_loader, "_drill_cwa", side_effect=lambda *a, **k: 0), \
             patch.object(bulk_loader, "_drill_sdwa", side_effect=lambda *a, **k: 0):
            bulk_loader.run_bulk(out_dir=out_dir, db_path=self.tmp / "snap.sqlite",
                                 cache_dir=self.tmp / "cache", states=["TX"],
                                 include_events=True)

        run_dirs = [p for p in out_dir.iterdir() if p.is_dir()]
        self.assertEqual(len(run_dirs), 1)
        health = json.loads((run_dirs[0] / "run_health.json").read_text())
        self.assertEqual(health["schema_version"], 3)
        drill = health["drilldown"]
        # All v3 breakdown keys present...
        for k in ("attempted", "with_events", "lookup_failed",
                  "gated_unactionable", "no_data",
                  "lookup_failed_by_state", "lookup_failed_keys",
                  "gated_unactionable_keys"):
            self.assertIn(k, drill)
        # ...alongside the bulk-specific fine-comb stat.
        self.assertIn("candidates", drill)
        # The lead had no events (drills no-op'd), didn't raise, and didn't
        # match the un-actionable gate predicate -> no_data.
        self.assertEqual(drill["attempted"], 1)
        self.assertEqual(drill["with_events"], 0)
        self.assertEqual(drill["lookup_failed"], 0)
        self.assertEqual(drill["gated_unactionable"], 0)
        self.assertEqual(drill["no_data"], 1)


if __name__ == "__main__":
    unittest.main()
