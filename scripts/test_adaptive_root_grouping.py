from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import unittest

from elliott_ai.adaptive_market_data import NativeOHLCVBundle
from elliott_ai.adaptive_recount import (
    AdaptiveRecountCoordinator,
    AdaptiveRootHypothesis,
    AdaptiveWaveCandidate,
    CandidateScreenStatus,
    adaptive_root_group_classification_schema_for_packet,
    build_adaptive_analysis_scope,
    build_candidate_scoped_evidence,
    build_root_group_classification_packet,
    build_root_grouping_packet,
    build_root_segmentation_packet,
    parse_root_grouping_output,
    parse_root_segmentation_output,
    screen_adaptive_hypothesis,
)
from elliott_ai.adaptive_reporting import render_adaptive_recount_report
from elliott_ai.adaptive_structure import (
    AdaptiveAnalysisScope,
    AnalysisBoundaryKind,
    AnalysisScopeBoundary,
    GroupClassification,
    PriceExtremumType,
    PriceSegmentCandidate,
    PriceSegmentKind,
    PriceSegmentationHypothesis,
    RootStructuralGroupingHypothesis,
    SegmentCompletionState,
    StructuralSegmentGroup,
)
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateGraphRole,
    PivotPriceField,
    PivotType,
    TypedPivot,
)

from scripts.test_adaptive_recount import STAMP, bundle_for_manifest, plan_and_manifest
import scripts.test_adaptive_root_stages as root_stage_fixtures


def _boundary(
    boundary_id: str,
    kind: AnalysisBoundaryKind,
    timestamp_utc: str,
    price: float,
    *,
    extremum_type: PriceExtremumType | None = None,
) -> AnalysisScopeBoundary:
    pivot_id = f"pivot:{boundary_id}" if kind is AnalysisBoundaryKind.CATALOG_PIVOT else None
    return AnalysisScopeBoundary(
        boundary_id=boundary_id,
        kind=kind,
        timestamp_utc=timestamp_utc,
        price=price,
        source_pivot_id=pivot_id,
        source_bar_hash=canonical_sha256({"boundary": boundary_id}),
        is_elliott_pivot=pivot_id is not None,
        extremum_type=extremum_type,
    )


