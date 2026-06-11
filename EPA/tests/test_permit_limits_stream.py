"""Pin behavior of stream_permit_limits and _classify_parameter.

The real npdes_limits.zip is 513MB / 7.2GB unzipped, so the streamer
is built around aggressive filtering and per-permit rollup. Tests
here cover the silent-failure surfaces:

  * permit-ID filter actually filters (an unfiltered scan would OOM
    on the real file).
  * status flag filter drops inactive limit-sets (else expired
    permits leak in as positive signal).
  * parameter classifier matches real EPA wordings AND doesn't
    over-match harmless ones (pH, Flow) — pattern drift is the most
    likely future regression.
  * multi-row rollup combines flags correctly.
  * empty / missing-file paths fail loudly, not silently.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from chemtreat_water_leads import bulk_loader
from tests._fixtures import make_permit_limits_zip


class TestClassifyParameter(unittest.TestCase):
    """Pattern-matching helper; the substrings were chosen from a real
    500k-row sample of the live file. These tests pin the matches that
    informed the choice so future pattern changes have to face them."""

    def test_real_phrases_classify_correctly(self):
        # Wordings sampled from the live 2026-05-30 file. Row counts in
        # comments are from a 1M-row sample — quick sanity that the
        # class will fire on meaningful volume.
        cases = [
            ("BOD, 5-day, 20 deg. C", "bod"),
            ("BOD, carbonaceous [5 day, 20 C]", "bod"),
            ("Solids, total suspended", "tss"),
            ("Phosphorus, total [as P]", "phosphorus"),
            ("Nitrogen, ammonia total [as N]", "ammonia"),
            ("Oil & Grease", "oil_grease"),
            ("Oil and grease", "oil_grease"),
            ("Chlorine, total residual", "chlorine_residual"),
            ("Lead, total recoverable", "metals"),
            ("Copper, total recoverable", "metals"),
            ("Zinc, total [as Zn]", "metals"),
            # Iron / manganese added 2026-06-02 — high-volume metals
            # (Iron ~7.7k + ~7.2k rows; Manganese ~4.4k + ~4.1k rows
            # per 1M-row sample). Scale/discoloration product line,
            # same precipitation chemistry → rolled into `metals`.
            ("Iron, total [as Fe]", "metals"),
            ("Iron, total recoverable", "metals"),
            ("Iron, dissolved [as Fe]", "metals"),
            ("Manganese, total [as Mn]", "metals"),
            ("Manganese, dissolved [as Mn]", "metals"),
            # Cyanide added 2026-06-02 as its own class — oxidation
            # chemistry (alkaline chlorination / H2O2), distinct
            # product line, plating-shop / electronics niche.
            ("Cyanide, total [as CN]", "cyanide"),
            ("Cyanide, free available", "cyanide"),
            ("Cyanide, weak acid, dissociable", "cyanide"),
            ("Cyanide, free [amenable to chlorination]", "cyanide"),
            # Microbiological added 2026-06-11. ChemTreat sells
            # biocide / disinfection chemistry, so coliform / E. coli
            # / enterococci exceedances belong in the treatable bucket
            # alongside BOD and metals. EPA's wording is highly
            # variable; the patterns catch the common forms.
            ("Coliform, fecal general", "microbiological"),
            ("Fecal coliform", "microbiological"),
            ("E. coli", "microbiological"),
            ("Escherichia coli", "microbiological"),
            ("Enterococci", "microbiological"),
            ("Enterococcus", "microbiological"),
        ]
        for desc, expected in cases:
            with self.subTest(desc=desc):
                self.assertEqual(
                    bulk_loader._classify_parameter(desc), expected,
                    msg=f"{desc!r} should classify as {expected!r}")

    def test_harmless_parameters_do_not_classify(self):
        """Watchdog: non-treatable parameters must NOT classify, or the
        permit_has_* signal becomes meaningless (every NPDES permit
        has pH and Flow). Add new entries here when adding categories."""
        non_treatable = [
            "pH",
            "Flow, in conduit or thru treatment plant",
            "Rainfall",
            "Temperature, water deg. centigrade",
            "Oxygen, dissolved [DO]",
            "Specific conductance",
            # Note: coliform / E. coli / Enterococci were here as
            # "bacterial, not chemistry-treatable" — moved to the
            # positive cases above 2026-06-11 once ChemTreat's
            # biocide / disinfection product line was confirmed in
            # scope.
        ]
        for desc in non_treatable:
            with self.subTest(desc=desc):
                self.assertIsNone(
                    bulk_loader._classify_parameter(desc),
                    msg=f"{desc!r} unexpectedly classified")

    def test_lead_pattern_requires_comma_to_avoid_word_collisions(self):
        """The metals pattern uses `LEAD,` (with comma) so an isolated
        word like "Leading" or "Misleading" — possible in a malformed
        PARAMETER_DESC or a future EPA schema addition — does not
        classify as a metal. This pins the comma so a refactor can't
        quietly drop it.

        Note: a string containing BOTH "lead" and "copper" (e.g. an
        SDWA Lead-and-Copper Rule label) WILL classify as metals via
        the `COPPER` pattern. That's fine — `_classify_parameter` is
        only called against NPDES_LIMITS.csv's PARAMETER_DESC, where
        such labels don't appear."""
        self.assertIsNone(bulk_loader._classify_parameter("Leading indicator"))
        self.assertIsNone(bulk_loader._classify_parameter("Misleading data"))
        # The literal "Lead" alone (without a comma) likewise does not
        # match — real EPA parameter rows always carry the comma form.
        self.assertIsNone(bulk_loader._classify_parameter("Lead"))


