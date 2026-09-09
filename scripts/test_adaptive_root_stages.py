from __future__ import annotations

from datetime import datetime
import unittest

from elliott_ai.adaptive_recount import (
    AdaptiveRecountCoordinator,
    CandidateScreenStatus,
    adaptive_root_classification_schema_for_packet,
    build_adaptive_analysis_scope,
    build_candidate_scoped_evidence,
    build_root_classification_packet,
    build_root_segmentation_packet,
    parse_root_segmentation_output,
)
from elliott_ai.adaptive_market_data import NativeOHLCVBundle
from elliott_ai.adaptive_reporting import render_adaptive_recount_report
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import CandidateGraphRole, NativeCandle

from scripts.test_adaptive_recount import STAMP, bundle_for_manifest, plan_and_manifest


class FakeStagedRootProvider:
    name = "fake-staged-root-provider"
    model = "fixture-model"

    def __init__(self, *, same_degree_nested: bool = False) -> None:
        self.calls: list[tuple[str, CandidateGraphRole, dict]] = []
        self.same_degree_nested = same_degree_nested

    def generate_root_segmentation(self, *, role, packet, output_schema):
        self.calls.append(("segmentation", role, packet))
        boundaries = packet["available_boundaries"]
        catalog = [item for item in boundaries if item["kind"] == "catalog_pivot"]
        self.assert_isolated(packet)
        self.assert_price_only(packet)
        if len(catalog) < 6:
            raise AssertionError("Fixture requires enough deterministic catalog pivots.")
        if role is CandidateGraphRole.PRIMARY:
            left, right = catalog[len(catalog) // 3], catalog[(len(catalog) * 2) // 3]
        else:
            left, right = catalog[len(catalog) // 4], catalog[(len(catalog) * 3) // 4]
        return {
            "segmentation_id": f"{role.value}-segmentation",
            "segments": [
                {
                    "segment_id": f"{role.value}-context",
                    "start_boundary_id": boundaries[0]["boundary_id"],
                    "end_boundary_id": left["boundary_id"],
                    "completion_state": "closed",
                },
                {
                    "segment_id": f"{role.value}-closed",
                    "start_boundary_id": left["boundary_id"],
                    "end_boundary_id": right["boundary_id"],
                    "completion_state": "closed",
                },
                {
                    "segment_id": f"{role.value}-active",
                    "start_boundary_id": right["boundary_id"],
                    "end_boundary_id": boundaries[-1]["boundary_id"],
                    "completion_state": "active",
                },
            ],
        }

    def classify_root_segments(self, *, role, packet, output_schema):
        self.calls.append(("classification", role, packet))
        self.assert_isolated(packet)
        segments = packet["segmentation"]["segments"]
        evidence_by_segment = {
            item["segment_id"]: item["evidence_id"]
            for item in packet["candidate_scoped_soft_evidence"]
        }
        pivots = {
            item["pivot_id"]: item
            for bundle in packet["price_evidence"]
            for item in bundle["pivot_catalog"]
        }

        def pivot_id(boundary):
            value = boundary.get("source_pivot_id")
            if not value:
                raise AssertionError("Structural fixture boundaries must be catalog pivots.")
            return value

        closed = segments[1]
        active = segments[2]
        closed_start = pivot_id(closed["start_boundary"])
        closed_end = pivot_id(closed["end_boundary"])
        active_start = pivot_id(active["start_boundary"])
        as_of = datetime.fromisoformat(active["end_boundary"]["timestamp_utc"])
        active_start_time = datetime.fromisoformat(pivots[active_start]["timestamp_utc"])
        later = sorted(
            (
                item
                for item in pivots.values()
                if active_start_time < datetime.fromisoformat(item["timestamp_utc"]) < as_of
            ),
            key=lambda item: item["timestamp_utc"],
        )
        if not later:
            raise AssertionError("Fixture requires a post-segment catalog pivot before as-of.")
        active_end = later[-1]["pivot_id"]

        closed_id = f"{role.value}-closed-wave"
        active_id = f"{role.value}-active-wave"
        closed_family = "zigzag" if role is CandidateGraphRole.PRIMARY else "impulse"
        active_family = "combination" if role is CandidateGraphRole.PRIMARY else "flat"
        nodes = [
            self.node(
                wave_id=closed_id,
                segment_id=closed["segment_id"],
                position="A" if role is CandidateGraphRole.PRIMARY else "1",
                family=closed_family,
                completion="completed",
                start_id=closed_start,
                end_id=closed_end,
                pivots=pivots,
            ),
            self.node(
                wave_id=active_id,
                segment_id=active["segment_id"],
                position="B" if role is CandidateGraphRole.PRIMARY else "2",
                family=active_family,
                completion="active",
                start_id=active_start,
                end_id=active_end,
                pivots=pivots,
            ),
        ]
        if self.same_degree_nested:
            nodes[0]["child_wave_ids"] = [active_id]
            nodes[1]["parent_wave_id"] = closed_id
        return {
            "hypothesis_id": f"{role.value}-staged-hypothesis",
            "root_timeframe": packet["price_evidence"][0]["bundle_identity"]["timeframe"],
            "nodes": nodes,
            "segment_classifications": [
                {
                    "segment_id": segments[0]["segment_id"],
                    "classification": "previous_parent_wave_tail",
                    "wave_ids": [],
                    "evidence_ids": [evidence_by_segment[segments[0]["segment_id"]]],
                },
                {
                    "segment_id": closed["segment_id"],
                    "classification": "abc" if role is CandidateGraphRole.PRIMARY else "impulse",
                    "wave_ids": [closed_id],
                    "evidence_ids": [evidence_by_segment[closed["segment_id"]]],
                },
                {
                    "segment_id": active["segment_id"],
                    "classification": "unfinished_structure",
                    "wave_ids": [active_id],
                    "evidence_ids": [evidence_by_segment[active["segment_id"]]],
                },
            ],
            "active_wave_id": active_id,
            "confirmation_conditions": ["Require a completed lower-degree child graph."],
            "unresolved_questions": ["The final price segment remains active at the cutoff."],
        }

    @staticmethod
    def node(*, wave_id, segment_id, position, family, completion, start_id, end_id, pivots):
        direction = "up" if pivots[end_id]["price"] > pivots[start_id]["price"] else "down"
        return {
            "wave_id": wave_id,
            "parent_wave_id": None,
            "degree": "Primary",
            "sequence_position": position,
            "timeframe": "daily",
            "direction": direction,
            "declared_family": family,
            "completion_state": completion,
            "start_pivot_id": start_id,
            "end_pivot_id": end_id,
            "invalidation": None,
            "child_wave_ids": [],
            "source_segment_id": segment_id,
            "diagonal_declaration": None,
        }

    @staticmethod
    def assert_isolated(packet):
        blind = packet["blind_isolation"]
        assert blind["strict"] is True
        assert blind["peer_candidate_available"] is False
        assert blind["prior_symbol_counts_available"] is False

    @staticmethod
    def assert_price_only(packet):
        serialized_keys = {
            key
            for bundle in packet["price_evidence_only"]
            for candle in bundle["native_price_candles"]
            for key in candle
        }
        assert "volume" not in serialized_keys
        assert "rsi" not in serialized_keys
        assert "ewo" not in serialized_keys


class AdaptiveRootStageTests(unittest.TestCase):
    @staticmethod
    def _wavy_bundle(base: NativeOHLCVBundle) -> NativeOHLCVBundle:
        pattern = (0.0, 2.0, 5.0, 2.0, -1.0, 1.0)
        candles = []
        for index, source in enumerate(base.candles):
            center = 20.0 + (index * 0.04) + pattern[index % len(pattern)]
            candles.append(
                NativeCandle.create(
                    candle_id=source.candle_id,
                    timestamp_utc=source.timestamp_utc,
                    open=center - 0.2,
                    high=center + 0.8,
                    low=center - 0.8,
                    close=center + 0.2,
                    volume=1000.0 + index,
                )
            )
        values = base.to_dict()
        values.pop("content_hash")
        values.pop("file_hash")
        values["bundle_id"] = base.bundle_id + "-wavy"
        values["candles"] = tuple(candles)
        values["source_response_hash"] = canonical_sha256(
            [item.to_dict() for item in candles]
        )
        return NativeOHLCVBundle.create(**values)

    def _run(self, provider):
        coordinator, trace, manifest = plan_and_manifest()
        bundle = self._wavy_bundle(
            bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        )
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        return coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=provider,
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )

    def test_two_stage_root_flow_uses_four_blind_calls_and_multiple_segments(self) -> None:
        provider = FakeStagedRootProvider()
        trace = self._run(provider)

        self.assertEqual(
            [(operation, role.value) for operation, role, _ in provider.calls],
            [
                ("segmentation", "primary"),
                ("classification", "primary"),
                ("segmentation", "alternative"),
                ("classification", "alternative"),
            ],
        )
        primary = trace.root_pair.primary
        self.assertIsNotNone(primary.analysis_scope)
        self.assertEqual(len(primary.segmentation.segments), 3)
        self.assertEqual(primary.segmentation.segments[0].completion_state.value, "closed")
        self.assertEqual(primary.segmentation.segments[-1].completion_state.value, "active")
        self.assertTrue(all(item.source_segment_id for item in primary.nodes))
        self.assertNotIn(
            primary.analysis_scope.as_of_observation.timestamp_utc,
            {item.end_pivot.timestamp_utc for item in primary.nodes},
        )
        self.assertEqual(
            primary.segment_classifications[0].classification.value,
            "previous_parent_wave_tail",
        )
        self.assertTrue(primary.candidate_evidence)
        self.assertIn("rsi", primary.candidate_evidence[0].active_features)
        self.assertIn("ewo", primary.candidate_evidence[0].active_features)
        self.assertNotEqual(
            trace.root_pair.primary.content_hash,
            trace.root_pair.alternative.content_hash,
        )
        report = render_adaptive_recount_report(trace)
        self.assertIn("Non-Elliott Analysis Scope", report)
        self.assertIn("this is not a wave node", report)

    def test_context_is_visible_but_mandatory_scope_starts_at_requested_start(self) -> None:
        context_start = "2026-03-01T00:00:00+00:00"
        coordinator, trace, manifest = plan_and_manifest(
            context_start_utc=context_start
        )
        bundle = self._wavy_bundle(
            bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        )
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        packet = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )

        self.assertLess(
            datetime.fromisoformat(context_start),
            datetime.fromisoformat(trace.plan.analysis_start_utc),
        )
        self.assertEqual(
            scope.window_start.timestamp_utc,
            trace.plan.analysis_start_utc,
        )
        context_candles = packet["context_price_evidence_only"][0][
            "native_price_candles"
        ]
        analysis_candles = packet["price_evidence_only"][0][
            "native_price_candles"
        ]
        self.assertTrue(context_candles)
        self.assertTrue(
            all(
                datetime.fromisoformat(item["timestamp_utc"])
                < datetime.fromisoformat(trace.plan.analysis_start_utc)
                for item in context_candles
            )
        )
        self.assertTrue(
            all(
                datetime.fromisoformat(item["timestamp_utc"])
                >= datetime.fromisoformat(trace.plan.analysis_start_utc)
                for item in analysis_candles
            )
        )
        self.assertFalse(
            packet["intervals"]["context_window"]["mandatory_segmentation"]
        )
        self.assertTrue(
            packet["intervals"]["requested_analysis_window"][
                "mandatory_segmentation"
            ]
        )

        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        provider = FakeStagedRootProvider()
        resolved = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=provider,
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        primary = resolved.root_pair.primary
        first_segment = primary.segmentation.segments[0]
        self.assertEqual(
            first_segment.start_boundary.timestamp_utc,
            trace.plan.analysis_start_utc,
        )
        self.assertEqual(
            primary.segment_classifications[0].classification.value,
            "previous_parent_wave_tail",
        )
        classification_packet = next(
            packet
            for operation, role, packet in provider.calls
            if operation == "classification" and role is CandidateGraphRole.PRIMARY
        )
        self.assertTrue(classification_packet["context_price_evidence_only"])
        self.assertTrue(
            classification_packet["instructions"]
            ["context_candles_are_not_required_segments_or_elliott_nodes"]
        )

    def test_classification_schema_limits_wave_pivots_to_their_source_segment(self) -> None:
        coordinator, pending_trace, manifest = plan_and_manifest()
        bundle = self._wavy_bundle(
            bundle_for_manifest(manifest, pending_trace.plan.provider_capabilities)
        )
        scope = build_adaptive_analysis_scope(pending_trace.plan, (bundle,))
        provider = FakeStagedRootProvider()
        segmentation_raw = provider.generate_root_segmentation(
            role=CandidateGraphRole.PRIMARY,
            packet=build_root_segmentation_packet(
                pending_trace.plan,
                (bundle,),
                role=CandidateGraphRole.PRIMARY,
                scope=scope,
            ),
            output_schema={},
        )
        segmentation = parse_root_segmentation_output(
            segmentation_raw,
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            bundles=(bundle,),
            provider_name=provider.name,
            provider_model=provider.model,
            generated_at_utc=STAMP,
        )
        evidence = build_candidate_scoped_evidence(
            pending_trace.plan,
            (bundle,),
            segmentation,
        )
        classification_packet = build_root_classification_packet(
            pending_trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
            segmentation=segmentation,
            evidence=evidence,
            expected_degree=manifest.expected_child_degree,
        )
        schema = adaptive_root_classification_schema_for_packet(classification_packet)
        variants = schema["properties"]["nodes"]["items"]["anyOf"]
        segments = {
            item["segment_id"]: item
            for item in classification_packet["segmentation"]["segments"]
        }
        pivots = {
            item["pivot_id"]: item
            for evidence in classification_packet["price_evidence"]
            for item in evidence["pivot_catalog"]
        }

        self.assertEqual(len(variants), len(segments))
        for variant in variants:
            segment_id = variant["properties"]["source_segment_id"]["enum"][0]
            segment = segments[segment_id]
            start = datetime.fromisoformat(segment["start_boundary"]["timestamp_utc"])
            end = datetime.fromisoformat(segment["end_boundary"]["timestamp_utc"])
            expected = {
                pivot_id
                for pivot_id, pivot in pivots.items()
                if start <= datetime.fromisoformat(pivot["timestamp_utc"]) <= end
            }
            self.assertEqual(
                set(variant["properties"]["start_pivot_id"]["enum"]),
                expected,
            )
            self.assertEqual(
                set(variant["properties"]["end_pivot_id"]["enum"]),
                expected,
            )

    def test_same_degree_nested_wave_is_rejected_by_runtime_screen(self) -> None:
        trace = self._run(FakeStagedRootProvider(same_degree_nested=True))

        self.assertEqual(
            trace.root_pair.primary_screen.status,
            CandidateScreenStatus.REJECTED,
        )
        self.assertTrue(
            any(
                "same_degree_parent_child_forbidden" in item
                for item in trace.root_pair.primary_screen.hard_rule_errors
            )
        )


if __name__ == "__main__":
    unittest.main()
