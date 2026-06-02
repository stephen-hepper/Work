"""Pin behavior of stream_attains_linkage.

Tests cover the silent-failure surfaces:

  * REGISTRY_ID filter actually filters (the file is 600MB unzipped).
  * WATER_CONDITION interpretation: "Impaired*" → True for every
    restoration-plan variant; "Good"/"Unknown" → False. Wrong
    interpretation here would either over-count (flagging Good waters
    as impaired) or under-count (missing 303(d) listed waters with
    restoration plans).
  * Multi-row rollup: a facility touching several assessment units
    aggregates correctly; sets are unioned across rows.
  * Cause-group parsing splits on "|" and dedupes.
  * E90 parameter match is captured separately from plain impaired.
  * Empty inputs short-circuit; missing CSV raises loud.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from chemtreat_water_leads import bulk_loader
from tests._fixtures import make_attains_zip


class TestImpairedConditionParsing(unittest.TestCase):
    """Empirically observed WATER_CONDITION values from the live
    2026-05-30 file (500k-row sample). The classifier MUST handle
    all the "Impaired*" variants — 303(d) Listed and With Restoration
    Plan together account for ~80% of impaired rows."""

    def test_impaired_variants_are_impaired(self):
        for cond in [
            "Impaired",
            "Impaired - 303(d) Listed",
            "Impaired - 303(d) Listed - With Restoration Plan",
            "Impaired - With Restoration Plan",
        ]:
            with self.subTest(cond=cond):
                self.assertTrue(bulk_loader._is_impaired_condition(cond))

    def test_non_impaired_variants_are_not(self):
        for cond in [
            "Good",
            "Good - With Restoration Plan",
            "Unknown",
            "Unknown - With Restoration Plan",
            "",
        ]:
            with self.subTest(cond=cond):
                self.assertFalse(bulk_loader._is_impaired_condition(cond))


class TestStreamAttainsLinkage(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_filters_to_kept_registry_ids(self):
        rows = [
            {"REGISTRY_ID": "KEEP01", "NPDES_ID": "TX01",
             "WATER_CONDITION": "Impaired", "CAUSE_GROUPS_IMPAIRED": "NUTRIENTS"},
            {"REGISTRY_ID": "DROP01", "NPDES_ID": "TX99",
             "WATER_CONDITION": "Impaired", "CAUSE_GROUPS_IMPAIRED": "MERCURY"},
        ]
        zip_path = make_attains_zip(self.tmp_path, rows)
        result = bulk_loader.stream_attains_linkage(
            zip_path, kept_registry_ids={"KEEP01"}, kept_npdes_permits=set())
        self.assertIn("KEEP01", result)
        self.assertNotIn("DROP01", result)

    def test_multi_au_rollup_unions_causes(self):
        """One facility, three assessment units. discharges_to_impaired
        must be True if ANY are impaired (not all). Causes must be
        unioned across rows and sorted for stable diff output."""
        rows = [
            {"REGISTRY_ID": "FAC1", "WATER_CONDITION": "Good"},
            {"REGISTRY_ID": "FAC1", "WATER_CONDITION": "Impaired",
             "CAUSE_GROUPS_IMPAIRED": "NUTRIENTS | PATHOGENS"},
            {"REGISTRY_ID": "FAC1",
             "WATER_CONDITION": "Impaired - With Restoration Plan",
             "CAUSE_GROUPS_IMPAIRED": "MERCURY | PATHOGENS"},
        ]
        zip_path = make_attains_zip(self.tmp_path, rows)
        result = bulk_loader.stream_attains_linkage(
            zip_path, {"FAC1"}, set())
        sig = result["FAC1"]
        self.assertEqual(sig["discharges_to_impaired"], 1)
        # Sorted, deduped, pipe-joined.
        self.assertEqual(sig["impairment_causes_text"],
                         "MERCURY | NUTRIENTS | PATHOGENS")

    def test_e90_parameter_match_recorded_separately(self):
        """The stronger signal — matching_impaired_parameters — must
        be recorded only when E90_POT_IMP_PARAMETERS is populated.
        Empty/blank fields must not produce a key (an empty string
        on this column would fire rule_discharges_to_impaired's
        +15 branch incorrectly)."""
        rows = [
            {"REGISTRY_ID": "FAC2", "WATER_CONDITION": "Impaired",
             "CAUSE_GROUPS_IMPAIRED": "ORGANIC ENRICHMENT/OXYGEN DEPLETION",
             "E90_POT_IMP_PARAMETERS": "BOD, 5-day, 20 deg. C"},
        ]
        zip_path = make_attains_zip(self.tmp_path, rows)
        result = bulk_loader.stream_attains_linkage(
            zip_path, {"FAC2"}, set())
        sig = result["FAC2"]
        self.assertEqual(sig["matching_impaired_parameters"],
                         "BOD, 5-day, 20 deg. C")

    def test_no_e90_no_matching_key(self):
        """If E90 is blank, the matching_impaired_parameters key MUST
        be absent (not empty string). Otherwise the +15 branch of
        rule_discharges_to_impaired would mis-fire — pinned for
        regression."""
        rows = [
            {"REGISTRY_ID": "FAC3", "WATER_CONDITION": "Impaired",
             "CAUSE_GROUPS_IMPAIRED": "MERCURY",
             "E90_POT_IMP_PARAMETERS": ""},
        ]
        zip_path = make_attains_zip(self.tmp_path, rows)
        result = bulk_loader.stream_attains_linkage(
            zip_path, {"FAC3"}, set())
        sig = result["FAC3"]
        self.assertNotIn("matching_impaired_parameters", sig)

    def test_good_only_facility_has_no_impaired_flag(self):
        """A facility whose every assessment unit is "Good" must not
        get discharges_to_impaired=1. Pinned against an off-by-one
        where the default-truthy initialization could leak."""
        rows = [
            {"REGISTRY_ID": "FAC4", "WATER_CONDITION": "Good"},
            {"REGISTRY_ID": "FAC4", "WATER_CONDITION": "Good - With Restoration Plan"},
        ]
        zip_path = make_attains_zip(self.tmp_path, rows)
        result = bulk_loader.stream_attains_linkage(
            zip_path, {"FAC4"}, set())
        # Either no entry, or an entry without the impaired flag.
        sig = result.get("FAC4", {})
        self.assertFalse(sig.get("discharges_to_impaired"))

    def test_empty_inputs_short_circuit(self):
        """Empty filter sets must return {} without opening the file —
        important for the SDWA-only-territory path."""
        rows = [{"REGISTRY_ID": "FAC5", "WATER_CONDITION": "Impaired"}]
        zip_path = make_attains_zip(self.tmp_path, rows)
        result = bulk_loader.stream_attains_linkage(
            zip_path, kept_registry_ids=set(), kept_npdes_permits=set())
        self.assertEqual(result, {})

    def test_missing_csv_raises_loud_error(self):
        bogus = self.tmp_path / "bogus.zip"
        with zipfile.ZipFile(bogus, "w") as zf:
            zf.writestr("README.txt", "no csv here")
        with self.assertRaises(RuntimeError) as ctx:
            bulk_loader.stream_attains_linkage(bogus, {"FAC1"}, set())
        self.assertIn("data-downloads", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
