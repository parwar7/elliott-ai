import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_rklb_high_degree as audit


class RklbHighDegreeAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = audit.build_report()

    def test_preferred_impulses_pass_mandatory_rules(self):
        preferred = self.report["preferred_count"]
        for name in ("Intermediate_1_internal", "Intermediate_3_internal"):
            rules = preferred[name]["rules"]
            self.assertTrue(rules["wave_3_not_shortest"])
            self.assertTrue(rules["wave_4_no_overlap"])

    def test_parent_log_waves_are_near_equal(self):
        ratio = self.report["preferred_count"]["parent_log_comparison"]["ratio_I3_to_I1"]
        self.assertGreater(ratio, 0.9)
        self.assertLess(ratio, 1.1)

    def test_normalized_ewo_divergence_is_detected(self):
        verdict = self.report["preferred_count"]["Intermediate_3_internal"]["indicator_verdict"]
        self.assertTrue(verdict["normalized_ewo_divergence_5_vs_3"])

    def test_current_c_is_price_valid_five(self):
        rules = self.report["current_correction"]["C_internal_rules"]
        self.assertTrue(rules["wave_3_not_shortest"])
        self.assertTrue(rules["wave_4_no_overlap"])

    def test_hard_invalidation_remains_below_intermediate_one(self):
        text = self.report["decision_levels"]["hard_invalidation"]
        self.assertIn("$33.34", text)


if __name__ == "__main__":
    unittest.main()
