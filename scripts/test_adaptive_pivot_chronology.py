from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

import elliott_ai.adaptive_recount as adaptive_recount_module

from elliott_ai.adaptive_market_data import NativeOHLCVBundle
from elliott_ai.adaptive_recount import (
    ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA,
    RootStageFailureCode,
    RootStageParseError,
    RootGenerationStage,
    RootStageValidationFailure,
    build_adaptive_analysis_scope,
    build_root_segmentation_packet,
    parse_root_segmentation_output,
)
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    CandidateGenerationError,
    CandidateGenerationFailureCode,
    CandidateGraphRole,
    LowerTimeframeCandidateGenerator,
    NativeCandle,
    PivotPriceField,
    PivotType,
    TypedPivot,
    candidate_generation_request_content_hash,
)
from elliott_ai.lower_timeframe_subdivision_verifier import (
    LowerTimeframeSubdivisionVerifier,
    NextEvidenceCode,
    SubdivisionVerificationStatus,
    VerificationReasonCode,
    build_subdivision_verification_request,
    googl_intermediate_to_minute_verifier_policy,
)
from elliott_ai.pivot_chronology import (
    ChronologyExtremum,
    IntrabarOrderStatus,
    PivotChronologyMember,
    build_pivot_chronology_groups,
    chronology_groups_for_typed_pivots,
    first_chronology_conflict,
)

from scripts.lower_timeframe_test_fixtures import (
    FakeGraphProvider,
    direct_graph,
    make_generation_request,
    pivot_for,
    raw_graph,
)
import scripts.test_adaptive_root_repair as root_repair_fixtures
from scripts.test_adaptive_recount import STAMP


