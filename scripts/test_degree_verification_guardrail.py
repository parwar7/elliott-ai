from __future__ import annotations

from copy import deepcopy
import unittest
from typing import Any

from elliott_ai.degrees import (
    assess_degree_readiness,
    recompute_degree_hierarchy_verification,
    validate_degree_hierarchy_rules,
)
from elliott_ai.schema import validate_degree_resolution_shape


def _node(
    wave_id: str,
    parent_wave_id: str | None,
    degree: str,
    position: str,
    start_date: str,
    start_price: float,
    end_date: str,
    end_price: float,
    structure: str,
    *,
    degree_status: str = "confirmed",
    completion_status: str = "completed",
    child_structure_status: str = "not_required",
    terminal: bool = True,
    child_wave_ids: list[str] | None = None,
    timeframe: str = "daily",
    invalidation: str | None = "Explicit price invalidation supplied.",
) -> dict[str, Any]:
    return {
        "wave_id": wave_id,
        "parent_wave_id": parent_wave_id,
        "degree": degree,
        "degree_status": degree_status,
        "degree_confidence": 0.99 if degree_status == "confirmed" else 0.55,
        "wave": position,
        "sequence_position": position,
        "timeframe": timeframe,
        "direction": "up" if end_price > start_price else "down",
        "start": {"date": start_date, "price": start_price},
        "end": {"date": end_date, "price": end_price},
        "structure": structure,
        "completion_status": completion_status,
        "child_structure_status": child_structure_status,
        "terminal_at_available_resolution": terminal,
        "child_wave_ids": list(child_wave_ids or []),
        "evidence": ["M1"],
        "confirmations": ["Explicit price anchors are supplied."],
        "concerns": [],
        "invalidation": invalidation,
    }


def _response(nodes: list[dict[str, Any]], *, active_wave_ids: list[str]) -> dict[str, Any]:
    return {
        "resolution_status": "final",
        "symbol": "NASDAQ:GOOGL",
        "source_run_id": 21,
        "data_cutoff": "2026-08-07T20:00:00+00:00",
        "scale_mode": "logarithmic",
        "scope": "Deterministic hierarchy regression fixture.",
        "degree_hierarchy": nodes,
        "active_position": {
            "summary": "Fixture active position.",
            "wave_ids": active_wave_ids,
            "confirmation": "Explicit nodes are supplied.",
            "invalidation": "Below the stated origin.",
        },
        "alternate_counts": [],
        "invalidation_levels": ["140.53"],
        "unresolved_items": [],
        "confidence": {"structure": 0.99, "degree": 0.99, "active_wave": 0.99},
        "citations": [{"evidence_id": "M1", "claim": "Fixture price anchors."}],
        "risk_note": "Research fixture only.",
    }


def _genuinely_verified_parent_response() -> dict[str, Any]:
    child_ids = [f"I{position}" for position in range(1, 6)]
    parent = _node(
        "P1",
        None,
        "Primary",
        "1",
        "2026-01-01",
        10.0,
        "2026-02-14",
        30.0,
        "Impulse",
        child_structure_status="unproven",
        terminal=False,
        child_wave_ids=child_ids,
    )
    children = [
        _node("I1", "P1", "Intermediate", "1", "2026-01-01", 10.0, "2026-01-05", 15.0, "Impulse"),
        _node("I2", "P1", "Intermediate", "2", "2026-01-05", 15.0, "2026-01-08", 12.0, "Zigzag"),
        _node("I3", "P1", "Intermediate", "3", "2026-01-08", 12.0, "2026-01-20", 25.0, "Impulse"),
        _node("I4", "P1", "Intermediate", "4", "2026-01-20", 25.0, "2026-01-25", 18.0, "Zigzag"),
        _node("I5", "P1", "Intermediate", "5", "2026-01-25", 18.0, "2026-02-14", 30.0, "Impulse"),
    ]
    return _response([parent, *children], active_wave_ids=["P1"])


