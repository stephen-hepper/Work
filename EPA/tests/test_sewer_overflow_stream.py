"""Pin behavior of stream_sewer_overflow_events.

The streamer reads two CSVs from a single zip (events + one-to-many
types lookup) and rolls them up per NPDES permit. Tests cover the
silent-failure surfaces specific to this feed:

  1. Permit-ID filter actually filters — the events file is small
     today (~4K rows) but will grow as more states onboard the eRule.
  2. Event-type code lives in a SEPARATE TABLE joined on
     sewer_overflow_bypass_event_key. A single event can carry multiple
     type codes (SSO + BYP, etc.). The streamer must pre-load that
     map before scanning events, not after.
  3. EPA's column names are lowercase_snake (verified 2026-06-15);
     fixture matches, streamer reads them as such.
  4. `wet_weather_occurance_indicator` is the tier dimension: dry-
     weather events trip `has_dry_weather_overflow`.
  5. Volume is sparse (~30% blank in the live data). Blank/zero/
     unparseable volumes don't pollute the sum and don't crash.
  6. Window filter cuts events with a parseable start_datetime older
     than the cutoff. Unparseable start datetimes are kept (defensive
     — the "current" archive's contents are already recent by design).
  7. `sewer_overflow_bypass_event_key` is the stable PK; used verbatim
     as `violation_id`. Without it, dedupe across daily refreshes
     would break.
  8. `permit_to_registry` backfills registry_id on event rows; an
     unmapped permit still emits the event (with registry_id=None) so
     the npdes_id fallback path in snapshot.diff_and_upsert_violations
     can still match.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from chemtreat_water_leads import bulk_loader
from tests._fixtures import make_sewer_overflow_zip


# A reference "now" so window-filter tests are deterministic. 2026-06-15
# matches the live refresh the streamer was designed against.
NOW = datetime(2026, 6, 15, 12, 0, 0)


class TestSafeVolumeGallons(unittest.TestCase):
    """Direct unit on the volume-parse helper."""

    def test_blank_returns_none(self):
        self.assertIsNone(bulk_loader._safe_volume_gallons(""))
        self.assertIsNone(bulk_loader._safe_volume_gallons("   "))
        self.assertIsNone(bulk_loader._safe_volume_gallons(None))

    def test_zero_or_negative_returns_none(self):
        """A reported 0 means "no measurable discharge" — don't count it."""
        self.assertIsNone(bulk_loader._safe_volume_gallons("0"))
        self.assertIsNone(bulk_loader._safe_volume_gallons("0.0"))
        self.assertIsNone(bulk_loader._safe_volume_gallons("-15"))

    def test_positive_float_returns_value(self):
        self.assertEqual(bulk_loader._safe_volume_gallons("192000.00"), 192000.0)
        self.assertEqual(bulk_loader._safe_volume_gallons("2400"), 2400.0)

    def test_unparseable_returns_none(self):
        self.assertIsNone(bulk_loader._safe_volume_gallons("NA"))
        self.assertIsNone(bulk_loader._safe_volume_gallons("unknown"))


class TestParseSewerDatetime(unittest.TestCase):
    """Direct unit on the datetime-parse helper. EPA writes naive
    timestamps; we accept them and treat as UTC-equivalent."""

    def test_valid_format(self):
        self.assertEqual(
            bulk_loader._parse_sewer_datetime("2025-09-09 20:30:00"),
            datetime(2025, 9, 9, 20, 30, 0),
        )

    def test_blank_or_unparseable_returns_none(self):
        self.assertIsNone(bulk_loader._parse_sewer_datetime(""))
        self.assertIsNone(bulk_loader._parse_sewer_datetime("   "))
        self.assertIsNone(bulk_loader._parse_sewer_datetime(None))
        self.assertIsNone(bulk_loader._parse_sewer_datetime("2025-09-09"))
        self.assertIsNone(bulk_loader._parse_sewer_datetime("not a date"))


