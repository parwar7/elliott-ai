import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RklbBlindRecountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((ROOT / "rklb_blind_recount_2026-07-15.json").read_text(encoding="utf-8"))

    def test_both_completed_impulses_pass_hard_rules(self):
        structures = self.report["completed_structures"]
        self.assertTrue(structures["first_impulse"]["rules"]["mandatory_pass"])
        self.assertTrue(structures["second_impulse"]["regular_session_rules"]["mandatory_pass"])
        self.assertTrue(structures["second_impulse"]["extended_hours_rules"]["mandatory_pass"])

    def test_primary_two_has_zigzag_fibonacci_proportions(self):
        correction = self.report["completed_structures"]["primary_2_zigzag"]
        self.assertGreaterEqual(correction["B_retracement_of_A_pct"], 38.2)
        self.assertLessEqual(correction["B_retracement_of_A_pct"], 50.0)
        self.assertAlmostEqual(correction["C_to_A_ratio"], 0.618, delta=0.05)

    def test_second_impulse_has_strict_terminal_divergence(self):
        verification = self.report["completed_structures"]["second_impulse"]["verification"]
        self.assertTrue(verification["wave_3_strength"])
        self.assertTrue(verification["correction_volume_dry_up"])
        self.assertTrue(verification["wave_4_ewo_zero_cross"])
        self.assertTrue(verification["strict_wave_5_divergence"])

    def test_current_hard_invalidation_is_primary_three_start(self):
        self.assertIn("$56.13", self.report["verdict"]["hard_invalidation"])


if __name__ == "__main__":
    unittest.main()