class RootPivotChronologyTests(unittest.TestCase):
    @staticmethod
    def _dual_extremum_bundle(base: NativeOHLCVBundle) -> NativeOHLCVBundle:
        candles = list(base.candles)
        index = len(candles) // 2
        source = candles[index]
        candles[index] = NativeCandle.create(
            candle_id=source.candle_id,
            timestamp_utc=source.timestamp_utc,
            open=source.open,
            high=max(item.high for item in candles) + 100.0,
            low=min(item.low for item in candles) - 100.0,
            close=source.close,
            volume=source.volume,
        )
        values = base.to_dict()
        values.pop("content_hash", None)
        values.pop("file_hash", None)
        values["bundle_id"] = base.bundle_id + "-dual-extremum"
        values["candles"] = tuple(candles)
        values["source_response_hash"] = canonical_sha256(
            [item.to_dict() for item in candles]
        )
        return NativeOHLCVBundle.create(**values)

    @classmethod
    def _packet(cls):
        _coordinator, trace, base = root_repair_fixtures.AdaptiveRootRepairTests._prepared()
        bundle = cls._dual_extremum_bundle(base)
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        packet = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )
        group = next(
            item
            for item in packet["mutually_exclusive_boundary_groups"]
            if set(item["member_extrema"]) == {"high", "low"}
        )
        by_id = {item["boundary_id"]: item for item in packet["available_boundaries"]}
        high = next(
            item for item in group["member_boundary_ids"]
            if by_id[item]["extremum_type"] == "high"
        )
        low = next(
            item for item in group["member_boundary_ids"]
            if by_id[item]["extremum_type"] == "low"
        )
        return scope, bundle, packet, group, high, low

    @staticmethod
    def _parse(scope, bundle, raw, *, role=CandidateGraphRole.PRIMARY):
        return parse_root_segmentation_output(
            raw,
            role=role,
            scope=scope,
            bundles=(bundle,),
            provider_name="fixture",
            provider_model=None,
            generated_at_utc=STAMP,
        )

    @staticmethod
    def _steps(segmentation_id, *boundary_kind_pairs):
        return {
            "segmentation_id": segmentation_id,
            "steps": [
                {"end_boundary_id": boundary_id, "segment_kind": kind}
                for boundary_id, kind in boundary_kind_pairs
            ],
        }

    def test_same_candle_extrema_share_one_unknown_chronology_group(self) -> None:
        _scope, _bundle, packet, group, high, low = self._packet()
        by_id = {item["boundary_id"]: item for item in packet["available_boundaries"]}

        self.assertEqual(
            by_id[high]["chronology_group_id"],
            by_id[low]["chronology_group_id"],
        )
        self.assertEqual(
            by_id[high]["chronology_ordinal"],
            by_id[low]["chronology_ordinal"],
        )
        self.assertEqual(group["intrabar_order_status"], "unknown")
        self.assertEqual(group["maximum_selectable_members"], 1)
        self.assertNotEqual(by_id[high]["boundary_ordinal"], by_id[low]["boundary_ordinal"])

    def test_both_same_candle_extrema_are_rejected_before_segment_construction(self) -> None:
        scope, bundle, packet, group, high, low = self._packet()
        raw = self._steps(
            "same-bar-rejected",
            (high, "boundary_context"),
            (low, "atomic_swing"),
            (packet["available_boundaries"][-1]["boundary_id"], "unresolved_interval"),
        )
        with patch(
            "elliott_ai.adaptive_recount.PriceSegmentCandidate",
            side_effect=AssertionError("segment construction must not run"),
        ):
            with self.assertRaises(RootStageParseError) as raised:
                self._parse(scope, bundle, raw)

        error = raised.exception
        self.assertEqual(error.error_code, RootStageFailureCode.SAME_BAR_INTRABAR_ORDER_UNKNOWN)
        self.assertEqual(error.required_relation, "select_at_most_one_boundary_from_chronology_group")
        self.assertEqual(error.offending_boundary_ids, (high, low))
        self.assertEqual(error.chronology_group_id, group["chronology_group_id"])
        self.assertEqual(error.chronology_group_ordinal, group["chronology_ordinal"])
        self.assertEqual(error.source_bar_hash, group["source_bar_hash"])
        self.assertEqual(error.shared_source_timestamp_utc, group["source_candle_timestamp_utc"])

    def test_either_same_candle_extremum_is_valid_independently_and_roles_stay_isolated(self) -> None:
        scope, bundle, packet, _group, high, low = self._packet()
        as_of = packet["available_boundaries"][-1]["boundary_id"]
        primary = self._parse(
            scope,
            bundle,
            self._steps(
                "primary-high-only",
                (high, "boundary_context"),
                (as_of, "unresolved_interval"),
            ),
            role=CandidateGraphRole.PRIMARY,
        )
        alternative = self._parse(
            scope,
            bundle,
            self._steps(
                "alternative-low-only",
                (low, "boundary_context"),
                (as_of, "unresolved_interval"),
            ),
            role=CandidateGraphRole.ALTERNATIVE,
        )

        self.assertEqual(primary.segments[0].end_boundary.boundary_id, high)
        self.assertEqual(alternative.segments[0].end_boundary.boundary_id, low)
        self.assertNotEqual(primary.content_hash, alternative.content_hash)

    def test_v4_derives_nonfinal_closed_and_final_active_even_for_unresolved_kind(self) -> None:
        scope, bundle, packet, _group, high, _low = self._packet()
        as_of = packet["available_boundaries"][-1]["boundary_id"]
        later = next(
            item["boundary_id"]
            for item in packet["available_boundaries"]
            if item["kind"] == "catalog_pivot"
            and item["chronology_ordinal"]
            > next(
                row["chronology_ordinal"]
                for row in packet["available_boundaries"]
                if row["boundary_id"] == high
            )
            and item["chronology_group_id"]
            not in {
                row["chronology_group_id"]
                for row in packet["available_boundaries"]
                if row["boundary_id"] in {high, as_of}
            }
        )
        parsed = self._parse(
            scope,
            bundle,
            self._steps(
                "deterministic-completion",
                (high, "boundary_context"),
                (later, "unresolved_interval"),
                (as_of, "unresolved_interval"),
            ),
        )

        self.assertEqual(
            tuple(item.completion_state.value for item in parsed.segments),
            ("closed", "closed", "active"),
        )
        self.assertTrue(all(":step:" in item.segment_id for item in parsed.segments))

    def test_v4_contract_cannot_express_descriptor_count_mismatch(self) -> None:
        self.assertEqual(
            set(ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA["required"]),
            {"segmentation_id", "steps"},
        )
        self.assertNotIn("boundary_path", ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA["properties"])
        step = ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA["properties"]["steps"]["items"]
        self.assertEqual(set(step["required"]), {"end_boundary_id", "segment_kind"})
        self.assertNotIn("completion_state", step["properties"])
        self.assertNotIn("segment_id", step["properties"])

    def test_historical_v3_same_bar_conflict_uses_chronology_repair_fact(self) -> None:
        scope, bundle, packet, group, high, low = self._packet()
        as_of = packet["available_boundaries"][-1]["boundary_id"]
        raw = {
            "segmentation_id": "historical-v3-same-bar",
            "boundary_path": [
                scope.window_start.boundary_id,
                high,
                low,
                as_of,
            ],
            "segments": [
                {"segment_id": "v3-1", "segment_kind": "boundary_context", "completion_state": "closed"},
                {"segment_id": "v3-2", "segment_kind": "atomic_swing", "completion_state": "closed"},
                {"segment_id": "v3-3", "segment_kind": "unresolved_interval", "completion_state": "active"},
            ],
        }
        with self.assertRaises(RootStageParseError) as raised:
            self._parse(scope, bundle, raw)

        self.assertEqual(
            raised.exception.error_code,
            RootStageFailureCode.SAME_BAR_INTRABAR_ORDER_UNKNOWN,
        )
        self.assertEqual(
            raised.exception.required_relation,
            "select_at_most_one_boundary_from_chronology_group",
        )
        self.assertEqual(raised.exception.chronology_group_id, group["chronology_group_id"])

        error = raised.exception
        failure = RootStageValidationFailure.create(
            role=CandidateGraphRole.PRIMARY,
            stage=RootGenerationStage.PRICE_SEGMENTATION,
            attempt_number=1,
            packet_hash=canonical_sha256(packet),
            output_schema_hash=canonical_sha256(ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA),
            returned_structured_output_hash=canonical_sha256(raw),
            semantic_signature=adaptive_recount_module.root_stage_semantic_signature(
                RootGenerationStage.PRICE_SEGMENTATION,
                raw,
            ),
            error_code=error.error_code,
            affected_id=error.affected_id,
            offending_boundary_ids=error.offending_boundary_ids,
            offending_boundary_ordinals=error.offending_boundary_ordinals,
            required_relation=error.required_relation,
            chronology_group_id=error.chronology_group_id,
            chronology_group_ordinal=error.chronology_group_ordinal,
            source_bundle_hash=error.source_bundle_hash,
            source_bar_hash=error.source_bar_hash,
            shared_source_timestamp_utc=error.shared_source_timestamp_utc,
            intrabar_order_status=error.intrabar_order_status,
            deterministic_error_message=str(error),
            repairable=True,
            schema_conforming=True,
            created_at_utc=STAMP,
        )
        context = adaptive_recount_module._root_repair_context(
            role=CandidateGraphRole.PRIMARY,
            stage=RootGenerationStage.PRICE_SEGMENTATION,
            attempt_number=2,
            failures=(failure,),
            base_packet=packet,
        )
        fact = context.to_dict()["deterministic_failures"][0]
        self.assertEqual(fact["error_code"], "same_bar_intrabar_order_unknown")
        self.assertEqual(
            fact["required_relation"],
            "select_at_most_one_boundary_from_chronology_group",
        )
        self.assertEqual(fact["chronology_group_id"], group["chronology_group_id"])
        self.assertEqual(fact["source_bar_hash"], group["source_bar_hash"])

    def test_historical_v3_descriptor_repair_reports_exact_required_count(self) -> None:
        scope, bundle, packet, _group, high, _low = self._packet()
        as_of = packet["available_boundaries"][-1]["boundary_id"]
        raw = {
            "segmentation_id": "historical-v3-count",
            "boundary_path": [scope.window_start.boundary_id, high, as_of],
            "segments": [
                {"segment_id": "v3-only", "segment_kind": "boundary_context", "completion_state": "active"},
            ],
        }
        with self.assertRaises(RootStageParseError) as raised:
            self._parse(scope, bundle, raw)

        self.assertEqual(
            raised.exception.error_code,
            RootStageFailureCode.SEGMENT_DESCRIPTOR_COUNT_MISMATCH,
        )
        self.assertEqual(
            raised.exception.required_relation,
            "segment_descriptor_count == 2",
        )
        self.assertIn("expected 2, received 1", str(raised.exception))

    def test_historical_v3_segmentation_hash_is_unchanged(self) -> None:
        _coordinator, trace, bundle = root_repair_fixtures.AdaptiveRootRepairTests._prepared()
        scope = build_adaptive_analysis_scope(trace.plan, (bundle,))
        packet = build_root_segmentation_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
            scope=scope,
        )
        parsed = self._parse(
            scope,
            bundle,
            root_repair_fixtures._path_response(packet, CandidateGraphRole.PRIMARY),
        )
        self.assertEqual(
            parsed.content_hash,
            "c30fca13bba1770494cb76981c22d959e1760d672b62eb30e548cc029c9ce9fc",
        )


