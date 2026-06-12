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

from chemtreat_water_leads import bulk_loader, dump_run, snapshot
from tests._fixtures import (
    make_attains_zip,
    make_dmr_zip,
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
        # DMR exceedances: phosphorus is BOTH permitted (above) AND
        # being exceeded by 175% (tier 100-200 → +10). This should
        # fire BOTH rule_recent_dmr_exceedance AND the composite
        # rule_exceeds_treatable_parameter. Plus a non-treatable
        # decoy row to confirm classifier doesn't false-positive.
        dmr_rows = [
            {"EXTERNAL_PERMIT_NMBR": "TX0009999",
             "PARAMETER_DESC": "Phosphorus, total [as P]",
             "LIMIT_VALUE_STANDARD_UNITS": "2.0",
             "DMR_VALUE_STANDARD_UNITS": "5.5",
             "STANDARD_UNIT_DESC": "mg/L",
             "EXCEEDENCE_PCT": "175",
             "MONITORING_PERIOD_END_DATE": "03/31/2026",
             "NPDES_VIOLATION_ID": "VEX1",
             "VIOLATION_CODE": "E90"},
            # Decoy: non-treatable parameter exceeding. Should NOT
            # fire the composite (no treatable class match), but the
            # base rule still tracks the worst overall — which here
            # is still phosphorus at 175%, NOT this decoy at 30%.
            {"EXTERNAL_PERMIT_NMBR": "TX0009999",
             "PARAMETER_DESC": "Whole effluent toxicity",
             "EXCEEDENCE_PCT": "30",
             "NPDES_VIOLATION_ID": "VDecoy"},
        ]

        exporter_zip = make_exporter_zip(self.tmp_path, exporter_rows)
        limits_zip = make_permit_limits_zip(self.tmp_path, limits_rows)
        attains_zip = make_attains_zip(self.tmp_path, attains_rows)
        dmr_zip = make_dmr_zip(self.tmp_path, dmr_rows)

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
                "dmr_fy2026": dmr_zip,
                # NPDES + SDWA event zips not needed — the streamer
                # mocks below return [] without ever opening a file.
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

        # Locate the run folder bulk_loader created — used to confirm
        # the run produced the inline artifacts only (run_health.json).
        run_dirs = [p for p in out_dir.iterdir() if p.is_dir()]
        self.assertEqual(len(run_dirs), 1)
        # bulk_loader no longer writes all_leads.csv inline (snapshot.sqlite
        # is the source of truth; materialize on demand). Build it from
        # the DB via dump_run, the same path sales would use.
        materialize_dir = out_dir / "_materialized"
        with snapshot.open_db(db_path) as conn:
            run_id = dump_run.resolve_run_id(conn, run_id=None, latest=True)
            dump_run.materialize_run(conn, run_id, materialize_dir)
        leads_csv = materialize_dir / "all_leads.csv"
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

        # DMR exceedance signals landed. top_exceeded_parameter is
        # the WORST single row, which is the +175% phosphorus row,
        # not the +30% decoy. exceeded_treatable_parameters_text
        # carries only the treatable class (phosphorus), not the
        # non-treatable decoy.
        self.assertEqual(row["top_exceeded_parameter"],
                         "Phosphorus, total [as P]")
        self.assertEqual(float(row["top_exceedance_pct"]), 175.0)
        self.assertEqual(row["exceeded_treatable_parameters_text"],
                         "phosphorus")
        # Count includes both rows (both are real exceedances), but
        # only the treatable one drives the composite.
        self.assertEqual(int(row["recent_dmr_exceedances_count"]), 2)

        # Tags follow.
        self.assertEqual(row["tag_treatable_permit"], "True")
        self.assertEqual(row["tag_discharges_to_impaired"], "True")
        self.assertEqual(row["tag_impairment_parameter_match"], "True")
        self.assertEqual(row["tag_recent_exceedance"], "True")
        self.assertEqual(row["tag_exceeds_treatable_parameter"], "True")
        self.assertEqual(row["tag_chemtreat_high_relevance"], "True")

        # Score includes ALL rule contributions. Expected composition:
        #   +40 rule_significant_violator (SNC flag + status text)
        #   +15 rule_treatable_permit_parameter (3 hits, capped)
        #   +15 rule_discharges_to_impaired (parameter-match branch)
        #   +10 rule_recent_dmr_exceedance (175% → 100-200 tier)
        #   +15 rule_exceeds_treatable_parameter (phosphorus
        #         permitted AND exceeded → composite)
        #   +10 rule_active_open_events (2 DMR-archive events, each
        #         emitted with status=Unresolved → 2 × 5 = 10)
        #   = 105
        # If HIGHER: cap math broken. If LOWER: wiring isn't getting
        # the new signals into __raw before re-scoring, OR an
        # expected rule didn't fire. Either is silent regression —
        # pin the exact number.
        self.assertEqual(int(row["lead_score"]), 105,
            msg=f"Lead score {row['lead_score']} != 105; reasons: "
                f"{row['score_reasons']}")

        # Score reasons string carries breakdown — sales must be able
        # to read every rule in the audit trail.
        reasons = row["score_reasons"]
        self.assertIn("treatable parameter", reasons.lower())
        self.assertIn("matching impaired", reasons.lower())
        self.assertIn("dmr exceedance", reasons.lower())
        self.assertIn("exceeding permitted", reasons.lower())

        # Violation events CSV should carry the per-DMR detail
        # populated — this is the depth gap the DMR archive integration
        # was built to close. Both the treatable row AND the decoy
        # land (both are real exceedances; the decoy just doesn't
        # match a treatable class).
        # Same path as all_leads.csv — materialized via dump_run.
        events_csv = materialize_dir / "violation_events.csv"
        with events_csv.open() as fh:
            event_rows = list(csv.DictReader(fh))
        treatable_event = next(
            (e for e in event_rows if e["violation_id"] == "VEX1"), None)
        self.assertIsNotNone(treatable_event,
            msg=f"DMR event VEX1 missing from violation_events.csv; "
                f"got {[e['violation_id'] for e in event_rows]}")
        # The per-DMR depth fields are exactly what the bulk
        # NPDES_SE feed leaves as None. Pinned because closing this
        # gap was the entire point of the integration.
        self.assertEqual(treatable_event["parameter"],
                         "Phosphorus, total [as P]")
        self.assertEqual(treatable_event["limit_value"], "2.0")
        self.assertEqual(treatable_event["dmr_value"], "5.5")
        self.assertEqual(float(treatable_event["exceedance_pct"]), 175.0)
        self.assertEqual(treatable_event["period_end"], "03/31/2026")


if __name__ == "__main__":
    unittest.main()
