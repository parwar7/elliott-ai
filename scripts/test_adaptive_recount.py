from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import elliott_ai.adaptive_recount as adaptive_recount_module

from elliott_ai.adaptive_market_data import (
    BundleValidationStatus,
    DataComparabilityStatus,
    DataComparisonPurpose,
    DataRequestManifest,
    NativeOHLCVBundle,
    compare_bundle_identity,
    current_codex_tradingview_mcp_availability,
    validate_bundle_for_manifest,
    write_immutable_bundle,
)
from elliott_ai.adaptive_recount import (
    ADAPTIVE_ROOT_OUTPUT_SCHEMA,
    AdaptiveRootHypothesis,
    AdaptiveRootPair,
    AdaptiveChildReconstructionRejection,
    AdaptiveRecountBudget,
    AdaptiveRecountCoordinator,
    AdaptiveRecountStatus,
    AdaptiveStopReason,
    CandidateScreenStatus,
    AdaptiveWaveCandidate,
    adaptive_parent_reconstruction_decision,
    adaptive_root_output_schema_for_packet,
    build_blind_root_packet,
    create_adaptive_recount_plan,
    effective_adaptive_proof_status,
    load_adaptive_recount_trace,
    screen_adaptive_hypothesis,
    write_immutable_adaptive_trace,
)
from elliott_ai.adaptive_recount_provider import AdaptiveRecountStructuredProvider
from elliott_ai.adaptive_reporting import render_adaptive_recount_report
from elliott_ai.adaptive_timeframe_planner import (
    AdaptiveTimeframePlanner,
    AdaptiveTimeframeRequest,
    StructuralQuestion,
    TimeframeEvidenceCoverage,
    TimeframePlanningStatus,
)
from elliott_ai.agent import ElliottAgent
from elliott_ai.cli import main
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.indicators import DEFAULT_CANDLE_FEATURES, features_for_profile
from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateGenerationFailureCode,
    CandidateGraphRole,
    NativeCandle,
    PivotPriceField,
    PivotType,
    ReconstructionExcludedGraph,
    TypedPivot,
    graph_generation_output_schema_for_packet,
)
from elliott_ai.lower_timeframe_subdivision_verifier import (
    SubdivisionVerificationStatus,
)
from elliott_ai.market_data import infer_timeframe, load_candles, load_market_metadata
from elliott_ai.timeframes import (
    ProviderCapabilitySet,
    ProviderTimeframeCapability,
    TimeframeClass,
    degree_timeframe_compatibility,
    normalize_timeframe_name,
    sort_timeframes,
    timeframe_is_finer,
)


STAMP = "2026-08-29T12:00:00+00:00"
CUTOFF = "2026-08-28T00:00:00+00:00"
START = "2026-05-27T00:00:00+00:00"


def capabilities(*timeframes: str) -> ProviderCapabilitySet:
    return ProviderCapabilitySet.create(
        provider="fixture-feed",
        capabilities=tuple(
            ProviderTimeframeCapability.create(
                canonical_name=timeframe,
                provider_alias=f"fixture-{timeframe}",
                native_available=True,
                derived=False,
                provider="fixture-feed",
                session_policy="nasdaq_regular_session",
                adjustment_policy="split_adjusted_dividend_unadjusted",
            )
            for timeframe in timeframes
        ),
        discovery_status="offline_fixture",
        source_interface="scripts.test_adaptive_recount.FakeMarketDataProvider",
        discovered_at_utc=STAMP,
        limitations=(),
    )


def plan_and_manifest(
    *,
    terminal_degree: str = "Subminuette",
    context_start_utc: str = START,
) -> tuple[AdaptiveRecountCoordinator, object, object]:
    provider_capabilities = capabilities("monthly", "weekly", "daily", "12h", "4h", "1h", "30m")
    plan = create_adaptive_recount_plan(
        symbol="NASDAQ:RKLB",
        analysis_start_utc=START,
        context_start_utc=context_start_utc,
        analysis_cutoff_utc=CUTOFF,
        market_data_provider=provider_capabilities.provider,
        provider_capabilities=provider_capabilities,
        created_at_utc=STAMP,
        budgets=AdaptiveRecountBudget(terminal_degree=terminal_degree),
    )
    coordinator = AdaptiveRecountCoordinator()
    trace = coordinator.initial_trace(plan)
    trace = coordinator.plan_initial_data(
        trace,
        created_at_utc=STAMP,
        source_interface=provider_capabilities.source_interface,
    )
    if not trace.data_manifests:
        raise AssertionError("Fixture planner did not emit a data manifest.")
    return coordinator, trace, trace.data_manifests[0]


def bundle_for_manifest(manifest: object, provider_capabilities: ProviderCapabilitySet, *, feed: str = "fixture-consolidated") -> NativeOHLCVBundle:
    start = datetime.fromisoformat(manifest.requested_start_utc)
    end = datetime.fromisoformat(manifest.requested_end_utc)
    seconds = next(
        item.duration_seconds
        for item in provider_capabilities.capabilities
        if item.canonical_name == manifest.canonical_timeframe
    )
    step = timedelta(seconds=seconds)
    timestamps: list[datetime] = []
    current = start
    while current < end:
        timestamps.append(current)
        current += step
    if not timestamps or timestamps[-1] != end:
        timestamps.append(end)
    candles = tuple(
        NativeCandle.create(
            candle_id=f"fixture-candle-{index:04d}",
            timestamp_utc=value.isoformat(),
            open=10.0 + index * 0.1,
            high=10.7 + index * 0.1,
            low=9.5 + index * 0.1,
            close=10.3 + index * 0.1,
            volume=1000.0 + index,
        )
        for index, value in enumerate(timestamps)
    )
    return NativeOHLCVBundle.create(
        bundle_id=f"fixture-bundle-{manifest.canonical_timeframe}-{feed}",
        source_request_id=manifest.request_id,
        source_request_hash=manifest.content_hash,
        provider_capability_set_hash=provider_capabilities.content_hash,
        canonical_symbol=manifest.symbol,
        provider_symbol="RKLB",
        exchange="NASDAQ",
        provider=manifest.provider,
        source_interface=manifest.source_interface,
        feed_identity=feed,
        session=manifest.session_policy,
        timezone="America/New_York",
        price_adjustment=manifest.adjustment_policy,
        dividend_adjustment="unadjusted",
        volume_adjustment="split_adjusted",
        price_basis="split-adjusted dividend-unadjusted OHLCV",
        timeframe=manifest.canonical_timeframe,
        provider_interval_alias=manifest.provider_interval_alias,
        native=True,
        derived=False,
        requested_start_utc=manifest.requested_start_utc,
        requested_end_utc=manifest.requested_end_utc,
        actual_first_candle_utc=candles[0].timestamp_utc,
        actual_final_completed_candle_utc=candles[-1].timestamp_utc,
        acquisition_timestamp_utc=STAMP,
        analysis_cutoff_utc=manifest.analysis_cutoff_utc,
        timestamp_semantics="bar_open_utc_with_provider_interval",
        completion_verification_method="fixture_completed_flag_and_session_policy",
        incomplete_candles_excluded=True,
        source_response_hash=canonical_sha256([item.to_dict() for item in candles]),
        candles=candles,
    )


