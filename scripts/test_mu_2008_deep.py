import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recount_mu_2008_deep as recount


class MuDeep2008Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = recount.build_report()

    def test_all_proposed_impulses_pass_hard_rules(self):
        p3 = self.report["preferred_high_degree_count"]["Cycle_I"]["Primary_3"]
        self.assertTrue(p3["rules"]["mandatory_pass"])
        self.assertTrue(self.report["primary_3_detail"]["Intermediate_3"]["rules"]["mandatory_pass"])
        self.assertTrue(self.report["primary_3_detail"]["Intermediate_5"]["rules"]["mandatory_pass"])
        self.assertTrue(self.report["lower_degree_detail"]["Intermediate_5_Minor_3"]["rules"]["mandatory_pass"])
        self.assertTrue(self.report["lower_degree_detail"]["Intermediate_5_Minor_5"]["rules"]["mandatory_pass"])

    def test_log_scale_drives_high_degree_proportions(self):
        p3 = self.report["preferred_high_degree_count"]["Cycle_I"]["Primary_3"]
        self.assertAlmostEqual(p3["log_fibonacci"]["wave_5_to_wave_1"], 1.6709, places=4)
        self.assertIn("Logarithmic", self.report["scale_decision"]["2008_present"])

    def test_2022_is_intermediate_four_in_preferred_count(self):
        i4 = self.report["primary_3_detail"]["Intermediate_4"]
        self.assertEqual(i4["path"], [96.24, 47.52])
        self.assertAlmostEqual(i4["log_retracement_pct"], 29.89, places=2)

    def test_current_four_hour_leg_passes_bearish_impulse_rules(self):
        current = self.report["current_four_hour"]
        self.assertEqual(current["rules"]["direction"], "down")
        self.assertTrue(current["rules"]["mandatory_pass"])
        self.assertIn("No reversal is confirmed", current["live_status"])

    def test_higher_timeframe_top_is_not_overclaimed(self):
        verdict = self.report["primary_3_detail"]["Intermediate_5"]["higher_timeframe_verdict"]
        self.assertIn("does not provide strong", verdict)
        self.assertEqual(self.report["verdict"]["confidence"]["Primary_3_top"], "Moderate")

    def test_four_hour_data_was_loaded(self):
        data = self.report["data"]["four_hour"]
        self.assertEqual(data["rows"], 333)
        self.assertEqual(data["end"], "2026-07-16T12:00:00")


if __name__ == "__main__":
    unittest.main()
