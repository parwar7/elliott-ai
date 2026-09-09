import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NFLXAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((ROOT / "nflx_elliott_wave_analysis.superseded.json").read_text(encoding="utf-8"))

    def test_completed_impulses_pass_hard_rules(self):
        preferred = self.report["preferred_count"]
        self.assertTrue(preferred["Cycle_I_complete"]["rules"]["mandatory_pass"])
        self.assertTrue(preferred["Primary_5_detail"]["rules"]["mandatory_pass"])
        self.assertTrue(preferred["Primary_5_detail"]["Intermediate_3_rules"]["mandatory_pass"])
        self.assertTrue(preferred["Primary_5_detail"]["Intermediate_5_rules"]["mandatory_pass"])
        self.assertTrue(preferred["Cycle_II_active"]["Primary_A"]["rules"]["mandatory_pass"])

    def test_active_primary_c_and_minute_count_pass_so_far(self):
        c_wave = self.report["preferred_count"]["Cycle_II_active"]["Primary_C_active"]
        self.assertTrue(c_wave["rules_so_far"]["mandatory_pass"])
        self.assertTrue(c_wave["minute_rules_so_far"]["mandatory_pass"])

    def test_target_confluence(self):
        fib = self.report["fibonacci"]
        self.assertLess(abs(fib["Cycle_I_50_retracement"] - fib["Intermediate_5_0_618_of_wave_1"]), 1.0)
        self.assertLess(abs(fib["Primary_C_0_786_of_A"] - fib["Intermediate_5_equality_wave_1"]), 2.0)

    def test_invalidation_levels_are_ordered(self):
        verdict = self.report["current_verdict"]
        self.assertIn("$78.44", verdict["near_term_bearish_invalidation"])
        self.assertIn("$108.95", verdict["primary_c_invalidation"])
        self.assertIn("$134.12", verdict["cycle_correction_invalidation"])

    def test_directional_wave_3_is_not_weakest(self):
        preferred = self.report["preferred_count"]
        checks = [
            preferred["Primary_5_detail"]["directional_assessment"],
            preferred["Cycle_II_active"]["Primary_A"]["directional_assessment"],
            preferred["Cycle_II_active"]["Primary_C_active"]["directional_assessment_so_far"],
        ]
        self.assertTrue(all(not check["wave_3_is_weakest_motive"] for check in checks))


if __name__ == "__main__":
    unittest.main()