class SharedChronologyContractTests(unittest.TestCase):
    @staticmethod
    def _members(index: int, timestamp: str):
        bundle_hash = canonical_sha256("bundle")
        bar_hash = canonical_sha256({"bar": index})
        return (
            PivotChronologyMember(
                boundary_id=f"catalog:window:high:{index}",
                extremum=ChronologyExtremum.HIGH,
                source_bundle_hash=bundle_hash,
                source_bar_hash=bar_hash,
                source_candle_timestamp_utc=timestamp,
                timeframe="4h",
            ),
            PivotChronologyMember(
                boundary_id=f"catalog:window:low:{index}",
                extremum=ChronologyExtremum.LOW,
                source_bundle_hash=bundle_hash,
                source_bar_hash=bar_hash,
                source_candle_timestamp_utc=timestamp,
                timeframe="4h",
            ),
        )

    def test_exact_rklb_indices_218_224_and_308_have_no_implied_intrabar_order(self) -> None:
        members = (
            *self._members(218, "2026-07-01T13:30:00+00:00"),
            *self._members(224, "2026-07-02T13:30:00+00:00"),
            *self._members(308, "2026-08-07T13:30:00+00:00"),
        )
        groups = build_pivot_chronology_groups(members)

        self.assertEqual(tuple(item.chronology_ordinal for item in groups), (0, 1, 2))
        self.assertTrue(all(item.intrabar_order_status is IntrabarOrderStatus.UNKNOWN for item in groups))
        self.assertTrue(all(item.maximum_selectable_members == 1 for item in groups))
        for group in groups:
            self.assertIsNotNone(
                first_chronology_conflict(group.member_boundary_ids, groups)
            )

    def test_finer_native_distinct_timestamps_resolve_order_without_ohlc_inference(self) -> None:
        bundle_hash = canonical_sha256("finer-bundle")
        first = PivotChronologyMember(
            boundary_id="finer-low",
            extremum=ChronologyExtremum.LOW,
            source_bundle_hash=bundle_hash,
            source_bar_hash=canonical_sha256("finer-bar-1"),
            source_candle_timestamp_utc="2026-07-01T13:30:00+00:00",
            timeframe="1h",
        )
        second = PivotChronologyMember(
            boundary_id="finer-high",
            extremum=ChronologyExtremum.HIGH,
            source_bundle_hash=bundle_hash,
            source_bar_hash=canonical_sha256("finer-bar-2"),
            source_candle_timestamp_utc="2026-07-01T14:30:00+00:00",
            timeframe="1h",
        )
        groups = build_pivot_chronology_groups((first, second))

        self.assertEqual(len(groups), 2)
        self.assertIsNone(first_chronology_conflict((first.boundary_id, second.boundary_id), groups))
        self.assertTrue(all(item.intrabar_order_status is IntrabarOrderStatus.SINGLE_MEMBER for item in groups))


