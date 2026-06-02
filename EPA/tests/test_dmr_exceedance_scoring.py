"""Pin the DMR-exceedance scoring rules and their tag projections.

These two rules turn the existing pre-violation signals into
actual-compliance signals:
  * rule_recent_dmr_exceedance — tiered points by severity of the
    worst single exceedance.
  * rule_exceeds_treatable_parameter — composite: facility is
    permitted on AND currently exceeding a ChemTreat-treatable
    class. The strongest single signal in the system.

Tests cover:
  1. Regression — when neither column is populated (every
     pipeline.run lead and every pre-integration bulk row), the new
     rules contribute zero. Load-bearing guarantee.
  2. Tier boundaries on rule_recent_dmr_exceedance — the exact
     thresholds (50/100/200/1000) are tuning, not arbitrary; pin so
     a change has to face the test.
  3. The composite rule fires only on intersection of
     permit_has_<X> AND exceeded_treatable containing <X>. False on
     either alone.
  4. Tag projections match rule logic; high_relevance composite
     still respects do-not-call guardrail.
  5. The two rules can stack — total contribution is bounded by
     +15 + +15 = +30 (matches the pre-violation pair's bound).
"""

from __future__ import annotations

import unittest

from chemtreat_water_leads import scoring


class TestNewRulesRegression(unittest.TestCase):
    def test_silent_when_columns_absent(self):
        """A raw with no exceedance columns must score identically
        to before this integration. Pinned against silent activation
        from default-None columns."""
        raw = {"RegistryID": "R1",
               "SNCFlag": "Y",
               "SNC": "Significant/Category I Noncompliance"}
        score, reasons = scoring.score_facility(raw)
        # SNC only.
        self.assertEqual(score, 40)
        for r in reasons:
            self.assertNotIn("exceedance", r.lower())
            self.assertNotIn("exceeding", r.lower())


class TestRecentDmrExceedanceTiers(unittest.TestCase):
    """Each tier has a specific real-world meaning (see rule docstring).
    Pin the boundaries with both sides of each threshold so a refactor
    can't silently widen or narrow a tier."""

    def _pts(self, pct):
        r = scoring.rule_recent_dmr_exceedance({"top_exceedance_pct": pct})
        return r[0] if r else None

    def test_zero_or_negative_returns_none(self):
        self.assertIsNone(self._pts(0))
        self.assertIsNone(self._pts(-1))
        self.assertIsNone(self._pts(None))
        # Unparseable strings count as 0 (defensive _safe_float).
        self.assertIsNone(self._pts("not a number"))

    def test_tier_boundaries(self):
        # (pct, expected_pts) — one row per side of each tier boundary.
        cases = [
            (0.1,    5),   # just over zero — minimum tier
            (49.9,   5),
            (50,     8),
            (99.9,   8),
            (100,   10),
            (199.9, 10),
            (200,   12),
            (999.9, 12),
            (1000,  15),
            (99999, 15),   # cap holds at the top
        ]
        for pct, expected in cases:
            with self.subTest(pct=pct):
                self.assertEqual(self._pts(pct), expected,
                    msg=f"pct={pct} should score {expected}")

    def test_reason_string_includes_pct(self):
        """Sales should see the actual %, not just a tier label."""
        _, reason = scoring.rule_recent_dmr_exceedance(
            {"top_exceedance_pct": 153})
        self.assertIn("153", reason)


class TestExceedsTreatableParameterRule(unittest.TestCase):

    def test_no_columns_returns_none(self):
        self.assertIsNone(scoring.rule_exceeds_treatable_parameter({}))

    def test_exceeded_without_permitted_returns_none(self):
        """The facility exceeded a treatable class but isn't
        permitted on it. Shouldn't fire — without a permit limit,
        there's no compliance angle to sell into."""
        f = {"exceeded_treatable_parameters_text": "bod | metals"}
        self.assertIsNone(scoring.rule_exceeds_treatable_parameter(f))

    def test_permitted_without_exceedance_returns_none(self):
        """The facility has a permit limit on bod but isn't
        exceeding. That's `rule_treatable_permit_parameter`'s
        domain, not this rule's. Pinned to avoid double-counting
        across the two rules."""
        f = {"permit_has_bod": 1}
        self.assertIsNone(scoring.rule_exceeds_treatable_parameter(f))

    def test_intersection_fires_at_fifteen(self):
        f = {"permit_has_phosphorus": 1,
             "permit_has_bod": 1,
             "exceeded_treatable_parameters_text": "phosphorus | tss"}
        result = scoring.rule_exceeds_treatable_parameter(f)
        self.assertIsNotNone(result)
        pts, reason = result
        self.assertEqual(pts, 15)
        # Reason names the matched class so sales can read it.
        self.assertIn("phosphorus", reason)
        # BOD is permitted but not exceeded — must NOT appear.
        self.assertNotIn("bod", reason.lower())
        # TSS is exceeded but not permitted — must NOT appear.
        self.assertNotIn("tss", reason.lower())

    def test_multi_match_lists_all_in_reason(self):
        f = {"permit_has_phosphorus": 1,
             "permit_has_bod": 1,
             "permit_has_metals": 1,
             "exceeded_treatable_parameters_text": "phosphorus | bod | cyanide"}
        pts, reason = scoring.rule_exceeds_treatable_parameter(f)
        # Same +15 — not multiplied (one rule, one cap).
        self.assertEqual(pts, 15)
        # Both matches surface in the reason, alphabetized for
        # stable diff.
        self.assertIn("bod", reason.lower())
        self.assertIn("phosphorus", reason.lower())
        # Cyanide is exceeded but NOT permitted on this facility.
        self.assertNotIn("cyanide", reason.lower())


