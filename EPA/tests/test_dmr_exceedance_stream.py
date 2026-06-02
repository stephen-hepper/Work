"""Pin behavior of stream_dmr_exceedances.

The real npdes_dmr_fy2026.zip is 344 MB compressed / ~5 GB unzipped, so
the streamer aggressively filters and rolls up per-permit. Tests here
cover the silent-failure surfaces specific to this feed:

  1. Permit-ID filter actually filters (an unfiltered scan would OOM
     on the 5 GB CSV).
  2. EXCEEDENCE_PCT is the EPA-misspelled column name — test fixtures
     write it as such, the streamer reads it as such, both confirmed
     against the live file. A "fix" of the spelling on either side
     would silently produce zero exceedances.
  3. EXCEEDENCE_PCT filtering: blank, "0", and unparseable rows are
     skipped. Real data: 99.65% of rows are compliant; without this
     filter every compliant row would inflate the count.
  4. Per-permit rollup correctly tracks the WORST single exceedance
     across multiple rows (not the most recent, not the first).
   5. Treatable-class union spans multiple rows on the same permit;
      mismatched classes (e.g. "Whole effluent toxicity") are not
      mis-classified.
  6. Per-row event payload populates the per-DMR fields the existing
     bulk NPDES_SE feed leaves None — closing the depth gap.
  7. Empty inputs short-circuit; missing CSV raises loud.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from chemtreat_water_leads import bulk_loader
from tests._fixtures import make_dmr_zip


class TestSafePct(unittest.TestCase):
    """Direct unit on the parse helper — covers the long tail of
    edge values that show up in real EPA exports."""

    def test_blank_returns_none(self):
        self.assertIsNone(bulk_loader._safe_pct(""))
        self.assertIsNone(bulk_loader._safe_pct("   "))
        self.assertIsNone(bulk_loader._safe_pct(None))

    def test_zero_or_negative_returns_none(self):
        """0 means "reported at limit, not over". Negative would mean
        "reported under limit by N%". Neither is an exceedance."""
        self.assertIsNone(bulk_loader._safe_pct("0"))
        self.assertIsNone(bulk_loader._safe_pct("0.0"))
        self.assertIsNone(bulk_loader._safe_pct("-15"))

    def test_positive_float_returns_value(self):
        self.assertEqual(bulk_loader._safe_pct("50"), 50.0)
        self.assertEqual(bulk_loader._safe_pct("1836.0"), 1836.0)
        self.assertEqual(bulk_loader._safe_pct("0.1"), 0.1)

    def test_unparseable_returns_none(self):
        """EPA sometimes writes "NA" or "" in numeric columns —
        defensive."""
        self.assertIsNone(bulk_loader._safe_pct("NA"))
        self.assertIsNone(bulk_loader._safe_pct("n/a"))
        self.assertIsNone(bulk_loader._safe_pct("--"))


class TestStreamDmrExceedances(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_filters_to_kept_permits(self):
        """Most important test: without this, the streamer would
        load every exceedance in the 5 GB file. A permit outside
        scope must yield NO signal."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "KEEP1",
             "PARAMETER_DESC": "Phosphorus, total [as P]",
             "EXCEEDENCE_PCT": "75",
             "NPDES_VIOLATION_ID": "V1"},
            {"EXTERNAL_PERMIT_NMBR": "DROP1",
             "PARAMETER_DESC": "Phosphorus, total [as P]",
             "EXCEEDENCE_PCT": "75",
             "NPDES_VIOLATION_ID": "V2"},
        ]
        zip_path = make_dmr_zip(self.tmp_path, rows)
        sig, events = bulk_loader.stream_dmr_exceedances(zip_path, {"KEEP1"})
        self.assertIn("KEEP1", sig)
        self.assertNotIn("DROP1", sig,
            msg="Out-of-scope permit leaked; would OOM on real 5 GB file.")
        # Events follow the same filter — no V2 should escape.
        self.assertEqual([e["violation_id"] for e in events], ["V1"])

    def test_skips_compliant_and_unparseable(self):
        """The 99.65% of rows that are compliant must be silently
        filtered. Blank, "0", "0.0", and unparseable must all be
        treated identically (no event emitted, no signal recorded)."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C",
             "EXCEEDENCE_PCT": ""},          # blank
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C",
             "EXCEEDENCE_PCT": "0"},         # at-limit
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C",
             "EXCEEDENCE_PCT": "NA"},        # unparseable
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C",
             "EXCEEDENCE_PCT": "-10"},       # negative — under limit
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C",
             "EXCEEDENCE_PCT": "25",         # genuine, kept
             "NPDES_VIOLATION_ID": "VYes"},
        ]
        zip_path = make_dmr_zip(self.tmp_path, rows)
        sig, events = bulk_loader.stream_dmr_exceedances(zip_path, {"P1"})
        # Only the +25% row counts.
        self.assertEqual(sig["P1"]["recent_dmr_exceedances_count"], 1)
        self.assertEqual(sig["P1"]["top_exceedance_pct"], 25.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["violation_id"], "VYes")

    def test_top_exceedance_is_max_not_first(self):
        """The rollup must track the MAX of top_exceedance_pct across
        all rows for the permit — not the first-encountered, not the
        most-recent-encountered. Pin against ordering bugs."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "Phosphorus, total [as P]",
             "EXCEEDENCE_PCT": "50"},
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C",
             "EXCEEDENCE_PCT": "2500"},      # max
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "Lead, total recoverable",
             "EXCEEDENCE_PCT": "300"},
        ]
        zip_path = make_dmr_zip(self.tmp_path, rows)
        sig, _ = bulk_loader.stream_dmr_exceedances(zip_path, {"P1"})
        self.assertEqual(sig["P1"]["top_exceedance_pct"], 2500.0)
        self.assertEqual(sig["P1"]["top_exceeded_parameter"],
                         "BOD, 5-day, 20 deg. C")
        self.assertEqual(sig["P1"]["recent_dmr_exceedances_count"], 3)

    def test_treatable_classes_union_alphabetized(self):
        """Treatable-class union spans multiple rows and dedupes;
        sorted output for stable snapshot diffs."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "Phosphorus, total [as P]",
             "EXCEEDENCE_PCT": "10"},
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C",
             "EXCEEDENCE_PCT": "20"},
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "BOD, carbonaceous [5 day, 20 C]",
             "EXCEEDENCE_PCT": "30"},   # duplicate bod class
            # Non-treatable — must NOT be in the treatable text.
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "Whole effluent toxicity",
             "EXCEEDENCE_PCT": "100"},
        ]
        zip_path = make_dmr_zip(self.tmp_path, rows)
        sig, _ = bulk_loader.stream_dmr_exceedances(zip_path, {"P1"})
        # `phosphorus` < `bod` alphabetically? No — "bod" < "phosphorus".
        self.assertEqual(sig["P1"]["exceeded_treatable_parameters_text"],
                         "bod | phosphorus")

    def test_event_payload_carries_per_dmr_detail(self):
        """The whole point: events from this stream carry the
        per-DMR fields (parameter, limit, dmr, exceedance %, period
        end) that bulk NPDES_SE events leave as None. Pinned because
        the entire reason for shipping this feed is that depth fill-
        in."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "Manganese, total recoverable",
             "LIMIT_VALUE_STANDARD_UNITS": "50",
             "STANDARD_UNIT_DESC": "mg/L",
             "DMR_VALUE_STANDARD_UNITS": "530",
             "DMR_UNIT_DESC": "mg/L",
             "EXCEEDENCE_PCT": "960",
             "MONITORING_PERIOD_END_DATE": "04/30/2026",
             "VIOLATION_CODE": "E90",
             "NPDES_VIOLATION_ID": "V12345"},
        ]
        zip_path = make_dmr_zip(self.tmp_path, rows)
        _, events = bulk_loader.stream_dmr_exceedances(zip_path, {"P1"})
        self.assertEqual(len(events), 1)
        e = events[0]
        # All the gold fields.
        self.assertEqual(e["parameter"], "Manganese, total recoverable")
        self.assertEqual(e["limit_value"], "50")
        self.assertEqual(e["dmr_value"], "530")
        self.assertEqual(e["exceedance_pct"], 960.0)
        self.assertEqual(e["period_end"], "04/30/2026")
        self.assertEqual(e["violation_code"], "E90")
        self.assertEqual(e["violation_id"], "V12345")
        self.assertEqual(e["program"], "CWA")
        # Status defaulted Unresolved — same convention as the API
        # path and stream_npdes_violations.
        self.assertEqual(e["status"], "Unresolved")

    def test_int32_max_sentinel_clamped_for_display(self):
        """EPA reports EXCEEDENCE_PCT as INT32_MAX (2,147,483,647)
        when the underlying LIMIT_VALUE is 0 — verified against the
        live FY2026 file (permit AKG528836, "Seafood Processing
        Waste"). The +15 severity tier still applies, but the raw
        value is useless for display. The streamer clamps the
        per-permit top_exceedance_pct to 99,999% so CSV cells and
        viewer renders stay readable, while the per-event payload
        keeps the raw float for downstream audit.

        Pinned because a future "let's strip the clamp" refactor
        would push 2-billion-percent values into the CSV and viewer,
        looking very broken to sales."""
        rows = [
            {"EXTERNAL_PERMIT_NMBR": "P1",
             "PARAMETER_DESC": "Seafood Processing Waste exceedance",
             "LIMIT_VALUE_STANDARD_UNITS": "0",
             "DMR_VALUE_STANDARD_UNITS": "30",
             "EXCEEDENCE_PCT": "2147483650"},
        ]
        zip_path = make_dmr_zip(self.tmp_path, rows)
        sig, events = bulk_loader.stream_dmr_exceedances(zip_path, {"P1"})
        # Rollup is clamped.
        self.assertEqual(sig["P1"]["top_exceedance_pct"], 99_999.0)
        # Event payload keeps the raw — downstream audit can still
        # see the sentinel and understand what EPA reported.
        self.assertEqual(events[0]["exceedance_pct"], 2147483650.0)

    def test_empty_kept_set_short_circuits(self):
        rows = [{"EXTERNAL_PERMIT_NMBR": "P1",
                 "PARAMETER_DESC": "Phosphorus",
                 "EXCEEDENCE_PCT": "50"}]
        zip_path = make_dmr_zip(self.tmp_path, rows)
        sig, events = bulk_loader.stream_dmr_exceedances(zip_path, set())
        self.assertEqual(sig, {})
        self.assertEqual(events, [])

    def test_missing_csv_raises_loud(self):
        bogus = self.tmp_path / "bogus.zip"
        with zipfile.ZipFile(bogus, "w") as zf:
            zf.writestr("README.txt", "no csv here")
        with self.assertRaises(RuntimeError) as ctx:
            bulk_loader.stream_dmr_exceedances(bogus, {"P1"})
        self.assertIn("data-downloads", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
