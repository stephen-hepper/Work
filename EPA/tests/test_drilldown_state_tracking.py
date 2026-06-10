"""Pin the per-row drill-down state writes that drive the
hands-off Snowflake rerun loop.

The pipeline writes five columns on `facilities` after every drill
attempt: `last_drilldown_attempt_at`, `last_drilldown_outcome`,
`last_drilldown_run_id`, `drilldown_failure_streak`,
`next_drilldown_eligible_at`. The Snowflake side filters on
`next_drilldown_eligible_at <= NOW()` to decide what to re-drill.

If any of these contracts silently change, the rerun loop either
re-drills too aggressively (wasted API quota) or never retries
genuinely-failed lookups (cold leads). The tests below pin:

  1. The streak math (lookup_failed ++, success/no_data → 0)
  2. The eligible-at offsets per outcome:
       * with_events → 7d, no_data → 30d (flat per outcome)
       * lookup_failed → streak-tiered: 6h (1-2) → 24h (3-4) → 7d (5+)
  3. The drill helpers actually call the recorder
  4. A second pass overwrites the first pass cleanly
  5. The short-circuit marks remaining candidates as lookup_failed
  6. `load_prior_drilldown_state` round-trips through the DB
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import requests

from chemtreat_water_leads import bulk_loader, pipeline, snapshot


FIXED_NOW = datetime(2026, 6, 8, 12, 0, 0)


def _make_429_error():
    resp = requests.Response()
    resp.status_code = 429
    return requests.exceptions.HTTPError(response=resp)


# ---------------------- _record_drilldown_outcome ----------------------

class TestRecordDrilldownOutcome(unittest.TestCase):
    """Pure-function tests; pin the streak math and eligible_at offsets
    with a fixed `now` so the assertions are exact."""

    def setUp(self):
        self.lead = {"registry_id": "R1", "program": "CWA"}

    def test_with_events_resets_streak(self):
        pipeline._record_drilldown_outcome(
            self.lead, "with_events", {("R1", "CWA"): 3}, FIXED_NOW)
        self.assertEqual(self.lead["last_drilldown_outcome"], "with_events")
        self.assertEqual(self.lead["drilldown_failure_streak"], 0)
        self.assertEqual(
            self.lead["last_drilldown_attempt_at"],
            FIXED_NOW.isoformat(timespec="seconds"))
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(days=7)).isoformat(timespec="seconds"))

    def test_no_data_resets_streak(self):
        """A clean-empty drill is genuine 'no records on file' — don't
        treat it as a failure that warrants short backoff."""
        pipeline._record_drilldown_outcome(
            self.lead, "no_data", {("R1", "CWA"): 5}, FIXED_NOW)
        self.assertEqual(self.lead["last_drilldown_outcome"], "no_data")
        self.assertEqual(self.lead["drilldown_failure_streak"], 0)
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(days=30)).isoformat(timespec="seconds"))

    def test_lookup_failed_increments_streak(self):
        """Prior streak 3 → new streak 4 lands in the (3, 24h) tier."""
        pipeline._record_drilldown_outcome(
            self.lead, "lookup_failed", {("R1", "CWA"): 3}, FIXED_NOW)
        self.assertEqual(self.lead["last_drilldown_outcome"], "lookup_failed")
        self.assertEqual(self.lead["drilldown_failure_streak"], 4)
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(days=1)).isoformat(timespec="seconds"))

    def test_no_prior_streak_starts_at_one_on_failure(self):
        """First-ever drill attempt that fails — streak goes 0 → 1 and
        lands in the 6h floor tier."""
        pipeline._record_drilldown_outcome(
            self.lead, "lookup_failed", {}, FIXED_NOW)
        self.assertEqual(self.lead["drilldown_failure_streak"], 1)
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(hours=6)).isoformat(timespec="seconds"))

    def test_lookup_failed_tier_floor_six_hours(self):
        """Streak 2 (a transient throttle) stays on the 6h tier."""
        pipeline._record_drilldown_outcome(
            self.lead, "lookup_failed", {("R1", "CWA"): 1}, FIXED_NOW)
        self.assertEqual(self.lead["drilldown_failure_streak"], 2)
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(hours=6)).isoformat(timespec="seconds"))

    def test_lookup_failed_tier_sustained_24h(self):
        """Streak 3 crosses into 'sustained throttle' — bump to 24h so
        we stop burning a daily run into an in-progress block."""
        pipeline._record_drilldown_outcome(
            self.lead, "lookup_failed", {("R1", "CWA"): 2}, FIXED_NOW)
        self.assertEqual(self.lead["drilldown_failure_streak"], 3)
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(days=1)).isoformat(timespec="seconds"))

    def test_lookup_failed_tier_persistent_seven_days(self):
        """Streak 5 means the throttle is sustained across at least
        several attempts — align with EPA's weekly bulk refresh
        cadence rather than re-attempting daily."""
        pipeline._record_drilldown_outcome(
            self.lead, "lookup_failed", {("R1", "CWA"): 4}, FIXED_NOW)
        self.assertEqual(self.lead["drilldown_failure_streak"], 5)
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(days=7)).isoformat(timespec="seconds"))

    def test_lookup_failed_high_streak_caps_at_seven_days(self):
        """The 7d tier is the ceiling — extreme streaks shouldn't
        keep escalating indefinitely. Validates the highest tier
        is also the cap."""
        pipeline._record_drilldown_outcome(
            self.lead, "lookup_failed", {("R1", "CWA"): 50}, FIXED_NOW)
        self.assertEqual(self.lead["drilldown_failure_streak"], 51)
        self.assertEqual(
            self.lead["next_drilldown_eligible_at"],
            (FIXED_NOW + timedelta(days=7)).isoformat(timespec="seconds"))

    def test_unknown_outcome_raises(self):
        """Defensive — refactor mustn't smuggle in a typo'd outcome."""
        with self.assertRaises(ValueError):
            pipeline._record_drilldown_outcome(
                self.lead, "wat", {}, FIXED_NOW)

    def test_run_id_omitted_from_writes(self):
        """The drill helpers don't know the run_id; snapshot.diff_and_upsert
        backfills it from the run_id parameter. Pinned to keep that
        contract explicit — if a future refactor adds run_id back into
        the helper signature, this fails and forces re-thinking the
        backfill path."""
        pipeline._record_drilldown_outcome(
            self.lead, "with_events", {}, FIXED_NOW)
        self.assertNotIn("last_drilldown_run_id", self.lead)


# ---------------------- _drill_cwa integration ----------------------

def _cwa_lead(permit, reg=None):
    return {"program": "CWA",
            "permit_id": permit,
            "registry_id": reg or f"REG-{permit}",
            "company": f"Co {permit}"}


class TestDrillCwaWritesOutcome(unittest.TestCase):
    """The drill helper must call _record_drilldown_outcome on every
    eligible lead it touches, with the right outcome enum."""

    def test_with_events_outcome(self):
        leads = [_cwa_lead("P1")]
        events = []
        fake_event = {"violation_id": "V1", "parameter": "BOD"}
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          return_value=iter([fake_event])):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(leads, "01/01/2026", "06/01/2026",
                                    events, inter_call_sleep=0,
                                    missed_out=None, failed_out=set(),
                                    prior_streaks={})
        self.assertEqual(leads[0]["last_drilldown_outcome"], "with_events")
        self.assertEqual(leads[0]["drilldown_failure_streak"], 0)
        self.assertIn("next_drilldown_eligible_at", leads[0])

    def test_no_data_outcome(self):
        leads = [_cwa_lead("P1")]
        events = []
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          return_value=iter([])):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(leads, "01/01/2026", "06/01/2026",
                                    events, inter_call_sleep=0,
                                    missed_out=None, failed_out=set(),
                                    prior_streaks={})
        self.assertEqual(leads[0]["last_drilldown_outcome"], "no_data")
        self.assertEqual(leads[0]["drilldown_failure_streak"], 0)

    def test_lookup_failed_outcome_increments_prior_streak(self):
        leads = [_cwa_lead("P1")]
        events = []
        prior = {(leads[0]["registry_id"], "CWA"): 2}
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          side_effect=_make_429_error):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(leads, "01/01/2026", "06/01/2026",
                                    events, inter_call_sleep=0,
                                    missed_out=None, failed_out=set(),
                                    prior_streaks=prior)
        self.assertEqual(leads[0]["last_drilldown_outcome"], "lookup_failed")
        self.assertEqual(leads[0]["drilldown_failure_streak"], 3)

    def test_second_pass_overwrites_first_pass(self):
        """A lead that fails first pass and succeeds on retry must
        end with outcome='with_events' and streak=0. Mirrors the
        existing failed_keys discard-on-success behavior."""
        lead = _cwa_lead("P1")
        leads = [lead]
        events = []
        # First pass: 429s
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          side_effect=_make_429_error):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(leads, "01/01/2026", "06/01/2026",
                                    events, inter_call_sleep=0,
                                    missed_out=None, failed_out=set(),
                                    prior_streaks={})
        self.assertEqual(lead["last_drilldown_outcome"], "lookup_failed")
        # Second pass: succeeds
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          return_value=iter([{"violation_id": "V1"}])):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(leads, "01/01/2026", "06/01/2026",
                                    events, inter_call_sleep=0,
                                    missed_out=None, failed_out=set(),
                                    prior_streaks={})
        self.assertEqual(lead["last_drilldown_outcome"], "with_events")
        self.assertEqual(lead["drilldown_failure_streak"], 0)


# ---------------------- _short_circuit_remaining ----------------------

class TestShortCircuitMarksLookupFailed(unittest.TestCase):
    """When the 429 streak trips, every remaining candidate must be
    marked with outcome='lookup_failed' so the Snowflake eligibility
    view queues them for re-run after the 6h backoff (not the 30d
    no_data backoff)."""

    def test_remaining_leads_marked_lookup_failed(self):
        # 30 candidates — past the 20-streak threshold with margin
        leads = [_cwa_lead(f"P{i:03d}") for i in range(30)]
        events = []
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          side_effect=_make_429_error):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(leads, "01/01/2026", "06/01/2026",
                                    events, inter_call_sleep=0,
                                    missed_out=[], failed_out=set(),
                                    prior_streaks={})
        # Every lead ends up with outcome='lookup_failed' — either
        # because its own call 429'd, or because the short-circuit
        # marked it.
        for lead in leads:
            with self.subTest(permit=lead["permit_id"]):
                self.assertEqual(lead["last_drilldown_outcome"],
                                  "lookup_failed")


# ---------------------- _drilldown_candidates eligibility gate -------

def _make_lead(reg, score, posture="no_events"):
    """Synthesize a bulk-shaped lead dict for candidate-selection tests."""
    return {
        "registry_id": reg,
        "program": "CWA",
        "permit_id": f"P-{reg}",
        "outreach_posture": posture,
        "lead_score": score,
        "company": f"Co {reg}",
    }


class TestDrilldownCandidatesBackoffGate(unittest.TestCase):
    """The eligibility gate is what makes the rerun loop self-throttling
    on the local side (matching the Snowflake v_drilldown_eligible view).
    A lead that just failed should NOT be re-attempted on the next bulk
    run within its 6h backoff — re-attempting just re-trips EPA's throttle."""

    def _candidates(self, leads, prior_eligibility, now_iso):
        return bulk_loader._drilldown_candidates(
            leads, prior_scores={}, prior_eligibility=prior_eligibility,
            now_iso=now_iso,
        )

    def test_no_prior_eligibility_means_eligible(self):
        """A lead that's never been drilled has no row in
        prior_eligibility. It should pass the gate."""
        leads = [_make_lead("R1", 75)]
        cand = self._candidates(leads, {}, "2026-06-09T08:00:00")
        self.assertEqual(len(cand), 1)

    def test_elapsed_backoff_means_eligible(self):
        """Eligible_at in the past → backoff elapsed → drill."""
        leads = [_make_lead("R1", 75)]
        prior = {("R1", "CWA"): "2026-06-08T02:00:00"}    # past
        cand = self._candidates(leads, prior, "2026-06-09T08:00:00")
        self.assertEqual(len(cand), 1)

    def test_future_backoff_means_skipped(self):
        """Eligible_at in the future → still in backoff → skip even
        though the lead's score crosses EVENT_DRILLDOWN_MIN_SCORE."""
        leads = [_make_lead("R1", 75)]
        prior = {("R1", "CWA"): "2026-06-09T14:00:00"}    # future
        cand = self._candidates(leads, prior, "2026-06-09T08:00:00")
        self.assertEqual(len(cand), 0)

    def test_mixed_set_filters_correctly(self):
        """Three leads: one never-drilled, one elapsed, one in-backoff.
        Only the first two should pass."""
        leads = [
            _make_lead("R1", 75),   # never drilled → eligible
            _make_lead("R2", 75),   # elapsed → eligible
            _make_lead("R3", 75),   # in backoff → skip
        ]
        prior = {
            ("R2", "CWA"): "2026-06-08T02:00:00",
            ("R3", "CWA"): "2026-06-09T14:00:00",
        }
        cand = self._candidates(leads, prior, "2026-06-09T08:00:00")
        self.assertEqual({c["registry_id"] for c in cand}, {"R1", "R2"})

    def test_backwards_compat_without_eligibility(self):
        """If a caller omits prior_eligibility, behavior must match the
        pre-2026-06-09 design (positive triggers only). Pinned so a
        refactor can't accidentally start failing closed."""
        leads = [_make_lead("R1", 75)]
        # No prior_eligibility kwarg
        cand = bulk_loader._drilldown_candidates(leads, prior_scores={})
        self.assertEqual(len(cand), 1)