def _resolution_five_shaped_i3_response() -> dict[str, Any]:
    """Mirror the meaningful I3/U0 properties of immutable resolution 5."""
    p0 = _node(
        "P0",
        None,
        "Primary",
        "0",
        "2022-11-01",
        83.34,
        "2026-08-07",
        343.14,
        "Active upward impulse candidate containing proposed Intermediate waves.",
        degree_status="candidate",
        completion_status="active",
        child_structure_status="unproven",
        terminal=False,
        child_wave_ids=["I1", "I2", "I3", "I4"],
    )
    i1 = _node(
        "I1",
        "P0",
        "Intermediate",
        "1",
        "2022-11-01",
        83.34,
        "2023-02-02",
        109.74,
        "Candidate completed motive leg; required five-wave internal structure remains unproven.",
        degree_status="candidate",
        child_structure_status="unproven",
        terminal=False,
    )
    i2 = _node(
        "I2",
        "P0",
        "Intermediate",
        "2",
        "2023-02-02",
        109.74,
        "2025-04-01",
        140.53,
        "Candidate completed correction; corrective family is unresolved.",
        degree_status="candidate",
        child_structure_status="unproven",
        terminal=False,
    )
    i3 = _node(
        "I3",
        "P0",
        "Intermediate",
        "3",
        "2025-04-01",
        140.53,
        "2026-05-18",
        408.61,
        "Candidate completed third wave, with an explicit candidate Minor 1-2-3-4-5 subdivision. The same leg can be Wave C of the A-B-C alternate.",
        degree_status="candidate",
        child_structure_status="verified",
        terminal=False,
        child_wave_ids=["M31", "M32", "M33", "M34", "M35"],
        invalidation="A decline below 140.53 invalidates the proposed completed upward leg from that origin.",
    )
    i4 = _node(
        "I4",
        "P0",
        "Intermediate",
        "4",
        "2026-05-18",
        408.61,
        "2026-08-07",
        343.14,
        "Active corrective candidate; no completed corrective family is established.",
        degree_status="candidate",
        completion_status="active",
        child_structure_status="unproven",
        terminal=False,
    )
    minor_specs = [
        ("M31", "1", "2025-04-01", 140.53, "2025-06-10", 181.11, "Candidate first motive wave within I3; internal structure unproven."),
        ("M32", "2", "2025-06-10", 181.11, "2025-06-23", 162.00, "Candidate corrective second wave within I3; corrective family unproven."),
        ("M33", "3", "2025-06-23", 162.00, "2025-09-19", 256.00, "Candidate third motive wave within I3; internal structure unproven."),
        ("M34", "4", "2025-09-19", 256.00, "2025-10-10", 235.84, "Candidate corrective fourth wave within I3; corrective family unproven."),
        ("M35", "5", "2025-10-10", 235.84, "2026-05-18", 408.61, "Candidate extended fifth motive wave within I3; internal structure unproven."),
    ]
    minors = [
        _node(
            wave_id,
            "I3",
            "Minor",
            position,
            start_date,
            start_price,
            end_date,
            end_price,
            structure,
            degree_status="candidate",
            child_structure_status="unproven",
            terminal=False,
        )
        for (
            wave_id,
            position,
            start_date,
            start_price,
            end_date,
            end_price,
            structure,
        ) in minor_specs
    ]
    origin_control = {
        "wave_id": "U0",
        "parent_wave_id": None,
        "degree": "Unassigned",
        "degree_status": "unassigned",
        "degree_confidence": 0.2,
        "wave": "Origin-control history",
        "sequence_position": "0",
        "timeframe": "monthly",
        "direction": "unknown",
        "start": {"date": "2006-09-21", "price": None},
        "end": {"date": "2022-11-01", "price": 83.34},
        "structure": "Observed origin-control interval preceding the proposed Primary advance. No recursively verified larger-degree structure is assigned.",
        "completion_status": "unknown",
        "child_structure_status": "unproven",
        "terminal_at_available_resolution": False,
        "child_wave_ids": [],
        "evidence": ["M1"],
        "confirmations": ["Monthly, weekly, and daily evidence cover the interval through the 2022 low."],
        "concerns": ["No explicit recursively verified family establishes a larger-degree label."],
        "invalidation": None,
    }
    return _response([origin_control, p0, i1, i2, i3, *minors, i4], active_wave_ids=["P0"])


