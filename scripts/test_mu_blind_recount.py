import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recount_mu_blind as recount


class MuBlindRecountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = recount.build_report()

    def test_first_supercycle_impulse_passes(self):
        rules = self.report["highest_degree_context"]["Supercycle_I"]["rules"]
        self.assertTrue(rules["mandatory_pass"])

    def test_nested_minute_three_passes(self):
        rules = self.report["preferred_count"]["Cycle_III"]["Primary_3"]["Intermediate_3"]["Minor_3"]["Minute_3"]["rules"]
        self.assertTrue(rules["mandatory_pass"])

    def test_primary_one_subdivision_passes(self):
        primary_one = self.report["preferred_count"]["Cycle_III"]["Primary_1"]
        self.assertTrue(primary_one["rules"]["mandatory_pass"])
        self.assertEqual(len(primary_one["internal_points"]), 6)

    def test_primary_two_is_not_overclaimed_as_zigzag(self):
        primary_two = self.report["preferred_count"]["Cycle_III"]["Primary_2"]
        self.assertIn("Complex correction candidate", primary_two["classification"])
        self.assertIn("unresolved", primary_two["status"])

    def test_cycle_one_uncertainty_and_alternate_are_preserved(self):
        cycle_one = self.report["highest_degree_context"]["Supercycle_III"]["Cycle_I"]
        self.assertIn("Low-moderate", cycle_one["confidence"])
        self.assertIn("Nested", cycle_one["alternate"]["classification"])

    def test_current_a_candidate_passes(self):
        rules = self.report["current_correction"]["rules"]
        self.assertEqual(rules["direction"], "down")
        self.assertTrue(rules["mandatory_pass"])

    def test_all_nested_second_waves_hold_origins(self):
        holds = self.report["preferred_count"]["nested_one_two_evidence"]["origin_holds"]
        self.assertTrue(all(holds.values()))

    def test_primary_four_is_preserved_as_alternate(self):
        names = [item["name"] for item in self.report["alternates"]]
        self.assertTrue(any("Primary 3 completed" in name for name in names))

    def test_extended_fifth_is_not_preferred(self):
        self.assertIn("degree scenario unresolved", self.report["preferred_count"]["name"])

    def test_method_does_not_force_nesting(self):
        self.assertTrue(self.report["analysis_policy"]["no_forced_nesting"])
        assessment = self.report["preferred_count"]["pattern_first_assessment"]
        self.assertEqual(len(assessment["degree_scenarios"]), 3)

    def test_momentum_and_volume_are_kept_separate(self):
        evidence = self.report["preferred_count"]["pattern_first_assessment"]["momentum_volume"]
        self.assertIn("volume confirmation is mixed", evidence["interpretation"])

    def test_mixed_indicators_are_not_overclaimed(self):
        verdict = self.report["preferred_count"]["Cycle_III"]["Primary_3"]["Intermediate_3"]["Minor_3"]["Minute_3"]["indicator_verdict"]
        self.assertTrue(verdict["normalized_ewo_wave_3_above_wave_1"])
        self.assertFalse(verdict["rsi_wave_3_above_wave_1"])
        self.assertFalse(verdict["correction_volume_dry_up"])
        self.assertTrue(verdict["wave_5_rsi_ewo_divergence"])

    def test_live_low_is_not_called_confirmed(self):
        self.assertIn("unconfirmed", self.report["current_correction"]["status"])
        self.assertEqual(self.report["current_correction"]["live_observation"]["low"], 866.12)


if __name__ == "__main__":
    unittest.main()
