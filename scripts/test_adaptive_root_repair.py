from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from elliott_ai.adaptive_recount import (
    ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA,
    AdaptiveRecountBudget,
    AdaptiveRecountCoordinator,
    AdaptiveRecountStatus,
    AdaptiveStopReason,
    RootStageFailureCode,
    RootStageParseError,
    build_adaptive_analysis_scope,
    build_root_segmentation_packet,
    create_adaptive_recount_plan,
    parse_root_segmentation_output,
)
from elliott_ai.adaptive_reporting import render_adaptive_recount_report
from elliott_ai.adaptive_structure import PriceSegmentationHypothesis
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import CandidateGraphRole

from scripts.test_adaptive_recount import (
    CUTOFF,
    OneInvalidRootProvider,
    STAMP,
    START,
    bundle_for_manifest,
    capabilities,
    plan_and_manifest,
)
from scripts.test_adaptive_root_grouping import (
    FakeGroupedRootProvider,
    NoCandidateGroupedRootProvider,
)
from scripts.test_adaptive_root_stages import AdaptiveRootStageTests


def _path_response(packet: dict, role: CandidateGraphRole, *, malformed: bool = False, tag: str = "ok") -> dict:
    boundaries = packet["available_boundaries"]
    catalogs = [item for item in boundaries if item["kind"] == "catalog_pivot"]
    selected = [
        catalogs[len(catalogs) // 4],
        catalogs[len(catalogs) // 2],
        catalogs[(len(catalogs) * 3) // 4],
    ]
    if malformed:
        selected[1], selected[2] = selected[2], selected[1]
    boundary_path = [
        boundaries[0]["boundary_id"],
        *(item["boundary_id"] for item in selected),
        boundaries[-1]["boundary_id"],
    ]
    kinds = (
        "boundary_context",
        "continuation_interval",
        "continuation_interval",
        "unresolved_interval",
    )
    return {
        "segmentation_id": f"{role.value}-{tag}-segmentation",
        "boundary_path": boundary_path,
        "segments": [
            {
                "segment_id": f"{role.value}-{tag}-s{index}",
                "segment_kind": kind,
                "completion_state": "active" if index == 3 else "closed",
            }
            for index, kind in enumerate(kinds)
        ],
    }


class RepairingGroupedProvider(FakeGroupedRootProvider):
    def __init__(
        self,
        *,
        fail_roles: frozenset[CandidateGraphRole] = frozenset(),
        duplicate_repairs: bool = False,
        malformed_schema_roles: frozenset[CandidateGraphRole] = frozenset(),
    ) -> None:
        super().__init__()
        self.fail_roles = fail_roles
        self.duplicate_repairs = duplicate_repairs
        self.malformed_schema_roles = malformed_schema_roles
        self.segmentation_attempts = {
            CandidateGraphRole.PRIMARY: 0,
            CandidateGraphRole.ALTERNATIVE: 0,
        }

    def generate_root_segmentation(self, *, role, packet, output_schema):
        self.calls.append(("segmentation", role, packet))
        self._assert_blind(packet)
        self.segmentation_attempts[role] += 1
        attempt = self.segmentation_attempts[role]
        if role in self.malformed_schema_roles:
            return {"segmentation_id": f"{role.value}-bad-schema", "segments": []}
        if role in self.fail_roles:
            return _path_response(
                packet,
                role,
                malformed=True,
                tag=f"bad-{attempt}" if self.duplicate_repairs else f"bad-{attempt}",
            )
        if role is CandidateGraphRole.PRIMARY and attempt == 1:
            return _path_response(packet, role, malformed=True, tag="bad-1")
        return _path_response(packet, role, tag=f"fixed-{attempt}")


class AlwaysValidPathProvider(FakeGroupedRootProvider):
    def generate_root_segmentation(self, *, role, packet, output_schema):
        self.calls.append(("segmentation", role, packet))
        self._assert_blind(packet)
        return _path_response(packet, role, tag="valid")


class RepairingB1Provider(AlwaysValidPathProvider):
    def __init__(self) -> None:
        super().__init__()
        self.grouping_attempts = {
            CandidateGraphRole.PRIMARY: 0,
            CandidateGraphRole.ALTERNATIVE: 0,
        }

    def generate_root_grouping(self, *, role, packet, output_schema):
        self.grouping_attempts[role] += 1
        attempt = self.grouping_attempts[role]
        if role is CandidateGraphRole.PRIMARY and attempt == 1:
            self.calls.append(("grouping", role, packet))
            self._assert_blind(packet)
            segments = packet["segmentation"]["segments"]
            selected = (segments[0], segments[2])
            return {
                "grouping_id": "primary-malformed-b1",
                "groups": [
                    {
                        "group_id": "primary-malformed-group",
                        "source_segment_ids": [
                            item["segment_id"] for item in selected
                        ],
                        "start_boundary_id": selected[0]["start_boundary"][
                            "boundary_id"
                        ],
                        "end_boundary_id": selected[-1]["end_boundary"][
                            "boundary_id"
                        ],
                        "completion_state": "closed",
                    }
                ],
                "unresolved_segment_ids": [
                    item["segment_id"]
                    for item in segments
                    if item not in selected
                ],
            }
        return super().generate_root_grouping(
            role=role,
            packet=packet,
            output_schema=output_schema,
        )


class RepairingB2Provider(AlwaysValidPathProvider):
    def __init__(self) -> None:
        super().__init__()
        self.classification_attempts = {
            CandidateGraphRole.PRIMARY: 0,
            CandidateGraphRole.ALTERNATIVE: 0,
        }

    def classify_root_groups(self, *, role, packet, output_schema):
        self.classification_attempts[role] += 1
        result = super().classify_root_groups(
            role=role,
            packet=packet,
            output_schema=output_schema,
        )
        if (
            role is CandidateGraphRole.PRIMARY
            and self.classification_attempts[role] == 1
        ):
            result["group_classifications"][0]["wave_id"] = "unknown-wave-id"
        return result


class AdaptiveRootRepairTests(unittest.TestCase):
    @staticmethod
    def _prepared(*, maximum_candidate_graphs: int = 20):
        if maximum_candidate_graphs == 20:
            coordinator, trace, manifest = plan_and_manifest()
        else:
            provider_capabilities = capabilities(
                "monthly", "weekly", "daily", "12h", "4h", "1h", "30m"
            )
            plan = create_adaptive_recount_plan(
                symbol="NASDAQ:RKLB",
                analysis_start_utc=START,
                context_start_utc=START,
                analysis_cutoff_utc=CUTOFF,
                market_data_provider=provider_capabilities.provider,
                provider_capabilities=provider_capabilities,
                created_at_utc=STAMP,
                budgets=AdaptiveRecountBudget(
                    maximum_candidate_graphs=maximum_candidate_graphs
                ),
            )
            coordinator = AdaptiveRecountCoordinator()
            trace = coordinator.initial_trace(plan)
            trace = coordinator.plan_initial_data(
                trace,
                created_at_utc=STAMP,
                source_interface=provider_capabilities.source_interface,
            )
            manifest = trace.data_manifests[0]
        bundle = AdaptiveRootStageTests._wavy_bundle(
            bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        )
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        return coordinator, trace, bundle

    def test_v4_step_schema_and_historical_v3_parser_create_connected_segments(self) -> None:
        coordinator, trace, bundle = self._prepared()
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        packet = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )
        self.assertEqual(
            [item["boundary_ordinal"] for item in packet["available_boundaries"]],
            list(range(len(packet["available_boundaries"]))),
        )
        step_properties = ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA[
            "properties"
        ]["steps"]["items"]["properties"]
        self.assertEqual(
            set(step_properties),
            {"end_boundary_id", "segment_kind"},
        )
        self.assertNotIn("boundary_path", ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA["properties"])
        self.assertNotIn("segments", ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA["properties"])
        parsed = parse_root_segmentation_output(
            _path_response(packet, CandidateGraphRole.PRIMARY),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            bundles=(bundle,),
            provider_name="fixture",
            provider_model=None,
            generated_at_utc=STAMP,
        )
        self.assertTrue(
            all(
                left.end_boundary.boundary_id == right.start_boundary.boundary_id
                for left, right in zip(parsed.segments, parsed.segments[1:])
            )
        )

    def test_reversed_path_is_rejected_without_sorting(self) -> None:
        coordinator, trace, bundle = self._prepared()
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        packet = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )
        raw = _path_response(packet, CandidateGraphRole.PRIMARY, malformed=True)
        original = tuple(raw["boundary_path"])
        with self.assertRaises(RootStageParseError) as raised:
            parse_root_segmentation_output(
                raw,
                role=CandidateGraphRole.PRIMARY,
                scope=scope,
                bundles=(bundle,),
                provider_name="fixture",
                provider_model=None,
                generated_at_utc=STAMP,
            )
        self.assertEqual(
            raised.exception.error_code,
            RootStageFailureCode.NON_INCREASING_PRICE_SEGMENT_BOUNDARIES,
        )
        self.assertEqual(tuple(raw["boundary_path"]), original)

    def test_legacy_v2_reversed_segment_remains_rejected(self) -> None:
        coordinator, trace, bundle = self._prepared()
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        packet = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )
        path = _path_response(packet, CandidateGraphRole.PRIMARY)
        boundary_path = path["boundary_path"]
        raw = {
            "segmentation_id": "legacy-v2-reversed",
            "segments": [
                {
                    "segment_id": "legacy-reversed-segment",
                    "start_boundary_id": boundary_path[2],
                    "end_boundary_id": boundary_path[1],
                    "segment_kind": "continuation_interval",
                    "completion_state": "active",
                }
            ],
        }
        original = json.loads(json.dumps(raw))
        with self.assertRaises(RootStageParseError) as raised:
            parse_root_segmentation_output(
                raw,
                role=CandidateGraphRole.PRIMARY,
                scope=scope,
                bundles=(bundle,),
                provider_name="fixture",
                provider_model=None,
                generated_at_utc=STAMP,
            )
        self.assertEqual(
            raised.exception.error_code,
            RootStageFailureCode.NON_INCREASING_PRICE_SEGMENT_BOUNDARIES,
        )
        self.assertEqual(raw, original)

    def test_repair_receives_only_typed_chronology_facts_and_succeeds(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingGroupedProvider()
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
            repair_packet = next(
                packet
                for operation, role, packet in provider.calls
                if operation == "segmentation"
                and role is CandidateGraphRole.PRIMARY
                and "root_stage_repair_context" in packet
            )
            context = repair_packet["root_stage_repair_context"]
            self.assertEqual(context["attempt_number"], 2)
            self.assertEqual(
                context["deterministic_failures"][0]["error_code"],
                "non_increasing_price_segment_boundaries",
            )
            self.assertIn("required_relation", context["deterministic_failures"][0])
            self.assertEqual(
                context["repair_instruction"],
                "return_materially_corrected_structured_output_with_a_new_semantic_signature",
            )
            self.assertNotIn("reasoning", json.dumps(context).lower())
            self.assertIsNotNone(result.root_pair.primary)
            self.assertIsNotNone(result.root_pair.alternative)
            self.assertEqual(result.root_stage_call_count, 7)
            self.assertEqual(len(result.root_stage_failures), 1)
            self.assertEqual(len(result.root_stage_diagnostics), 1)
            diagnostic = Path(result.root_stage_diagnostics[0].artifact_path)
            self.assertTrue(diagnostic.is_file())
            payload = json.loads(diagnostic.read_text(encoding="utf-8"))
            self.assertEqual(payload["artifact_kind"], "non_evidence_root_stage_diagnostic")
            self.assertNotIn("api_key", json.dumps(payload).lower())

    def test_duplicate_malformed_repairs_consume_three_attempts(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingGroupedProvider(
            fail_roles=frozenset({CandidateGraphRole.PRIMARY}),
            duplicate_repairs=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertEqual(provider.segmentation_attempts[CandidateGraphRole.PRIMARY], 3)
        primary_codes = [
            item.error_code
            for item in result.root_stage_failures
            if item.role is CandidateGraphRole.PRIMARY
        ]
        self.assertEqual(
            primary_codes,
            [
                RootStageFailureCode.NON_INCREASING_PRICE_SEGMENT_BOUNDARIES,
                RootStageFailureCode.DUPLICATE_ROOT_STAGE_CANDIDATE,
                RootStageFailureCode.DUPLICATE_ROOT_STAGE_CANDIDATE,
            ],
        )

    def test_primary_failure_does_not_block_or_contaminate_alternative(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingGroupedProvider(
            fail_roles=frozenset({CandidateGraphRole.PRIMARY}),
            duplicate_repairs=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertIsNone(result.root_pair.primary)
        self.assertIsNotNone(result.root_pair.alternative)
        alternative_first = next(
            packet
            for operation, role, packet in provider.calls
            if operation == "segmentation" and role is CandidateGraphRole.ALTERNATIVE
        )
        self.assertNotIn("root_stage_repair_context", alternative_first)
        self.assertEqual(result.stop_reason, AdaptiveStopReason.ONE_VALID_CANDIDATE)

    def test_alternative_failure_does_not_erase_primary(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingGroupedProvider(
            fail_roles=frozenset({CandidateGraphRole.ALTERNATIVE}),
            duplicate_repairs=True,
        )
        provider.segmentation_attempts[CandidateGraphRole.PRIMARY] = 1
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertIsNotNone(result.root_pair.primary)
        self.assertIsNone(result.root_pair.alternative)
        self.assertEqual(result.stop_reason, AdaptiveStopReason.ONE_VALID_CANDIDATE)

    def test_both_role_failures_are_contract_exhaustion_and_reported(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingGroupedProvider(
            fail_roles=frozenset(
                {CandidateGraphRole.PRIMARY, CandidateGraphRole.ALTERNATIVE}
            ),
            duplicate_repairs=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertIsNone(result.root_pair)
        self.assertEqual(
            result.status, AdaptiveRecountStatus.ROOT_CANDIDATE_GENERATION_FAILED
        )
        self.assertEqual(
            result.stop_reason, AdaptiveStopReason.ROOT_STAGE_CONTRACT_EXHAUSTED
        )
        report = render_adaptive_recount_report(result)
        self.assertIn("ROOT GENERATION CONTRACT FAILURE", report)
        self.assertIn("Primary Stage A exhausted", report)
        self.assertIn("Alternative Stage A exhausted", report)
        self.assertIn("**Alternative provider called:** yes", report)
        self.assertNotIn("No second structurally distinct candidate", report)

    def test_valid_empty_hypotheses_remain_no_market_candidate(self) -> None:
        coordinator, trace, bundle = self._prepared()
        result = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=NoCandidateGroupedRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        self.assertEqual(result.status, AdaptiveRecountStatus.UNRESOLVED)
        self.assertEqual(result.stop_reason, AdaptiveStopReason.NO_VALID_CANDIDATE)
        report = render_adaptive_recount_report(result)
        self.assertIn("NO MARKET CANDIDATE", report)
        self.assertNotIn("ROOT GENERATION CONTRACT FAILURE", report)

    def test_valid_candidate_hard_rule_rejection_has_distinct_report_state(self) -> None:
        coordinator, trace, bundle = self._prepared()
        result = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=OneInvalidRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        report = render_adaptive_recount_report(result)
        self.assertIn("CANDIDATE HARD-RULE REJECTION", report)
        self.assertNotIn("ROOT GENERATION CONTRACT FAILURE", report)

    def test_b1_domain_failure_receives_bounded_typed_repair(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingB1Provider()
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertEqual(provider.grouping_attempts[CandidateGraphRole.PRIMARY], 2)
        b1_failure = next(
            item
            for item in result.root_stage_failures
            if item.role is CandidateGraphRole.PRIMARY
            and item.stage.value == "stage_b1_structural_grouping"
        )
        self.assertEqual(
            b1_failure.error_code,
            RootStageFailureCode.NON_CONTIGUOUS_STRUCTURAL_GROUP,
        )
        repair_packet = next(
            packet
            for operation, role, packet in provider.calls
            if operation == "grouping"
            and role is CandidateGraphRole.PRIMARY
            and "root_stage_repair_context" in packet
        )
        self.assertEqual(
            repair_packet["root_stage_repair_context"]["deterministic_failures"][0][
                "error_code"
            ],
            "non_contiguous_structural_group",
        )

    def test_b2_domain_failure_receives_bounded_typed_repair(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingB2Provider()
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertEqual(
            provider.classification_attempts[CandidateGraphRole.PRIMARY],
            2,
        )
        b2_failure = next(
            item
            for item in result.root_stage_failures
            if item.role is CandidateGraphRole.PRIMARY
            and item.stage.value == "stage_b2_family_classification"
        )
        self.assertEqual(
            b2_failure.error_code,
            RootStageFailureCode.GROUPED_NODE_BOUNDARY_MISMATCH,
        )
        self.assertIsNotNone(result.root_pair.primary)

    def test_non_schema_output_is_not_retried_or_persisted(self) -> None:
        coordinator, trace, bundle = self._prepared()
        provider = RepairingGroupedProvider(
            malformed_schema_roles=frozenset({CandidateGraphRole.PRIMARY})
        )
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertEqual(provider.segmentation_attempts[CandidateGraphRole.PRIMARY], 1)
        primary_failure = next(
            item
            for item in result.root_stage_failures
            if item.role is CandidateGraphRole.PRIMARY
        )
        self.assertEqual(
            primary_failure.error_code, RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT
        )
        self.assertFalse(primary_failure.repairable)
        self.assertFalse(
            any(
                item.role is CandidateGraphRole.PRIMARY
                for item in result.root_stage_diagnostics
            )
        )

    def test_global_attempt_and_artifact_budget_is_not_exceeded(self) -> None:
        coordinator, trace, bundle = self._prepared(maximum_candidate_graphs=1)
        provider = RepairingGroupedProvider(
            fail_roles=frozenset({CandidateGraphRole.PRIMARY}),
            duplicate_repairs=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
                diagnostic_output_directory=Path(directory),
            )
        self.assertEqual(result.root_stage_call_count, 1)
        self.assertEqual(sum(provider.segmentation_attempts.values()), 1)
        self.assertLessEqual(len(result.root_stage_diagnostics), 1)
        report = render_adaptive_recount_report(result)
        self.assertIn("**Alternative provider called:** no", report)

    def test_historical_v2_segmentation_hash_round_trip_is_unchanged(self) -> None:
        coordinator, trace, bundle = self._prepared()
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        packet = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )
        parsed = parse_root_segmentation_output(
            _path_response(packet, CandidateGraphRole.PRIMARY),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            bundles=(bundle,),
            provider_name="fixture",
            provider_model=None,
            generated_at_utc=STAMP,
        )
        legacy = replace(
            parsed,
            schema_version="adaptive-price-segmentation-2.0.0",
            content_hash="",
        )
        legacy = replace(
            legacy,
            content_hash=canonical_sha256(
                {
                    key: value
                    for key, value in legacy.to_dict().items()
                    if key != "content_hash"
                }
            ),
        )
        serialized = legacy.to_dict()
        restored = PriceSegmentationHypothesis.from_dict(serialized)
        self.assertEqual(restored.content_hash, legacy.content_hash)
        self.assertEqual(restored.to_dict(), serialized)


if __name__ == "__main__":
    unittest.main()
