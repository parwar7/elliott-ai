import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NFLXBlindRecountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((ROOT / "nflx_blind_recount_2026-07-15.json").read_text(encoding="utf-8"))

    def test_full_weekly_history_is_used(self):
        weekly = self.report["data_provenance"]["weekly"]
        self.assertGreaterEqual(weekly["rows"], 1250)
        self.assertTrue(weekly["start"].startswith("2002"))

    def test_cycle_one_and_audited_internals_pass(self):
        cycle = self.report["preferred_count"]["Cycle_I_complete"]
        for key in ("rules", "Primary_1_rules", "Primary_3_rules", "Primary_5_rules"):
            self.assertTrue(cycle[key]["mandatory_pass"], key)

    def test_cycle_three_primary_one_passes(self):
        wave = self.report["preferred_count"]["Cycle_III_active"]["Primary_1_complete"]
        self.assertTrue(wave["rules"]["mandatory_pass"])
        self.assertTrue(wave["Intermediate_3_rules"]["mandatory_pass"])
        self.assertTrue(wave["Intermediate_5_rules"]["mandatory_pass"])

    def test_active_c_and_minor_five_pass_so_far(self):
        wave = self.report["preferred_count"]["Cycle_III_active"]["Primary_2_active"]["Intermediate_C_active"]
        self.assertTrue(wave["rules_so_far"]["mandatory_pass"])
        self.assertTrue(wave["minute_rules_so_far"]["mandatory_pass"])

    def test_target_confluence(self):
        targets = self.report["targets"]
        self.assertLess(abs(targets["Primary_1_61_8_retracement"] - targets["Minor_5_equality_wave_1"]), 1.0)
        self.assertLess(abs(targets["Intermediate_C_0_786_of_A"] - targets["Primary_1_61_8_retracement"]), 1.5)


if __name__ == "__main__":
    unittest.main()