class TestNewTags(unittest.TestCase):

    def test_tag_recent_exceedance_tracks_top_pct(self):
        self.assertTrue(scoring.compute_tags(
            {"top_exceedance_pct": 50})["tag_recent_exceedance"])
        self.assertFalse(scoring.compute_tags(
            {"top_exceedance_pct": 0})["tag_recent_exceedance"])
        self.assertFalse(scoring.compute_tags(
            {})["tag_recent_exceedance"])
        # Defensive: unparseable strings (legacy/malformed rows)
        # must not crash and must evaluate False.
        self.assertFalse(scoring.compute_tags(
            {"top_exceedance_pct": "n/a"})["tag_recent_exceedance"])

    def test_tag_exceeds_treatable_matches_rule(self):
        # Same intersection logic as the rule.
        self.assertTrue(scoring.compute_tags({
            "permit_has_bod": 1,
            "exceeded_treatable_parameters_text": "bod"
        })["tag_exceeds_treatable_parameter"])
        self.assertFalse(scoring.compute_tags({
            "permit_has_bod": 1,
            "exceeded_treatable_parameters_text": "phosphorus"
        })["tag_exceeds_treatable_parameter"])

    def test_high_relevance_composite_picks_up_exceedance(self):
        tags = scoring.compute_tags({
            "permit_has_bod": 1,
            "exceeded_treatable_parameters_text": "bod",
        })
        self.assertTrue(tags["tag_chemtreat_high_relevance"])

    def test_do_not_call_guardrail_still_wins(self):
        """The strongest positive signal in the system
        (tag_exceeds_treatable_parameter) MUST still be demoted to
        False on the composite when every drilled event is
        Resolved/Archived. Pinned — adding more positive signals to
        the OR side can't be allowed to bypass the guardrail."""
        f = {"permit_has_bod": 1,
             "exceeded_treatable_parameters_text": "bod",
             "top_exceedance_pct": 500}
        events = [
            {"status": "Resolved", "violation_category": "Treatment Technique"},
            {"status": "Archived"},
        ]
        tags = scoring.compute_tags(f, events)
        self.assertTrue(tags["tag_exceeds_treatable_parameter"])
        self.assertTrue(tags["tag_only_resolved_events"])
        self.assertFalse(tags["tag_chemtreat_high_relevance"])


class TestPerRuleCapsAreIndependent(unittest.TestCase):
    """Each new rule has its own +15 cap. They legitimately co-fire
    with the existing pre-violation pair — a facility permitted on
    AND exceeding BOD picks up rule_treatable_permit_parameter (+5)
    AND rule_recent_dmr_exceedance (+15) AND
    rule_exceeds_treatable_parameter (+15) for +35 total. That's the
    intended behavior — the rules describe different but additive
    signals. The cap on each individual rule is what matters for
    score-distribution stability."""

    def test_recent_dmr_rule_caps_at_fifteen(self):
        """No tier above +15 even at extreme exceedance — pin so a
        well-meaning future tier like 'over 5000% gets +20' has to
        face this test."""
        pts, _ = scoring.rule_recent_dmr_exceedance(
            {"top_exceedance_pct": 999999})
        self.assertEqual(pts, 15)

    def test_composite_rule_caps_at_fifteen(self):
        """No matter how many treatable classes are both permitted
        AND exceeded, the composite rule contributes +15. Same cap
        rationale as rule_treatable_permit_parameter."""
        f = {col: 1 for col in scoring.PERMIT_HAS_COLS}
        f["exceeded_treatable_parameters_text"] = " | ".join(
            c.replace("permit_has_", "") for c in scoring.PERMIT_HAS_COLS)
        pts, _ = scoring.rule_exceeds_treatable_parameter(f)
        self.assertEqual(pts, 15)

    def test_full_stack_pinned_value(self):
        """The fully-stacked pre-violation + active-exceedance value
        for a facility permitted on AND exceeding one treatable
        class with a severe exceedance: 5 + 15 + 15 = 35. Pinned
        so a refactor that changes one rule's weight is forced to
        update this number deliberately."""
        raw = {
            "top_exceedance_pct": 2500,
            "permit_has_bod": 1,
            "exceeded_treatable_parameters_text": "bod",
        }
        score, reasons = scoring.score_facility(raw)
        self.assertEqual(score, 35,
            msg=f"Expected full stack 35; got {score}: {reasons}")


if __name__ == "__main__":
    unittest.main()