class TestStreamPermitLimits(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_filters_to_kept_permit_ids(self):
        """The MOST IMPORTANT test in this file: without this filter
        the real 7.2GB file would balloon memory. A row outside the
        scope must produce NO entry, period."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "KEEP001",
             "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "Phosphorus, total [as P]"},
            {"EXTERNAL_PERMIT_NMBR": "DROP001",
             "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "Phosphorus, total [as P]"},
        ]
        zip_path = make_permit_limits_zip(self.tmp_path, rows)
        result = bulk_loader.stream_permit_limits(zip_path, {"KEEP001"})
        self.assertIn("KEEP001", result)
        self.assertNotIn("DROP001", result,
            msg="Permit outside kept-set leaked into result — would OOM "
                "on the real 7GB file.")

    def test_inactive_limit_sets_dropped(self):
        """LIMIT_SET_STATUS_FLAG='I' means the permit revision is
        superseded. Counting them as signal would surface expired
        compliance history as a current opportunity."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "LIMIT_SET_STATUS_FLAG": "I",   # inactive
             "PARAMETER_DESC": "Phosphorus, total [as P]"},
        ]
        zip_path = make_permit_limits_zip(self.tmp_path, rows)
        result = bulk_loader.stream_permit_limits(zip_path, {"P1"})
        self.assertNotIn("P1", result)

    def test_multi_parameter_rollup(self):
        """One permit with several treatable parameter rows produces
        ONE dict with multiple permit_has_* flags. Different outfalls
        and statistic-bases for the same parameter dedup correctly."""
        rows = [
            # Two outfalls, same phosphorus → one flag.
            {"EXTERNAL_PERMIT_NMBR": "P1", "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "Phosphorus, total [as P]",
             "PERM_FEATURE_NMBR": "001"},
            {"EXTERNAL_PERMIT_NMBR": "P1", "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "Phosphorus, total [as P]",
             "PERM_FEATURE_NMBR": "002"},
            # Different parameter class on the same permit.
            {"EXTERNAL_PERMIT_NMBR": "P1", "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C"},
            # Non-treatable — shouldn't add any flag.
            {"EXTERNAL_PERMIT_NMBR": "P1", "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "pH"},
        ]
        zip_path = make_permit_limits_zip(self.tmp_path, rows)
        result = bulk_loader.stream_permit_limits(zip_path, {"P1"})
        sig = result["P1"]
        self.assertEqual(sig["permit_has_phosphorus"], 1)
        self.assertEqual(sig["permit_has_bod"], 1)
        # No other permit_has_* keys (would cause false positives).
        flags = {k for k in sig if k.startswith("permit_has_")}
        self.assertEqual(flags, {"permit_has_phosphorus", "permit_has_bod"})
        # Parameters text alphabetized + deduped + treatable-only.
        self.assertEqual(
            sig["permitted_parameters_text"],
            "BOD, 5-day, 20 deg. C | Phosphorus, total [as P]")

    def test_empty_kept_set_short_circuits(self):
        """When no CWA leads exist, we shouldn't even open the file.
        Pins a fast-path that matters for SDWA-only runs."""
        rows = [{"EXTERNAL_PERMIT_NMBR": "P1", "LIMIT_SET_STATUS_FLAG": "A",
                 "PARAMETER_DESC": "Phosphorus"}]
        zip_path = make_permit_limits_zip(self.tmp_path, rows)
        result = bulk_loader.stream_permit_limits(zip_path, set())
        self.assertEqual(result, {})

    def test_missing_csv_raises_loud_error(self):
        """If EPA renames the CSV inside the zip (which has happened
        per MEMORY.md), we want a loud error pointing at the catalog,
        not a silent empty result."""
        bogus = self.tmp_path / "bogus.zip"
        with zipfile.ZipFile(bogus, "w") as zf:
            zf.writestr("README.txt", "no csv here")
        with self.assertRaises(RuntimeError) as ctx:
            bulk_loader.stream_permit_limits(bogus, {"P1"})
        self.assertIn("data-downloads", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
