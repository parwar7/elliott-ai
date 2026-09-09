from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from elliott_ai.adaptive_structure import (
    AdaptiveAnalysisScope,
    AdaptiveChildAttemptDisposition,
    AdaptiveParentProofDisposition,
    AdaptiveSearchOutcome,
    AnalysisBoundaryKind,
    AnalysisScopeBoundary,
    BoundaryValidityStatus,
    DiagonalDeclaration,
    DiagonalGeometry,
    DiagonalType,
    DiagonalWave5Termination,
    ParentStructuralAssessment,
    PriceSegmentCandidate,
    PriceSegmentationHypothesis,
    SegmentCompletionState,
    calculate_diagonal_geometry,
    calculate_motive_hard_rules,
    evaluate_candidate_boundary,
    same_degree_parent_child_forbidden,
    summarize_child_reconstruction,
    validate_direct_child_degree,
)
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import NativeCandle


HASH_A = canonical_sha256("adaptive-structure-a")
HASH_B = canonical_sha256("adaptive-structure-b")


def boundary(
    boundary_id: str,
    kind: AnalysisBoundaryKind,
    timestamp: str,
    price: float,
    *,
    pivot_id: str | None = None,
) -> AnalysisScopeBoundary:
    return AnalysisScopeBoundary(
        boundary_id=boundary_id,
        kind=kind,
        timestamp_utc=timestamp,
        price=price,
        source_pivot_id=pivot_id,
        source_bar_hash=canonical_sha256({"boundary": boundary_id}),
        is_elliott_pivot=pivot_id is not None,
    )