class FakeRootProvider:
    name = "fake-root-provider"
    model = "fixture-model"

    def __init__(self, *, duplicate: bool = False) -> None:
        self.calls: list[tuple[CandidateGraphRole, dict]] = []
        self.duplicate = duplicate

    def generate_root_candidate(self, *, role, packet, output_schema):
        self.calls.append((role, packet))
        catalog = packet["price_evidence_only"][0]["pivot_catalog"]
        start = next(item for item in catalog if item["pivot_type"] == "low")
        end = next(item for item in reversed(catalog) if item["pivot_type"] == "high")
        family = "impulse" if role is CandidateGraphRole.PRIMARY or self.duplicate else "zigzag"
        position = "1" if family == "impulse" else "A"
        wave_id = f"{role.value}-root"
        return {
            "hypothesis_id": f"{role.value}-hypothesis",
            "root_timeframe": packet["price_evidence_only"][0]["bundle_identity"]["timeframe"],
            "nodes": [
                {
                    "wave_id": wave_id,
                    "parent_wave_id": None,
                    "degree": "Primary",
                    "sequence_position": position,
                    "timeframe": packet["price_evidence_only"][0]["bundle_identity"]["timeframe"],
                    "direction": "up",
                    "declared_family": family,
                    "completion_state": "active",
                    "start_pivot_id": start["pivot_id"],
                    "end_pivot_id": end["pivot_id"],
                    "invalidation": {
                        "invalidation_id": f"{role.value}-invalidation",
                        "threshold_price": start["price"],
                        "direction": "below",
                        "evaluation_basis": "intrabar_touch_or_breach",
                        "source_pivot_id": start["pivot_id"],
                    },
                    "child_wave_ids": [],
                }
            ],
            "active_wave_id": wave_id,
            "confirmation_conditions": ["Require connected lower-degree price structure."],
            "unresolved_questions": ["Active endpoint remains provisional."],
        }


class OneInvalidRootProvider(FakeRootProvider):
    def generate_root_candidate(self, *, role, packet, output_schema):
        result = super().generate_root_candidate(
            role=role,
            packet=packet,
            output_schema=output_schema,
        )
        if role is CandidateGraphRole.PRIMARY:
            result["nodes"][0]["child_wave_ids"] = ["missing-primary-child"]
        return result


class FakeStrictTransport:
    name = "fake-strict-transport"
    model = "fixture-model"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_strict_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"fixture": "candidate-only"}


def ready_trace():
    coordinator, trace, manifest = plan_and_manifest(terminal_degree="Primary")
    bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
    trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
    trace = coordinator.generate_initial_candidates(
        trace,
        (bundle,),
        provider=FakeRootProvider(),
        allow_model_calls=True,
        generated_at_utc=STAMP,
    )
    trace = coordinator.mark_ready_for_final_comparison(trace, updated_at_utc=STAMP)
    return coordinator, trace, bundle