class DegreeVerificationGuardrailTests(unittest.TestCase):
    def test_resolution_five_shaped_i3_is_downgraded_without_mutating_input(self) -> None:
        raw = _resolution_five_shaped_i3_response()
        original = deepcopy(raw)

        normalized = recompute_degree_hierarchy_verification(raw)

        self.assertEqual(raw, original)
        self.assertEqual(
            next(node for node in raw["degree_hierarchy"] if node["wave_id"] == "I3")[
                "child_structure_status"
            ],
            "verified",
        )
        self.assertNotIn("U0", {node["wave_id"] for node in normalized["degree_hierarchy"]})
        self.assertEqual(normalized["hierarchy_metadata"][0]["metadata_id"], "U0")
        self.assertEqual(normalized["hierarchy_metadata"][0]["kind"], "origin_control")

        i3 = next(node for node in normalized["degree_hierarchy"] if node["wave_id"] == "I3")
        self.assertEqual(i3["child_structure_status"], "unproven")
        self.assertTrue(
            {
                "unsupported_parent_structure",
                "child_degree_not_confirmed",
                "child_not_structurally_verified",
            }.issubset(i3["verification_reason_codes"])
        )
        self.assertEqual(validate_degree_resolution_shape(normalized, source_run_id=21), [])

        readiness = assess_degree_readiness(
            raw,
            validation_errors=[],
            source_run={
                "validation": [],
                "evidence": {"data_reconciliation": {"overall_status": "passed"}},
            },
            impulse_verification=[],
        )
        self.assertFalse(readiness["final_report_ready"])
        self.assertFalse(readiness["model_assertions_used_for_readiness"])
        self.assertIn(
            "At least one completed parent still has unproven internal structure.",
            readiness["blockers"],
        )

    def test_genuinely_verified_parent_requires_the_complete_child_graph(self) -> None:
        normalized = recompute_degree_hierarchy_verification(
            _genuinely_verified_parent_response()
        )
        parent = next(node for node in normalized["degree_hierarchy"] if node["wave_id"] == "P1")

        self.assertEqual(parent["child_structure_status"], "verified")
        self.assertEqual(parent["verification_reason_codes"], [])
        self.assertEqual(validate_degree_resolution_shape(normalized, source_run_id=21), [])
        self.assertEqual(validate_degree_hierarchy_rules(normalized), [])

    def test_non_metadata_unassigned_node_remains_a_rejected_wave_node(self) -> None:
        response = _genuinely_verified_parent_response()
        invalid_node = _node(
            "U1",
            None,
            "Unassigned",
            "0",
            "2006-09-21",
            1.0,
            "2022-11-01",
            83.34,
            "Observed interval without a validated degree.",
            degree_status="unassigned",
            completion_status="unknown",
            child_structure_status="unproven",
            terminal=False,
            timeframe="monthly",
            invalidation=None,
        )
        invalid_node["start"]["price"] = None
        response["degree_hierarchy"].append(invalid_node)

        normalized = recompute_degree_hierarchy_verification(response)
        self.assertIn("U1", {node["wave_id"] for node in normalized["degree_hierarchy"]})
        errors = validate_degree_resolution_shape(normalized, source_run_id=21)
        self.assertTrue(
            any("outside the mandatory scale for Unassigned" in error for error in errors)
        )
        self.assertTrue(any("start.price must be numeric" in error for error in errors))

    def test_final_readiness_ignores_model_status_confidence_and_unresolved_text(self) -> None:
        response = _genuinely_verified_parent_response()
        response["resolution_status"] = "provisional"
        response["confidence"] = {"structure": 0.0, "degree": 0.0, "active_wave": 0.0}
        response["unresolved_items"] = ["Model-reported uncertainty is not a deterministic gate."]

        readiness = assess_degree_readiness(
            response,
            validation_errors=[],
            source_run={
                "validation": [],
                "evidence": {"data_reconciliation": {"overall_status": "passed"}},
            },
            impulse_verification=[{"parent_wave_id": "P1", "status": "Passed"}],
        )

        self.assertTrue(readiness["final_report_ready"])
        self.assertFalse(readiness["model_assertions_used_for_readiness"])
        self.assertFalse(
            any("Degree resolver status" in blocker for blocker in readiness["blockers"])
        )


if __name__ == "__main__":
    unittest.main()
