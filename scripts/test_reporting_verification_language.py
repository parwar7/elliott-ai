from __future__ import annotations

import unittest

from elliott_ai.reporting import render_degree_report


def _node(*, status: str, structure: str) -> dict[str, object]:
    return {
        "wave_id": "I3",
        "parent_wave_id": None,
        "degree": "Intermediate",
        "wave": "3",
        "degree_status": "candidate" if status != "verified" else "confirmed",
        "degree_confidence": 0.8,
        "completion_status": "completed",
        "child_structure_status": "unproven" if status != "verified" else "verified",
        "subdivision_verification_status": status,
        "verification_reason_codes": ["terminal_degree_not_reached"] if status != "verified" else [],
        "start": {"date": "2025-01-01", "price": 100.0},
        "end": {"date": "2025-02-01", "price": 180.0},
        "structure": structure,
        "child_wave_ids": [],
        "confirmations": ["Model says this is a verified impulse."],
        "concerns": ["Model says verification is complete."],
        "invalidation": "Model verified the 100.00 invalidation.",
    }


def _report(node: dict[str, object], alternate: dict[str, object] | None = None) -> str:
    return render_degree_report(
        {
            "id": 21,
            "symbol": "NASDAQ:GOOGL",
            "evidence": {"context_data": []},
        },
        {
            "id": 7,
            "response": {
                "symbol": "NASDAQ:GOOGL",
                "data_cutoff": "2026-08-07T20:00:00+00:00",
                "scale_mode": "logarithmic",
                "scope": "Fixture report.",
                "confidence": {"structure": 0.5, "degree": 0.5, "active_wave": 0.5},
                "degree_hierarchy": [node],
                "active_position": {},
                "alternate_counts": [alternate] if alternate else [],
                "invalidation_levels": [],
                "unresolved_items": [],
            },
            "readiness": {"final_report_ready": False},
        },
    )


class ReportingVerificationLanguageTests(unittest.TestCase):
    def test_unproven_node_omits_raw_model_verification_language(self) -> None:
        report = _report(
            _node(
                status="unproven",
                structure="Model says this is a verified impulse with complete proof.",
            )
        )

        self.assertIn("Candidate; deterministic subdivision remains unproven.", report)
        self.assertIn("Deterministic structure state: `unproven`.", report)
        self.assertNotIn("Model says this is a verified impulse", report)
        self.assertNotIn("Model says verification is complete", report)

    def test_verified_node_can_use_its_structured_family_language(self) -> None:
        report = _report(
            _node(
                status="verified",
                structure="Deterministically verified impulse.",
            )
        )

        self.assertIn("Deterministically verified impulse.", report)
        self.assertIn("Model says this is a verified impulse.", report)

    def test_recursive_proof_state_overrides_legacy_single_graph_state(self) -> None:
        node = _node(
            status="verified",
            structure="Model says this is a verified impulse with complete proof.",
        )
        node["recursive_multi_timeframe_proof_status"] = "unproven"

        report = _report(node)

        self.assertIn("Candidate; deterministic subdivision remains unproven.", report)
        self.assertNotIn("Model says this is a verified impulse", report)

    def test_unproven_alternate_does_not_promote_raw_model_prose(self) -> None:
        report = _report(
            _node(status="unproven", structure="Candidate motive leg."),
            {
                "description": "Model says this alternate is verified.",
                "status": "candidate",
                "trigger": "Model says verified trigger.",
                "invalidation": "Model says verified invalidation.",
                "supporting_evidence": ["Model says verified evidence."],
            },
        )

        self.assertIn("Alternate candidate retained; deterministic subdivision remains unproven.", report)
        self.assertNotIn("Model says this alternate is verified.", report)
        self.assertNotIn("Model says verified trigger.", report)

    def test_derived_4h_provenance_is_explicit_and_not_called_native(self) -> None:
        node = _node(
            status="unproven",
            structure="Candidate motive leg.",
        )
        node["recursive_multi_timeframe_proof_evidence_kind"] = "derived_4h"

        report = _report(node)

        self.assertIn("`derived_4h` session-aligned aggregation", report)
        self.assertNotIn("native_4h", report)


if __name__ == "__main__":
    unittest.main()
