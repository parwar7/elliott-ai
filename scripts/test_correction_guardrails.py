from __future__ import annotations

import json
import unittest
from typing import Any

from elliott_ai.degrees import validate_degree_hierarchy_rules
from elliott_ai.schema import (
    validate_degree_resolution_shape,
)
from elliott_ai.wave_metrics import (
    calculate_fibonacci_and_duration,
    calculate_fibonacci_reference_levels,
)
from elliott_analysis_layers import classify_early_leg_1, forecast_correction
from scripts.test_elliott_ai import _degree_response


def _node(
    wave_id: str,
    position: str,
    structure: str,
    *,
    parent: str | None = "P",
    children: list[str] | None = None,
    start_price: float = 100.0,
    end_price: float = 90.0,
) -> dict[str, Any]:
    child_ids = children or []
    return {
        "wave_id": wave_id,
        "parent_wave_id": parent,
        "degree": "Intermediate" if parent is None else "Minor",
        "wave": position,
        "sequence_position": position,
        "direction": "up" if end_price > start_price else "down",
        "start": {"date": "2026-01-01", "price": start_price},
        "end": {"date": "2026-01-31", "price": end_price},
        "structure": structure,
        "child_structure_status": "verified" if child_ids else "not_required",
        "child_wave_ids": child_ids,
    }


def _correction_response(
    structure: str, child_specs: list[tuple[str, str]]
) -> dict[str, Any]:
    child_ids = [f"C{index}" for index in range(1, len(child_specs) + 1)]
    parent = _node(
        "P",
        "2",
        structure,
        parent=None,
        children=child_ids,
        start_price=110.0,
        end_price=80.0,
    )
    children = [
        _node(wave_id, position, family)
        for wave_id, (position, family) in zip(child_ids, child_specs)
    ]
    return {"degree_hierarchy": [parent, *children]}


def _combination_metrics_response(*, legacy_second_x: bool = False) -> dict[str, Any]:
    positions = ["W", "X", "Y", "X" if legacy_second_x else "X2", "Z"]
    prices = [(100, 90), (90, 94), (94, 82), (82, 87), (87, 72)]
    child_ids = [f"L{index}" for index in range(1, 6)]
    parent = _node(
        "P",
        "2",
        "Triple Three",
        parent=None,
        children=child_ids,
        start_price=100,
        end_price=72,
    )
    children = [
        _node(
            wave_id,
            position,
            "Zigzag" if position not in {"X", "X2"} else "Flat",
            start_price=start,
            end_price=end,
        )
        for wave_id, position, (start, end) in zip(child_ids, positions, prices)
    ]
    return {"degree_hierarchy": [parent, *children]}


