"""Pin viewer copy that's been a documented source of confusion, so
edits don't quietly drift back to the misleading wording.
"""

import unittest
from pathlib import Path


VIEWER = (Path(__file__).resolve().parent.parent
          / "chemtreat_water_leads_viewer" / "index.html")


class TestViewerCopy(unittest.TestCase):

    def setUp(self):
        self.html = VIEWER.read_text()

    def test_coverage_card_clarifies_facility_only_score(self):
        """The Run Health "Drill-down coverage" card must spell out that
        its denominator is the *facility-only* ≥threshold set, not the
        final post-event-rescore one the Inventory tile shows.

        Without this clarification, users see e.g. 297 in Run Health and
        ~286 in the Inventory at the same threshold and think one of the
        numbers is wrong — the gap is leads that scored ≥threshold on
        facility flags but were demoted below it after event-aware
        re-scoring (e.g. all-resolved -30). See commit log."""
        self.assertIn("facility-only", self.html,
                      "Coverage card must call out that the score is "
                      "facility-only, not the final re-scored value")
        self.assertIn("Event-aware re-scoring", self.html,
                      "Coverage card must warn that re-scoring can shift "
                      "leads out of the >=threshold bucket the Inventory shows")

    def test_coverage_bar_visual_wired(self):
        """The stacked horizontal bar (with_events / no_data / lookup_failed)
        is the at-a-glance visual on the coverage card. Keep it wired."""
        self.assertIn("function coverageBar(", self.html)
        for cls in ("cov-with", "cov-nodata", "cov-failed"):
            self.assertIn(cls, self.html, f"missing coverage-bar class {cls}")


if __name__ == "__main__":
    unittest.main()
