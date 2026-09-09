from __future__ import annotations

from dataclasses import replace
import unittest

from elliott_ai.adaptive_recount import (
    AdaptiveRecountCoordinator,
    adaptive_hypothesis_signature,
    adaptive_root_group_classification_schema_for_packet,
    build_adaptive_analysis_scope,
    build_candidate_scoped_evidence,
    build_root_group_classification_packet,
    build_root_grouping_packet,
    build_root_segmentation_packet,
    parse_root_grouping_output,
    parse_root_segmentation_output,
)
from elliott_ai.adaptive_structure import validate_direct_child_degree
from elliott_ai.lower_timeframe_candidate_generator import CandidateGraphRole

from scripts.test_adaptive_recount import STAMP, bundle_for_manifest, plan_and_manifest
from scripts.test_adaptive_root_grouping import FakeGroupedRootProvider
from scripts.test_adaptive_root_stages import AdaptiveRootStageTests


class DegreeDistinctGroupedRootProvider(FakeGroupedRootProvider):
    """Use two legitimate daily-root degree interpretations."""

    def classify_root_groups(self, *, role, packet, output_schema):
        result = super().classify_root_groups(
            role=role,
            packet=packet,
            output_schema=output_schema,
        )
        result["nodes"][0]["degree"] = (
            "Intermediate"
            if role is CandidateGraphRole.PRIMARY
            else "Primary"
        )
        return result


class RootDegreeDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _bundle_and_scope():
        coordinator, trace, manifest = plan_and_manifest()
        base = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        bundle = AdaptiveRootStageTests._wavy_bundle(base)
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        return coordinator, trace, manifest, bundle, scope

    def test_initial_scope_planning_has_no_synthetic_parent_or_child_degree(self) -> None:
        _coordinator, trace, manifest = plan_and_manifest()
        request = trace.planner_requests[0]

        self.assertEqual(request.structural_question.value, "discover_root_structure")
        self.assertIsNone(request.parent_degree)
        self.assertIsNone(request.expected_child_degree)
        self.assertIsNone(request.parent_timeframe)
        self.assertIsNone(manifest.parent_degree)
        self.assertIsNone(manifest.expected_child_degree)
        self.assertEqual(trace.planner_decisions[0].selected_timeframe, "daily")

    def test_scope_stage_a_and_stage_b1_are_degree_neutral(self) -> None:
        _coordinator, trace, _manifest, bundle, scope = self._bundle_and_scope()
        provider = FakeGroupedRootProvider()
        stage_a = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )
        segmentation = parse_root_segmentation_output(
            provider.generate_root_segmentation(
                role=CandidateGraphRole.PRIMARY,
                packet=stage_a,
                output_schema={},
            ),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            bundles=(bundle,),
            provider_name=provider.name,
            provider_model=provider.model,
            generated_at_utc=STAMP,
        )
        stage_b1 = build_root_grouping_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            segmentation=segmentation,
        )

        self.assertFalse(
            any("degree" in key for key in scope.to_dict())
        )
        self.assertNotIn("degree", stage_a["instructions"])
        self.assertNotIn("expected_root_degree", stage_a["instructions"])
        self.assertNotIn("allowed_root_degrees", stage_a["instructions"])
        self.assertNotIn("expected_root_degree", stage_b1["instructions"])
        self.assertNotIn("allowed_root_degrees", stage_b1["instructions"])

    def test_daily_b2_schema_exposes_all_deterministically_compatible_root_degrees(self) -> None:
        _coordinator, trace, _manifest, bundle, scope = self._bundle_and_scope()
        provider = FakeGroupedRootProvider()
        segmentation = parse_root_segmentation_output(
            provider.generate_root_segmentation(
                role=CandidateGraphRole.PRIMARY,
                packet=build_root_segmentation_packet(
                    trace.plan,
                    (bundle,),
                    role=CandidateGraphRole.PRIMARY,
                    scope=scope,
                ),
                output_schema={},
            ),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            bundles=(bundle,),
            provider_name=provider.name,
            provider_model=provider.model,
            generated_at_utc=STAMP,
        )
        grouping_packet = build_root_grouping_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            segmentation=segmentation,
        )
        grouping = parse_root_grouping_output(
            provider.generate_root_grouping(
                role=CandidateGraphRole.PRIMARY,
                packet=grouping_packet,
                output_schema={},
            ),
            role=CandidateGraphRole.PRIMARY,
            segmentation=segmentation,
            provider_name=provider.name,
            provider_model=provider.model,
            generated_at_utc=STAMP,
        )
        evidence = build_candidate_scoped_evidence(
            trace.plan,
            (bundle,),
            segmentation,
        )
        stage_b2 = build_root_group_classification_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            segmentation=segmentation,
            grouping=grouping,
            evidence=evidence,
        )
        schema = adaptive_root_group_classification_schema_for_packet(stage_b2)
        degree_enum = schema["properties"]["nodes"]["items"]["anyOf"][0][
            "properties"
        ]["degree"]["enum"]

        self.assertEqual(
            stage_b2["instructions"]["allowed_root_degrees"],
            ["Primary", "Intermediate", "Minor"],
        )
        self.assertEqual(degree_enum, ["Primary", "Intermediate", "Minor"])
        self.assertTrue(stage_b2["instructions"]["root_degree_is_provisional"])
        self.assertNotIn("expected_root_degree", stage_b2["instructions"])

    def test_primary_and_alternative_may_freeze_different_compatible_root_degrees(self) -> None:
        coordinator, trace, _manifest, bundle, _scope = self._bundle_and_scope()
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        result = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=DegreeDistinctGroupedRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )

        self.assertEqual(result.root_pair.primary.nodes[0].degree, "Intermediate")
        self.assertEqual(result.root_pair.alternative.nodes[0].degree, "Primary")
        primary_request = coordinator.plan_parent_subdivision(
            result,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=result.root_pair.primary.nodes[0].wave_id,
            bundles=(bundle,),
            requested_timeframe="4h",
            created_at_utc=STAMP,
            source_interface=result.plan.provider_capabilities.source_interface,
        ).planner_requests[-1]
        alternative_request = coordinator.plan_parent_subdivision(
            result,
            role=CandidateGraphRole.ALTERNATIVE,
            parent_wave_id=result.root_pair.alternative.nodes[0].wave_id,
            bundles=(bundle,),
            requested_timeframe="4h",
            created_at_utc=STAMP,
            source_interface=result.plan.provider_capabilities.source_interface,
        ).planner_requests[-1]
        self.assertEqual(primary_request.expected_child_degree, "Minor")
        self.assertEqual(alternative_request.expected_child_degree, "Intermediate")

    def test_strict_descent_resumes_after_root_degree_selection(self) -> None:
        self.assertEqual(validate_direct_child_degree("Primary", "Intermediate"), ())
        self.assertEqual(validate_direct_child_degree("Intermediate", "Minor"), ())
        self.assertIn(
            "same_degree_parent_child_forbidden",
            validate_direct_child_degree("Intermediate", "Intermediate"),
        )

    def test_root_degree_is_material_to_semantic_distinctness(self) -> None:
        coordinator, trace, _manifest, bundle, _scope = self._bundle_and_scope()
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        result = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=DegreeDistinctGroupedRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        primary = result.root_pair.primary
        node = primary.nodes[0]
        degree_only_alternative = replace(
            primary,
            nodes=(replace(node, degree="Primary"),),
            content_hash="",
        )

        self.assertNotEqual(
            adaptive_hypothesis_signature(primary),
            adaptive_hypothesis_signature(degree_only_alternative),
        )


if __name__ == "__main__":
    unittest.main()