class FakeGroupedRootProvider:
    name = "fake-grouped-root-provider"
    model = "fixture-model"

    def __init__(self) -> None:
        self.calls: list[tuple[str, CandidateGraphRole, dict]] = []

    @staticmethod
    def _assert_blind(packet: dict) -> None:
        blind = packet["blind_isolation"]
        assert blind["strict"] is True
        assert blind["peer_candidate_available"] is False
        assert blind["prior_symbol_counts_available"] is False
        assert blind["accepted_cases_available"] is False
        assert blind["forecasts_outcomes_lessons_available"] is False

    def generate_root_segmentation(self, *, role, packet, output_schema):
        self.calls.append(("segmentation", role, packet))
        self._assert_blind(packet)
        boundaries = packet["available_boundaries"]
        catalog = [item for item in boundaries if item["kind"] == "catalog_pivot"]
        selected = (catalog[len(catalog) // 4], catalog[len(catalog) // 2], catalog[(len(catalog) * 3) // 4])
        ids = (boundaries[0]["boundary_id"], *(item["boundary_id"] for item in selected), boundaries[-1]["boundary_id"])
        return {
            "segmentation_id": f"{role.value}-atomic-segmentation",
            "segments": [
                {
                    "segment_id": f"{role.value}-s0",
                    "start_boundary_id": ids[0],
                    "end_boundary_id": ids[1],
                    "completion_state": "closed",
                    "segment_kind": "boundary_context",
                },
                {
                    "segment_id": f"{role.value}-s1",
                    "start_boundary_id": ids[1],
                    "end_boundary_id": ids[2],
                    "completion_state": "closed",
                    "segment_kind": "continuation_interval",
                },
                {
                    "segment_id": f"{role.value}-s2",
                    "start_boundary_id": ids[2],
                    "end_boundary_id": ids[3],
                    "completion_state": "closed",
                    "segment_kind": "continuation_interval",
                },
                {
                    "segment_id": f"{role.value}-s3",
                    "start_boundary_id": ids[3],
                    "end_boundary_id": ids[4],
                    "completion_state": "active",
                    "segment_kind": "unresolved_interval",
                },
            ],
        }

    def generate_root_grouping(self, *, role, packet, output_schema):
        self.calls.append(("grouping", role, packet))
        self._assert_blind(packet)
        segments = packet["segmentation"]["segments"]
        selected = segments[1:3] if role is CandidateGraphRole.PRIMARY else segments[1:2]
        unresolved = [item["segment_id"] for item in segments if item not in selected]
        return {
            "grouping_id": f"{role.value}-grouping",
            "groups": [
                {
                    "group_id": f"{role.value}-g1",
                    "source_segment_ids": [item["segment_id"] for item in selected],
                    "start_boundary_id": selected[0]["start_boundary"]["boundary_id"],
                    "end_boundary_id": selected[-1]["end_boundary"]["boundary_id"],
                    "completion_state": "closed",
                }
            ],
            "unresolved_segment_ids": unresolved,
        }

    def classify_root_groups(self, *, role, packet, output_schema):
        self.calls.append(("classification", role, packet))
        self._assert_blind(packet)
        group = packet["grouping"]["groups"][0]
        family = "combination" if role is CandidateGraphRole.PRIMARY else "zigzag"
        wave_id = f"{role.value}-grouped-parent"
        evidence_ids = [
            item["evidence_id"]
            for item in packet["candidate_scoped_soft_evidence"]
            if item["segment_id"] in set(group["source_segment_ids"])
        ]
        return {
            "hypothesis_id": f"{role.value}-grouped-hypothesis",
            "root_timeframe": packet["price_evidence"][0]["bundle_identity"]["timeframe"],
            "nodes": [
                {
                    "wave_id": wave_id,
                    "parent_wave_id": None,
                    "degree": "Primary",
                    "sequence_position": "W" if family == "combination" else "A",
                    "timeframe": "daily",
                    "direction": "down",
                    "declared_family": family,
                    "completion_state": "completed",
                    "start_pivot_id": group["start_boundary"]["source_pivot_id"],
                    "end_pivot_id": group["end_boundary"]["source_pivot_id"],
                    "invalidation": None,
                    "child_wave_ids": [],
                    "source_group_id": group["group_id"],
                    "source_segment_ids": group["source_segment_ids"],
                    "diagonal_declaration": None,
                }
            ],
            "group_classifications": [
                {
                    "group_id": group["group_id"],
                    "classification": family,
                    "wave_id": wave_id,
                    "evidence_ids": evidence_ids,
                }
            ],
            "active_wave_id": None,
            "confirmation_conditions": ["Require native lower-timeframe subdivision proof."],
            "unresolved_questions": ["Direct children remain unproven."],
        }


class DuplicateGroupedRootProvider(FakeGroupedRootProvider):
    def generate_root_grouping(self, *, role, packet, output_schema):
        if role is CandidateGraphRole.PRIMARY:
            return super().generate_root_grouping(
                role=role, packet=packet, output_schema=output_schema
            )
        self.calls.append(("grouping", role, packet))
        self._assert_blind(packet)
        segments = packet["segmentation"]["segments"]
        selected = segments[1:3]
        return {
            "grouping_id": "alternative-renamed-grouping",
            "groups": [
                {
                    "group_id": "alternative-renamed-group",
                    "source_segment_ids": [item["segment_id"] for item in selected],
                    "start_boundary_id": selected[0]["start_boundary"]["boundary_id"],
                    "end_boundary_id": selected[-1]["end_boundary"]["boundary_id"],
                    "completion_state": "closed",
                }
            ],
            "unresolved_segment_ids": [
                item["segment_id"] for item in segments if item not in selected
            ],
        }

    def classify_root_groups(self, *, role, packet, output_schema):
        result = super().classify_root_groups(
            role=role, packet=packet, output_schema=output_schema
        )
        if role is CandidateGraphRole.ALTERNATIVE:
            result["nodes"][0]["declared_family"] = "combination"
            result["nodes"][0]["sequence_position"] = "W"
            result["group_classifications"][0]["classification"] = "combination"
        return result


class NoCandidateGroupedRootProvider(FakeGroupedRootProvider):
    def generate_root_grouping(self, *, role, packet, output_schema):
        self.calls.append(("grouping", role, packet))
        self._assert_blind(packet)
        return {
            "grouping_id": f"{role.value}-no-candidate-grouping",
            "groups": [],
            "unresolved_segment_ids": [
                item["segment_id"] for item in packet["segmentation"]["segments"]
            ],
        }

    def classify_root_groups(self, *, role, packet, output_schema):
        self.calls.append(("classification", role, packet))
        self._assert_blind(packet)
        return {
            "hypothesis_id": f"{role.value}-no-candidate",
            "root_timeframe": packet["price_evidence"][0]["bundle_identity"]["timeframe"],
            "nodes": [],
            "group_classifications": [],
            "active_wave_id": None,
            "confirmation_conditions": [],
            "unresolved_questions": ["No coherent contiguous grouping was proposed."],
        }


class AtomicSegmentContractTests(unittest.TestCase):
    def test_closed_atomic_swing_requires_alternating_extrema(self) -> None:
        low_1 = _boundary(
            "low-1", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-05-28T20:00:00Z", 99.61,
            extremum_type=PriceExtremumType.LOW,
        )
        low_2 = _boundary(
            "low-2", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-06-02T20:00:00Z", 80.00,
            extremum_type=PriceExtremumType.LOW,
        )

        with self.assertRaisesRegex(ValueError, "alternate high and low extrema"):
            PriceSegmentCandidate(
                "same-low",
                low_1,
                low_2,
                SegmentCompletionState.CLOSED,
                PriceSegmentKind.ATOMIC_SWING,
            )

    def test_same_extremum_continuation_interval_is_explicitly_valid(self) -> None:
        low_1 = _boundary(
            "low-1", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-05-28T20:00:00Z", 99.61,
            extremum_type=PriceExtremumType.LOW,
        )
        low_2 = _boundary(
            "low-2", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-06-02T20:00:00Z", 80.00,
            extremum_type=PriceExtremumType.LOW,
        )
        segment = PriceSegmentCandidate(
            "continuation",
            low_1,
            low_2,
            SegmentCompletionState.CLOSED,
            PriceSegmentKind.CONTINUATION_INTERVAL,
        )
        self.assertEqual(segment.segment_kind, PriceSegmentKind.CONTINUATION_INTERVAL)

    def test_structural_group_cannot_skip_an_intervening_segment(self) -> None:
        start = _boundary(
            "start", AnalysisBoundaryKind.WINDOW_START, "2026-05-27T20:00:00Z", 149.72
        )
        low = _boundary(
            "low", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-05-28T20:00:00Z", 99.61,
            extremum_type=PriceExtremumType.LOW,
        )
        high = _boundary(
            "high", AnalysisBoundaryKind.CATALOG_PIVOT, "2026-05-29T20:00:00Z", 107.60,
            extremum_type=PriceExtremumType.HIGH,
        )
        as_of = _boundary(
            "as-of", AnalysisBoundaryKind.AS_OF_OBSERVATION, "2026-06-01T20:00:00Z", 64.39
        )
        scope = AdaptiveAnalysisScope.create(
            scope_id="",
            symbol="NASDAQ:RKLB",
            timeframe="daily",
            window_start=start,
            as_of_observation=as_of,
            analysis_cutoff_utc=as_of.timestamp_utc,
            source_bundle_hashes=(canonical_sha256("skip-fixture"),),
        )
        segmentation = PriceSegmentationHypothesis.create(
            segmentation_id="skip-segmentation",
            role="primary",
            analysis_scope_hash=scope.content_hash,
            segments=(
                PriceSegmentCandidate("S1", start, low, "closed", "boundary_context"),
                PriceSegmentCandidate("S2", low, high, "closed", "atomic_swing"),
                PriceSegmentCandidate("S3", high, as_of, "active", "unresolved_interval"),
            ),
            provider="fixture",
            model=None,
            generated_at_utc=as_of.timestamp_utc,
        )
        grouping = RootStructuralGroupingHypothesis.create(
            grouping_id="skip-grouping",
            role="primary",
            segmentation_content_hash=segmentation.content_hash,
            groups=(
                StructuralSegmentGroup("G1", ("S1", "S3"), start, as_of, "active"),
            ),
            unresolved_segment_ids=("S2",),
            provider="fixture",
            model=None,
            generated_at_utc=as_of.timestamp_utc,
        )
        with self.assertRaisesRegex(ValueError, "ordered and contiguous"):
            grouping.validate_against(segmentation)


class RootGroupingWorkflowTests(unittest.TestCase):
    @staticmethod
    def _wavy_bundle(base: NativeOHLCVBundle) -> NativeOHLCVBundle:
        return root_stage_fixtures.AdaptiveRootStageTests._wavy_bundle(base)

    def test_grouped_flow_is_blind_and_creates_multi_segment_unproven_parents(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = self._wavy_bundle(bundle_for_manifest(manifest, trace.plan.provider_capabilities))
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        provider = FakeGroupedRootProvider()

        trace = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=provider,
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )

        self.assertEqual(
            [(operation, role.value) for operation, role, _ in provider.calls],
            [
                ("segmentation", "primary"),
                ("grouping", "primary"),
                ("classification", "primary"),
                ("segmentation", "alternative"),
                ("grouping", "alternative"),
                ("classification", "alternative"),
            ],
        )
        primary = trace.root_pair.primary
        alternative = trace.root_pair.alternative
        self.assertEqual(len(primary.nodes[0].source_segment_ids), 2)
        self.assertEqual(len(alternative.nodes[0].source_segment_ids), 1)
        self.assertEqual(primary.root_grouping.role, "primary")
        self.assertEqual(alternative.root_grouping.role, "alternative")
        self.assertEqual(primary.root_grouping.content_hash, primary.nodes[0].source_grouping_hash)
        self.assertEqual(primary.segment_classifications, ())
        self.assertEqual(primary.candidate_state, "candidate")
        self.assertEqual(trace.root_pair.primary_screen.status, CandidateScreenStatus.SURVIVING_UNPROVEN)
        self.assertIn(
            "completed_parent_subdivision_unproven",
            " ".join(trace.root_pair.primary_screen.unresolved_requirements),
        )
        pending = coordinator.pending_proof_targets(trace, role=CandidateGraphRole.PRIMARY)
        self.assertEqual([item["parent_wave_id"] for item in pending], [primary.nodes[0].wave_id])
        planned_4h = coordinator.plan_parent_subdivision(
            trace,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=primary.nodes[0].wave_id,
            bundles=(bundle,),
            requested_timeframe="4h",
            created_at_utc=STAMP,
            source_interface=trace.plan.provider_capabilities.source_interface,
        )
        planned_alternative_4h = coordinator.plan_parent_subdivision(
            trace,
            role=CandidateGraphRole.ALTERNATIVE,
            parent_wave_id=alternative.nodes[0].wave_id,
            bundles=(bundle,),
            requested_timeframe="4h",
            created_at_utc=STAMP,
            source_interface=trace.plan.provider_capabilities.source_interface,
        )
        self.assertEqual(planned_4h.data_manifests[-1].canonical_timeframe, "4h")
        self.assertEqual(
            planned_alternative_4h.data_manifests[-1].canonical_timeframe,
            "4h",
        )
        report = render_adaptive_recount_report(trace)
        self.assertIn("Grouped wave candidates may span several connected Stage-A segments", report)
        self.assertIn("overlapping groups are alternative proposals", report)
        self.assertIn("unproven", report)
        self.assertNotIn("| verified |", report)

        primary_group_packet = next(
            packet for operation, role, packet in provider.calls
            if operation == "grouping" and role is CandidateGraphRole.PRIMARY
        )
        alternative_group_packet = next(
            packet for operation, role, packet in provider.calls
            if operation == "grouping" and role is CandidateGraphRole.ALTERNATIVE
        )
        self.assertNotIn("primary", str(alternative_group_packet.get("peer_candidate", "")))
        self.assertEqual(
            primary_group_packet["analysis_scope"]["content_hash"],
            alternative_group_packet["analysis_scope"]["content_hash"],
        )

    def test_generated_ids_cannot_make_a_duplicate_grouping_materially_distinct(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = self._wavy_bundle(bundle_for_manifest(manifest, trace.plan.provider_capabilities))
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        with self.assertRaisesRegex(ValueError, "duplicates the Primary"):
            coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=DuplicateGroupedRootProvider(),
                allow_model_calls=True,
                generated_at_utc=STAMP,
            )

    def test_two_empty_blind_searches_finish_as_no_candidate_not_duplicate_error(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = self._wavy_bundle(bundle_for_manifest(manifest, trace.plan.provider_capabilities))
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        result = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=NoCandidateGroupedRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        self.assertEqual(result.root_pair.primary_screen.status, CandidateScreenStatus.NO_CANDIDATE)
        self.assertEqual(result.root_pair.alternative_screen.status, CandidateScreenStatus.NO_CANDIDATE)
        self.assertEqual(result.stop_reason.value, "no_valid_candidate")

    def test_group_classification_schema_binds_exact_contiguous_segment_lineage(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = self._wavy_bundle(bundle_for_manifest(manifest, trace.plan.provider_capabilities))
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        provider = FakeGroupedRootProvider()
        segmentation = parse_root_segmentation_output(
            provider.generate_root_segmentation(
                role=CandidateGraphRole.PRIMARY,
                packet=build_root_segmentation_packet(
                    trace.plan, (bundle,), role=CandidateGraphRole.PRIMARY, scope=scope
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
        evidence = build_candidate_scoped_evidence(trace.plan, (bundle,), segmentation)
        grouping_packet = build_root_grouping_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            segmentation=segmentation,
            expected_degree=manifest.expected_child_degree,
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
        packet = build_root_group_classification_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            segmentation=segmentation,
            grouping=grouping,
            evidence=evidence,
            expected_degree=manifest.expected_child_degree,
        )
        schema = adaptive_root_group_classification_schema_for_packet(packet)
        variant = schema["properties"]["nodes"]["items"]["anyOf"][0]
        group = grouping.groups[0]
        self.assertEqual(
            variant["properties"]["source_group_id"]["enum"], [group.group_id]
        )
        self.assertEqual(
            variant["properties"]["source_segment_ids"]["minItems"],
            len(group.source_segment_ids),
        )
        self.assertEqual(
            variant["properties"]["start_pivot_id"]["enum"],
            [group.start_boundary.source_pivot_id],
        )
        self.assertEqual(
            variant["properties"]["end_pivot_id"]["enum"],
            [group.end_boundary.source_pivot_id],
        )

    def test_exact_rklb_anchor_shape_supports_s2_through_s6_group_without_claiming_proof(self) -> None:
        prices = (149.72, 99.61, 80.00, 107.60, 64.51, 72.94, 58.20, 86.83, 64.39)
        kinds = (
            None,
            PriceExtremumType.LOW,
            PriceExtremumType.LOW,
            PriceExtremumType.HIGH,
            PriceExtremumType.LOW,
            PriceExtremumType.HIGH,
            PriceExtremumType.LOW,
            PriceExtremumType.HIGH,
            None,
        )
        boundaries = tuple(
            _boundary(
                f"rklb-b{index}",
                AnalysisBoundaryKind.WINDOW_START if index == 0 else AnalysisBoundaryKind.AS_OF_OBSERVATION if index == 8 else AnalysisBoundaryKind.CATALOG_PIVOT,
                f"2026-{5 + (index // 4):02d}-{27 + (index % 4):02d}T20:00:00Z",
                price,
                extremum_type=kinds[index],
            )
            for index, price in enumerate(prices)
        )
        segment_kinds = (
            PriceSegmentKind.BOUNDARY_CONTEXT,
            PriceSegmentKind.CONTINUATION_INTERVAL,
            PriceSegmentKind.ATOMIC_SWING,
            PriceSegmentKind.ATOMIC_SWING,
            PriceSegmentKind.ATOMIC_SWING,
            PriceSegmentKind.ATOMIC_SWING,
            PriceSegmentKind.ATOMIC_SWING,
            PriceSegmentKind.UNRESOLVED_INTERVAL,
        )
        segments = tuple(
            PriceSegmentCandidate(
                f"S{index + 1}",
                boundaries[index],
                boundaries[index + 1],
                SegmentCompletionState.ACTIVE if index == 7 else SegmentCompletionState.CLOSED,
                segment_kinds[index],
            )
            for index in range(8)
        )
        scope = AdaptiveAnalysisScope.create(
            scope_id="",
            symbol="NASDAQ:RKLB",
            timeframe="daily",
            window_start=boundaries[0],
            as_of_observation=boundaries[-1],
            analysis_cutoff_utc=boundaries[-1].timestamp_utc,
            source_bundle_hashes=(canonical_sha256("rklb-fixture"),),
        )
        segmentation = PriceSegmentationHypothesis.create(
            segmentation_id="rklb-segmentation",
            role="primary",
            analysis_scope_hash=scope.content_hash,
            segments=segments,
            provider="fixture",
            model=None,
            generated_at_utc=boundaries[-1].timestamp_utc,
        )
        grouping = RootStructuralGroupingHypothesis.create(
            grouping_id="rklb-primary-groups",
            role="primary",
            segmentation_content_hash=segmentation.content_hash,
            groups=(
                StructuralSegmentGroup(
                    group_id="G-S2-S4",
                    source_segment_ids=("S2", "S3", "S4"),
                    start_boundary=boundaries[1],
                    end_boundary=boundaries[4],
                    completion_state=SegmentCompletionState.CLOSED,
                ),
                StructuralSegmentGroup(
                    group_id="G-S2-S6",
                    source_segment_ids=("S2", "S3", "S4", "S5", "S6"),
                    start_boundary=boundaries[1],
                    end_boundary=boundaries[6],
                    completion_state=SegmentCompletionState.CLOSED,
                ),
                StructuralSegmentGroup(
                    group_id="G-S3-S7",
                    source_segment_ids=("S3", "S4", "S5", "S6", "S7"),
                    start_boundary=boundaries[2],
                    end_boundary=boundaries[7],
                    completion_state=SegmentCompletionState.CLOSED,
                ),
            ),
            unresolved_segment_ids=("S1", "S8"),
            provider="fixture",
            model=None,
            generated_at_utc=boundaries[-1].timestamp_utc,
        )
        grouping.validate_against(segmentation)
        classifications = (
            GroupClassification(
                group_id="G-S2-S4",
                classification="unresolved",
                wave_id=None,
                evidence_ids=(),
            ),
            GroupClassification(
                group_id="G-S2-S6",
                classification="combination",
                wave_id="rklb-parent-candidate",
                evidence_ids=(),
            ),
            GroupClassification(
                group_id="G-S3-S7",
                classification="unresolved",
                wave_id=None,
                evidence_ids=(),
            ),
        )
        start_pivot = TypedPivot.create(
            pivot_id=boundaries[1].source_pivot_id,
            timestamp_utc=boundaries[1].timestamp_utc,
            price=boundaries[1].price,
            price_field=PivotPriceField.LOW,
            pivot_type=PivotType.LOW,
            timeframe="daily",
            source_window_hash=canonical_sha256("rklb-window"),
            source_bar_hash=boundaries[1].source_bar_hash,
        )
        end_pivot = TypedPivot.create(
            pivot_id=boundaries[6].source_pivot_id,
            timestamp_utc=boundaries[6].timestamp_utc,
            price=boundaries[6].price,
            price_field=PivotPriceField.LOW,
            pivot_type=PivotType.LOW,
            timeframe="daily",
            source_window_hash=canonical_sha256("rklb-window"),
            source_bar_hash=boundaries[6].source_bar_hash,
        )
        node = AdaptiveWaveCandidate(
            wave_id="rklb-parent-candidate",
            parent_wave_id=None,
            degree="Intermediate",
            sequence_position="W",
            timeframe="daily",
            direction="down",
            declared_family="combination",
            completion_state="completed",
            start_pivot=start_pivot,
            end_pivot=end_pivot,
            invalidation=None,
            child_wave_ids=(),
            source_segment_ids=grouping.groups[1].source_segment_ids,
            source_group_id=grouping.groups[1].group_id,
            source_grouping_hash=grouping.content_hash,
        )
        hypothesis = AdaptiveRootHypothesis.create(
            hypothesis_id="rklb-primary-grouped",
            role=CandidateGraphRole.PRIMARY,
            plan_id="rklb-fixture-plan",
            plan_content_hash=canonical_sha256("rklb-plan"),
            symbol="NASDAQ:RKLB",
            analysis_start_utc=boundaries[0].timestamp_utc,
            analysis_cutoff_utc=boundaries[-1].timestamp_utc,
            root_timeframe="daily",
            nodes=(node,),
            active_wave_id=None,
            confirmation_conditions=("Require native lower-timeframe child proof.",),
            unresolved_questions=("Internal family remains unproven.",),
            source_bundle_hashes=scope.source_bundle_hashes,
            rules_pack_content_hash=canonical_sha256("rklb-rules"),
            provider="fixture",
            model=None,
            generated_at_utc=boundaries[-1].timestamp_utc,
            analysis_scope=scope,
            segmentation=segmentation,
            root_grouping=grouping,
            group_classifications=classifications,
        )
        screen = screen_adaptive_hypothesis(
            hypothesis,
            screened_at_utc=boundaries[-1].timestamp_utc,
        )

        self.assertEqual(grouping.groups[1].source_segment_ids, ("S2", "S3", "S4", "S5", "S6"))
        self.assertEqual(node.degree, "Intermediate")
        self.assertEqual(classifications[1].wave_id, "rklb-parent-candidate")
        self.assertEqual(len(grouping.groups), 3)
        self.assertEqual(grouping.candidate_state, "candidate")
        self.assertEqual(screen.status, CandidateScreenStatus.SURVIVING_UNPROVEN)
        self.assertIn(
            "completed_parent_subdivision_unproven",
            " ".join(screen.unresolved_requirements),
        )
        self.assertNotIn("verified", grouping.to_dict())

        no_grouping = RootStructuralGroupingHypothesis.create(
            grouping_id="rklb-no-candidate-groups",
            role="primary",
            segmentation_content_hash=segmentation.content_hash,
            groups=(),
            unresolved_segment_ids=tuple(item.segment_id for item in segments),
            provider="fixture",
            model=None,
            generated_at_utc=boundaries[-1].timestamp_utc,
        )
        no_candidate = AdaptiveRootHypothesis.create(
            hypothesis_id="rklb-no-candidate",
            role=CandidateGraphRole.PRIMARY,
            plan_id="rklb-fixture-plan",
            plan_content_hash=canonical_sha256("rklb-plan"),
            symbol="NASDAQ:RKLB",
            analysis_start_utc=boundaries[0].timestamp_utc,
            analysis_cutoff_utc=boundaries[-1].timestamp_utc,
            root_timeframe="daily",
            nodes=(),
            active_wave_id=None,
            confirmation_conditions=(),
            unresolved_questions=("No group could yet be classified.",),
            source_bundle_hashes=scope.source_bundle_hashes,
            rules_pack_content_hash=canonical_sha256("rklb-rules"),
            provider="fixture",
            model=None,
            generated_at_utc=boundaries[-1].timestamp_utc,
            analysis_scope=scope,
            segmentation=segmentation,
            root_grouping=no_grouping,
            group_classifications=(),
        )
        no_candidate_screen = screen_adaptive_hypothesis(
            no_candidate,
            screened_at_utc=boundaries[-1].timestamp_utc,
        )
        self.assertEqual(no_candidate_screen.status, CandidateScreenStatus.NO_CANDIDATE)
        self.assertNotEqual(no_candidate_screen.status, screen.status)


if __name__ == "__main__":
    unittest.main()
