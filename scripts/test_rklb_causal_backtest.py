import json
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RklbCausalBacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((ROOT / "rklb_causal_backtest_2026-07-15.json").read_text(encoding="utf-8"))

    def test_no_confirmation_precedes_its_pivot(self):
        for checkpoint in self.report["causal_checkpoints"]:
            if checkpoint["confirmed"]:
                self.assertGreater(
                    datetime.fromisoformat(checkpoint["confirmation_date"]),
                    datetime.fromisoformat(checkpoint["pivot_date"]),
                )

    def test_all_completed_sequences_pass_hard_rules(self):
        for sequence in self.report["completed_sequences"].values():
            self.assertTrue(sequence["verification"]["hard_price_rules"]["mandatory_pass"])

    def test_current_low_is_not_mislabeled_as_higher_degree_confirmation(self):
        verdict = self.report["verdict"]["Intermediate_4_low_at_75_45"]
        self.assertIn("Unresolved", verdict)
        self.assertIn("2-hour", verdict)

    def test_hard_invalidation_is_intermediate_one_high(self):
        self.assertIn("$33.34", self.report["verdict"]["hard_invalidation"])


if __name__ == "__main__":
    unittest.main()
