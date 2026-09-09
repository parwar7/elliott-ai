from __future__ import annotations

from dataclasses import replace
import inspect
import unittest

from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    CandidateGraphRole,
    CandidateInvalidation,
    InvalidationDirection,
    InvalidationEvaluationBasis,
    PivotPriceField,
    TypedPivot,
)
from elliott_ai.lower_timeframe_subdivision_verifier import (
    LowerTimeframeSubdivisionVerifier,
    SubdivisionVerificationStatus,
    VerificationReasonCode,
    _invalidation_breached,
    build_subdivision_verification_request,
    googl_intermediate_to_minute_verifier_policy,
)
from scripts.lower_timeframe_test_fixtures import (
    direct_family_graph,
    direct_graph,
    make_generation_request,
    pivot_for,
)


def _verification_request(
    *,
    parent_degree: str = "Minor",
    target_child_degree: str = "Minute",
    timeframe: str = "4h",
    generation_depth: int = 1,
    end_is_date_only: bool = False,
    native: bool = True,
    family: str = "impulse",
    malformed_boundary: bool = False,
    role: CandidateGraphRole = CandidateGraphRole.PRIMARY,
    row_count: int = 8,
):
    generation, catalog = make_generation_request(
        parent_degree=parent_degree,
        target_child_degree=target_child_degree,
        timeframe=timeframe,
        generation_depth=generation_depth,
        end_is_date_only=end_is_date_only,
        native=native,
        row_count=row_count,
    )
    graph = direct_graph(
        generation,
        catalog,
        role=role,
        family=family,
        malformed_boundary=malformed_boundary,
    )
    return build_subdivision_verification_request(
        generation,
        graph,
        verifier_policy=googl_intermediate_to_minute_verifier_policy(),
        shadow_mode=True,
        created_at_utc=generation.created_at_utc,
    )