class AdaptiveScopeAndHierarchyTests(unittest.TestCase):
    def test_same_degree_direct_child_is_forbidden(self) -> None:
        self.assertTrue(same_degree_parent_child_forbidden("Primary", "Primary"))
        self.assertEqual(
            validate_direct_child_degree("Primary", "Primary"),
            ("same_degree_parent_child_forbidden",),
        )
        self.assertEqual(validate_direct_child_degree("Primary", "Intermediate"), ())
        self.assertEqual(
            validate_direct_child_degree("Primary", "Minor"),
            ("direct_child_degree_descent_invalid",),
        )

    def test_analysis_scope_is_not_an_elliott_node_and_allows_siblings(self) -> None:
        start = boundary(
            "scope-start",
            AnalysisBoundaryKind.WINDOW_START,
            "2026-05-27T13:30:00Z",
            149.72,
        )
        pivot_1 = boundary(
            "pivot-1",
            AnalysisBoundaryKind.CATALOG_PIVOT,
            "2026-06-25T13:30:00Z",
            80.0,
            pivot_id="catalog-low-1",
        )
        pivot_2 = boundary(
            "pivot-2",
            AnalysisBoundaryKind.CATALOG_PIVOT,
            "2026-08-10T13:30:00Z",
            86.83,
            pivot_id="catalog-high-2",
        )
        as_of = boundary(
            "scope-as-of",
            AnalysisBoundaryKind.AS_OF_OBSERVATION,
            "2026-08-28T20:00:00Z",
            63.495,
        )
        scope = AdaptiveAnalysisScope.create(
            scope_id="",
            symbol="NASDAQ:RKLB",
            timeframe="daily",
            window_start=start,
            as_of_observation=as_of,
            analysis_cutoff_utc="2026-08-28T20:00:00Z",
            source_bundle_hashes=(HASH_A,),
        )
        segmentation = PriceSegmentationHypothesis.create(
            segmentation_id="primary-price-segmentation",
            role="primary",
            analysis_scope_hash=scope.content_hash,
            segments=(
                PriceSegmentCandidate("tail", start, pivot_1, SegmentCompletionState.CLOSED),
                PriceSegmentCandidate("correction", pivot_1, pivot_2, SegmentCompletionState.CLOSED),
                PriceSegmentCandidate("active", pivot_2, as_of, SegmentCompletionState.ACTIVE),
            ),
            provider="fixture",
            model=None,
            generated_at_utc="2026-08-29T12:00:00Z",
        )

        self.assertEqual(len(segmentation.segments), 3)
        self.assertFalse(scope.window_start.is_elliott_pivot)
        self.assertFalse(scope.as_of_observation.is_elliott_pivot)
        self.assertNotIn("degree", scope.to_dict())
        self.assertNotIn("family", scope.to_dict())
        self.assertNotIn("invalidation", scope.to_dict())
        self.assertNotIn("verified", scope.to_dict())

    def test_segmentation_cannot_skip_an_interval(self) -> None:
        start = boundary("s", AnalysisBoundaryKind.WINDOW_START, "2026-01-01T00:00:00Z", 10)
        middle = boundary("m", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-02-01T00:00:00Z", 20, pivot_id="p-m")
        disconnected = boundary("x", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-02-15T00:00:00Z", 15, pivot_id="p-x")
        end = boundary("e", AnalysisBoundaryKind.AS_OF_OBSERVATION, "2026-03-01T00:00:00Z", 18)
        with self.assertRaises(ValueError):
            PriceSegmentationHypothesis.create(
                segmentation_id="bad",
                role="primary",
                analysis_scope_hash=HASH_A,
                segments=(
                    PriceSegmentCandidate("one", start, middle, SegmentCompletionState.CLOSED),
                    PriceSegmentCandidate("two", disconnected, end, SegmentCompletionState.ACTIVE),
                ),
                provider="fixture",
                model=None,
                generated_at_utc="2026-03-01T00:00:00Z",
            )


class AdaptiveRulesAndReconstructionTests(unittest.TestCase):
    def test_boundary_failure_is_not_an_elliott_invalidation(self) -> None:
        candles = (
            NativeCandle.create(
                candle_id="c0",
                timestamp_utc="2026-01-01T00:00:00Z",
                open=101.2,
                high=103.0,
                low=101.2,
                close=102.0,
                volume=100,
            ),
            NativeCandle.create(
                candle_id="c1",
                timestamp_utc="2026-01-02T00:00:00Z",
                open=102.0,
                high=104.0,
                low=99.0,
                close=103.0,
                volume=100,
            ),
            NativeCandle.create(
                candle_id="c2",
                timestamp_utc="2026-01-03T00:00:00Z",
                open=103.0,
                high=106.0,
                low=102.0,
                close=105.0,
                volume=100,
            ),
        )
        result = evaluate_candidate_boundary(
            candidate_id="bullish-correction",
            direction="up",
            start_timestamp_utc=candles[0].timestamp_utc,
            end_timestamp_utc=candles[-1].timestamp_utc,
            start_price=101.2,
            end_price=106.0,
            candles=candles,
        )

        self.assertEqual(result.status, BoundaryValidityStatus.PIVOT_NOT_TRUE_EXTREME)
        self.assertIn("boundary_requires_reselection", result.reason_codes)
        self.assertNotIn("wave_2_beyond_wave_1_origin", result.reason_codes)

    def test_exact_rklb_shape_rejects_wave_3_shortest_but_keeps_parent_unproven(self) -> None:
        calculation = calculate_motive_hard_rules(
            (150.99989, 113.66, 122.63, 99.61, 110.78, 80.00),
            direction="down",
            family="impulse",
        )
        failed = {item.rule for item in calculation.rule_results if not item.passed}
        self.assertIn("wave_3_is_shortest", failed)
        self.assertFalse(calculation.all_hard_rules_passed)
        self.assertEqual(calculation.p0, 150.99989)
        self.assertEqual(calculation.p5, 80.0)
        self.assertGreater(calculation.wave_1_length, calculation.wave_3_length)
        self.assertGreater(calculation.wave_5_length, calculation.wave_3_length)
        decision = summarize_child_reconstruction(
            parent_wave_id="rklb-parent",
            attempt_dispositions=(AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,),
        )
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.UNPROVEN)
        self.assertEqual(decision.search_outcome, AdaptiveSearchOutcome.RECOUNT_REQUIRED)

    def test_second_distinct_graph_can_keep_parent_alive(self) -> None:
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
                AdaptiveChildAttemptDisposition.UNPROVEN,
            ),
        )
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.UNPROVEN)
        self.assertEqual(decision.search_outcome, AdaptiveSearchOutcome.RECOUNT_REQUIRED)

    def test_valid_second_reconstruction_verifies_parent(self) -> None:
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
                AdaptiveChildAttemptDisposition.VERIFIED,
            ),
        )
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.VERIFIED)
        self.assertEqual(decision.search_outcome, AdaptiveSearchOutcome.VERIFIED)

    def test_duplicate_reconstruction_uses_budget_without_invalidating_parent(self) -> None:
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
                AdaptiveChildAttemptDisposition.DUPLICATE_RECONSTRUCTION_CANDIDATE,
            ),
        )
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.UNPROVEN)
        self.assertEqual(decision.search_outcome, AdaptiveSearchOutcome.RECOUNT_REQUIRED)

        exhausted = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
                AdaptiveChildAttemptDisposition.DUPLICATE_RECONSTRUCTION_CANDIDATE,
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
            ),
        )
        self.assertEqual(
            exhausted.parent_status,
            AdaptiveParentProofDisposition.UNPROVEN,
        )
        self.assertEqual(
            exhausted.search_outcome,
            AdaptiveSearchOutcome.CANDIDATE_SEARCH_EXHAUSTED,
        )

    def test_no_child_graph_found_is_unproven_not_market_invalidation(self) -> None:
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(),
        )
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.UNPROVEN)
        self.assertEqual(decision.search_outcome, AdaptiveSearchOutcome.UNPROVEN)

    def test_standard_impulse_wave_4_overlap_is_a_hard_failure(self) -> None:
        calculation = calculate_motive_hard_rules(
            (100.0, 130.0, 115.0, 160.0, 125.0, 175.0),
            direction="up",
            family="impulse",
        )
        failed = {item.rule for item in calculation.rule_results if not item.passed}
        self.assertIn("standard_impulse_wave_4_overlaps_wave_1", failed)
        self.assertGreater(calculation.wave_4_wave_1_overlap_amount, 0.0)
        self.assertFalse(calculation.all_hard_rules_passed)

    def test_bounded_search_exhaustion_is_unproven_without_impossibility_proof(self) -> None:
        attempts = (AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,) * 3
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=attempts,
        )
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.UNPROVEN)
        self.assertEqual(decision.search_outcome, AdaptiveSearchOutcome.CANDIDATE_SEARCH_EXHAUSTED)

    def test_actual_parent_hard_rule_failure_is_inconsistent_immediately(self) -> None:
        assessment = ParentStructuralAssessment.assess(
            parent_wave_id="parent",
            failed_parent_rules=("wave_2_beyond_wave_1_origin",),
            admissible_family_space=("impulse", "diagonal"),
        )
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(),
            parent_assessment=assessment,
        )
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.INCONSISTENT)
        self.assertTrue(assessment.structural_impossibility_proven)
        self.assertFalse(assessment.family_exhaustive_impossibility_supported)
        self.assertEqual(
            decision.search_outcome,
            AdaptiveSearchOutcome.PARENT_STRUCTURALLY_INCONSISTENT,
        )

    def test_family_exhaustion_claim_is_rejected_by_active_capability(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Exhaustive family elimination is not supported",
        ):
            ParentStructuralAssessment.assess(
                parent_wave_id="parent",
                child_attempts_examined=2,
                admissible_family_space=("impulse",),
                examined_family_space=("impulse",),
                deterministically_eliminated_families=("impulse",),
                evidence_complete=True,
            )

    def test_exhausted_same_family_search_remains_unproven(self) -> None:
        assessment = ParentStructuralAssessment.assess(
            parent_wave_id="parent",
            child_attempts_examined=3,
            admissible_family_space=("impulse",),
            examined_family_space=("impulse",),
            evidence_complete=True,
        )
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
                AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
            ),
            parent_assessment=assessment,
        )
        self.assertFalse(assessment.structural_impossibility_proven)
        self.assertFalse(assessment.family_exhaustive_impossibility_supported)
        self.assertEqual(assessment.remaining_admissible_reconstructions, ("impulse",))
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.UNPROVEN)
        self.assertEqual(
            decision.search_outcome,
            AdaptiveSearchOutcome.CANDIDATE_SEARCH_EXHAUSTED,
        )

    def test_incomplete_data_cannot_prove_structural_impossibility(self) -> None:
        assessment = ParentStructuralAssessment.assess(
            parent_wave_id="parent",
            child_attempts_examined=2,
            admissible_family_space=("impulse",),
            examined_family_space=("impulse",),
            evidence_complete=False,
            missing_material_timeframe=True,
        )
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(
                AdaptiveChildAttemptDisposition.NOT_COVERED,
                AdaptiveChildAttemptDisposition.NOT_COVERED,
            ),
            parent_assessment=assessment,
        )
        self.assertFalse(assessment.structural_impossibility_proven)
        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.NOT_COVERED)

    def test_parent_family_reclassification_request_stays_unproven(self) -> None:
        assessment = ParentStructuralAssessment.assess(
            parent_wave_id="parent",
            admissible_family_space=("impulse",),
            parent_family_reclassification_required=True,
        )
        decision = summarize_child_reconstruction(
            parent_wave_id="parent",
            attempt_dispositions=(
                AdaptiveChildAttemptDisposition.PARENT_FAMILY_RECLASSIFICATION_REQUIRED,
            ),
            parent_assessment=assessment,
        )

        self.assertEqual(decision.parent_status, AdaptiveParentProofDisposition.UNPROVEN)
        self.assertEqual(decision.search_outcome, AdaptiveSearchOutcome.RECOUNT_REQUIRED)
        self.assertIn(
            "parent_family_reclassification_required",
            decision.reason_codes,
        )

    def test_contracting_leading_diagonal_is_deterministically_calculated(self) -> None:
        declaration = DiagonalDeclaration(
            diagonal_type=DiagonalType.LEADING,
            geometry=DiagonalGeometry.CONTRACTING,
            wave5_termination=DiagonalWave5Termination.NORMAL,
        )
        result = calculate_diagonal_geometry(
            (100, 130, 110, 140, 125, 145),
            (
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
                "2026-01-03T00:00:00Z",
                "2026-01-04T00:00:00Z",
                "2026-01-05T00:00:00Z",
                "2026-01-06T00:00:00Z",
            ),
            direction="up",
            declaration=declaration,
            child_families=("impulse", "zigzag", "impulse", "zigzag", "impulse"),
        )

        self.assertTrue(result.all_hard_rules_passed)
        self.assertEqual(result.convergence_or_divergence, "converging")
        self.assertLess(result.terminal_boundary_width, result.initial_boundary_width)
        self.assertGreater(result.wave_4_overlap_passed, False)
        self.assertEqual(result.observed_wave_5_termination, DiagonalWave5Termination.NORMAL)

    def test_ending_diagonal_requires_corrective_children(self) -> None:
        declaration = DiagonalDeclaration(
            diagonal_type=DiagonalType.ENDING,
            geometry=DiagonalGeometry.CONTRACTING,
            wave5_termination=DiagonalWave5Termination.NORMAL,
        )
        result = calculate_diagonal_geometry(
            (100, 130, 110, 140, 125, 145),
            tuple(f"2026-01-0{index}T00:00:00Z" for index in range(1, 7)),
            direction="up",
            declaration=declaration,
            child_families=("impulse", "zigzag", "impulse", "zigzag", "impulse"),
        )
        self.assertFalse(result.child_family_rules_passed)
        self.assertFalse(result.all_hard_rules_passed)

    def test_ending_diagonal_accepts_only_five_zigzag_family_legs(self) -> None:
        declaration = DiagonalDeclaration(
            diagonal_type=DiagonalType.ENDING,
            geometry=DiagonalGeometry.CONTRACTING,
            wave5_termination=DiagonalWave5Termination.NORMAL,
        )
        result = calculate_diagonal_geometry(
            (100, 130, 110, 140, 125, 145),
            tuple(f"2026-01-0{index}T00:00:00Z" for index in range(1, 7)),
            direction="up",
            declaration=declaration,
            child_families=("zigzag", "zigzag", "zigzag", "zigzag", "zigzag"),
        )
        self.assertTrue(result.child_family_rules_passed)
        self.assertEqual(result.diagonal_type, DiagonalType.ENDING)
        self.assertEqual(
            result.expected_child_structure,
            ("zigzag_three",) * 5,
        )
        self.assertEqual(result.observed_child_structure, ("zigzag",) * 5)
        self.assertIn("ending-zigzag-five", result.internal_structure_policy)
        self.assertEqual(
            result.policy_source,
            "repository-approved-active-standard-doctrine",
        )

    def test_ending_diagonal_rejects_flat_triangle_and_combination_legs(self) -> None:
        declaration = DiagonalDeclaration(
            diagonal_type=DiagonalType.ENDING,
            geometry=DiagonalGeometry.CONTRACTING,
            wave5_termination=DiagonalWave5Termination.NORMAL,
        )
        for family in ("flat", "triangle", "combination"):
            result = calculate_diagonal_geometry(
                (100, 130, 110, 140, 125, 145),
                tuple(f"2026-01-0{index}T00:00:00Z" for index in range(1, 7)),
                direction="up",
                declaration=declaration,
                child_families=("zigzag", family, "zigzag", "zigzag", "zigzag"),
            )
            self.assertFalse(result.child_family_rules_passed, family)

    def test_calculation_contract_is_immutable_and_auditable(self) -> None:
        calculation = calculate_motive_hard_rules(
            (100, 120, 110, 150, 130, 180),
            direction="up",
            family="impulse",
        )
        payload = calculation.to_dict()
        self.assertEqual(
            set((f"p{index}" for index in range(6))),
            set(payload) & {f"p{index}" for index in range(6)},
        )
        self.assertIn("wave_4_wave_1_overlap_amount", payload)
        self.assertIn("rule_results", payload)
        self.assertEqual(len(payload["content_hash"]), 64)
        with self.assertRaises(FrozenInstanceError):
            calculation.p0 = 0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
