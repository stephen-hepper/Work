"""--no-events must skip ALL event loading and ALL API drill-down."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chemtreat_water_leads import bulk_loader
from tests._fixtures import make_exporter_zip


class TestNoEventsFlag(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_no_events_makes_zero_event_calls(self):
        rows = [{
            "REGISTRY_ID": "110000000001",
            "FAC_NAME": "Acme Chemical",
            "FAC_STATE": "TX",
            "FAC_NAICS_CODES": "325",
            "NPDES_IDS": "TX0000001",
            "CWA_SNC_FLAG": "Y",
            "CWA_COMPLIANCE_STATUS": "Significant Violator",
        }]
        exporter_zip = make_exporter_zip(self.tmp_path, rows)

        out_dir = self.tmp_path / "out"
        db_path = self.tmp_path / "snap.sqlite"
        cache_dir = self.tmp_path / "cache"

        # Pretend the cache miss path would download — but assert it
        # only ever fetches the exporter, never the event zips. The
        # `max_age_days` kwarg was added when the sewer-overflow daily
        # refresh landed; accept it so the signature matches the real
        # _download_cached even though we don't use it here.
        def fake_download_cached(url, cache_dir_arg, name, max_age_days=None):
            if name == "echo_exporter":
                return exporter_zip
            raise AssertionError(
                f"_download_cached called for '{name}' with --no-events; "
                "expected only 'echo_exporter'."
            )

        with patch.object(bulk_loader, "_download_cached",
                          side_effect=fake_download_cached) as dl_mock, \
             patch.object(bulk_loader, "_drill_cwa") as cwa_mock, \
             patch.object(bulk_loader, "_drill_sdwa") as sdwa_mock, \
             patch.object(bulk_loader, "stream_npdes_violations") as npdes_mock, \
             patch.object(bulk_loader, "stream_sdwa_violations") as sdwa_stream_mock, \
             patch.object(bulk_loader, "stream_permit_limits") as limits_mock, \
             patch.object(bulk_loader, "stream_attains_linkage") as attains_mock, \
             patch.object(bulk_loader, "stream_dmr_exceedances") as dmr_mock, \
             patch.object(bulk_loader, "stream_sewer_overflow_events") as sewer_mock, \
             patch.object(bulk_loader, "stream_collection_system_permits") as cs_mock, \
             patch.object(bulk_loader, "stream_cso_inventory") as cso_mock:
            bulk_loader.run_bulk(
                out_dir=out_dir,
                db_path=db_path,
                cache_dir=cache_dir,
                states=["TX"],
                include_events=False,
            )

        # Exactly one download (the exporter). The fake_download_cached
        # AssertionError above already enforces this for the six
        # known event/signal feeds, but pinning the count explicitly
        # guards against accidental future BULK_URLS entries.
        self.assertEqual(dl_mock.call_count, 1)
        self.assertEqual(dl_mock.call_args.args[2], "echo_exporter")

        # No API drill, no bulk-event streaming, no pre-violation
        # signal streaming, no sewer-overflow / CSS streaming. The
        # last three were added in the CSO/SSO integration — pinned
        # here so the `--no-events` "zero downloads, fully offline"
        # contract still holds end-to-end.
        self.assertEqual(cwa_mock.call_count, 0)
        self.assertEqual(sdwa_mock.call_count, 0)
        self.assertEqual(npdes_mock.call_count, 0)
        self.assertEqual(sdwa_stream_mock.call_count, 0)
        self.assertEqual(limits_mock.call_count, 0,
            msg="stream_permit_limits called under --no-events")
        self.assertEqual(attains_mock.call_count, 0,
            msg="stream_attains_linkage called under --no-events")
        self.assertEqual(dmr_mock.call_count, 0,
            msg="stream_dmr_exceedances called under --no-events")
        self.assertEqual(sewer_mock.call_count, 0,
            msg="stream_sewer_overflow_events called under --no-events")
        self.assertEqual(cs_mock.call_count, 0,
            msg="stream_collection_system_permits called under --no-events")
        self.assertEqual(cso_mock.call_count, 0,
            msg="stream_cso_inventory called under --no-events")

        # The facility row still landed in the DB (persistence is
        # independent of event loading).
        self.assertTrue(db_path.exists())


if __name__ == "__main__":
    unittest.main()