class TimeframeAndPlannerTests(unittest.TestCase):
    def test_root_planner_selects_by_scope_bar_utility_without_a_forced_degree(self) -> None:
        coordinator, trace, _manifest = plan_and_manifest()
        request = trace.planner_requests[0]
        decision = trace.planner_decisions[0]

        self.assertIsNone(request.parent_degree)
        self.assertIsNone(request.expected_child_degree)
        self.assertIsNone(request.parent_timeframe)
        self.assertEqual(decision.selected_timeframe, "daily")
        weekly = next(
            item for item in decision.assessments if item.timeframe == "weekly"
        )
        daily = next(
            item for item in decision.assessments if item.timeframe == "daily"
        )
        self.assertTrue(weekly.useful)
        self.assertEqual(weekly.reason, "native useful candidate")
        self.assertLess(daily.score, weekly.score)

    def test_recursive_proof_timeframe_respects_expected_child_degree(self) -> None:
        available = capabilities("4h", "1h", "15m")
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol="NASDAQ:RKLB",
            parent_candidate_id="primary:intermediate-1",
            parent_degree="Intermediate",
            parent_timeframe="daily",
            parent_start_utc="2026-05-27T17:30:00Z",
            parent_end_utc="2026-06-25T13:30:00Z",
            structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
            expected_child_degree="Minor",
            current_evidence_coverage=(),
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=available.content_hash,
            provider_supported_intervals=available.supported_timeframes,
            requested_timeframe=None,
            reason_code="recursive-proof-fixture",
            cutoff_utc=CUTOFF,
            maximum_request_budget=8,
            requests_already_used=3,
            maximum_bar_budget=25000,
            created_at_utc=STAMP,
        )

        decision = AdaptiveTimeframePlanner().plan(request, available)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "4h")
        four_hour = next(
            item for item in decision.assessments if item.timeframe == "4h"
        )
        one_hour = next(
            item for item in decision.assessments if item.timeframe == "1h"
        )
        self.assertTrue(four_hour.useful)
        self.assertTrue(one_hour.useful)
        self.assertEqual(one_hour.reason, "native useful candidate")
        self.assertEqual(
            decision.proof_feasibility.expected_child_degree,
            "Minor",
        )

    def test_canonical_normalization_preserves_month_minute_distinction(self) -> None:
        self.assertEqual(normalize_timeframe_name("1M"), "monthly")
        self.assertEqual(normalize_timeframe_name("1m"), "1m")
        self.assertEqual(normalize_timeframe_name("45 minutes"), "45m")
        self.assertEqual(normalize_timeframe_name("2-day"), "2d")
        self.assertEqual(normalize_timeframe_name("1week"), "weekly")

    def test_granularity_order_is_dynamic_and_legacy_compatible(self) -> None:
        values = sort_timeframes(("15m", "monthly", "3h", "daily", "weekly", "4h"))
        self.assertEqual(values, ("monthly", "weekly", "daily", "4h", "3h", "15m"))
        self.assertTrue(timeframe_is_finer("45m", "1h"))

    def test_unsupported_timeframe_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_timeframe_name("quarterly")

    def test_provider_capability_distinguishes_native_and_derived(self) -> None:
        native = ProviderTimeframeCapability.create(
            canonical_name="3h",
            provider_alias="180min",
            native_available=True,
            derived=False,
            provider="fixture",
            session_policy="regular",
            adjustment_policy="splits",
        )
        derived = ProviderTimeframeCapability.create(
            canonical_name="4h",
            provider_alias="derived-4h",
            native_available=False,
            derived=True,
            provider="fixture",
            session_policy="regular",
            adjustment_policy="splits",
        )
        capability_set = ProviderCapabilitySet.create(
            provider="fixture",
            capabilities=(native, derived),
            discovery_status="fixture",
            source_interface="fixture",
            discovered_at_utc=STAMP,
        )
        self.assertIsNotNone(capability_set.available("3h"))
        self.assertIsNone(capability_set.available("4h"))
        self.assertIsNotNone(capability_set.available("4h", native_only=False))
        self.assertEqual(native.classification, TimeframeClass.INTRADAY)

    def test_degree_compatibility_accepts_dynamic_intervals_and_legacy_pairs(self) -> None:
        self.assertTrue(degree_timeframe_compatibility("Primary", "12h").compatible)
        self.assertTrue(degree_timeframe_compatibility("Primary", "daily").compatible)
        self.assertTrue(degree_timeframe_compatibility("Minute", "15m").compatible)
        self.assertFalse(degree_timeframe_compatibility("Cycle", "15m").compatible)

    def test_planner_selects_12h_for_a_sixty_day_structural_segment(self) -> None:
        available = capabilities("daily", "12h", "8h", "6h", "4h")
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol="NASDAQ:TEST",
            parent_candidate_id="parent",
            parent_degree="Cycle",
            parent_timeframe="monthly",
            parent_start_utc="2026-01-01T00:00:00Z",
            parent_end_utc="2026-03-02T00:00:00Z",
            structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
            expected_child_degree="Primary",
            current_evidence_coverage=(),
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=available.content_hash,
            provider_supported_intervals=available.supported_timeframes,
            requested_timeframe=None,
            reason_code="fixture",
            cutoff_utc="2026-03-02T00:00:00Z",
            maximum_request_budget=4,
            requests_already_used=0,
            maximum_bar_budget=1000,
            created_at_utc=STAMP,
        )
        decision = AdaptiveTimeframePlanner().plan(request, available)
        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "12h")
        self.assertEqual(decision.estimated_native_bars, 120)

    def test_existing_full_coverage_prevents_redundant_request(self) -> None:
        available = capabilities("daily", "12h", "4h")
        coverage = TimeframeEvidenceCoverage(
            timeframe="12h",
            first_timestamp_utc="2026-01-01T00:00:00Z",
            last_completed_timestamp_utc="2026-03-02T00:00:00Z",
            native_bar_count=120,
            full_parent_coverage=True,
            native=True,
            source_reference="existing",
        )
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol="NASDAQ:TEST",
            parent_candidate_id="parent",
            parent_degree="Cycle",
            parent_timeframe="monthly",
            parent_start_utc="2026-01-01T00:00:00Z",
            parent_end_utc="2026-03-02T00:00:00Z",
            structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
            expected_child_degree="Primary",
            current_evidence_coverage=(coverage,),
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=available.content_hash,
            provider_supported_intervals=available.supported_timeframes,
            requested_timeframe=None,
            reason_code="fixture",
            cutoff_utc="2026-03-02T00:00:00Z",
            maximum_request_budget=4,
            requests_already_used=1,
            maximum_bar_budget=1000,
            created_at_utc=STAMP,
        )
        decision = AdaptiveTimeframePlanner().plan(request, available)
        self.assertEqual(decision.status, TimeframePlanningStatus.ALREADY_COVERED)
        self.assertEqual(decision.requests_after_decision, 1)

    def test_request_budget_stops_before_selection(self) -> None:
        available = capabilities("daily", "12h")
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol="NASDAQ:TEST",
            parent_candidate_id="parent",
            parent_degree="Cycle",
            parent_timeframe="monthly",
            parent_start_utc="2026-01-01T00:00:00Z",
            parent_end_utc="2026-03-02T00:00:00Z",
            structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
            expected_child_degree="Primary",
            current_evidence_coverage=(),
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=available.content_hash,
            provider_supported_intervals=available.supported_timeframes,
            requested_timeframe=None,
            reason_code="fixture",
            cutoff_utc="2026-03-02T00:00:00Z",
            maximum_request_budget=1,
            requests_already_used=1,
            maximum_bar_budget=1000,
            created_at_utc=STAMP,
        )
        decision = AdaptiveTimeframePlanner().plan(request, available)
        self.assertEqual(decision.status, TimeframePlanningStatus.BUDGET_EXHAUSTED)

    def test_empty_provider_capability_set_returns_not_covered(self) -> None:
        unavailable = ProviderCapabilitySet.create(
            provider="tradingview-mcp",
            capabilities=(),
            discovery_status="insufficient_capability",
            source_interface="mcp__codex_apps__tradingcursor_request_analysis",
            discovered_at_utc=STAMP,
            limitations=("Raw provenance-complete OHLCV is unavailable.",),
        )
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol="NASDAQ:RKLB",
            parent_candidate_id="analysis-window",
            parent_degree="Cycle",
            parent_timeframe="monthly",
            parent_start_utc=START,
            parent_end_utc=CUTOFF,
            structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
            expected_child_degree="Primary",
            current_evidence_coverage=(),
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=unavailable.content_hash,
            provider_supported_intervals=(),
            requested_timeframe=None,
            reason_code="fixture-unavailable",
            cutoff_utc=CUTOFF,
            maximum_request_budget=4,
            requests_already_used=0,
            maximum_bar_budget=1000,
            created_at_utc=STAMP,
        )
        decision = AdaptiveTimeframePlanner().plan(request, unavailable)
        self.assertEqual(decision.status, TimeframePlanningStatus.NOT_COVERED)
        self.assertIsNone(decision.selected_timeframe)


