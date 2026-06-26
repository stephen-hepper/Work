"""Pin behavior of the sewer-overflow / collection-system scoring rules
and tags.

Covers:
  1. `rule_recent_sewer_overflow` tier ladder across (type, wet/dry,
     volume) combinations. The fall-through to MINOR for count-only
     rows matters — EPA leaves volume blank on ~30% of events.
  2. `rule_combined_sewer_system` flat bump, stacks with the event
     rule (intentional — CSS POTWs overflow more in wet weather).
  3. `rule_collection_system_population` tier thresholds match the
     SDWA population rule by design.
  4. New tags evaluate correctly, including the composite update
     (`tag_recent_sewer_overflow` joins `tag_chemtreat_high_relevance`
     on the OR-side).
  5. The do-not-call guardrail (`tag_only_resolved_events`) still
     demotes the composite even when sewer signal is positive.
"""

from __future__ import annotations

import unittest

from chemtreat_water_leads import scoring


class TestRuleRecentSewerOverflow(unittest.TestCase):
    """Tier ladder coverage for `rule_recent_sewer_overflow`. Each test
    pins one path through the if-chain; the ordering of asserts mirrors
    the ladder so regressions show up at the right level."""

    def test_no_events_returns_none(self):
        """Rule cleanly returns None when no sewer-overflow data has
        been merged into the lead row (the common case nationally —
        only 608 of ~80K NPDES permits had events in the 2026-06-15
        refresh)."""
        self.assertIsNone(scoring.rule_recent_sewer_overflow({}))
        self.assertIsNone(scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 0}))

    def test_severe_dry_weather_sso(self):
        """Dry-weather SSO ≥100K gal → SEVERE. The headline signal —
        sanitary sewer overflow in dry weather means raw sewage where
        it shouldn't be AND the treatment process isn't keeping up."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 3,
            "recent_sewer_overflow_volume_gal": 200_000,
            "recent_sewer_overflow_types": "SSO",
            "has_dry_weather_overflow": 1,
        })
        self.assertIsNotNone(result)
        pts, reason = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_severe"])
        self.assertIn("dry-weather SSO", reason)

    def test_severe_huge_volume_regardless_of_type(self):
        """Any event ≥1M gal scores SEVERE even without SSO or dry —
        that's catastrophic-tier (the live data's max was 2.8B gal)."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_volume_gal": 2_000_000,
            "recent_sewer_overflow_types": "CSO",
            "has_dry_weather_overflow": 0,
        })
        pts, reason = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_severe"])
        self.assertIn("2,000,000", reason)

    def test_high_wet_weather_sso(self):
        """Wet-weather SSO scores HIGH (not severe) — still raw sewage,
        but storm-driven mixes signals. Volume below 1M."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 2,
            "recent_sewer_overflow_volume_gal": 50_000,
            "recent_sewer_overflow_types": "SSO",
            "has_dry_weather_overflow": 0,
        })
        pts, reason = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_high"])
        self.assertIn("SSO", reason)

    def test_high_dry_weather_non_sso_large_volume(self):
        """Dry-weather CSO/BYP ≥100K gal scores HIGH — treatment plant
        bypass at scale in dry weather is the alarming signature."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_volume_gal": 500_000,
            "recent_sewer_overflow_types": "CSO",
            "has_dry_weather_overflow": 1,
        })
        pts, _ = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_high"])

    def test_moderate_wet_non_sso_mid_volume(self):
        """Wet-weather BYP at 50K gal → MODERATE. Not SSO, not big
        enough for HIGH, but enough volume to matter."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_volume_gal": 50_000,
            "recent_sewer_overflow_types": "BYP",
            "has_dry_weather_overflow": 0,
        })
        pts, _ = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_moderate"])

    def test_moderate_dry_unknown_volume(self):
        """Dry-weather event with no volume reported still scores
        MODERATE. EPA's volume cell is ~30% blank; dry-weather alone
        is a strong-enough signal not to fall through to MINOR."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_volume_gal": 0,
            "recent_sewer_overflow_types": "CSO",
            "has_dry_weather_overflow": 1,
        })
        pts, _ = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_moderate"])

    def test_minor_count_only(self):
        """Wet-weather CSO/BYP with no volume reported → MINOR. A
        non-zero signal but low confidence — caught explicitly so the
        rule doesn't silently zero out type-and-volume-blank rows."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_volume_gal": 0,
            "recent_sewer_overflow_types": "CSO",
            "has_dry_weather_overflow": 0,
        })
        pts, _ = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_minor"])

    def test_minor_blank_types(self):
        """Even with empty types text and no dry flag, a positive
        count still trips MINOR. Defensive — the type-code join could
        be lossy and we still want some signal."""
        result = scoring.rule_recent_sewer_overflow({
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_volume_gal": 0,
            "recent_sewer_overflow_types": "",
            "has_dry_weather_overflow": 0,
        })
        self.assertIsNotNone(result)
        pts, _ = result
        self.assertEqual(pts, scoring.WEIGHTS["sewer_overflow_minor"])


class TestRuleCombinedSewerSystem(unittest.TestCase):

    def test_returns_none_when_no_css(self):
        self.assertIsNone(scoring.rule_combined_sewer_system({}))
        self.assertIsNone(scoring.rule_combined_sewer_system(
            {"has_combined_sewer_system": 0}))

    def test_flat_bump_when_css(self):
        result = scoring.rule_combined_sewer_system(
            {"has_combined_sewer_system": 1})
        self.assertIsNotNone(result)
        pts, reason = result
        self.assertEqual(pts, scoring.WEIGHTS["combined_sewer_system"])
        self.assertIn("combined sewer", reason.lower())

    def test_stacks_with_event_rule(self):
        """A CSS POTW that also had a recent overflow scores BOTH
        rules — the design intent. Reason strings concatenated by the
        scorer; we just confirm both fire independently here."""
        f = {
            "has_combined_sewer_system": 1,
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_volume_gal": 200_000,
            "recent_sewer_overflow_types": "SSO",
            "has_dry_weather_overflow": 1,
        }
        self.assertIsNotNone(scoring.rule_combined_sewer_system(f))
        self.assertIsNotNone(scoring.rule_recent_sewer_overflow(f))
        total, reasons = scoring.score_facility(f)
        # +15 severe + +5 css = +20 (other rules return None on this
        # bare facility dict)
        self.assertEqual(total,
            scoring.WEIGHTS["sewer_overflow_severe"]
            + scoring.WEIGHTS["combined_sewer_system"])
        self.assertEqual(len(reasons), 2)


class TestRuleCollectionSystemPopulation(unittest.TestCase):
    """Tier thresholds intentionally mirror `rule_population_served` —
    equivalent-size POTW and PWS should score the same regardless of
    program."""

    def test_returns_none_below_small_threshold(self):
        self.assertIsNone(scoring.rule_collection_system_population(
            {"collection_system_population": 2_999}))
        self.assertIsNone(scoring.rule_collection_system_population({}))
        self.assertIsNone(scoring.rule_collection_system_population(
            {"collection_system_population": 0}))

    def test_small_tier(self):
        result = scoring.rule_collection_system_population(
            {"collection_system_population": 5_000})
        pts, reason = result
        self.assertEqual(pts, scoring.WEIGHTS["collection_system_pop_small"])
        self.assertIn("5,000", reason)

    def test_medium_tier(self):
        result = scoring.rule_collection_system_population(
            {"collection_system_population": 20_000})
        pts, _ = result
        self.assertEqual(pts, scoring.WEIGHTS["collection_system_pop_medium"])

    def test_large_tier(self):
        result = scoring.rule_collection_system_population(
            {"collection_system_population": 100_000})
        pts, reason = result
        self.assertEqual(pts, scoring.WEIGHTS["collection_system_pop_large"])
        self.assertIn("major collection system", reason)

    def test_tier_thresholds_match_sdwa(self):
        """Pin the SDWA-parity contract — both rules should fire at
        the same population numbers. If someone bumps one without the
        other, this test catches it."""
        self.assertEqual(
            scoring.WEIGHTS["collection_system_pop_large_threshold"],
            scoring.WEIGHTS["population_large_threshold"])
        self.assertEqual(
            scoring.WEIGHTS["collection_system_pop_medium_threshold"],
            scoring.WEIGHTS["population_medium_threshold"])
        self.assertEqual(
            scoring.WEIGHTS["collection_system_pop_small_threshold"],
            scoring.WEIGHTS["population_small_threshold"])


class TestSewerTags(unittest.TestCase):

    def test_all_off_when_signals_absent(self):
        tags = scoring.compute_tags({})
        self.assertFalse(tags["tag_recent_sewer_overflow"])
        self.assertFalse(tags["tag_recent_sso"])
        self.assertFalse(tags["tag_dry_weather_overflow"])
        self.assertFalse(tags["tag_combined_sewer_system"])

    def test_recent_overflow_on(self):
        tags = scoring.compute_tags(
            {"recent_sewer_overflow_count": 1})
        self.assertTrue(tags["tag_recent_sewer_overflow"])
        self.assertFalse(tags["tag_recent_sso"])

    def test_sso_substring_match(self):
        """The streamer pipe-joins types ("CSO | SSO" — sorted). Tag
        matches by substring so it survives any ordering."""
        for types_text in ("SSO", "CSO | SSO", "SSO | BYP"):
            with self.subTest(types_text=types_text):
                tags = scoring.compute_tags({
                    "recent_sewer_overflow_count": 1,
                    "recent_sewer_overflow_types": types_text,
                })
                self.assertTrue(tags["tag_recent_sso"])

    def test_dry_weather_tag(self):
        tags = scoring.compute_tags(
            {"has_dry_weather_overflow": 1})
        self.assertTrue(tags["tag_dry_weather_overflow"])

    def test_combined_sewer_tag(self):
        tags = scoring.compute_tags(
            {"has_combined_sewer_system": 1})
        self.assertTrue(tags["tag_combined_sewer_system"])

    def test_composite_picks_up_sewer_overflow(self):
        """`tag_chemtreat_high_relevance` should flip True purely on a
        recent sewer overflow, even with no other signals — the OR-side
        is liberal."""
        tags = scoring.compute_tags({
            "recent_sewer_overflow_count": 1,
            "recent_sewer_overflow_types": "SSO",
        })
        self.assertTrue(tags["tag_recent_sewer_overflow"])
        self.assertTrue(tags["tag_chemtreat_high_relevance"])

    def test_composite_does_not_pick_up_css_alone(self):
        """CSS is too common (267+ permits) to keep the composite at
        the "pare 7K rows to 50" goal. CSS alone must NOT flip the
        composite — it earns its keep as a standalone chip instead."""
        tags = scoring.compute_tags({"has_combined_sewer_system": 1})
        self.assertTrue(tags["tag_combined_sewer_system"])
        self.assertFalse(tags["tag_chemtreat_high_relevance"])

    def test_do_not_call_guardrail_still_demotes(self):
        """If every drilled event is Resolved/Archived, the composite
        demotes to False even when sewer signal is otherwise positive.
        This preserves the do-not-call contract."""
        tags = scoring.compute_tags(
            {"recent_sewer_overflow_count": 1,
             "recent_sewer_overflow_types": "SSO"},
            events=[{"status": "Resolved"}, {"status": "Archived"}],
        )
        self.assertTrue(tags["tag_recent_sewer_overflow"])
        self.assertTrue(tags["tag_only_resolved_events"])
        self.assertFalse(tags["tag_chemtreat_high_relevance"])


class TestRuleListMembership(unittest.TestCase):
    """All three new rules are registered in RULES so score_facility
    actually invokes them. Catches the "forgot to add to RULES" bug
    that's easy when adding rule functions in isolation."""

    def test_new_rules_in_RULES(self):
        names = {r.__name__ for r in scoring.RULES}
        self.assertIn("rule_recent_sewer_overflow", names)
        self.assertIn("rule_combined_sewer_system", names)
        self.assertIn("rule_collection_system_population", names)


if __name__ == "__main__":
    unittest.main()
