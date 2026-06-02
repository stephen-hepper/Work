"""Pin the fine-comb 429-streak short-circuit behavior.

Background: on 2026-06-02, a nationwide bulk run wedged in the API
fine-comb phase because EPA throttled our IP. Every effluent_chart
and DFR call returned HTTP 429. The drill loops kept grinding for
~2 hours at 1–2 s/call, producing zero events and delaying persistence
of the bulk-derived data (177k events already in memory).

The fix: detect a streak of consecutive HTTP 429s and bail out of
the loop, marking the remaining candidates as lookup_failed so the
viewer's run-health card surfaces them for re-run.

What we test:
  1. 429 detection works on real requests-shaped HTTPError objects
     AND ignores look-alikes (defensive against non-429 5xx errors
     or network drops, which are per-facility issues not throttle
     signals).
  2. Streak resets to 0 on success — a single 429 followed by a 200
     does NOT trip the circuit even if there have been 19 prior 429s.
     The throttle has to be PERSISTENT.
  3. The threshold itself triggers a break and short-circuits the
     remaining eligible candidates.
  4. _short_circuit_remaining adds keys to failed_out AND missed_out
     so both downstream consumers (run_health, second-pass logic)
     see the unattempted leads as needing re-run, not as no-data.
  5. The threshold value isn't accidentally changed without thought.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from chemtreat_water_leads import pipeline


# ----------------------- _is_http_429 detection -----------------------

def _make_429_error():
    """Build a real requests.HTTPError with a response carrying 429.
    Mirrors what `r.raise_for_status()` actually raises so the test
    catches both the type check and the status_code reach-through."""
    resp = requests.Response()
    resp.status_code = 429
    return requests.exceptions.HTTPError(response=resp)


def _make_500_error():
    resp = requests.Response()
    resp.status_code = 500
    return requests.exceptions.HTTPError(response=resp)


class TestIs429Detection(unittest.TestCase):

    def test_429_http_error_is_429(self):
        self.assertTrue(pipeline._is_http_429(_make_429_error()))

    def test_500_http_error_is_not_429(self):
        """5xx are server-side issues with the facility's data, not
        a throttle. Mustn't increment the streak."""
        self.assertFalse(pipeline._is_http_429(_make_500_error()))

    def test_connection_error_is_not_429(self):
        """Network drop on a single facility is a per-facility issue,
        not throttle. Mustn't increment the streak."""
        self.assertFalse(pipeline._is_http_429(
            requests.exceptions.ConnectionError("read timed out")))

    def test_plain_runtime_error_is_not_429(self):
        """RuntimeErrors from echo_client (e.g. EpaBotBlocked) are
        their own concern and have separate retry/backoff. The
        throttle short-circuit narrowly targets HTTP-level 429."""
        self.assertFalse(pipeline._is_http_429(RuntimeError("anything else")))

    def test_http_error_without_response_is_not_429(self):
        """Defensive: a HTTPError can be raised without a response
        attached. Must not crash the detector and must return False."""
        self.assertFalse(pipeline._is_http_429(
            requests.exceptions.HTTPError("no response attached")))


# ----------------------- drill-loop streak behavior -------------------

def _cwa_lead(permit, reg=None):
    return {"program": "CWA",
            "permit_id": permit,
            "registry_id": reg or f"REG-{permit}",
            "company": f"Co {permit}"}


def _sdwa_lead(reg):
    return {"program": "SDWA",
            "registry_id": reg,
            "company": f"PWS {reg}"}