class AdaptiveDataAndCandidateTests(unittest.TestCase):
    def test_malformed_attempt_is_ledgered_and_supplied_as_typed_exclusion(self) -> None:
        exclusion = ReconstructionExcludedGraph.create(
            graph_signature=canonical_sha256("malformed-graph"),
            pivot_sequence=("pivot-start", "pivot-end"),
            declared_family="impulse",
            child_family_sequence=("impulse",),
            deterministic_failure_ids=("child_endpoint_order_invalid",),
            hard_rule_calculations=(),
            failed_boundary_checks=(
                {
                    "reason_code": "child_endpoint_order_invalid",
                    "start_pivot_id": "pivot-end",
                    "end_pivot_id": "pivot-start",
                },
            ),
            rejected_diagonal=None,
            verifier_result_hash=canonical_sha256("malformed-validation"),
        )
        rejection = AdaptiveChildReconstructionRejection.create(
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id="parent-1",
            attempt_number=1,
            generation_request_hash=canonical_sha256("request-1"),
            returned_graph_signature=exclusion.graph_signature,
            excluded_graph_signature=exclusion.graph_signature,
            reason_code=CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT.value,
            created_at_utc=STAMP,
            excluded_graph=exclusion,
        )

        context = adaptive_recount_module._child_reconstruction_context(
            parent=SimpleNamespace(declared_family="impulse"),
            records=(),
            rejections=(rejection,),
            attempt_number=2,
        )

        self.assertEqual(rejection.attempt_number, 1)
        self.assertEqual(context.attempt_number, 2)
        self.assertEqual(context.excluded_graphs, (exclusion,))
        self.assertNotIn("raw_output", rejection.to_dict())

    def test_frozen_parent_hard_rule_failure_is_really_inconsistent(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        prices = (100.0, 130.0, 115.0, 160.0, 125.0, 175.0)
        pivots = tuple(
            TypedPivot.create(
                pivot_id=f"frozen-parent-p{index}",
                pivot_type=(PivotType.LOW if index % 2 == 0 else PivotType.HIGH),
                timestamp_utc=(
                    datetime(2026, 6, 1, tzinfo=timezone.utc)
                    + timedelta(days=index)
                ).isoformat(),
                price=price,
                price_field=(
                    PivotPriceField.LOW if index % 2 == 0 else PivotPriceField.HIGH
                ),
                timeframe="daily",
                source_window_hash=canonical_sha256("frozen-parent-window"),
                source_bar_hash=canonical_sha256({"bar": index}),
            )
            for index, price in enumerate(prices)
        )
        child_ids = tuple(f"frozen-child-{index}" for index in range(1, 6))
        parent = AdaptiveWaveCandidate(
            wave_id="frozen-parent",
            parent_wave_id=None,
            degree="Primary",
            sequence_position="1",
            timeframe="daily",
            direction="up",
            declared_family="impulse",
            completion_state="completed",
            start_pivot=pivots[0],
            end_pivot=pivots[5],
            invalidation=None,
            child_wave_ids=child_ids,
        )
        families = ("impulse", "zigzag", "impulse", "zigzag", "impulse")
        children = tuple(
            AdaptiveWaveCandidate(
                wave_id=child_ids[index],
                parent_wave_id=parent.wave_id,
                degree="Intermediate",
                sequence_position=str(index + 1),
                timeframe="daily",
                direction="up" if index % 2 == 0 else "down",
                declared_family=families[index],
                completion_state="completed",
                start_pivot=pivots[index],
                end_pivot=pivots[index + 1],
                invalidation=None,
                child_wave_ids=(),
            )
            for index in range(5)
        )
        hypothesis = AdaptiveRootHypothesis.create(
            hypothesis_id="frozen-parent-hypothesis",
            role=CandidateGraphRole.PRIMARY,
            plan_id=trace.plan.plan_id,
            plan_content_hash=trace.plan.content_hash,
            symbol=trace.plan.symbol,
            analysis_start_utc=trace.plan.analysis_start_utc,
            analysis_cutoff_utc=trace.plan.analysis_cutoff_utc,
            root_timeframe="daily",
            nodes=(parent, *children),
            active_wave_id=None,
            confirmation_conditions=(),
            unresolved_questions=(),
            source_bundle_hashes=(canonical_sha256("frozen-source-bundle"),),
            rules_pack_content_hash=trace.plan.rules_pack.content_hash,
            provider="fixture",
            model=None,
            generated_at_utc=STAMP,
        )
        screen = screen_adaptive_hypothesis(hypothesis, screened_at_utc=STAMP)
        trace = coordinator._replace_trace(
            trace,
            root_pair=AdaptiveRootPair.create(
                primary=hypothesis,
                alternative=None,
                primary_screen=screen,
                alternative_screen=None,
                generated_at_utc=STAMP,
            ),
            status=AdaptiveRecountStatus.CANDIDATES_FROZEN,
            updated_at_utc=STAMP,
        )

        decision = adaptive_parent_reconstruction_decision(
            trace,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=parent.wave_id,
        )

        self.assertTrue(decision.parent_assessment.parent_hard_rule_failed)
        self.assertIn(
            "standard_impulse_wave_4_overlaps_wave_1",
            decision.parent_assessment.failed_parent_rules,
        )
        self.assertEqual(
            decision.parent_status.value,
            "inconsistent",
        )

    def test_inconsistent_descendant_keeps_parent_unproven_for_recount(self) -> None:
        local_unproven = SimpleNamespace(
            status=SubdivisionVerificationStatus.UNPROVEN,
            checks=(
                SimpleNamespace(
                    required=True,
                    passed=None,
                    reason_code=SimpleNamespace(value="diagonal_geometry_unproven"),
                ),
            ),
        )
        child_inconsistent = SimpleNamespace(
            status=SubdivisionVerificationStatus.INCONSISTENT,
            checks=(),
        )
        root_record = SimpleNamespace(
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id="root",
            deterministic_result=local_unproven,
            candidate_graph=SimpleNamespace(
                children=(
                    SimpleNamespace(child_id="child-1", completion_state="completed"),
                )
            ),
        )
        child_record = SimpleNamespace(
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id="child-1",
            deterministic_result=child_inconsistent,
            candidate_graph=SimpleNamespace(children=()),
        )
        trace = SimpleNamespace(child_proofs=(root_record, child_record))

        self.assertEqual(
            effective_adaptive_proof_status(
                trace,
                role=CandidateGraphRole.PRIMARY,
                parent_wave_id="root",
            ),
            SubdivisionVerificationStatus.UNPROVEN,
        )

    def test_effective_status_rejects_verified_cross_family_child_graph(self) -> None:
        parent = SimpleNamespace(
            wave_id="frozen-parent",
            degree="Primary",
            timeframe="daily",
            direction="up",
            declared_family="impulse",
            completion_state="completed",
            start_pivot=SimpleNamespace(),
            end_pivot=SimpleNamespace(),
            invalidation=None,
            diagonal_declaration=None,
        )
        root_pair = SimpleNamespace(
            primary=SimpleNamespace(nodes=(parent,)),
            alternative=None,
        )
        verified = SimpleNamespace(
            status=SubdivisionVerificationStatus.VERIFIED,
            checks=(),
            reason_codes=(),
        )
        mismatch = SimpleNamespace(
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=parent.wave_id,
            deterministic_result=verified,
            candidate_graph=SimpleNamespace(
                declared_family="diagonal",
                children=(),
            ),
        )
        trace = SimpleNamespace(root_pair=root_pair, child_proofs=(mismatch,))

        self.assertEqual(
            effective_adaptive_proof_status(
                trace,
                role=CandidateGraphRole.PRIMARY,
                parent_wave_id=parent.wave_id,
            ),
            SubdivisionVerificationStatus.UNPROVEN,
        )

    def test_effective_status_accepts_verified_matching_family_child_graph(self) -> None:
        parent = SimpleNamespace(
            wave_id="frozen-parent",
            degree="Primary",
            timeframe="daily",
            direction="up",
            declared_family="impulse",
            completion_state="completed",
            start_pivot=SimpleNamespace(),
            end_pivot=SimpleNamespace(),
            invalidation=None,
            diagonal_declaration=None,
        )
        root_pair = SimpleNamespace(
            primary=SimpleNamespace(nodes=(parent,)),
            alternative=None,
        )
        verified = SimpleNamespace(
            status=SubdivisionVerificationStatus.VERIFIED,
            checks=(),
            reason_codes=(),
        )
        matching = SimpleNamespace(
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=parent.wave_id,
            deterministic_result=verified,
            candidate_graph=SimpleNamespace(
                declared_family="impulse",
                children=(),
            ),
        )
        trace = SimpleNamespace(root_pair=root_pair, child_proofs=(matching,))

        self.assertEqual(
            effective_adaptive_proof_status(
                trace,
                role=CandidateGraphRole.PRIMARY,
                parent_wave_id=parent.wave_id,
            ),
            SubdivisionVerificationStatus.VERIFIED,
        )

    def test_native_lineage_accepts_same_feed_sub_tick_price_representation(self) -> None:
        source = NativeCandle.create(
            candle_id="daily-source",
            timestamp_utc="2026-05-27T00:00:00+00:00",
            open=149.72,
            high=151.0,
            low=137.91,
            close=150.23,
            volume=29_193_300.0,
        )
        lower = NativeCandle.create(
            candle_id="four-hour-source",
            timestamp_utc="2026-05-27T17:30:00+00:00",
            open=148.38499,
            high=150.99989,
            low=146.13,
            close=150.23,
            volume=5_685_582.0,
        )
        source_pivot = TypedPivot.create(
            pivot_id="daily-high",
            pivot_type="high",
            timestamp_utc=source.timestamp_utc,
            price=source.high,
            price_field="high",
            timeframe="daily",
            source_window_hash=canonical_sha256("daily-window"),
            source_bar_hash=source.source_row_hash,
        )
        lower_pivot = TypedPivot.create(
            pivot_id="four-hour-high",
            pivot_type="high",
            timestamp_utc=lower.timestamp_utc,
            price=lower.high,
            price_field="high",
            timeframe="4h",
            source_window_hash=canonical_sha256("four-hour-window"),
            source_bar_hash=lower.source_row_hash,
        )

        matched = AdaptiveRecountCoordinator._lineage_pivot(
            source_pivot,
            (lower_pivot,),
        )

        self.assertEqual(matched, lower_pivot)

        outside_tolerance = TypedPivot.create(
            pivot_id="outside-tolerance",
            pivot_type="high",
            timestamp_utc=lower.timestamp_utc,
            price=150.999,
            price_field="high",
            timeframe="4h",
            source_window_hash=canonical_sha256("outside-window"),
            source_bar_hash=lower.source_row_hash,
        )
        self.assertIsNone(
            AdaptiveRecountCoordinator._lineage_pivot(
                source_pivot,
                (outside_tolerance,),
            )
        )

    def test_mcp_unavailability_is_explicit_and_does_not_fabricate_candles(self) -> None:
        result = current_codex_tradingview_mcp_availability(inspected_at_utc=STAMP)
        self.assertFalse(result.raw_ohlcv_available)
        self.assertIn("mcp__codex_apps__tradingcursor_request_analysis", result.inspected_tool_names)
        self.assertIn("does not expose raw OHLCV", result.exact_limitation)

    def test_bundle_rejects_incomplete_or_derived_rows(self) -> None:
        with self.assertRaises(ValueError):
            NativeCandle.create(
                candle_id="bad",
                timestamp_utc=START,
                open=1,
                high=2,
                low=0,
                close=1,
                volume=1,
                completed=False,
            )
        coordinator, trace, manifest = plan_and_manifest()
        good = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        raw = good.to_dict()
        raw.update({"native": False, "derived": True, "content_hash": "", "file_hash": ""})
        with self.assertRaises(ValueError):
            NativeOHLCVBundle.create(**raw)

    def test_cross_feed_indicator_and_volume_comparisons_are_quarantined(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        left = bundle_for_manifest(manifest, trace.plan.provider_capabilities, feed="feed-a")
        right = bundle_for_manifest(manifest, trace.plan.provider_capabilities, feed="feed-b")
        volume = compare_bundle_identity(left, right, purpose=DataComparisonPurpose.SAME_TIMEFRAME_VOLUME)
        indicator = compare_bundle_identity(left, right, purpose=DataComparisonPurpose.SAME_WAVE_INDICATOR)
        self.assertEqual(volume.status, DataComparabilityStatus.INCOMPARABLE)
        self.assertEqual(indicator.status, DataComparabilityStatus.INCOMPARABLE)
        self.assertIn("legacy_feed_identity", volume.incomparable_fields)

    def test_native_bundle_is_readable_by_legacy_market_summarizer_without_rewrite(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        with tempfile.TemporaryDirectory() as directory:
            path = write_immutable_bundle(bundle, Path(directory))
            metadata = load_market_metadata(path)
            candles = load_candles(path)
            self.assertEqual(infer_timeframe(path), bundle.timeframe)
            self.assertEqual(metadata["feed_identity"], bundle.feed_identity)
            self.assertEqual(len(candles), len(bundle.candles))
            self.assertEqual(candles[-1]["parsed_date"].isoformat(), bundle.actual_final_completed_candle_utc)

    def test_completed_weekly_bar_open_label_covers_its_friday_cutoff(self) -> None:
        provider_capabilities = capabilities("weekly")
        manifest = DataRequestManifest.create(
            request_id="weekly-cutoff-regression",
            planner_request_id="weekly-cutoff-regression",
            planner_request_hash="1" * 64,
            planner_decision_hash="2" * 64,
            symbol="NASDAQ:RKLB",
            provider="fixture-feed",
            source_interface="scripts.test_adaptive_recount.FakeMarketDataProvider",
            provider_capability_set_hash=provider_capabilities.content_hash,
            canonical_timeframe="weekly",
            provider_interval_alias="fixture-weekly",
            parent_candidate_id="analysis_window",
            parent_degree="Cycle",
            expected_child_degree="Primary",
            schema_version="adaptive-data-request-manifest-1.3.0",
            structural_question=StructuralQuestion.DISTINGUISH_IMPULSE_FROM_CORRECTION,
            requested_start_utc="2026-01-01T00:00:00Z",
            requested_end_utc="2026-08-28T20:00:00Z",
            analysis_cutoff_utc="2026-08-28T20:00:00Z",
            session_policy="nasdaq_regular_session",
            adjustment_policy="splits",
            require_native=True,
            completed_candles_only=True,
            required_metadata_fields=("canonical_symbol",),
            created_at_utc=STAMP,
        )
        rows = tuple(
            NativeCandle.create(
                candle_id=f"weekly-{timestamp[:10]}",
                timestamp_utc=timestamp,
                open=open_price,
                high=open_price + 2.0,
                low=open_price - 1.0,
                close=open_price + 1.0,
                volume=1000.0,
            )
            for timestamp, open_price in (
                ("2025-12-29T00:00:00Z", 10.0),
                ("2026-08-24T00:00:00Z", 20.0),
            )
        )
        bundle = NativeOHLCVBundle.create(
            bundle_id="weekly-cutoff-regression-bundle",
            source_request_id=manifest.request_id,
            source_request_hash=manifest.content_hash,
            provider_capability_set_hash=provider_capabilities.content_hash,
            canonical_symbol="NASDAQ:RKLB",
            provider_symbol="RKLB",
            exchange="NASDAQ",
            provider="fixture-feed",
            source_interface=manifest.source_interface,
            feed_identity="fixture-feed",
            session="nasdaq_regular_session",
            timezone="America/New_York",
            price_adjustment="splits",
            dividend_adjustment="unadjusted",
            volume_adjustment="split_adjusted",
            price_basis="split_adjusted_dividend_unadjusted_ohlc",
            timeframe="weekly",
            provider_interval_alias="fixture-weekly",
            native=True,
            derived=False,
            requested_start_utc=manifest.requested_start_utc,
            requested_end_utc=manifest.requested_end_utc,
            actual_first_candle_utc=rows[0].timestamp_utc,
            actual_final_completed_candle_utc=rows[-1].timestamp_utc,
            acquisition_timestamp_utc="2026-08-29T12:00:00Z",
            analysis_cutoff_utc=manifest.analysis_cutoff_utc,
            timestamp_semantics="bar_open_utc_with_provider_interval",
            completion_verification_method="provider_completed_week_after_official_session_close",
            incomplete_candles_excluded=True,
            source_response_hash=canonical_sha256([item.to_dict() for item in rows]),
            candles=rows,
        )
        result = validate_bundle_for_manifest(
            manifest,
            bundle,
            provider_capabilities,
        )
        self.assertEqual(result.status, BundleValidationStatus.VALID)
        self.assertTrue(result.native_complete_coverage)

    def test_blind_root_packet_has_no_prior_rklb_analysis_or_peer_output(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        packet = build_blind_root_packet(trace.plan, (bundle,), role=CandidateGraphRole.PRIMARY)
        serialized_rules = json.dumps(packet["rules_pack"], sort_keys=True).lower()
        self.assertNotIn("rklb", serialized_rules)
        self.assertFalse(packet["blind_isolation"]["peer_candidate_available"])
        self.assertFalse(packet["blind_isolation"]["prior_symbol_counts_available"])
        self.assertTrue(packet["instructions"]["indicators_not_supplied"])
        self.assertNotIn("previous_count", json.dumps(packet, sort_keys=True))

    def test_root_schema_binds_validated_timeframe_and_compatible_degrees(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        manifest_values = manifest.to_dict()
        manifest_values.pop("content_hash")
        manifest_values["canonical_timeframe"] = "weekly"
        manifest_values["provider_interval_alias"] = "fixture-weekly"
        weekly_manifest = DataRequestManifest.create(**manifest_values)
        bundle = bundle_for_manifest(weekly_manifest, trace.plan.provider_capabilities)
        packet = build_blind_root_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
        )

        schema = adaptive_root_output_schema_for_packet(packet)
        properties = schema["properties"]
        node_properties = properties["nodes"]["items"]["properties"]

        self.assertEqual(properties["root_timeframe"]["enum"], ["weekly"])
        self.assertEqual(node_properties["timeframe"]["enum"], ["weekly"])
        self.assertEqual(
            set(node_properties["degree"]["enum"]),
            {"Grand Supercycle", "Supercycle", "Cycle"},
        )
        self.assertNotIn("Primary", node_properties["degree"]["enum"])

    def test_child_schema_binds_parent_family_and_admissible_child_families(self) -> None:
        schema = graph_generation_output_schema_for_packet(
            {
                "parent_candidate": {
                    "declared_family": "diagonal",
                }
            }
        )
        properties = schema["properties"]
        child_schema = properties["children"]
        variants = child_schema["items"]["anyOf"]

        self.assertEqual(properties["declared_family"]["enum"], ["diagonal"])
        self.assertEqual(child_schema["minItems"], 5)
        self.assertEqual(child_schema["maxItems"], 5)
        self.assertEqual(
            [item["properties"]["sequence_position"]["enum"] for item in variants],
            [["1"], ["2"], ["3"], ["4"], ["5"]],
        )
        self.assertEqual(
            set(variants[0]["properties"]["declared_family"]["enum"]),
            {"impulse", "diagonal", "zigzag"},
        )
        self.assertEqual(
            set(variants[1]["properties"]["declared_family"]["enum"]),
            {"zigzag"},
        )

    def test_declared_diagonal_type_binds_exact_internal_family_template(self) -> None:
        common = {
            "geometry": "contracting",
            "wave5_termination": "normal",
            "rule_policy_version": "fixture-diagonal-policy",
        }
        ending = graph_generation_output_schema_for_packet(
            {
                "parent_candidate": {
                    "declared_family": "diagonal",
                    "diagonal_declaration": {
                        **common,
                        "diagonal_type": "ending",
                    },
                }
            }
        )
        ending_variants = ending["properties"]["children"]["items"]["anyOf"]
        self.assertTrue(
            all(
                item["properties"]["declared_family"]["enum"] == ["zigzag"]
                for item in ending_variants
            )
        )

        leading = graph_generation_output_schema_for_packet(
            {
                "parent_candidate": {
                    "declared_family": "diagonal",
                    "diagonal_declaration": {
                        **common,
                        "diagonal_type": "leading",
                    },
                }
            }
        )
        leading_variants = leading["properties"]["children"]["items"]["anyOf"]
        self.assertEqual(
            set(leading_variants[0]["properties"]["declared_family"]["enum"]),
            {"impulse", "diagonal"},
        )
        self.assertEqual(
            leading_variants[1]["properties"]["declared_family"]["enum"],
            ["zigzag"],
        )

    def test_primary_and_alternative_calls_are_independent_and_distinct(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        provider = FakeRootProvider()
        result = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=provider,
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        self.assertEqual([item[0] for item in provider.calls], [CandidateGraphRole.PRIMARY, CandidateGraphRole.ALTERNATIVE])
        self.assertFalse(provider.calls[0][1]["blind_isolation"]["peer_candidate_available"])
        self.assertFalse(provider.calls[1][1]["blind_isolation"]["peer_candidate_available"])
        self.assertNotEqual(result.root_pair.primary.content_hash, result.root_pair.alternative.content_hash)

    def test_one_rejected_candidate_does_not_erase_its_valid_peer(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        result = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=OneInvalidRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        self.assertEqual(result.root_pair.primary_screen.status, CandidateScreenStatus.REJECTED)
        self.assertEqual(
            result.root_pair.alternative_screen.status,
            CandidateScreenStatus.SURVIVING_UNPROVEN,
        )
        self.assertEqual(result.stop_reason, AdaptiveStopReason.ONE_VALID_CANDIDATE)
        self.assertIsNotNone(result.root_pair.alternative)

    def test_strict_provider_wrapper_uses_exact_schema_and_fake_transport(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        packet = build_blind_root_packet(
            trace.plan,
            (bundle,),
            role=CandidateGraphRole.PRIMARY,
        )
        transport = FakeStrictTransport()
        provider = AdaptiveRecountStructuredProvider(transport)
        output_schema = adaptive_root_output_schema_for_packet(packet)
        result = provider.generate_root_candidate(
            role=CandidateGraphRole.PRIMARY,
            packet=packet,
            output_schema=output_schema,
        )
        self.assertEqual(result, {"fixture": "candidate-only"})
        self.assertEqual(provider.call_accounting()["attempted_provider_calls"], 1)
        self.assertEqual(provider.call_accounting()["returned_provider_calls"], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("previous_count", transport.calls[0]["user_prompt"])

    def test_identical_renamed_alternative_is_rejected(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        provider = FakeRootProvider(duplicate=True)
        with self.assertRaises(ValueError):
            coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
            )

    def test_no_provider_call_after_uningested_bundle_preflight_failure(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        provider = FakeRootProvider()
        with self.assertRaises(ValueError):
            coordinator.generate_initial_candidates(
                trace,
                (bundle,),
                provider=provider,
                allow_model_calls=True,
                generated_at_utc=STAMP,
            )
        self.assertEqual(provider.calls, [])

    def test_unproven_candidate_is_never_reported_as_verified(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        trace = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=FakeRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        report = render_adaptive_recount_report(trace)
        self.assertIn("unproven", report)
        self.assertNotIn("| verified |", report)
        self.assertIn("Model-generated labels and prose remain candidate evidence", report)

    def test_elliott_full_enables_rsi_without_changing_legacy_defaults(self) -> None:
        self.assertNotIn("rsi", DEFAULT_CANDLE_FEATURES)
        self.assertIn("rsi", features_for_profile("elliott_full"))
        self.assertIn("ewo", features_for_profile("elliott_full"))
        self.assertIn("macd", features_for_profile("elliott_full"))

    def test_trace_artifacts_are_immutable_and_hash_validated(self) -> None:
        coordinator, trace, manifest = plan_and_manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = write_immutable_adaptive_trace(trace, Path(directory))
            loaded = load_adaptive_recount_trace(path)
            self.assertEqual(loaded.content_hash, trace.content_hash)
            with self.assertRaises(FileExistsError):
                write_immutable_adaptive_trace(trace, Path(directory))
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["warnings"] = ["tampered"]
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_adaptive_recount_trace(path)


class AdaptiveBoundaryAndCliTests(unittest.TestCase):
    def test_file_only_cli_plan_does_not_create_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "must-not-exist.sqlite3"
            artifacts = Path(directory) / "artifacts"
            output = io.StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "--database",
                        str(database),
                        "adaptive-recount",
                        "plan",
                        "--symbol",
                        "NASDAQ:RKLB",
                        "--start",
                        START,
                        "--cutoff",
                        CUTOFF,
                        "--market-data-provider",
                        "tradingview-mcp",
                        "--output-dir",
                        str(artifacts),
                    ]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(result["trace_status"], "not_covered")
            self.assertFalse(database.exists())
            self.assertTrue(Path(result["trace_file"]).exists())

    def test_final_report_not_ready_cannot_cross_authoritative_freeze_boundary(self) -> None:
        coordinator, trace, bundle = ready_trace()
        self.assertEqual(trace.status, AdaptiveRecountStatus.READY_FOR_FINAL_COMPARISON)

        class FakeFinalAgent(ElliottAgent):
            def analyze(self, request):
                self.last_request = request
                return {"run_id": 41, "validation_errors": []}

            def resolve_degrees(self, source_run_id, question=None):
                self.resolved_run_id = source_run_id
                return {
                    "resolution_id": 73,
                    "validation_errors": [],
                    "readiness": {
                        "final_report_ready": False,
                        "hierarchy_valid": True,
                        "blockers": ["Completed parent subdivision remains unproven."],
                    },
                }

        agent = FakeFinalAgent(workspace=Path.cwd(), store=object(), provider=object())
        result = agent.finalize_adaptive_recount(
            trace,
            market_data_paths=("fixture-bundle.json",),
            finalized_at_utc=STAMP,
        )
        self.assertFalse(result["frozen"])
        self.assertEqual(result["blocker"], "degree_resolution_not_ready")
        self.assertIsNone(result["adaptive_trace"]["source_analysis_run_id"])
        self.assertIsNone(
            result["adaptive_trace"]["authoritative_degree_resolution_id"]
        )
        self.assertEqual(result["adaptive_trace"]["status"], "ready_for_final_comparison")
        self.assertTrue(agent.last_request.blind)
        self.assertEqual(agent.last_request.metadata["phase11_activation"], False)

    def test_degree_resolution_validation_error_blocks_freeze(self) -> None:
        coordinator, trace, bundle = ready_trace()

        class InvalidResolutionAgent(ElliottAgent):
            def analyze(self, request):
                return {"run_id": 42, "validation_errors": []}

            def resolve_degrees(self, source_run_id, question=None):
                return {
                    "resolution_id": 74,
                    "validation_errors": ["hierarchy invalid"],
                    "readiness": {
                        "final_report_ready": True,
                        "hierarchy_valid": False,
                        "blockers": [],
                    },
                }

        agent = InvalidResolutionAgent(
            workspace=Path.cwd(), store=object(), provider=object()
        )
        result = agent.finalize_adaptive_recount(
            trace,
            market_data_paths=("fixture-bundle.json",),
            finalized_at_utc=STAMP,
        )
        self.assertFalse(result["frozen"])
        self.assertEqual(result["blocker"], "degree_resolution_validation_failed")
        self.assertIsNone(
            result["adaptive_trace"]["authoritative_degree_resolution_id"]
        )

    def test_unresolved_completed_parent_readiness_blocker_prevents_freeze(self) -> None:
        coordinator, trace, bundle = ready_trace()

        class UnresolvedParentAgent(ElliottAgent):
            def analyze(self, request):
                return {"run_id": 43, "validation_errors": []}

            def resolve_degrees(self, source_run_id, question=None):
                return {
                    "resolution_id": 75,
                    "validation_errors": [],
                    "readiness": {
                        "final_report_ready": False,
                        "hierarchy_valid": True,
                        "blockers": [
                            "At least one completed parent still has unproven internal structure."
                        ],
                    },
                }

        agent = UnresolvedParentAgent(
            workspace=Path.cwd(), store=object(), provider=object()
        )
        result = agent.finalize_adaptive_recount(
            trace,
            market_data_paths=("fixture-bundle.json",),
            finalized_at_utc=STAMP,
        )
        self.assertFalse(result["frozen"])
        self.assertEqual(result["blocker"], "degree_resolution_not_ready")

    def test_valid_ready_resolution_is_the_only_successful_freeze(self) -> None:
        coordinator, trace, bundle = ready_trace()

        class ReadyResolutionAgent(ElliottAgent):
            def analyze(self, request):
                return {"run_id": 44, "validation_errors": []}

            def resolve_degrees(self, source_run_id, question=None):
                return {
                    "resolution_id": 76,
                    "validation_errors": [],
                    "readiness": {
                        "final_report_ready": True,
                        "hierarchy_valid": True,
                        "blockers": [],
                    },
                }

        agent = ReadyResolutionAgent(
            workspace=Path.cwd(), store=object(), provider=object()
        )
        result = agent.finalize_adaptive_recount(
            trace,
            market_data_paths=("fixture-bundle.json",),
            finalized_at_utc=STAMP,
        )
        self.assertTrue(result["frozen"])
        self.assertEqual(result["adaptive_trace"]["source_analysis_run_id"], 44)
        self.assertEqual(
            result["adaptive_trace"]["authoritative_degree_resolution_id"],
            76,
        )
        self.assertEqual(
            result["adaptive_trace"]["status"],
            "frozen_by_resolve_degrees",
        )

    def test_frozen_contract_cannot_be_mutated(self) -> None:
        coordinator, trace, bundle = ready_trace()
        with self.assertRaises(FrozenInstanceError):
            trace.status = AdaptiveRecountStatus.FROZEN_BY_RESOLVE_DEGREES  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
