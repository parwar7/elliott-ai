from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import unittest

from elliott_ai.adaptive_structure import (
    DiagonalDeclaration,
    DiagonalGeometry,
    DiagonalType,
    DiagonalWave5Termination,
)
from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateGenerationError,
    CandidateGenerationFailureCode,
    CandidateGraphRole,
    CandidateProofScope,
    ChildReconstructionContext,
    CoverageLimitationCode,
    LowerTimeframeCandidateGenerator,
    RECONSTRUCTION_ALLOWED_CHANGES,
    ReconstructionExcludedGraph,
    build_generation_packet,
    candidate_generation_request_content_hash,
    graph_signature,
    googl_intermediate_to_minute_policy,
    parent_candidate_content_hash,
    validate_generation_request,
)
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.timeframes import TimeframeUsePurpose
from scripts.lower_timeframe_test_fixtures import (
    FakeGraphProvider,
    make_generation_request,
    raw_graph,
)


def distinct_same_family_alternative(request, catalog):
    graph = raw_graph(
        request,
        catalog,
        role=CandidateGraphRole.ALTERNATIVE,
    )
    graph["children"][0]["invalidation"]["threshold_price"] = 49.0
    return graph


class LowerTimeframeCandidateGeneratorTests(unittest.TestCase):
    @staticmethod
    def _exclusion_for(graph) -> ReconstructionExcludedGraph:
        return ReconstructionExcludedGraph.create(
            graph_signature=graph_signature(graph),
            pivot_sequence=(
                graph.children[0].start_pivot.pivot_id,
                *(item.end_pivot.pivot_id for item in graph.children),
            ),
            declared_family=graph.declared_family,
            child_family_sequence=tuple(
                item.declared_family for item in graph.children
            ),
            deterministic_failure_ids=("wave_3_is_shortest",),
            hard_rule_calculations=(
                {
                    "rule": "wave_3_is_shortest",
                    "wave_1_length": 20.0,
                    "wave_3_length": 10.0,
                    "wave_5_length": 30.0,
                },
            ),
            failed_boundary_checks=(),
            rejected_diagonal=None,
            verifier_result_hash=canonical_sha256("rejected-verifier-result"),
        )

    @staticmethod
    def _with_reconstruction(request, context):
        rebuilt = replace(
            request,
            reconstruction_context=context,
            content_hash="",
        )
        return replace(
            rebuilt,
            content_hash=candidate_generation_request_content_hash(rebuilt),
        )

    @staticmethod
    def _with_parent_family(request, family, *, diagonal_declaration=None):
        parent = request.parent_candidate
        parent = replace(
            parent,
            parent_candidate_id=f"{parent.parent_candidate_id}-{family}",
            declared_family=family,
            diagonal_declaration=diagonal_declaration,
            content_hash="",
        )
        parent = replace(
            parent,
            content_hash=parent_candidate_content_hash(parent),
        )
        rebuilt = replace(
            request,
            parent_candidate=parent,
            content_hash="",
        )
        return replace(
            rebuilt,
            content_hash=candidate_generation_request_content_hash(rebuilt),
        )

    def test_same_degree_parent_child_is_rejected_before_provider_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "same_degree_parent_child_forbidden"):
            make_generation_request(
                parent_degree="Minor",
                target_child_degree="Minor",
                timeframe="4h",
                generation_depth=1,
            )

    def test_subdivision_parent_keeps_intermediate_degree_on_one_hour_evidence(self) -> None:
        request, _catalog = make_generation_request()
        parent = replace(
            request.parent_candidate,
            timeframe="1h",
            timeframe_use_purpose=TimeframeUsePurpose.SUBDIVISION_PROOF,
            structural_parent_degree="Primary",
            parent_evidence_timeframe="4h",
            content_hash="",
        )
        parent = replace(parent, content_hash=parent_candidate_content_hash(parent))

        self.assertEqual(parent.degree, "Intermediate")
        self.assertEqual(parent.timeframe, "1h")
        self.assertEqual(
            parent.timeframe_use_purpose,
            TimeframeUsePurpose.SUBDIVISION_PROOF,
        )

    def test_legacy_absolute_degree_timeframe_check_remains_unchanged(self) -> None:
        request, _catalog = make_generation_request()

        with self.assertRaisesRegex(ValueError, "incompatible with its degree"):
            replace(request.parent_candidate, timeframe="1h", content_hash="")

    def test_subdivision_snapshot_rejects_same_degree_lineage(self) -> None:
        request, _catalog = make_generation_request()

        with self.assertRaisesRegex(
            ValueError,
            "direct_child_degree_descent_invalid",
        ):
            replace(
                request.parent_candidate,
                timeframe="1h",
                timeframe_use_purpose=TimeframeUsePurpose.SUBDIVISION_PROOF,
                structural_parent_degree="Intermediate",
                parent_evidence_timeframe="4h",
                content_hash="",
            )

    def test_default_invocation_cannot_call_a_provider(self) -> None:
        request, catalog = make_generation_request()
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY),
                CandidateGraphRole.ALTERNATIVE: distinct_same_family_alternative(request, catalog),
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_pair(request, provider=provider)

        self.assertEqual(raised.exception.failure.code, CandidateGenerationFailureCode.MODEL_CALL_NOT_AUTHORIZED)
        self.assertEqual(provider.calls, [])

    def test_two_blind_candidate_graphs_are_frozen_and_distinct(self) -> None:
        request, catalog = make_generation_request()
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY),
                CandidateGraphRole.ALTERNATIVE: distinct_same_family_alternative(request, catalog),
            }
        )

        pair = LowerTimeframeCandidateGenerator().generate_pair(
            request,
            provider=provider,
            allow_model_call=True,
        )

        self.assertEqual([role for role, _ in provider.calls], [CandidateGraphRole.PRIMARY, CandidateGraphRole.ALTERNATIVE])
        self.assertEqual(pair.primary_graph.role, CandidateGraphRole.PRIMARY)
        self.assertEqual(pair.alternative_graph.role, CandidateGraphRole.ALTERNATIVE)
        self.assertNotEqual(pair.primary_graph.content_hash, pair.alternative_graph.content_hash)
        self.assertIsNone(pair.primary_graph.proof_scope.verified_to_timeframe)
        self.assertTrue(all(child.candidate_state == "candidate" for child in pair.primary_graph.children))
        self.assertEqual(pair.primary_graph.proof_scope.terminal_degree, "Minute")
        limitations = {
            (item.timeframe, item.code)
            for item in pair.primary_graph.proof_scope.coverage_limitations
        }
        self.assertIn(("1h", CoverageLimitationCode.SUPPORTING_ONLY), limitations)
        self.assertIn(("15m", CoverageLimitationCode.POLICY_NOT_REQUIRED), limitations)
        with self.assertRaises(FrozenInstanceError):
            pair.primary_graph.graph_id = "rewritten"  # type: ignore[misc]

    def test_provider_cannot_claim_a_verification_boundary(self) -> None:
        request, catalog = make_generation_request()
        primary = raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY)
        primary["proof_scope"]["verified_to_timeframe"] = "daily"
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: primary,
                CandidateGraphRole.ALTERNATIVE: distinct_same_family_alternative(request, catalog),
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_pair(request, provider=provider, allow_model_call=True)

        self.assertEqual(raised.exception.failure.code, CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT)

    def test_provider_cannot_add_a_structural_status_field(self) -> None:
        request, catalog = make_generation_request()
        primary = raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY)
        primary["status"] = "verified"
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: primary,
                CandidateGraphRole.ALTERNATIVE: distinct_same_family_alternative(request, catalog),
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_pair(request, provider=provider, allow_model_call=True)

        self.assertEqual(raised.exception.failure.code, CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT)

    def test_malformed_child_chronology_returns_typed_reconstruction_exclusion(self) -> None:
        request, catalog = make_generation_request()
        primary = raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY)
        first = primary["children"][0]
        first["start_pivot_id"], first["end_pivot_id"] = (
            first["end_pivot_id"],
            first["start_pivot_id"],
        )
        provider = FakeGraphProvider({CandidateGraphRole.PRIMARY: primary})

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_one(
                request,
                role=CandidateGraphRole.PRIMARY,
                provider=provider,
                allow_model_call=True,
            )

        failure = raised.exception.failure
        self.assertEqual(
            failure.code,
            CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT,
        )
        self.assertIsNotNone(failure.reconstruction_exclusion)
        self.assertEqual(
            failure.reconstruction_exclusion.declared_family,
            request.parent_candidate.declared_family,
        )
        self.assertIn(
            "child_endpoint_order_invalid",
            failure.reconstruction_exclusion.deterministic_failure_ids,
        )
        self.assertEqual(len(provider.calls), 1)

    def test_duplicate_alternative_is_rejected_instead_of_copied(self) -> None:
        request, catalog = make_generation_request()
        primary = raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY)
        alternative = dict(primary)
        alternative["graph_id"] = "alternative-copy"
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: primary,
                CandidateGraphRole.ALTERNATIVE: alternative,
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_pair(request, provider=provider, allow_model_call=True)

        self.assertEqual(raised.exception.failure.code, CandidateGenerationFailureCode.DUPLICATE_GRAPH_SIGNATURE)
        self.assertEqual(raised.exception.failure.role, CandidateGraphRole.ALTERNATIVE)

    def test_policy_authorizes_the_two_explicit_googl_descents_only(self) -> None:
        policy = googl_intermediate_to_minute_policy()
        daily_request, _ = make_generation_request()
        four_hour_request, _ = make_generation_request(
            parent_degree="Minor",
            target_child_degree="Minute",
            timeframe="4h",
            generation_depth=1,
        )

        self.assertEqual(policy.maximum_generation_depth, 2)
        self.assertEqual(policy.timeframe_by_child_degree["Minor"], "daily")
        self.assertEqual(policy.timeframe_by_child_degree["Minute"], "4h")
        self.assertEqual(validate_generation_request(daily_request), ())
        self.assertEqual(validate_generation_request(four_hour_request), ())

    def test_preflight_failure_occurs_before_provider_construction_or_call(self) -> None:
        request, catalog = make_generation_request()
        invalid_scope = CandidateProofScope(
            target_timeframe="daily",
            verified_to_timeframe=None,
            terminal_degree="Primary",
            coverage_limitations=(),
        )
        invalid_request = replace(request, proof_scope=invalid_scope, content_hash="")
        invalid_request = replace(
            invalid_request,
            content_hash=candidate_generation_request_content_hash(invalid_request),
        )
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY),
                CandidateGraphRole.ALTERNATIVE: distinct_same_family_alternative(request, catalog),
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_pair(invalid_request, provider=provider, allow_model_call=True)

        self.assertEqual(raised.exception.failure.code, CandidateGenerationFailureCode.TARGET_CHILD_DEGREE_INVALID)
        self.assertEqual(provider.calls, [])

    def test_third_descent_is_rejected_by_the_two_level_policy_before_any_call(self) -> None:
        request, catalog = make_generation_request(
            parent_degree="Minor",
            target_child_degree="Minute",
            timeframe="4h",
            generation_depth=2,
        )
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY),
                CandidateGraphRole.ALTERNATIVE: distinct_same_family_alternative(request, catalog),
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_pair(request, provider=provider, allow_model_call=True)

        self.assertEqual(raised.exception.failure.code, CandidateGenerationFailureCode.RECURSION_BUDGET_EXHAUSTED)
        self.assertEqual(provider.calls, [])

    def test_attempt_two_receives_only_typed_deterministic_exclusions(self) -> None:
        request, catalog = make_generation_request()
        first_provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(
                    request, catalog, role=CandidateGraphRole.PRIMARY
                )
            }
        )
        first = LowerTimeframeCandidateGenerator().generate_one(
            request,
            role=CandidateGraphRole.PRIMARY,
            provider=first_provider,
            allow_model_call=True,
        )
        exclusion = self._exclusion_for(first)
        context = ChildReconstructionContext.create(
            attempt_number=2,
            excluded_graphs=(exclusion,),
            admissible_parent_families=("impulse",),
            allowed_changes=RECONSTRUCTION_ALLOWED_CHANGES,
            planner_may_request_another_timeframe=True,
        )
        reconstructed = self._with_reconstruction(request, context)
        packet = build_generation_packet(reconstructed)

        self.assertEqual(packet["reconstruction_context"]["attempt_number"], 2)
        self.assertEqual(
            tuple(packet["reconstruction_context"]["admissible_parent_families"]),
            ("impulse",),
        )
        self.assertTrue(
            packet["reconstruction_instructions"]["same_parent_family_only"]
        )
        self.assertEqual(
            packet["reconstruction_instructions"]["frozen_parent_family"],
            "impulse",
        )
        self.assertEqual(
            packet["reconstruction_context"]["excluded_graphs"][0][
                "graph_signature"
            ],
            graph_signature(first),
        )
        self.assertIn(
            "wave_3_is_shortest",
            packet["reconstruction_context"]["excluded_graphs"][0][
                "deterministic_failure_ids"
            ],
        )
        self.assertFalse(
            packet["reconstruction_instructions"]
            ["previous_model_reasoning_available"]
        )
        self.assertFalse(
            packet["reconstruction_instructions"]
            ["previous_model_prose_available"]
        )
        self.assertTrue(
            packet["reconstruction_instructions"]
            ["do_not_recreate_excluded_semantic_graphs"]
        )

    def test_excluded_semantic_graph_is_rejected_without_retry(self) -> None:
        request, catalog = make_generation_request()
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(
                    request, catalog, role=CandidateGraphRole.PRIMARY
                )
            }
        )
        first = LowerTimeframeCandidateGenerator().generate_one(
            request,
            role=CandidateGraphRole.PRIMARY,
            provider=provider,
            allow_model_call=True,
        )
        context = ChildReconstructionContext.create(
            attempt_number=2,
            excluded_graphs=(self._exclusion_for(first),),
            admissible_parent_families=("impulse",),
            allowed_changes=RECONSTRUCTION_ALLOWED_CHANGES,
            planner_may_request_another_timeframe=True,
        )
        reconstructed = self._with_reconstruction(request, context)
        duplicate_provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(
                    reconstructed,
                    catalog,
                    role=CandidateGraphRole.PRIMARY,
                )
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_one(
                reconstructed,
                role=CandidateGraphRole.PRIMARY,
                provider=duplicate_provider,
                allow_model_call=True,
            )

        self.assertEqual(
            raised.exception.failure.code,
            CandidateGenerationFailureCode.DUPLICATE_RECONSTRUCTION_CANDIDATE,
        )
        self.assertEqual(len(duplicate_provider.calls), 1)

    def test_frozen_impulse_parent_cannot_be_reconstructed_as_diagonal(self) -> None:
        request, catalog = make_generation_request()
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(
                    request,
                    catalog,
                    role=CandidateGraphRole.PRIMARY,
                    family="diagonal",
                )
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_one(
                request,
                role=CandidateGraphRole.PRIMARY,
                provider=provider,
                allow_model_call=True,
            )

        self.assertEqual(
            raised.exception.failure.code,
            CandidateGenerationFailureCode.PARENT_FAMILY_RECLASSIFICATION_REQUIRED,
        )
        self.assertEqual(len(provider.calls), 1)

    def test_frozen_diagonal_parent_cannot_be_reconstructed_as_impulse(self) -> None:
        request, catalog = make_generation_request()
        diagonal = DiagonalDeclaration(
            diagonal_type=DiagonalType.LEADING,
            geometry=DiagonalGeometry.CONTRACTING,
            wave5_termination=DiagonalWave5Termination.NORMAL,
        )
        diagonal_request = self._with_parent_family(
            request,
            "diagonal",
            diagonal_declaration=diagonal,
        )
        provider = FakeGraphProvider(
            {
                CandidateGraphRole.PRIMARY: raw_graph(
                    diagonal_request,
                    catalog,
                    role=CandidateGraphRole.PRIMARY,
                    family="impulse",
                )
            }
        )

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_one(
                diagonal_request,
                role=CandidateGraphRole.PRIMARY,
                provider=provider,
                allow_model_call=True,
            )

        self.assertEqual(
            raised.exception.failure.code,
            CandidateGenerationFailureCode.PARENT_FAMILY_RECLASSIFICATION_REQUIRED,
        )

    def test_frozen_zigzag_parent_cannot_be_reconstructed_as_flat(self) -> None:
        request, catalog = make_generation_request()
        zigzag_request = self._with_parent_family(request, "zigzag")
        flat = raw_graph(
            zigzag_request,
            catalog,
            role=CandidateGraphRole.PRIMARY,
            family="zigzag",
        )
        flat["graph_id"] = "primary-flat-reclassification"
        flat["declared_family"] = "flat"
        flat["children"][0]["declared_family"] = "zigzag"
        provider = FakeGraphProvider({CandidateGraphRole.PRIMARY: flat})

        with self.assertRaises(CandidateGenerationError) as raised:
            LowerTimeframeCandidateGenerator().generate_one(
                zigzag_request,
                role=CandidateGraphRole.PRIMARY,
                provider=provider,
                allow_model_call=True,
            )

        self.assertEqual(
            raised.exception.failure.code,
            CandidateGenerationFailureCode.PARENT_FAMILY_RECLASSIFICATION_REQUIRED,
        )

    def test_same_parent_family_allows_distinct_pivot_decomposition_exclusions(self) -> None:
        request, catalog = make_generation_request()
        first = LowerTimeframeCandidateGenerator().generate_one(
            request,
            role=CandidateGraphRole.PRIMARY,
            provider=FakeGraphProvider(
                {
                    CandidateGraphRole.PRIMARY: raw_graph(
                        request, catalog, role=CandidateGraphRole.PRIMARY
                    )
                }
            ),
            allow_model_call=True,
        )
        first_exclusion = self._exclusion_for(first)
        alternate_pivots = (
            first_exclusion.pivot_sequence[0],
            "alternate-catalog-pivot-1",
            "alternate-catalog-pivot-2",
            "alternate-catalog-pivot-3",
            "alternate-catalog-pivot-4",
            first_exclusion.pivot_sequence[-1],
        )
        second_exclusion = ReconstructionExcludedGraph.create(
            graph_signature=canonical_sha256("same-family-alternate-pivots"),
            pivot_sequence=alternate_pivots,
            declared_family="impulse",
            child_family_sequence=first_exclusion.child_family_sequence,
            deterministic_failure_ids=("candidate_segmentation_failed",),
            hard_rule_calculations=(),
            failed_boundary_checks=(),
            rejected_diagonal=None,
            verifier_result_hash=canonical_sha256("second-rejected-verifier-result"),
        )

        context = ChildReconstructionContext.create(
            attempt_number=3,
            excluded_graphs=(first_exclusion, second_exclusion),
            admissible_parent_families=("impulse",),
            allowed_changes=RECONSTRUCTION_ALLOWED_CHANGES,
            planner_may_request_another_timeframe=True,
        )

        self.assertNotEqual(
            context.excluded_graphs[0].pivot_sequence,
            context.excluded_graphs[1].pivot_sequence,
        )
        self.assertEqual(context.admissible_parent_families, ("impulse",))
        self.assertNotIn("different_family", context.allowed_changes)


if __name__ == "__main__":
    unittest.main()
