"""End-to-end wiring test for the permit-limits + ATTAINS integration.

The unit tests cover each piece in isolation (scoring rules,
classifier, stream readers, --no-events gate). This test exists
because the wiring between them — exporter → lead dict → __raw →
re-score → snapshot → CSV — has many opportunities for silent
regression that no isolated unit test would catch. Specifically:

  * Did the stream readers actually get called?
  * Did their output get merged into both `lead` (for snapshot
    upsert) AND `lead["__raw"]` (so the scorer sees the columns)?
  * Did the re-score happen BEFORE the events arrived (so the new
    rules contribute to drill-down candidate selection)?
  * Did the new columns land in the snapshot DB and the CSV in the
    expected positions?

This is the test that catches "everything compiles, --no-events
still passes, but a real run shows no signal."
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chemtreat_water_leads import bulk_loader
from tests._fixtures import (
    make_attains_zip,
    make_exporter_zip,
    make_permit_limits_zip,
)


class TestPermitAttainsIntegration(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_signals_flow_through_to_csv_and_lift_score(self):
        # One CWA-signal facility. Modest summary signals so the new
        # rules genuinely affect the total (and we can pin the math).
        exporter_rows = [{
            "REGISTRY_ID": "110000999001",
            "FAC_NAME": "Acme Refining Co",
            "FAC_STATE": "TX",
            "FAC_NAICS_CODES": "324",   # petroleum/coal — in TARGET_NAICS
            "NPDES_IDS": "TX0009999",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant/Category I Noncompliance",
        }]
        # Permit limits: 3 distinct treatable classes (phosphorus, BOD,
        # oil/grease) — should fire rule_treatable_permit_parameter at
        # the cap (+15) and set the three permit_has_* booleans.
        limits_rows = [
            {"EXTERNAL_PERMIT_NMBR": "TX0009999", "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "Phosphorus, total [as P]"},
            {"EXTERNAL_PERMIT_NMBR": "TX0009999", "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "BOD, 5-day, 20 deg. C"},
            {"EXTERNAL_PERMIT_NMBR": "TX0009999", "LIMIT_SET_STATUS_FLAG": "A",
             "PARAMETER_DESC": "Oil and grease"},
            # Decoy: inactive permit revision — must be filtered out.
            {"EXTERNAL_PERMIT_NMBR": "TX0009999", "LIMIT_SET_STATUS_FLAG": "I",
             "PARAMETER_DESC": "Mercury, total recoverable"},
        ]
        # ATTAINS: this facility discharges into one impaired AU with
        # an E90 parameter match (Phosphorus) and one Good AU.
        # The match should fire rule_discharges_to_impaired at +15
        # (parameter-match branch, not the +10 plain branch).
        attains_rows = [
            {"REGISTRY_ID": "110000999001",
             "NPDES_ID": "TX0009999",
             "WATER_CONDITION": "Impaired - 303(d) Listed",
             "CAUSE_GROUPS_IMPAIRED": "NUTRIENTS",
             "E90_POT_IMP_PARAMETERS": "Phosphorus, total [as P]"},
            {"REGISTRY_ID": "110000999001",
             "NPDES_ID": "TX0009999",
             "WATER_CONDITION": "Good"},
        ]

        exporter_zip = make_exporter_zip(self.tmp_path, exporter_rows)
        limits_zip = make_permit_limits_zip(self.tmp_path, limits_rows)
        attains_zip = make_attains_zip(self.tmp_path, attains_rows)

        out_dir = self.tmp_path / "out"
        db_path = self.tmp_path / "snap.sqlite"
        cache_dir = self.tmp_path / "cache"

        # Route _download_cached to our four fixture paths. Anything
        # else is a wiring bug — fail loudly instead of producing a
        # silent empty result.
        def fake_download_cached(url, cache_dir_arg, name):
            mapping = {
                "echo_exporter": exporter_zip,
                "npdes_limits": limits_zip,
                "npdes_attains": attains_zip,
                # No event zips needed; the streamer mocks below
                # return [] without ever opening a file.
            }
            if name in mapping:
                return mapping[name]
            raise AssertionError(
                f"_download_cached called for unexpected feed: {name!r}")

        # Stub event streamers so we don't have to construct empty
        # event zips. _drill_* are also stubbed so no API path runs.
        with patch.object(bulk_loader, "_download_cached",
                          side_effect=fake_download_cached), \
             patch.object(bulk_loader, "stream_npdes_violations",
                          return_value=[]), \
             patch.object(bulk_loader, "stream_sdwa_violations",
                          return_value=[]), \
             patch.object(bulk_loader, "_drill_cwa", return_value=0), \
             patch.object(bulk_loader, "_drill_sdwa", return_value=0):
            bulk_loader.run_bulk(
                out_dir=out_dir,
                db_path=db_path,
                cache_dir=cache_dir,
                states=["TX"],
                include_events=True,
            )

        # Locate the run folder bulk_loader created.
        run_dirs = [p for p in out_dir.iterdir() if p.is_dir()]
        self.assertEqual(len(run_dirs), 1)
        leads_csv = run_dirs[0] / "all_leads.csv"
        self.assertTrue(leads_csv.exists())

        with leads_csv.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        row = rows[0]

        # Permit signals landed on the persisted row.
        self.assertEqual(row["permit_has_phosphorus"], "1")
        self.assertEqual(row["permit_has_bod"], "1")
        self.assertEqual(row["permit_has_oil_grease"], "1")
        # The decoy inactive Mercury row must NOT have leaked through.
        self.assertEqual(row["permit_has_metals"], "")
        # Text column carries the parameter list (alphabetized).
        self.assertIn("Phosphorus", row["permitted_parameters_text"])
        self.assertIn("BOD", row["permitted_parameters_text"])

        # ATTAINS signals landed and the stronger (parameter-match)
        # branch was selected (not the weaker plain-impaired branch).
        self.assertEqual(row["discharges_to_impaired"], "1")
        self.assertEqual(row["matching_impaired_parameters"],
                         "Phosphorus, total [as P]")
        self.assertEqual(row["impairment_causes_text"], "NUTRIENTS")

        # Tags follow.
        self.assertEqual(row["tag_treatable_permit"], "True")
        self.assertEqual(row["tag_discharges_to_impaired"], "True")
        self.assertEqual(row["tag_impairment_parameter_match"], "True")
        self.assertEqual(row["tag_chemtreat_high_relevance"], "True")

        # Score includes the new rule contributions. Expected
        # composition for this row:
        #   +40 rule_significant_violator (SNC flag + status text)
        #   +15 rule_treatable_permit_parameter (3 hits, capped)
        #   +15 rule_discharges_to_impaired (parameter-match branch)
        #   = 70
        # If this assertion ever fails on a value that's HIGHER, the
        # cap math is broken; if it's LOWER, the wiring isn't getting
        # the new signals into __raw before re-scoring. Either is a
        # silent regression — pin the exact number.
        self.assertEqual(int(row["lead_score"]), 70,
            msg=f"Lead score {row['lead_score']} != 70; reasons: "
                f"{row['score_reasons']}")

        # Score reasons string carries breakdown — sales must be able
        # to read the pre-violation rules in the audit trail.
        reasons = row["score_reasons"]
        self.assertIn("treatable parameter", reasons.lower())
        self.assertIn("matching impaired", reasons.lower())


if __name__ == "__main__":
    unittest.main()