class TestLoadPriorDrilldownEligibility(unittest.TestCase):
    """Round-trip an eligibility timestamp through the DB."""

    def test_roundtrip(self):
        with snapshot.open_db(":memory:") as conn:
            conn.execute(
                "INSERT INTO facilities (registry_id, program, lead_score, "
                "next_drilldown_eligible_at, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("R1", "CWA", 75, "2026-06-09T14:00:00",
                 "2026-06-08T00:00:00", "2026-06-08T00:00:00"))
            conn.execute(
                "INSERT INTO facilities (registry_id, program, lead_score, "
                "first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                ("R2", "SDWA", 50, "2026-06-08T00:00:00",
                 "2026-06-08T00:00:00"))
            elig = snapshot.load_prior_drilldown_eligibility(conn)
        self.assertEqual(elig, {("R1", "CWA"): "2026-06-09T14:00:00"})
        self.assertNotIn(("R2", "SDWA"), elig)


# ---------------------- snapshot.load_prior_drilldown_state ------------

class TestLoadPriorDrilldownState(unittest.TestCase):
    """Round-trip the streak through the DB: insert a row with a
    streak, read it back via load_prior_drilldown_state."""

    def test_roundtrip(self):
        with snapshot.open_db(":memory:") as conn:
            # Hand-insert a facility row with a streak — bypasses the
            # full upsert path so the test stays focused on the loader.
            conn.execute(
                "INSERT INTO facilities (registry_id, program, lead_score, "
                "drilldown_failure_streak, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("R1", "CWA", 75, 3, "2026-06-08T00:00:00", "2026-06-08T00:00:00"))
            conn.execute(
                "INSERT INTO facilities (registry_id, program, lead_score, "
                "first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                ("R2", "SDWA", 50, "2026-06-08T00:00:00", "2026-06-08T00:00:00"))
            state = snapshot.load_prior_drilldown_state(conn)
        self.assertEqual(state, {("R1", "CWA"): 3})
        # R2 has NULL streak — should NOT appear in the result
        self.assertNotIn(("R2", "SDWA"), state)


# ---------------------- snapshot upsert backfills run_id --------------

class TestUpsertBackfillsRunId(unittest.TestCase):
    """diff_and_upsert_facilities must populate last_drilldown_run_id
    from its run_id parameter when the lead carries a fresh outcome
    but no explicit run_id (the drill helpers don't write it)."""

    def test_run_id_backfilled_when_outcome_set(self):
        lead = {
            "registry_id": "R1", "program": "CWA",
            "lead_score": 75, "company": "Acme",
            "last_drilldown_attempt_at": "2026-06-08T12:00:00",
            "last_drilldown_outcome": "with_events",
            "drilldown_failure_streak": 0,
            "next_drilldown_eligible_at": "2026-06-15T12:00:00",
        }
        with snapshot.open_db(":memory:") as conn:
            run_id = snapshot.record_run(conn, notes="test")
            snapshot.diff_and_upsert_facilities(conn, [lead], run_id)
            row = conn.execute(
                "SELECT last_drilldown_run_id, last_drilldown_outcome, "
                "drilldown_failure_streak "
                "FROM facilities WHERE registry_id = 'R1'"
            ).fetchone()
        self.assertEqual(row["last_drilldown_run_id"], run_id)
        self.assertEqual(row["last_drilldown_outcome"], "with_events")
        self.assertEqual(row["drilldown_failure_streak"], 0)


if __name__ == "__main__":
    unittest.main()
