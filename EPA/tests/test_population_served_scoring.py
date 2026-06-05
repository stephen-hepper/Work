"""Pin rule_population_served — the SDWA revenue-proxy tier rule.

Population served is the only direct dollar-proxy signal we have for
drinking-water systems. The three tiers (50K / 10K / 3K) and their
points (10 / 7 / 4) reflect the spread between major utilities and
the long tail of community/transient systems. Tests cover:

  1. Threshold boundaries — pin both sides of each tier so a refactor
     can't silently widen or narrow a band.
  2. Source of truth — the rule's returned points equal WEIGHTS[...]
     for each tier; pins the WEIGHTS-dict refactor so a future rename
     can't desync the rule from the dict.
  3. Regression — the rule cleanly returns None for facilities without
     a PopulationServedCount column (CWA leads, bulk SDWA leads where
     ECHO Exporter doesn't carry the field).
  4. Integration — score_facility(raw) on an SDWA raw with both SNC
     and population contributions returns the expected sum.
"""

from __future__ import annotations

import unittest

from chemtreat_water_leads import scoring


class TestPopulationServedTiers(unittest.TestCase):
    def _pts(self, pop):
        r = scoring.rule_population_served({"PopulationServedCount": pop})
        return r[0] if r else None

    def test_below_smallest_tier(self):
        # Just under the small tier — no contribution.
        self.assertIsNone(self._pts(2_999))
        self.assertIsNone(self._pts(0))
        self.assertIsNone(self._pts(None))
        self.assertIsNone(self._pts(""))

    def test_tier_boundaries(self):
        cases = [
            (3_000,   scoring.WEIGHTS["population_small"]),
            (9_999,   scoring.WEIGHTS["population_small"]),
            (10_000,  scoring.WEIGHTS["population_medium"]),
            (49_999,  scoring.WEIGHTS["population_medium"]),
            (50_000,  scoring.WEIGHTS["population_large"]),
            (1_000_000, scoring.WEIGHTS["population_large"]),
        ]
        for pop, expected in cases:
            with self.subTest(pop=pop):
                self.assertEqual(self._pts(pop), expected)

    def test_reason_string_includes_locale_formatted_count(self):
        _, reason = scoring.rule_population_served(
            {"PopulationServedCount": 60_000})
        # Locale-formatted with comma; "major system" tag only on the
        # large tier so sales can scan for it.
        self.assertIn("60,000", reason)
        self.assertIn("major system", reason)

    def test_unparseable_returns_none(self):
        # _safe_int catches; not a tier hit.
        self.assertIsNone(self._pts("not a number"))


class TestRuleMatchesWeightsDict(unittest.TestCase):
    """A future weight-rename in WEIGHTS must keep the rule in sync.
    Pinned so a desync (rule body forgets to follow the dict) breaks
    the suite loudly instead of silently producing the old number."""

    def test_each_tier_returns_weights_value(self):
        self.assertEqual(
            scoring.rule_population_served({"PopulationServedCount": 100_000})[0],
            scoring.WEIGHTS["population_large"])
        self.assertEqual(
            scoring.rule_population_served({"PopulationServedCount": 20_000})[0],
            scoring.WEIGHTS["population_medium"])
        self.assertEqual(
            scoring.rule_population_served({"PopulationServedCount": 5_000})[0],
            scoring.WEIGHTS["population_small"])


class TestRegressionForNonSdwaShapes(unittest.TestCase):
    """The rule must be silent for raws that don't carry the column.
    Bulk SDWA leads and every CWA lead fall in this bucket today."""

    def test_cwa_shape_no_population(self):
        raw = {"RegistryID": "R1", "SNCFlag": "Y", "CWPSNCStatus": "..."}
        self.assertIsNone(scoring.rule_population_served(raw))

    def test_bulk_sdwa_shape_no_population(self):
        # Bulk's SDWA raw (per _bulk_to_program_shapes) carries
        # SourceID, SNCFlag, SNC, SeriousViolator, Feas, Ifea — but
        # NOT PopulationServedCount.
        raw = {"RegistryID": "R1", "SNCFlag": "N", "Feas": "1"}
        self.assertIsNone(scoring.rule_population_served(raw))


class TestScoreFacilityIntegration(unittest.TestCase):
    def test_sdwa_with_snc_and_population(self):
        raw = {
            "PWSName": "BIG CITY UTILITY",
            "SNCFlag": "Y",
            "PopulationServedCount": 60_000,
        }
        score, reasons = scoring.score_facility(raw)
        expected = scoring.WEIGHTS["snc"] + scoring.WEIGHTS["population_large"]
        self.assertEqual(score, expected)
        # Both rules contribute a reason line; population reason
        # carries the locale-formatted count.
        self.assertTrue(any("Serves 60,000" in r for r in reasons))
        self.assertTrue(any("Significant Non-Complier" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