class TestSewerOverflowStreamer(unittest.TestCase):
    """End-to-end tests against synthetic zips. Mirrors the shape of
    test_dmr_exceedance_stream.TestDMRStreamer."""

    def _build(self, tmp: Path, events: list[dict],
               types: list[dict]) -> Path:
        return make_sewer_overflow_zip(tmp, events, types)

    def test_empty_filter_short_circuits(self):
        """No permits in scope → returns empty without opening the zip."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, [], [])
            sigs, events = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits=set(), now=NOW)
        self.assertEqual(sigs, {})
        self.assertEqual(events, [])

    def test_filter_actually_filters(self):
        """Only events whose permit is in scope come back."""
        events = [
            {"sewer_overflow_bypass_event_key": "1",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00"},
            {"sewer_overflow_bypass_event_key": "2",
             "permit_identifier": "TX9999",   # NOT in scope
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00"},
        ]
        types = [
            {"sewer_overflow_bypass_event_key": "1",
             "sewer_overflow_bypass_type_code": "SSO"},
            {"sewer_overflow_bypass_event_key": "2",
             "sewer_overflow_bypass_type_code": "SSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            sigs, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, now=NOW)
        self.assertEqual(set(sigs), {"IL0001"})
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["permit_id"], "IL0001")

    def test_type_join_one_to_many(self):
        """A single event with two type rows shows up as a sorted,
        pipe-joined union in the per-permit signal AND the per-event
        parameter follows the SSO > CSO > BYP precedence."""
        events = [
            {"sewer_overflow_bypass_event_key": "100",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00",
             "sewer_overflow_bypass_discharge_volume_gallons": "5000"},
        ]
        types = [
            # Multi-type: SSO + BYP for an overflow that was both
            {"sewer_overflow_bypass_event_key": "100",
             "sewer_overflow_bypass_type_code": "BYP"},
            {"sewer_overflow_bypass_event_key": "100",
             "sewer_overflow_bypass_type_code": "SSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            sigs, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, now=NOW)
        # Sorted union — BYP comes before SSO alphabetically
        self.assertEqual(sigs["IL0001"]["recent_sewer_overflow_types"],
                         "BYP | SSO")
        # SSO wins the parameter mapping (highest precedence)
        self.assertEqual(evs[0]["parameter"], "Sanitary Sewer Overflow")

    def test_type_precedence_in_parameter(self):
        """Multi-event check: SSO > CSO > BYP > fallback."""
        events = [
            {"sewer_overflow_bypass_event_key": str(i),
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00"}
            for i in range(1, 5)
        ]
        types = [
            {"sewer_overflow_bypass_event_key": "1",
             "sewer_overflow_bypass_type_code": "SSO"},
            {"sewer_overflow_bypass_event_key": "2",
             "sewer_overflow_bypass_type_code": "CSO"},
            {"sewer_overflow_bypass_event_key": "3",
             "sewer_overflow_bypass_type_code": "BYP"},
            # Event 4 has no type row — falls back to "Sewer Overflow"
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            _, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, now=NOW)
        params_by_key = {e["violation_id"]: e["parameter"] for e in evs}
        self.assertEqual(params_by_key["1"], "Sanitary Sewer Overflow")
        self.assertEqual(params_by_key["2"], "Combined Sewer Overflow")
        self.assertEqual(params_by_key["3"], "Bypass")
        self.assertEqual(params_by_key["4"], "Sewer Overflow")

    def test_rollup_count_volume_recent_drylag(self):
        """Three events on one permit → count=3, volume summed,
        most_recent picked, dry-weather flag if ANY event is dry."""
        events = [
            # Oldest, dry-weather, 100 gal
            {"sewer_overflow_bypass_event_key": "1",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-01-15 08:00:00",
             "sewer_overflow_bypass_discharge_volume_gallons": "100",
             "wet_weather_occurance_indicator": "N"},
            # Middle, wet, blank volume — count increments but sum doesn't
            {"sewer_overflow_bypass_event_key": "2",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-03-20 14:00:00",
             "sewer_overflow_bypass_discharge_volume_gallons": "",
             "wet_weather_occurance_indicator": "Y"},
            # Newest, wet, 5000 gal
            {"sewer_overflow_bypass_event_key": "3",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-30 18:00:00",
             "sewer_overflow_bypass_discharge_volume_gallons": "5000",
             "wet_weather_occurance_indicator": "Y"},
        ]
        types = [
            {"sewer_overflow_bypass_event_key": "1",
             "sewer_overflow_bypass_type_code": "SSO"},
            {"sewer_overflow_bypass_event_key": "2",
             "sewer_overflow_bypass_type_code": "CSO"},
            {"sewer_overflow_bypass_event_key": "3",
             "sewer_overflow_bypass_type_code": "SSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            sigs, _ = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, now=NOW)
        sig = sigs["IL0001"]
        self.assertEqual(sig["recent_sewer_overflow_count"], 3)
        # 100 + 5000; blank doesn't contribute
        self.assertEqual(sig["recent_sewer_overflow_volume_gal"], 5100.0)
        self.assertEqual(sig["most_recent_sewer_overflow_at"],
                         "2026-05-30 18:00:00")
        # At least one dry-weather event in the set
        self.assertEqual(sig["has_dry_weather_overflow"], 1)
        self.assertEqual(sig["recent_sewer_overflow_types"], "CSO | SSO")

    def test_window_filter_cuts_old_events(self):
        """Events older than `now - window_days` are dropped."""
        events = [
            # 2 years before NOW — should be dropped at default window_days=365
            {"sewer_overflow_bypass_event_key": "old",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2024-06-01 10:00:00"},
            # Within window
            {"sewer_overflow_bypass_event_key": "new",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00"},
        ]
        types = [
            {"sewer_overflow_bypass_event_key": "old",
             "sewer_overflow_bypass_type_code": "SSO"},
            {"sewer_overflow_bypass_event_key": "new",
             "sewer_overflow_bypass_type_code": "SSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            sigs, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, window_days=365, now=NOW)
        self.assertEqual(sigs["IL0001"]["recent_sewer_overflow_count"], 1)
        self.assertEqual([e["violation_id"] for e in evs], ["new"])

    def test_unparseable_start_datetime_is_kept(self):
        """Defensive: a row with a blank/junk start_datetime stays in
        the result rather than being silently filtered out."""
        events = [
            {"sewer_overflow_bypass_event_key": "blank-dt",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "",
             "sewer_overflow_bypass_discharge_volume_gallons": "500"},
        ]
        types = [
            {"sewer_overflow_bypass_event_key": "blank-dt",
             "sewer_overflow_bypass_type_code": "SSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            sigs, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, now=NOW)
        self.assertEqual(sigs["IL0001"]["recent_sewer_overflow_count"], 1)
        self.assertEqual(len(evs), 1)

    def test_registry_id_backfill(self):
        """Mapped permits get a registry_id; unmapped still emit the
        event (with registry_id=None) so the npdes_id fallback in
        snapshot.diff_and_upsert_violations can still match."""
        events = [
            {"sewer_overflow_bypass_event_key": "1",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00"},
            {"sewer_overflow_bypass_event_key": "2",
             "permit_identifier": "IL0002",   # in scope but NOT in map
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00"},
        ]
        types = [
            {"sewer_overflow_bypass_event_key": "1",
             "sewer_overflow_bypass_type_code": "SSO"},
            {"sewer_overflow_bypass_event_key": "2",
             "sewer_overflow_bypass_type_code": "SSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            _, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001", "IL0002"},
                permit_to_registry={"IL0001": "REG_A"},
                now=NOW)
        by_permit = {e["permit_id"]: e for e in evs}
        self.assertEqual(by_permit["IL0001"]["registry_id"], "REG_A")
        self.assertIsNone(by_permit["IL0002"]["registry_id"])
        # Both still emitted — defensive contract
        self.assertEqual(len(evs), 2)

    def test_missing_event_key_skipped(self):
        """Without the stable PK, dedupe-across-refreshes is impossible.
        Skip rather than synthesize — upstream contract guarantees it."""
        events = [
            {"sewer_overflow_bypass_event_key": "",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, [])
            sigs, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, now=NOW)
        self.assertEqual(sigs, {})
        self.assertEqual(evs, [])

    def test_event_payload_shape(self):
        """Per-event row has the fields snapshot.diff_and_upsert_violations
        expects from CWA events. Description carries structure + wet/dry
        tag so the viewer's event row reads cleanly."""
        events = [
            {"sewer_overflow_bypass_event_key": "42",
             "permit_identifier": "IL0001",
             "sewer_overflow_bypass_start_datetime": "2026-05-01 10:00:00",
             "sewer_overflow_bypass_end_datetime": "2026-05-01 14:00:00",
             "sewer_overflow_bypass_discharge_volume_gallons": "192000.00",
             "wet_weather_occurance_indicator": "N",
             "sewer_overflow_structure_type_desc": "Pump Station"},
        ]
        types = [
            {"sewer_overflow_bypass_event_key": "42",
             "sewer_overflow_bypass_type_code": "SSO"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            zp = self._build(tmp, events, types)
            _, evs = bulk_loader.stream_sewer_overflow_events(
                zp, kept_npdes_permits={"IL0001"}, now=NOW)
        e = evs[0]
        self.assertEqual(e["violation_id"], "42")
        self.assertEqual(e["permit_id"], "IL0001")
        self.assertEqual(e["npdes_id"], "IL0001")
        self.assertEqual(e["program"], "CWA")
        self.assertEqual(e["violation_category"], "Sewer Overflow / Bypass Event")
        self.assertEqual(e["parameter"], "Sanitary Sewer Overflow")
        self.assertEqual(e["dmr_value"], "192000")
        self.assertEqual(e["dmr_unit"], "gallons")
        self.assertEqual(e["period_begin"], "2026-05-01 10:00:00")
        self.assertEqual(e["period_end"], "2026-05-01 14:00:00")
        self.assertEqual(e["violation_description"], "Pump Station — dry-weather")
        self.assertEqual(e["status"], "Unresolved")
        self.assertIn("eRule Phase 2", e["data_lag_note"])

    def test_missing_csv_raises_loud(self):
        """If EPA renames a file, fail loudly rather than silently
        produce zero rows — same convention as the DMR streamer."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Build a zip with the WRONG file name
            zp = tmp / "broken.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("renamed_events.csv", "permit_identifier\nIL0001\n")
            with self.assertRaises(RuntimeError) as ctx:
                bulk_loader.stream_sewer_overflow_events(
                    zp, kept_npdes_permits={"IL0001"}, now=NOW)
            self.assertIn("data-downloads", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