class ChildPivotChronologyTests(unittest.TestCase):
    @staticmethod
    def _request_with_same_bar_low():
        request, catalog = make_generation_request()
        window = request.native_ohlcv_scope.required_window
        high = pivot_for(
            catalog,
            window=window,
            index=1,
            field=PivotPriceField.HIGH,
        )
        source = window.candles[1]
        same_bar_low = TypedPivot.create(
            pivot_id=f"{window.window_id}:low:1",
            timestamp_utc=source.timestamp_utc,
            price=source.low,
            price_field=PivotPriceField.LOW,
            pivot_type=PivotType.LOW,
            timeframe=window.timeframe,
            source_window_hash=window.native_rows_hash,
            source_bar_hash=source.source_row_hash,
        )
        updated_catalog = tuple(sorted((*catalog, same_bar_low), key=lambda item: (item.timestamp_utc, item.pivot_id)))
        updated = replace(request, pivot_catalog=updated_catalog, content_hash="")
        updated = replace(
            updated,
            content_hash=candidate_generation_request_content_hash(updated),
        )
        return updated, updated_catalog, high, same_bar_low

    def test_child_provider_graph_rejects_same_source_candle_extrema_with_typed_exclusion(self) -> None:
        request, catalog, _high, same_bar_low = self._request_with_same_bar_low()
        raw = raw_graph(request, catalog, role=CandidateGraphRole.PRIMARY)
        raw["children"][1]["start_pivot_id"] = same_bar_low.pivot_id
        provider = FakeGraphProvider({CandidateGraphRole.PRIMARY: raw})

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
            CandidateGenerationFailureCode.INTRABAR_ORDER_RESOLUTION_REQUIRED,
        )
        self.assertIn(
            "required_relation=select_at_most_one_boundary_from_chronology_group",
            failure.details,
        )
        self.assertIsNotNone(failure.reconstruction_exclusion)
        self.assertIn(
            "intrabar_pivot_order_unresolved",
            failure.reconstruction_exclusion.deterministic_failure_ids,
        )
        self.assertEqual(len(provider.calls), 1)

    def test_verifier_returns_unproven_and_requests_finer_resolution(self) -> None:
        request, catalog, _high, same_bar_low = self._request_with_same_bar_low()
        graph = direct_graph(
            request,
            catalog,
            role=CandidateGraphRole.PRIMARY,
        )
        children = list(graph.children)
        children[1] = replace(children[1], start_pivot=same_bar_low)
        conflicted = CandidateChildGraph.create(
            graph_id="same-bar-child-graph",
            role=graph.role,
            request_id=graph.request_id,
            request_content_hash=graph.request_content_hash,
            parent_candidate_id=graph.parent_candidate_id,
            target_child_degree=graph.target_child_degree,
            target_timeframe=graph.target_timeframe,
            declared_family=graph.declared_family,
            children=tuple(children),
            proof_scope=graph.proof_scope,
            provider=graph.provider,
            model=graph.model,
            generated_at_utc=graph.generated_at_utc,
            policy_version=graph.policy_version,
        )
        verification_request = build_subdivision_verification_request(
            request,
            conflicted,
            verifier_policy=googl_intermediate_to_minute_verifier_policy(),
            shadow_mode=True,
            created_at_utc=request.created_at_utc,
        )
        result = LowerTimeframeSubdivisionVerifier().verify(
            verification_request,
            shadow_mode=True,
        )

        self.assertEqual(result.status, SubdivisionVerificationStatus.UNPROVEN)
        self.assertIn(
            VerificationReasonCode.INTRABAR_PIVOT_ORDER_UNRESOLVED,
            result.reason_codes,
        )
        self.assertIn(
            NextEvidenceCode.LOWER_TIMEFRAME_RESOLUTION_REQUIRED,
            result.required_next_evidence,
        )
        self.assertNotIn(
            VerificationReasonCode.HARD_PRICE_RULE_FAILURE,
            result.reason_codes,
        )


if __name__ == "__main__":
    unittest.main()