class CorrectionFamilyValidationTests(unittest.TestCase):
    def assert_valid(self, response: dict[str, Any]) -> None:
        self.assertEqual(validate_degree_hierarchy_rules(response), [])

    def test_valid_zigzag_535(self) -> None:
        self.assert_valid(
            _correction_response(
                "Zigzag",
                [("A", "Impulse"), ("B", "Flat"), ("C", "Ending Diagonal")],
            )
        )

    def test_invalid_zigzag_with_corrective_a(self) -> None:
        errors = validate_degree_hierarchy_rules(
            _correction_response(
                "Zigzag",
                [("A", "Double Three"), ("B", "Flat"), ("C", "Impulse")],
            )
        )
        self.assertTrue(any("requires a motive structure" in error and "at A" in error for error in errors))

    def test_valid_flat_335(self) -> None:
        self.assert_valid(
            _correction_response(
                "Flat",
                [("A", "Zigzag"), ("B", "Double Three"), ("C", "Impulse")],
            )
        )

    def test_invalid_flat_with_five_wave_a(self) -> None:
        errors = validate_degree_hierarchy_rules(
            _correction_response(
                "Flat",
                [("A", "Impulse"), ("B", "Zigzag"), ("C", "Ending Diagonal")],
            )
        )
        self.assertTrue(any("requires a corrective structure" in error and "at A" in error for error in errors))

    def test_valid_triangle_with_five_corrective_legs(self) -> None:
        self.assert_valid(
            _correction_response(
                "Triangle",
                [
                    ("A", "Zigzag"),
                    ("B", "Flat"),
                    ("C", "Double Three"),
                    ("D", "Zigzag"),
                    ("E", "Flat"),
                ],
            )
        )

    def test_invalid_triangle_containing_motive_leg(self) -> None:
        errors = validate_degree_hierarchy_rules(
            _correction_response(
                "Triangle",
                [
                    ("A", "Zigzag"),
                    ("B", "Flat"),
                    ("C", "Impulse"),
                    ("D", "Zigzag"),
                    ("E", "Flat"),
                ],
            )
        )
        self.assertTrue(any("requires a corrective structure" in error and "at C" in error for error in errors))

    def test_valid_wxy(self) -> None:
        self.assert_valid(
            _correction_response(
                "Double Three W-X-Y",
                [("W", "Zigzag"), ("X", "Flat"), ("Y", "Double Three")],
            )
        )

    def test_valid_wxy_x2_z(self) -> None:
        self.assert_valid(
            _correction_response(
                "Triple Three W-X-Y-X2-Z",
                [
                    ("W", "Zigzag"),
                    ("X", "Flat"),
                    ("Y", "Double Three"),
                    ("X2", "Zigzag"),
                    ("Z", "Flat"),
                ],
            )
        )

    def test_nested_wxy_can_be_larger_w(self) -> None:
        inner_ids = ["IW", "IX", "IY"]
        outer = _node(
            "P",
            "2",
            "Double Three",
            parent=None,
            children=["OW", "OX", "OY"],
        )
        outer_w = _node("OW", "W", "Double Three", children=inner_ids)
        outer_x = _node("OX", "X", "Flat")
        outer_y = _node("OY", "Y", "Zigzag")
        inner = [
            _node("IW", "W", "Zigzag", parent="OW"),
            _node("IX", "X", "Flat", parent="OW"),
            _node("IY", "Y", "Zigzag", parent="OW"),
        ]
        self.assert_valid({"degree_hierarchy": [outer, outer_w, outer_x, outer_y, *inner]})

    def test_nested_wxy_can_be_larger_a(self) -> None:
        inner_ids = ["IW", "IX", "IY"]
        outer = _node(
            "P",
            "2",
            "Flat",
            parent=None,
            children=["OA", "OB", "OC"],
        )
        outer_a = _node("OA", "A", "Double Three", children=inner_ids)
        outer_b = _node("OB", "B", "Zigzag")
        outer_c = _node("OC", "C", "Impulse")
        inner = [
            _node("IW", "W", "Zigzag", parent="OA"),
            _node("IX", "X", "Flat", parent="OA"),
            _node("IY", "Y", "Zigzag", parent="OA"),
        ]
        self.assert_valid({"degree_hierarchy": [outer, outer_a, outer_b, outer_c, *inner]})


class X2CompatibilityTests(unittest.TestCase):
    def test_metrics_preserve_separate_x_and_x2(self) -> None:
        response = _combination_metrics_response()
        metrics = calculate_fibonacci_and_duration(response)[0]
        self.assertEqual(metrics["combination_variant"], "triple_three")
        self.assertEqual(metrics["price_lengths"]["X"], 4.0)
        self.assertEqual(metrics["price_lengths"]["X2"], 5.0)
        self.assertIn("wave_X2_retracement_of_Y", metrics["ratios"])
        references = calculate_fibonacci_reference_levels(response)[0]["references"]
        self.assertTrue(any(item["name"] == "Wave Z extension from X2 using Y" for item in references))

    def test_legacy_second_x_is_read_as_x2_without_overwriting_first_x(self) -> None:
        response = _combination_metrics_response(legacy_second_x=True)
        nodes = {node["wave_id"]: node for node in response["degree_hierarchy"]}
        nodes["L2"]["start"]["date"] = "2026-01-05"
        nodes["L4"]["start"]["date"] = "2026-01-20"
        nodes["P"]["child_wave_ids"] = ["L1", "L4", "L3", "L2", "L5"]
        self.assertEqual(validate_degree_hierarchy_rules(response), [])
        children = response["degree_hierarchy"][1:]
        self.assertEqual([child["sequence_position"] for child in children], ["W", "X", "Y", "X", "Z"])
        metrics = calculate_fibonacci_and_duration(response)[0]
        self.assertEqual(metrics["price_lengths"]["X"], 4.0)
        self.assertEqual(metrics["price_lengths"]["X2"], 5.0)

    def test_existing_x_only_schema_remains_readable(self) -> None:
        response = json.loads(json.dumps(_degree_response(7)))
        self.assertEqual(validate_degree_resolution_shape(response, source_run_id=7), [])

    def test_schema_accepts_distinct_x2_position(self) -> None:
        response = _degree_response(7)
        response["degree_hierarchy"][1]["sequence_position"] = "X2"
        self.assertEqual(validate_degree_resolution_shape(response, source_run_id=7), [])