class LowerTimeframeSubdivisionVerifierTests(unittest.TestCase):
    def test_terminal_minute_impulse_is_verified_from_native_rows(self) -> None:
        request = _verification_request()

        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.VERIFIED)
        self.assertEqual(result.proof_scope.verified_to_timeframe, "4h")
        self.assertTrue(all(item.status is SubdivisionVerificationStatus.VERIFIED for item in result.child_results))
        self.assertEqual(result.reason_codes, (VerificationReasonCode.TERMINAL_LEAF_VERIFIED,))
        self.assertEqual(result.content_hash, result.content_hash)
        motive = next(item for item in result.checks if item.scope == "motive_price_rules")
        self.assertTrue({f"p{index}" for index in range(6)}.issubset(motive.details))
        self.assertIn("wave_1_length", motive.details)
        self.assertIn("wave_3_length", motive.details)
        self.assertIn("wave_5_length", motive.details)
        self.assertIn("wave_2_origin_clearance", motive.details)
        self.assertIn("wave_4_wave_1_overlap_amount", motive.details)
        self.assertTrue(motive.details["all_hard_rules_passed"])

    def test_intermediate_to_minor_is_unproven_until_explicit_minute_descent(self) -> None:
        request = _verification_request(
            parent_degree="Intermediate",
            target_child_degree="Minor",
            timeframe="daily",
            generation_depth=0,
        )

        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.UNPROVEN)
        self.assertIsNone(result.proof_scope.verified_to_timeframe)
        self.assertIn(VerificationReasonCode.TERMINAL_DEGREE_NOT_REACHED, result.reason_codes)
        self.assertNotIn(
            VerificationReasonCode.DIAGONAL_GEOMETRY_UNPROVEN,
            result.reason_codes,
        )
        self.assertTrue(all(item.status is SubdivisionVerificationStatus.UNPROVEN for item in result.child_results))

    def test_date_only_endpoint_requires_the_complete_trading_day(self) -> None:
        request = _verification_request(end_is_date_only=True, row_count=6)

        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.NOT_COVERED)
        self.assertIn(VerificationReasonCode.FULL_COVERAGE_MISSING, result.reason_codes)
        self.assertIsNone(result.proof_scope.verified_to_timeframe)

    def test_non_native_rows_cannot_support_a_proof(self) -> None:
        request = _verification_request(native=False)

        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(VerificationReasonCode.REQUIRED_WINDOW_NOT_NATIVE, result.reason_codes)

    def test_discontinuous_child_pivots_reject_only_that_candidate(self) -> None:
        valid = _verification_request()
        invalid = _verification_request(
            malformed_boundary=True,
            role=CandidateGraphRole.ALTERNATIVE,
        )

        pair = LowerTimeframeSubdivisionVerifier().verify_pair(
            valid,
            invalid,
            shadow_mode=True,
        )

        self.assertEqual(pair.primary_result.status, SubdivisionVerificationStatus.VERIFIED)
        self.assertEqual(pair.alternative_result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(VerificationReasonCode.CHILD_PIVOT_CHAIN_DISCONTINUOUS, pair.alternative_result.reason_codes)
        self.assertNotEqual(pair.primary_result.content_hash, pair.alternative_result.content_hash)

    def test_overlap_free_diagonal_is_rejected_without_approved_geometry(self) -> None:
        request = _verification_request(family="diagonal")

        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(VerificationReasonCode.DIAGONAL_DECLARATION_REQUIRED, result.reason_codes)

    def test_supported_corrective_families_have_explicit_child_semantics(self) -> None:
        generation, catalog = make_generation_request(
            parent_degree="Minor",
            target_child_degree="Minute",
            timeframe="4h",
            generation_depth=1,
        )
        cases = (
            ("zigzag", ("A", "B", "C"), ("impulse", "zigzag", "impulse")),
            ("flat", ("A", "B", "C"), ("zigzag", "flat", "impulse")),
            ("triangle", ("A", "B", "C", "D", "E"), ("zigzag",) * 5),
            ("combination", ("W", "X", "Y"), ("zigzag",) * 3),
            ("combination", ("W", "X", "Y", "X2", "Z"), ("zigzag",) * 5),
        )

        for index, (family, positions, child_families) in enumerate(cases, start=1):
            with self.subTest(family=family, positions=positions):
                graph = direct_family_graph(
                    generation,
                    catalog,
                    role=CandidateGraphRole.PRIMARY,
                    family=family,
                    positions=positions,
                    child_families=child_families,
                )
                request = build_subdivision_verification_request(
                    generation,
                    graph,
                    verifier_policy=googl_intermediate_to_minute_verifier_policy(),
                    shadow_mode=True,
                    created_at_utc=generation.created_at_utc,
                    request_id=f"family-{index}",
                )
                result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)
                self.assertEqual(result.status, SubdivisionVerificationStatus.VERIFIED)
                self.assertEqual(tuple(child.sequence_position for child in graph.children), positions)
                if positions[-1] == "Z":
                    self.assertEqual(graph.children[1].sequence_position, "X")
                    self.assertEqual(graph.children[3].sequence_position, "X2")

    def test_result_round_trip_and_tamper_rejection_are_deterministic(self) -> None:
        request = _verification_request()
        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)
        restored = type(result).from_dict(result.to_dict())
        tampered = result.to_dict()
        tampered["status"] = "unproven"

        self.assertEqual(restored, result)
        with self.assertRaises(ValueError):
            type(result).from_dict(tampered)

    def test_repeated_verification_is_deterministic(self) -> None:
        request = _verification_request()
        verifier = LowerTimeframeSubdivisionVerifier()

        first = verifier.verify(request, shadow_mode=True)
        second = verifier.verify(request, shadow_mode=True)

        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_candidate_invalidation_breach_does_not_override_elliott_structure(self) -> None:
        request = _verification_request()
        graph = request.candidate_graph
        first = graph.children[0]
        breached_first = replace(
            first,
            invalidation=CandidateInvalidation(
                invalidation_id="breached-invalidation",
                threshold_price=170.0,
                direction=InvalidationDirection.BELOW,
                evaluation_basis=InvalidationEvaluationBasis.INTRABAR_TOUCH_OR_BREACH,
                source_pivot_id=first.start_pivot.pivot_id,
            ),
        )
        breached_graph = CandidateChildGraph.create(
            graph_id="breached-graph",
            role=graph.role,
            request_id=graph.request_id,
            request_content_hash=graph.request_content_hash,
            parent_candidate_id=graph.parent_candidate_id,
            target_child_degree=graph.target_child_degree,
            target_timeframe=graph.target_timeframe,
            declared_family=graph.declared_family,
            children=(breached_first, *graph.children[1:]),
            proof_scope=graph.proof_scope,
            provider=graph.provider,
            model=graph.model,
            generated_at_utc=graph.generated_at_utc,
            policy_version=graph.policy_version,
        )
        breached_request = build_subdivision_verification_request(
            request.generation_request,
            breached_graph,
            verifier_policy=request.verifier_policy,
            shadow_mode=True,
            created_at_utc=request.created_at_utc,
            request_id="breached-request",
        )

        result = LowerTimeframeSubdivisionVerifier().verify(breached_request, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.VERIFIED)
        self.assertIn(VerificationReasonCode.INVALIDATION_BREACHED, result.reason_codes)

    def test_completed_child_is_not_retroactively_invalidated_by_later_wave(self) -> None:
        request = _verification_request()
        first = request.candidate_graph.children[0]
        completed_child = replace(
            first,
            invalidation=CandidateInvalidation(
                invalidation_id="post-completion-breach",
                threshold_price=112.0,
                direction=InvalidationDirection.BELOW,
                evaluation_basis=InvalidationEvaluationBasis.INTRABAR_TOUCH_OR_BREACH,
                source_pivot_id=first.start_pivot.pivot_id,
            ),
        )

        self.assertFalse(
            _invalidation_breached(
                completed_child,
                request.generation_request.native_ohlcv_scope.required_window,
            )
        )

    def test_unknown_pivot_lineage_cannot_be_repaired_from_price_data(self) -> None:
        request = _verification_request()
        graph = request.candidate_graph
        first = graph.children[0]
        window = request.generation_request.native_ohlcv_scope.required_window
        replacement = TypedPivot.create(
            pivot_id="invented-pivot",
            timestamp_utc=first.start_pivot.timestamp_utc,
            price=first.start_pivot.price,
            price_field=PivotPriceField.LOW,
            pivot_type=first.start_pivot.pivot_type,
            timeframe=window.timeframe,
            source_window_hash=window.native_rows_hash,
            source_bar_hash=first.start_pivot.source_bar_hash,
        )
        altered_first = replace(first, start_pivot=replacement)
        altered_graph = CandidateChildGraph.create(
            graph_id="invented-pivot-graph",
            role=graph.role,
            request_id=graph.request_id,
            request_content_hash=graph.request_content_hash,
            parent_candidate_id=graph.parent_candidate_id,
            target_child_degree=graph.target_child_degree,
            target_timeframe=graph.target_timeframe,
            declared_family=graph.declared_family,
            children=(altered_first, *graph.children[1:]),
            proof_scope=graph.proof_scope,
            provider=graph.provider,
            model=graph.model,
            generated_at_utc=graph.generated_at_utc,
            policy_version=graph.policy_version,
        )
        altered_request = build_subdivision_verification_request(
            request.generation_request,
            altered_graph,
            verifier_policy=request.verifier_policy,
            shadow_mode=True,
            created_at_utc=request.created_at_utc,
            request_id="invented-pivot-request",
        )

        result = LowerTimeframeSubdivisionVerifier().verify(altered_request, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(VerificationReasonCode.PIVOT_NOT_CATALOGUED, result.reason_codes)

    def test_indicator_terms_are_not_verifier_inputs_or_hard_statuses(self) -> None:
        request = _verification_request()
        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=True)
        serialized = result.to_dict()

        self.assertNotIn("rsi", serialized)
        self.assertNotIn("volume", serialized)
        self.assertNotIn("ewo", serialized)
        self.assertNotIn("macd", serialized)

    def test_both_shadow_gates_are_required_and_no_provider_is_available(self) -> None:
        request = _verification_request()

        result = LowerTimeframeSubdivisionVerifier().verify(request, shadow_mode=False)

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(VerificationReasonCode.SHADOW_MODE_REQUIRED, result.reason_codes)
        self.assertNotIn("provider", inspect.signature(LowerTimeframeSubdivisionVerifier.verify).parameters)


if __name__ == "__main__":
    unittest.main()