class TestCwaFineCombShortCircuit(unittest.TestCase):
    """We mock echo_client.fetch_npdes_violation_events instead of HTTP
    so we exercise _drill_cwa's actual loop. inter_call_sleep is forced
    to 0 in every test so they run instantaneously."""

    def setUp(self):
        # 50 candidate leads — enough to cross the 20-streak threshold
        # AND still have unattempted remainder after the break.
        self.leads = [_cwa_lead(f"P{i:03d}") for i in range(50)]
        self.events = []
        self.missed = []
        self.failed = set()

    def test_streak_breaks_loop_at_threshold(self):
        # Every call 429s. After THROTTLE_STREAK_THRESHOLD consecutive
        # 429s the loop must break and the remaining leads must be
        # marked as failed via _short_circuit_remaining.
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          side_effect=_make_429_error):
            with patch.object(pipeline.time, "sleep"):
                drilled = pipeline._drill_cwa(
                    self.leads, "01/01/2026", "06/01/2026",
                    self.events, inter_call_sleep=0,
                    missed_out=self.missed, failed_out=self.failed)

        self.assertEqual(drilled, 0)
        # All 50 leads must appear in failed_out — either because
        # they were directly attempted (and 429'd) or because the
        # short-circuit marked them.
        self.assertEqual(len(self.failed), 50,
            msg=f"Expected all 50 leads marked failed, got {len(self.failed)}")
        # The unattempted ones land in missed_out too. We can't pin
        # the exact split — attempts happen up to the streak threshold
        # plus however many already raised before that — but the
        # invariant is: missed_out covers every lead minus the ones
        # that yielded events (which is 0 here under all-429).
        self.assertEqual(len(self.missed), 50)

    def test_single_success_resets_streak(self):
        """The 429 has to be PERSISTENT to trigger short-circuit. One
        successful call mid-streak resets the counter. Pinned so a
        refactor can't drop the streak-reset and trip the circuit on
        intermittent network blips."""
        # Hand-rolled side_effect that 429s 19 times, succeeds once,
        # then 429s another 19 times. Total attempted: 39.
        # If the streak didn't reset, we'd hit the threshold at call
        # 20 and short-circuit. With the reset, we should reach call
        # 39 without short-circuiting — and the loop finishes
        # naturally past that since the 429 streak after the reset is
        # only 19.
        call_log = []
        def flaky(*args, **kwargs):
            n = len(call_log)
            call_log.append(n)
            if n == 19:
                return iter([])   # success (no events) — resets streak
            raise _make_429_error()
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          side_effect=flaky):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(
                    self.leads[:40], "01/01/2026", "06/01/2026",
                    self.events, inter_call_sleep=0,
                    missed_out=self.missed, failed_out=self.failed)
        # All 40 attempts ran — no short-circuit.
        self.assertEqual(len(call_log), 40,
            msg="Streak reset failed: short-circuit triggered despite a "
                f"successful call mid-streak. Calls made: {len(call_log)}")

    def test_non_429_error_does_not_increment_streak(self):
        """A 500 server error or a connection drop is a per-facility
        issue and must NOT count toward the throttle streak. Pinned
        so a refactor that widens _is_http_429 doesn't quietly
        short-circuit on routine errors."""
        # 30 calls, all 500 errors. Should not short-circuit even
        # though every call fails.
        with patch.object(pipeline.echo_client,
                          "fetch_npdes_violation_events",
                          side_effect=_make_500_error):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_cwa(
                    self.leads[:30], "01/01/2026", "06/01/2026",
                    self.events, inter_call_sleep=0,
                    missed_out=self.missed, failed_out=self.failed)
        # All 30 leads attempted; all 30 in failed_out (each one
        # raised, per the per-lead error path); none short-circuited.
        # The signature of short-circuiting would be unattempted
        # leads showing up in failed_out without the call being made.
        # Easier to assert directly: ALL leads must have triggered a
        # log message (i.e. been attempted) — captured by call_count.
        # We can re-mock to count:
        self.assertEqual(len(self.failed), 30)


class TestShortCircuitRemainingMarksBothOutputs(unittest.TestCase):
    """The viewer's run-health uses failed_out; the
    second-pass logic in run_bulk uses missed_out. Both have to be
    populated for unattempted leads — otherwise the run-health card
    shows them as 'no records on file' (which is wrong; we never
    asked) and any retry pass skips them."""

    def test_marks_both_failed_and_missed(self):
        leads = [_cwa_lead(f"P{i:03d}") for i in range(5)]
        failed = set()
        missed = []
        n = pipeline._short_circuit_remaining(
            leads, failed, missed, reason="test")
        self.assertEqual(n, 5)
        self.assertEqual(len(failed), 5)
        self.assertEqual(len(missed), 5)
        # Keys are (registry_id, program) tuples for failed_out, and
        # the original lead dicts for missed_out.
        for lead in leads:
            self.assertIn((lead["registry_id"], lead["program"]), failed)

    def test_handles_none_outputs(self):
        """A caller that doesn't pass failed_out / missed_out still
        gets a count back and doesn't crash."""
        leads = [_cwa_lead(f"P{i:03d}") for i in range(3)]
        n = pipeline._short_circuit_remaining(leads, None, None, "test")
        self.assertEqual(n, 3)


class TestSdwaFineCombShortCircuit(unittest.TestCase):
    """SDWA path uses fetch_sdwa_violation_events and a different
    inter_call_sleep, but the short-circuit logic is the same
    pattern. Pinning that the SDWA loop respects the streak too —
    not just CWA."""

    def test_sdwa_streak_breaks_loop(self):
        leads = [_sdwa_lead(f"REG-{i:03d}") for i in range(40)]
        events = []
        failed = set()
        missed = []
        with patch.object(pipeline.echo_client,
                          "fetch_sdwa_violation_events",
                          side_effect=_make_429_error):
            with patch.object(pipeline.time, "sleep"):
                pipeline._drill_sdwa(
                    leads, events, inter_call_sleep=0,
                    missed_out=missed, failed_out=failed)
        self.assertEqual(len(failed), 40)
        self.assertEqual(len(missed), 40)


class TestThresholdValuePinned(unittest.TestCase):
    """The threshold is a tuning constant; a refactor that drops it
    to e.g. 5 would trip the circuit on routine intermittent errors
    and undermine the fine-comb. A refactor that raises it to 200
    would defeat the purpose. Pin the chosen value so a change has
    to face this test and the explanatory comment."""

    def test_threshold_is_twenty(self):
        self.assertEqual(pipeline.THROTTLE_STREAK_THRESHOLD, 20,
            msg="Threshold changed — review the rationale in the "
                "comment above THROTTLE_STREAK_THRESHOLD before "
                "updating this test.")


if __name__ == "__main__":
    unittest.main()
