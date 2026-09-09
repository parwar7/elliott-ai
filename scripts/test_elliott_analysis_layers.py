import unittest

import pandas as pd

from elliott_analysis_layers import (
    IMPULSE_DIVERGENCE,
    IMPULSE_HEALTHY,
    IMPULSE_WEAK_WARNING,
    add_impulse_verification,
    aggregate_wave_metrics,
    calculate_ewo,
    classify_early_leg_1,
    forecast_correction,
)


def impulse_rows(volumes, ewo, prices=(10, 8, 20, 16, 22), zero_touch=True):
    return [
        {
            "sequence_id": "test",
            "wave_label": number,
            "wave_status": "completed",
            "start_price": 0 if number == 1 else prices[number - 2],
            "end_price": prices[number - 1],
            "cumulative_volume": volumes[number - 1],
            "wave_ewo_peak": ewo[number - 1],
            "ewo_zero_touch": zero_touch if number == 4 else False,
        }
        for number in range(1, 6)
    ]


class EwoTests(unittest.TestCase):
    def test_ewo_uses_median_price_sma_5_minus_sma_35(self):
        frame = pd.DataFrame({"high": range(2, 42), "low": range(0, 40)})
        result = calculate_ewo(frame)
        self.assertTrue(pd.isna(result.loc[33, "ewo"]))
        self.assertAlmostEqual(result.loc[34, "ewo"], 15.0)

    def test_wave_aggregation_records_peak_range_zero_touch_and_volume(self):
        count = 45
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=count, freq="D"),
                "high": [20 + abs(20 - i) for i in range(count)],
                "low": [18 + abs(20 - i) for i in range(count)],
                "volume": [100] * count,
            }
        )
        rows = [{"wave_label": 1, "wave_status": "completed", "start_date": "2024-02-04", "end_date": "2024-02-14"}]
        updated = aggregate_wave_metrics(frame, rows)
        self.assertIsNotNone(updated[0]["wave_ewo_peak"])
        self.assertEqual(updated[0]["cumulative_volume"], 1100.0)
        self.assertLessEqual(updated[0]["wave_ewo_min"], updated[0]["wave_ewo_max"])


class ImpulseTests(unittest.TestCase):
    def status(self, rows):
        updated, _ = add_impulse_verification(rows)
        return updated[0]["impulse_status"]

    def test_wave_3_weakest_is_indicator_warning(self):
        self.assertEqual(
            self.status(impulse_rows([100, 60, 50, 40, 80], [10, 4, 3, 2, 8])),
            IMPULSE_WEAK_WARNING,
        )

    def test_healthy_corrections_and_zero_cross_pass(self):
        rows = impulse_rows([100, 50, 200, 70, 210], [10, 4, 20, 3, 22], prices=(10, 8, 20, 16, 19))
        self.assertEqual(self.status(rows), IMPULSE_HEALTHY)

    def test_wave_5_dual_divergence(self):
        rows = impulse_rows([100, 50, 220, 70, 180], [10, 4, 25, 3, 18])
        self.assertEqual(self.status(rows), IMPULSE_DIVERGENCE)


class CorrectionTests(unittest.TestCase):
    def test_early_wave_a_evidence_preserves_wave_w(self):
        result = classify_early_leg_1({
            "leg_1_subwaves": 3,
            "leg_1_ewo_peak": 4,
            "preceding_impulse_ewo_peak": 10,
            "effort_result_ratio": 0.8,
            "historical_50_effort_result_avg": 1.0,
            "leg_1_pierced_bollinger_2": True,
            "leg_1_pierced_keltner": False,
        })
        self.assertEqual(result["early_correction_label"], "Unresolved: Wave A versus Wave W")
        self.assertIsNone(result["confidence_score"])
        assessment = result["early_correction_assessment"]
        self.assertEqual(assessment["status"], "ambiguous")
        self.assertEqual(len(assessment["candidate_hypotheses"]), 2)
        wave_a = next(
            item for item in assessment["candidate_hypotheses"]
            if item["hypothesis_id"] == "corrective_wave_a"
        )
        self.assertEqual(len(wave_a["supporting_evidence"]), 3)

    def test_early_wave_w_evidence_does_not_win_tie(self):
        result = classify_early_leg_1({
            "leg_1_subwaves": 3,
            "leg_1_ewo_peak": 8,
            "preceding_impulse_ewo_peak": 10,
            "effort_result_ratio": 0.8,
            "historical_50_effort_result_avg": 1.0,
        })
        self.assertEqual(result["early_correction_label"], "Unresolved: Wave A versus Wave W")
        self.assertIsNone(result["early_correction_assessment"]["selected_hypothesis"])

    def test_five_subwaves_preserve_wave_1_and_zigzag_a(self):
        result = forecast_correction({"leg_1_subwaves": 5})
        self.assertEqual(result["correction_structure_label"], "Unresolved: Wave 1 versus Wave A")
        self.assertEqual(
            {item["hypothesis_id"] for item in result["correction_assessment"]["candidate_hypotheses"]},
            {"new_motive_wave_1", "zigzag_wave_a"},
        )

    def test_deep_leg_2_supports_flat_without_selecting_it(self):
        result = forecast_correction({"leg_1_subwaves": 3, "leg_2_retracement_pct": 100})
        self.assertIn("Unresolved", result["correction_structure_label"])
        flat = next(
            item for item in result["correction_assessment"]["candidate_hypotheses"]
            if item["hypothesis_id"] == "flat_wave_a"
        )
        self.assertTrue(flat["supporting_evidence"])

    def test_shallow_leg_2_supports_wxy_without_selecting_it(self):
        result = forecast_correction({"leg_1_subwaves": 3, "leg_2_retracement_pct": 60})
        self.assertIn("Unresolved", result["correction_structure_label"])
        combination = next(
            item for item in result["correction_assessment"]["candidate_hypotheses"]
            if item["hypothesis_id"] == "combination_wave_w"
        )
        self.assertTrue(combination["supporting_evidence"])

    def test_second_low_volume_connector_supports_but_does_not_force_triple_three(self):
        result = forecast_correction({
            "completed_local_structure": "W-X-Y",
            "second_shallow_pullback": True,
            "second_pullback_low_volume": True,
        })
        self.assertEqual(result["final_leg_label"], "")
        continuation = next(
            item for item in result["correction_assessment"]["candidate_hypotheses"]
            if item["hypothesis_id"] == "triple_three_continuation"
        )
        self.assertGreaterEqual(len(continuation["supporting_evidence"]), 3)


if __name__ == "__main__":
    unittest.main()
