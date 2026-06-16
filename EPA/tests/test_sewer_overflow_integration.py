"""End-to-end wiring test for the sewer-overflow integration.

The streamers + scoring + schema each have unit tests. This test
exists because the wiring between them — exporter → lead dict →
__raw → re-score → snapshot → CSV — has many silent-failure
surfaces no isolated unit test would catch. Same intent as
`test_permit_attains_integration.py`.

Specifically:
  * Did `stream_sewer_overflow_events`, `stream_collection_system_
    permits`, and `stream_cso_inventory` actually get called?
  * Did their output merge into both `lead` AND `lead["__raw"]`?
  * Did the re-score pick up the new rules BEFORE drill-down
    candidate selection?
  * Do the new columns land in the materialized CSV?
  * Are sewer-event rows persisted to the violations table with
    the right shape?
  * Does `_download_cached` get called with `max_age_days=1` for
    the daily-refresh sewer-overflow feed (not the default 7d)?
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chemtreat_water_leads import bulk_loader, dump_run, snapshot
from tests._fixtures import (
    make_cso_inventory_zip,
    make_exporter_zip,
    make_sewer_overflow_zip,
)


class TestSewerOverflowIntegration(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_sewer_signals_flow_through_to_csv_and_lift_score(self):
        # One CWA-signal facility with sewer-overflow signal attached.
        # NAICS 325 (chemical manufacturing) is in `TARGET_NAICS` so
        # the bulk loader keeps the row. **Real POTW NAICS (2213x) is
        # NOT currently in TARGET_NAICS — see the open product
        # question in CSO_SSO_PLAN.md.** The wiring contract this test
        # pins is "if a permit has sewer-overflow data, the signal
        # lands on the lead row." Whether POTW-NAICS leads ENTER the
        # inventory in the first place is a separate decision.
        exporter_rows = [{
            "REGISTRY_ID": "110000888001",
            "FAC_NAME": "Springfield Industrial",
            "FAC_STATE": "IL",
            "FAC_NAICS_CODES": "325",
            "NPDES_IDS": "IL0888888",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant/Category I Noncompliance",
        }]

        # Two sewer-overflow events on the permit:
        #   * One dry-weather SSO of 250K gal — should drive the SEVERE
        #     tier (+15) via the (dry AND SSO AND ≥100K) branch.
        #   * One wet-weather CSO of 5K gal — keeps multi-row + CSO
        #     coverage exercised; doesn't change the tier (severe wins).
        sewer_events = [
            {"sewer_overflow_bypass_event_key": "E1",
             "permit_identifier": "IL0888888",
             "sewer_overflow_bypass_start_datetime": "2026-05-15 09:00:00",
             "sewer_overflow_bypass_end_datetime": "2026-05-15 13:00:00",
             "sewer_overflow_bypass_discharge_volume_gallons": "250000",
             "wet_weather_occurance_indicator": "N",
             "sewer_overflow_structure_type_desc": "Pump Station"},
            {"sewer_overflow_bypass_event_key": "E2",
             "permit_identifier": "IL0888888",
             "sewer_overflow_bypass_start_datetime": "2026-05-20 14:30:00",
             "sewer_overflow_bypass_end_datetime": "2026-05-20 15:00:00",
             "sewer_overflow_bypass_discharge_volume_gallons": "5000",
             "wet_weather_occurance_indicator": "Y",
             "sewer_overflow_structure_type_desc": "Outfall"},
        ]
        sewer_types = [
            {"sewer_overflow_bypass_event_key": "E1",
             "sewer_overflow_bypass_type_code": "SSO"},
            {"sewer_overflow_bypass_event_key": "E2",
             "sewer_overflow_bypass_type_code": "CSO"},
        ]
        # Collection-system row with css_pct=20 (partially combined) +
        # population 25K — drives rule_combined_sewer_system (+5) AND
        # rule_collection_system_population at the medium tier (+7).
        sewer_permits = [
            {"permit_identifier": "IL0888888",
             "collection_system_identifier": "001",
             "collection_system_population": "25000",
             "percent_collection_system_css": "20"},
        ]
        sewer_zip = make_sewer_overflow_zip(
            self.tmp_path, sewer_events, sewer_types, sewer_permits)

        # CSO inventory: same permit also has CSO outfalls listed —
        # OR-merge with css_pct>0 still gives has_combined=1
        # (idempotent). Tests the wiring even though the eRule data
        # already covered it.
        cso_rows = [
            {"NPDES_ID": "IL0888888", "FACILITY_TYPE_INDICATOR": "POTW"},
        ]
        cso_zip = make_cso_inventory_zip(self.tmp_path, cso_rows)
        exporter_zip = make_exporter_zip(self.tmp_path, exporter_rows)

        out_dir = self.tmp_path / "out"
        db_path = self.tmp_path / "snap.sqlite"
        cache_dir = self.tmp_path / "cache"

        downloads_seen: list[tuple[str, int | None]] = []

        def fake_download_cached(url, cache_dir_arg, name,
                                 max_age_days=None):
            downloads_seen.append((name, max_age_days))
            mapping = {
                "echo_exporter": exporter_zip,
                "sewer_overflow": sewer_zip,
                "cso_inventory": cso_zip,
            }
            if name in mapping:
                return mapping[name]
            # Permit-limits / ATTAINS / DMR / NPDES / SDWA: stubbed
            # below so we never actually need the zips. Return a
            # sentinel path the mocks will never open.
            return cache_dir_arg / f"{name}.zip"

        # Stub the streamers we're NOT testing (everything except
        # sewer overflow / CS permits / CSO inventory). They return
        # empties so the lead row reflects ONLY the sewer signal
        # arithmetic — score math becomes inspectable below.
        with patch.object(bulk_loader, "_download_cached",
                          side_effect=fake_download_cached), \
             patch.object(bulk_loader, "stream_npdes_violations",
                          return_value=[]), \
             patch.object(bulk_loader, "stream_sdwa_violations",
                          return_value=[]), \
             patch.object(bulk_loader, "stream_permit_limits",
                          return_value={}), \
             patch.object(bulk_loader, "stream_attains_linkage",
                          return_value={}), \
             patch.object(bulk_loader, "stream_dmr_exceedances",
                          return_value=({}, [])), \
             patch.object(bulk_loader, "_drill_cwa", return_value=0), \
             patch.object(bulk_loader, "_drill_sdwa", return_value=0):
            bulk_loader.run_bulk(
                out_dir=out_dir,
                db_path=db_path,
                cache_dir=cache_dir,
                states=["IL"],
                include_events=True,
            )

        # ----- Wiring assertions ------------------------------------
        # The sewer-overflow daily-refresh feed was downloaded with
        # max_age_days=1 (not the default 7). The cso_inventory feed
        # was downloaded with the default (None passed through).
        sewer_calls = [m for n, m in downloads_seen if n == "sewer_overflow"]
        self.assertEqual(sewer_calls, [1, 1],
            msg="sewer_overflow downloaded with the wrong cache window. "
                "Called twice (once for events, once for cs permits) — "
                "both should pass max_age_days=1.")
        cso_calls = [m for n, m in downloads_seen if n == "cso_inventory"]
        self.assertEqual(cso_calls, [None],
            msg="cso_inventory should use the default cache window")

        # ----- Persistence assertions -------------------------------
        materialize_dir = out_dir / "_materialized"
        with snapshot.open_db(db_path) as conn:
            run_id = dump_run.resolve_run_id(conn, run_id=None, latest=True)
            dump_run.materialize_run(conn, run_id, materialize_dir)
        leads_csv = materialize_dir / "all_leads.csv"
        events_csv = materialize_dir / "violation_events.csv"
        self.assertTrue(leads_csv.exists())
        self.assertTrue(events_csv.exists())

        with leads_csv.open() as fh:
            leads = list(csv.DictReader(fh))
        self.assertEqual(len(leads), 1)
        row = leads[0]

        # Sewer signals landed on the persisted row.
        self.assertEqual(int(row["recent_sewer_overflow_count"]), 2)
        self.assertEqual(float(row["recent_sewer_overflow_volume_gal"]),
                         255000.0)  # 250K + 5K
        self.assertIn("SSO", row["recent_sewer_overflow_types"])
        self.assertIn("CSO", row["recent_sewer_overflow_types"])
        self.assertEqual(int(row["has_dry_weather_overflow"]), 1)
        # Collection-system signals landed.
        self.assertEqual(int(row["percent_collection_system_css"]), 20)
        self.assertEqual(int(row["collection_system_population"]), 25000)
        # has_combined_sewer_system = OR of eRule (css_pct>0) and CSO
        # inventory (permit present). Either branch flips it to 1; the
        # final value is 1.
        self.assertEqual(int(row["has_combined_sewer_system"]), 1)

        # Tags follow.
        self.assertEqual(row["tag_recent_sewer_overflow"], "True")
        self.assertEqual(row["tag_recent_sso"], "True")
        self.assertEqual(row["tag_dry_weather_overflow"], "True")
        self.assertEqual(row["tag_combined_sewer_system"], "True")
        # Composite picks up the sewer signal even with no other CWA
        # active-compliance signal in this fixture.
        self.assertEqual(row["tag_chemtreat_high_relevance"], "True")

        # Score math (pinned to current WEIGHTS). The sewer events the
        # streamer emits are status="Unresolved", which means they're
        # genuine open violations — `rule_active_open_events` fires
        # on them too (+5/event, 2 events = +10). This stacking is
        # the design intent: a sewer overflow event IS an open
        # violation, the score should reflect both the categorical
        # signal AND the "open right now" signal.
        #   +40  SNC text
        #   +15  rule_recent_sewer_overflow (SEVERE: dry SSO ≥100K)
        #   + 5  rule_combined_sewer_system
        #   + 7  rule_collection_system_population (medium: ≥10K)
        #   +10  rule_active_open_events (2 Unresolved events × +5)
        # Total = 77.
        from chemtreat_water_leads import scoring
        expected = (
            scoring.WEIGHTS["snc"]
            + scoring.WEIGHTS["sewer_overflow_severe"]
            + scoring.WEIGHTS["combined_sewer_system"]
            + scoring.WEIGHTS["collection_system_pop_medium"]
            + 2 * scoring.WEIGHTS["active_open_event_per"]
        )
        self.assertEqual(int(row["lead_score"]), expected,
            msg=f"Expected SNC+SEVERE+CSS+POP = {expected}; "
                f"got {row['lead_score']}. Score reasons: "
                f"{row['score_reasons']!r}")

        # ----- Event-row assertions ---------------------------------
        with events_csv.open() as fh:
            events = list(csv.DictReader(fh))
        # Two sewer events persisted as CWA-shaped violations.
        sewer_event_rows = [
            e for e in events
            if e.get("violation_category") == "Sewer Overflow / Bypass Event"
        ]
        self.assertEqual(len(sewer_event_rows), 2)
        by_key = {e["violation_id"]: e for e in sewer_event_rows}
        self.assertIn("E1", by_key)
        self.assertIn("E2", by_key)
        # The dry-weather SSO event carries the right parameter,
        # volume, and description.
        e1 = by_key["E1"]
        self.assertEqual(e1["parameter"], "Sanitary Sewer Overflow")
        self.assertEqual(e1["dmr_value"], "250000")
        self.assertEqual(e1["dmr_unit"], "gallons")
        self.assertIn("dry-weather", e1["violation_description"])
        # Lag note carries the eRule caveat.
        self.assertIn("eRule Phase 2", e1["data_lag_note"])


if __name__ == "__main__":
    unittest.main()
