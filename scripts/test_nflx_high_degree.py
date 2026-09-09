import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NFLXHighDegreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((ROOT / "nflx_high_degree_reaudit_2026-07-15.json").read_text(encoding="utf-8"))

    def test_full_history_and_preferred_hard_rules(self):
        self.assertGreaterEqual(self.report["data_provenance"]["weekly"]["rows"], 1250)
        self.assertTrue(self.report["candidate_comparison"]["preferred_2025_cycle_top"]["hard_rules"]["mandatory_pass"])

    def test_primary_one_three_five_internals(self):
        cycle = self.report["preferred_count"]["Cycle_I_complete"]
        checks = [
            cycle["Primary_1"]["rules"],
            cycle["Primary_1"]["Intermediate_3_rules"],
            cycle["Primary_3"]["rules"],
            cycle["Primary_3"]["Intermediate_3_rules"],
            cycle["Primary_3"]["Intermediate_5_rules"],
            cycle["Primary_5"]["rules"],
        ]
        self.assertTrue(all(check["mandatory_pass"] for check in checks))

    def test_log_scale_favors_nonextended_primary_five(self):
        comparison = self.report["candidate_comparison"]
        preferred = comparison["preferred_2025_cycle_top"]
        alternate = comparison["alternate_2021_cycle_top"]
        self.assertLess(abs(preferred["wave_3_to_wave_1_log_ratio"] - 1), 0.02)
        self.assertLess(preferred["wave_5_to_wave_3_log_ratio"], 0.6)
        self.assertGreater(alternate["wave_5_to_wave_3_log_ratio"], 1.25)

    def test_terminal_rsi_and_ewo_divergence(self):
        stats = self.report["preferred_count"]["terminal_confirmation"]["wave_statistics"]
        self.assertLess(stats["Primary_5"]["maximum_rsi"], stats["Primary_3"]["maximum_rsi"])
        self.assertLess(stats["Primary_5"]["maximum_directional_ewo_pct"], stats["Primary_3"]["maximum_directional_ewo_pct"])

    def test_normalized_volume_is_not_overclaimed(self):
        result = self.report["preferred_count"]["terminal_confirmation"]["volume_result"]
        self.assertIn("does not provide strict divergence", result)

    def test_active_decline_passes_so_far(self):
        active = self.report["preferred_count"]["Cycle_II_active"]["Primary_C_active"]
        self.assertTrue(active["rules_so_far"]["mandatory_pass"])
        self.assertTrue(active["minute_rules_so_far"]["mandatory_pass"])


if __name__ == "__main__":
    unittest.main()