class AmbiguityGuardrailTests(unittest.TestCase):
    def test_initial_five_preserves_wave_1_and_wave_a(self) -> None:
        result = forecast_correction({"leg_1_subwaves": 5})
        assessment = result["correction_assessment"]
        self.assertEqual(assessment["status"], "ambiguous")
        self.assertIsNone(assessment["selected_hypothesis"])
        self.assertEqual(
            {item["hypothesis_id"] for item in assessment["candidate_hypotheses"]},
            {"new_motive_wave_1", "zigzag_wave_a"},
        )

    def test_completed_wxy_preserves_complete_larger_w_and_larger_a(self) -> None:
        result = forecast_correction({"completed_local_structure": "W-X-Y"})
        candidate_ids = {
            item["hypothesis_id"]
            for item in result["correction_assessment"]["candidate_hypotheses"]
        }
        self.assertTrue(
            {"correction_complete", "larger_wave_w", "larger_wave_a"}.issubset(candidate_ids)
        )

    def test_apparent_y_preserves_possible_x2_z_continuation(self) -> None:
        result = forecast_correction({"completed_local_structure": "W-X-Y"})
        continuation = next(
            item
            for item in result["correction_assessment"]["candidate_hypotheses"]
            if item["hypothesis_id"] == "triple_three_continuation"
        )
        self.assertIn("X2-Z", continuation["label"])
        self.assertTrue(continuation["evidence_needed_for_confirmation"])

    def test_alternative_is_removed_only_after_structural_invalidation(self) -> None:
        unresolved = forecast_correction({"leg_1_subwaves": 5})["correction_assessment"]
        self.assertEqual(len(unresolved["candidate_hypotheses"]), 2)
        resolved = forecast_correction(
            {
                "leg_1_subwaves": 5,
                "hypothesis_invalidations": [
                    {
                        "hypothesis_id": "zigzag_wave_a",
                        "basis": "hard_rule",
                        "reason": "The required Wave B is itself motive, not corrective.",
                    }
                ],
            }
        )["correction_assessment"]
        self.assertEqual(resolved["status"], "single_structural_candidate")
        self.assertEqual(resolved["selected_hypothesis"]["hypothesis_id"], "new_motive_wave_1")
        self.assertEqual(len(resolved["structural_invalidations"]), 1)

    def test_explicit_price_invalidation_removes_candidate(self) -> None:
        assessment = forecast_correction(
            {
                "leg_1_subwaves": 5,
                "explicit_invalidation_evidence": [
                    {
                        "hypothesis_id": "new_motive_wave_1",
                        "basis": "explicit_invalidation_level",
                        "reason": "Price moved through the Wave 1 origin.",
                    }
                ],
            }
        )["correction_assessment"]
        self.assertEqual(assessment["status"], "single_structural_candidate")
        self.assertEqual(assessment["selected_hypothesis"]["hypothesis_id"], "zigzag_wave_a")

    def test_rsi_volume_ewo_and_macd_never_hard_invalidate(self) -> None:
        for indicator in ("rsi", "volume", "ewo", "macd"):
            with self.subTest(indicator=indicator):
                assessment = forecast_correction(
                    {
                        "leg_1_subwaves": 5,
                        "hypothesis_invalidations": [
                            {
                                "hypothesis_id": "zigzag_wave_a",
                                "basis": indicator,
                                "reason": f"{indicator.upper()} disagrees with Wave A.",
                            }
                        ],
                    }
                )["correction_assessment"]
                candidate_ids = {
                    item["hypothesis_id"] for item in assessment["candidate_hypotheses"]
                }
                self.assertIn("zigzag_wave_a", candidate_ids)
                self.assertEqual(assessment["structural_invalidations"], [])
                self.assertEqual(len(assessment["ignored_nonstructural_invalidations"]), 1)

    def test_early_classifier_returns_required_evidence_categories(self) -> None:
        result = classify_early_leg_1({"leg_1_subwaves": 3})
        assessment = result["early_correction_assessment"]
        self.assertEqual(assessment["status"], "ambiguous")
        self.assertIsNone(result["confidence_score"])
        for candidate in assessment["candidate_hypotheses"]:
            for field in (
                "supporting_evidence",
                "contradictory_evidence",
                "unavailable_or_incomparable_evidence",
                "evidence_needed_for_confirmation",
            ):
                self.assertIn(field, candidate)
        self.assertIn("structural_invalidations", assessment)


if __name__ == "__main__":
    unittest.main()
