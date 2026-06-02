"""Pin the new pre-violation scoring rules and their tag projections.

These rules add facility-level points for two signals that don't depend
on the violation feed:
  * permit_has_* columns from npdes_limits.zip
  * discharges_to_impaired / matching_impaired_parameters from
    npdes_attains_downloads.zip

The tests cover:
  1. Regression — when the new columns are absent (every existing
     CSV / DB / fixture from before the integration), the new rules
     contribute zero. This is the load-bearing "don't break what
     works" guarantee.
  2. Cap math on rule_treatable_permit_parameter (+5 per hit, max +15).
  3. rule_discharges_to_impaired picks the stronger reason (+15
     parameter-match) over the weaker one (+10 plain impaired) when
     both signals are present — no double-counting.
  4. Tag projections match the rule logic (no drift between rule and
     tag).
  5. tag_chemtreat_high_relevance composite still demotes to False
     when every drilled event is Resolved/Archived — the new signals
     OR into the positive side but the do-not-call guardrail wins.
"""

from __future__ import annotations

import unittest

from chemtreat_water_leads import scoring


class TestNewRulesRegression(unittest.TestCase):
    """A facility with none of the new columns must score identically
    to what it scored before this integration shipped."""

    def test_new_rules_silent_when_columns_absent(self):
        # A typical SDWA-shape raw dict from bulk_loader._bulk_to_program_shapes.
        # No permit_has_*, no discharges_to_impaired, no
        # matching_impaired_parameters keys at all — the realistic
        # state for every pipeline.run lead and for every bulk lead
        # before the augmentation step lands.
        raw = {
            "RegistryID": "110000000001",
            "SNCFlag": "Y",
            "SNC": "Significant/Category I Noncompliance",
            "Feas": "1",
        }
        score, reasons = scoring.score_facility(raw)
        # SNC (+40) + formal (+15) = 55 — neither new rule should add.
        self.assertEqual(score, 55,
            msg=f"Unexpected score {score}; reasons: {reasons}")
        for r in reasons:
            self.assertNotIn("permit", r.lower(),
                msg=f"Permit-limit rule fired on a row with no permit columns: {r}")
            self.assertNotIn("impaired", r.lower(),
                msg=f"Impaired-water rule fired on a row with no ATTAINS columns: {r}")


class TestTreatableParameterRule(unittest.TestCase):

    def test_zero_hits_returns_none(self):
        f = {col: 0 for col in scoring.PERMIT_HAS_COLS}
        self.assertIsNone(scoring.rule_treatable_permit_parameter(f))

    def test_one_hit_scores_five(self):
        f = {"permit_has_phosphorus": 1}
        result = scoring.rule_treatable_permit_parameter(f)
        self.assertIsNotNone(result)
        pts, reason = result
        self.assertEqual(pts, 5)
        self.assertIn("1", reason)
        self.assertIn("treatable", reason.lower())

    def test_three_hits_scores_fifteen(self):
        f = {"permit_has_phosphorus": 1,
             "permit_has_ammonia": 1,
             "permit_has_tss": 1}
        pts, _ = scoring.rule_treatable_permit_parameter(f)
        self.assertEqual(pts, 15)

    def test_cap_at_fifteen_with_every_class_hitting(self):
        """Cap exists because the rule otherwise dominates the score —
        a facility with broad permits would crowd out the SNC/quarters
        signals the scorer is built around. Pinned so future schema
        additions can't quietly raise the ceiling.

        The count assertion is a guard: if PERMIT_HAS_COLS grows but
        the rule's cap doesn't get re-reviewed, this test forces the
        author to look at the cap. Bump the count if you add a class;
        review the cap math in the same change."""
        f = {col: 1 for col in scoring.PERMIT_HAS_COLS}
        self.assertEqual(len(scoring.PERMIT_HAS_COLS), 8,
            msg="PERMIT_HAS_COLS count changed — re-review the +15 cap "
                "in rule_treatable_permit_parameter and update this "
                "assertion deliberately.")
        pts, _ = scoring.rule_treatable_permit_parameter(f)
        self.assertEqual(pts, 15)


class TestDischargesToImpairedRule(unittest.TestCase):

    def test_no_signal_returns_none(self):
        self.assertIsNone(scoring.rule_discharges_to_impaired({}))
        # Explicit-False forms still return None — `bool("")` and `bool(0)`
        # are False, the rule must not fire on those.
        self.assertIsNone(scoring.rule_discharges_to_impaired(
            {"discharges_to_impaired": 0,
             "matching_impaired_parameters": ""}))

    def test_plain_impaired_scores_ten(self):
        f = {"discharges_to_impaired": 1}
        pts, reason = scoring.rule_discharges_to_impaired(f)
        self.assertEqual(pts, 10)
        self.assertIn("impaired", reason.lower())

    def test_parameter_match_scores_fifteen(self):
        """The stronger signal wins — state has documented THIS
        facility's discharge as a cause of THIS waterbody's
        impairment. Higher tightening-permit probability."""
        f = {"matching_impaired_parameters": "Phosphorus, total [as P]"}
        pts, reason = scoring.rule_discharges_to_impaired(f)
        self.assertEqual(pts, 15)
        self.assertIn("matching", reason.lower())

    def test_no_double_counting_when_both_signals_present(self):
        """Rule must return exactly one (points, reason) tuple. The
        scorer is sum-of-contributions, so a double-fire here would
        silently add +25 instead of +15 to every parameter-match lead."""
        f = {"discharges_to_impaired": 1,
             "matching_impaired_parameters": "BOD, 5-day, 20 deg. C"}
        result = scoring.rule_discharges_to_impaired(f)
        self.assertIsNotNone(result)
        pts, _ = result
        self.assertEqual(pts, 15,
            msg="Parameter-match must shadow plain-impaired; both firing "
                "would double-count.")


class TestNewTags(unittest.TestCase):

    def test_treatable_permit_tag_tracks_any_permit_has(self):
        for col in scoring.PERMIT_HAS_COLS:
            tags = scoring.compute_tags({col: 1})
            self.assertTrue(tags["tag_treatable_permit"],
                msg=f"tag_treatable_permit should fire on {col}=1")

    def test_treatable_permit_tag_false_when_no_permit_columns(self):
        tags = scoring.compute_tags({})
        self.assertFalse(tags["tag_treatable_permit"])

    def test_impaired_tags_split_correctly(self):
        tags_plain = scoring.compute_tags({"discharges_to_impaired": 1})
        self.assertTrue(tags_plain["tag_discharges_to_impaired"])
        self.assertFalse(tags_plain["tag_impairment_parameter_match"])

        tags_match = scoring.compute_tags(
            {"matching_impaired_parameters": "Copper, total recoverable"})
        self.assertTrue(tags_match["tag_impairment_parameter_match"])
        # Plain-impaired tag is independent — a parameter match doesn't
        # automatically set the plain flag (the upstream loader may set
        # both, but that's its job, not compute_tags').
        self.assertFalse(tags_match["tag_discharges_to_impaired"])


class TestHighRelevanceComposite(unittest.TestCase):
    """The composite is the "if a rep had one filter, this is it"
    column. It absolutely must respect the do-not-call guardrail —
    adding new positive signals to the OR side cannot allow a
    facility with all-resolved events to slip back into the
    high-relevance bucket."""

    def test_new_signals_make_composite_true(self):
        tags = scoring.compute_tags({"permit_has_phosphorus": 1})
        self.assertTrue(tags["tag_chemtreat_high_relevance"])

        tags = scoring.compute_tags({"matching_impaired_parameters": "BOD"})
        self.assertTrue(tags["tag_chemtreat_high_relevance"])

    def test_do_not_call_guardrail_still_demotes_new_signals(self):
        """A facility with a treatable permit AND only-resolved events
        must NOT be tagged high-relevance. The Resolved guardrail (per
        MEMORY.md: 'they fixed it — do not call') is load-bearing and
        must keep winning over the new positive signals."""
        facility = {"permit_has_phosphorus": 1,
                    "matching_impaired_parameters": "BOD"}
        events = [
            {"status": "Resolved", "violation_category": "Treatment Technique"},
            {"status": "Archived", "violation_category": "MCL"},
        ]
        tags = scoring.compute_tags(facility, events)
        self.assertTrue(tags["tag_only_resolved_events"])
        self.assertTrue(tags["tag_treatable_permit"])
        self.assertFalse(tags["tag_chemtreat_high_relevance"],
            msg="Resolved-only guardrail must beat the new positive "
                "signals in the high-relevance composite.")

    def test_composite_false_when_nothing_fires(self):
        tags = scoring.compute_tags({})
        self.assertFalse(tags["tag_chemtreat_high_relevance"])


class TestEndToEndScoreContribution(unittest.TestCase):
    """One end-to-end check that the new rules participate in
    score_facility() and show up in the reasons list — pins that
    they're actually registered in RULES, not just defined."""

    def test_combined_pre_violation_score_is_25_max(self):
        """Bound the pre-violation contribution. +15 treatable + +15
        parameter-match = +30 — but plain-impaired is shadowed by
        parameter-match, so the real max is treatable cap (+15) plus
        the impaired rule (+15) = +30. Pin so future tuning doesn't
        let this silently dominate the SNC/chronic signals."""
        raw = {col: 1 for col in scoring.PERMIT_HAS_COLS}
        raw["matching_impaired_parameters"] = "Phosphorus, total [as P]"
        score, reasons = scoring.score_facility(raw)
        self.assertEqual(score, 30,
            msg=f"Pre-violation rules should contribute +30; got {score}: "
                f"{reasons}")
        self.assertTrue(any("treatable" in r.lower() for r in reasons))
        self.assertTrue(any("matching" in r.lower() for r in reasons))


if __name__ == "__main__":
    unittest.main()
