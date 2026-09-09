from __future__ import annotations

import unittest

from elliott_ai.adaptive_recount import (
    AdaptiveRecountBudget,
    AdaptiveRecountCoordinator,
    create_adaptive_recount_plan,
)
from elliott_ai.adaptive_market_data import DataRequestManifest
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.adaptive_timeframe_planner import (
    AdaptiveTimeframePlanner,
    AdaptiveTimeframeRequest,
    ParentProofFeasibility,
    ParentProofFeasibilityStatus,
    ROOT_TIMEFRAME_DECISION_SCHEMA_VERSION,
    ROOT_TIMEFRAME_PLANNER_POLICY_VERSION,
    StructuralQuestion,
    TimeframePlanningReason,
    TimeframePlanningStatus,
)
from elliott_ai.timeframes import (
    ProviderCapabilitySet,
    ProviderTimeframeCapability,
)
from scripts.test_adaptive_recount import (
    FakeRootProvider,
    STAMP,
    bundle_for_manifest,
)


ANALYSIS_START = "2026-05-27T00:00:00+00:00"
CONTEXT_START = "2026-01-01T00:00:00+00:00"
CUTOFF = "2026-08-28T20:00:00+00:00"


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
        source_interface="scripts.test_adaptive_timeframe_planner_boundaries",
        discovered_at_utc=STAMP,
        limitations=(),
    )


def planned_root(*, context_start_utc: str):
    provider_capabilities = capabilities("weekly", "2d", "daily", "12h", "4h", "1h")
    plan = create_adaptive_recount_plan(
        symbol="NASDAQ:RKLB",
        analysis_start_utc=ANALYSIS_START,
        context_start_utc=context_start_utc,
        analysis_cutoff_utc=CUTOFF,
        market_data_provider=provider_capabilities.provider,
        provider_capabilities=provider_capabilities,
        created_at_utc=STAMP,
        budgets=AdaptiveRecountBudget(),
    )
    coordinator = AdaptiveRecountCoordinator()
    trace = coordinator.plan_initial_data(
        coordinator.initial_trace(plan),
        created_at_utc=STAMP,
        source_interface=provider_capabilities.source_interface,
    )
    return coordinator, trace


def recursive_request(
    *,
    parent_degree: str,
    parent_timeframe: str,
    expected_child_degree: str,
    requested_timeframe: str,
) -> tuple[AdaptiveTimeframeRequest, ProviderCapabilitySet]:
    provider_capabilities = capabilities("weekly", "daily", "4h", "1h", "30m")
    request = AdaptiveTimeframeRequest.create(
        request_id="",
        symbol="NASDAQ:RKLB",
        parent_candidate_id=f"primary:{parent_degree}",
        parent_degree=parent_degree,
        parent_timeframe=parent_timeframe,
        parent_start_utc="2026-05-27T00:00:00+00:00",
        parent_end_utc="2026-07-27T00:00:00+00:00",
        structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
        expected_child_degree=expected_child_degree,
        current_evidence_coverage=(),
        desired_native_bar_minimum=100,
        desired_native_bar_maximum=140,
        provider_capability_set_hash=provider_capabilities.content_hash,
        provider_supported_intervals=provider_capabilities.supported_timeframes,
        requested_timeframe=requested_timeframe,
        reason_code="recursive-compatibility-regression",
        cutoff_utc=CUTOFF,
        maximum_request_budget=8,
        requests_already_used=1,
        maximum_bar_budget=25000,
        created_at_utc=STAMP,
    )
    return request, provider_capabilities


class StructuralAndFetchWindowTests(unittest.TestCase):
    def test_rklb_root_scores_analysis_window_but_fetches_context(self) -> None:
        _coordinator, trace = planned_root(context_start_utc=CONTEXT_START)
        request = trace.planner_requests[-1]
        decision = trace.planner_decisions[-1]
        manifest = trace.data_manifests[-1]

        self.assertEqual(request.parent_start_utc, ANALYSIS_START)
        self.assertEqual(request.parent_end_utc, CUTOFF)
        self.assertEqual(request.requested_data_start_utc, CONTEXT_START)
        self.assertEqual(request.requested_data_end_utc, CUTOFF)
        self.assertEqual(decision.selected_timeframe, "daily")
        daily = next(item for item in decision.assessments if item.timeframe == "daily")
        self.assertEqual(daily.estimated_native_bars, 94)

        self.assertEqual(manifest.structural_scoring_start_utc, ANALYSIS_START)
        self.assertEqual(manifest.structural_scoring_end_utc, CUTOFF)
        self.assertEqual(manifest.requested_start_utc, CONTEXT_START)
        self.assertEqual(manifest.requested_end_utc, CUTOFF)

    def test_context_length_cannot_change_root_timeframe_selection(self) -> None:
        _short_coordinator, short_trace = planned_root(
            context_start_utc=ANALYSIS_START
        )
        _long_coordinator, long_trace = planned_root(
            context_start_utc=CONTEXT_START
        )

        short_decision = short_trace.planner_decisions[-1]
        long_decision = long_trace.planner_decisions[-1]
        self.assertEqual(short_decision.selected_timeframe, "daily")
        self.assertEqual(
            short_decision.selected_timeframe,
            long_decision.selected_timeframe,
        )
        self.assertEqual(
            short_decision.estimated_native_bars,
            long_decision.estimated_native_bars,
        )
        self.assertNotEqual(
            short_trace.data_manifests[-1].requested_start_utc,
            long_trace.data_manifests[-1].requested_start_utc,
        )

    def test_root_discovery_remains_degree_neutral(self) -> None:
        _coordinator, trace = planned_root(context_start_utc=CONTEXT_START)
        request = trace.planner_requests[-1]
        decision = trace.planner_decisions[-1]
        manifest = trace.data_manifests[-1]
        self.assertIs(request.structural_question, StructuralQuestion.DISCOVER_ROOT_STRUCTURE)
        self.assertIsNone(request.parent_degree)
        self.assertIsNone(request.expected_child_degree)
        self.assertIsNone(request.parent_timeframe)
        self.assertEqual(request.policy_version, ROOT_TIMEFRAME_PLANNER_POLICY_VERSION)
        self.assertEqual(decision.schema_version, ROOT_TIMEFRAME_DECISION_SCHEMA_VERSION)
        self.assertEqual(decision.policy_version, ROOT_TIMEFRAME_PLANNER_POLICY_VERSION)
        self.assertEqual(manifest.schema_version, "adaptive-data-request-manifest-1.2.0")

    def test_legacy_request_and_manifest_serialization_preserve_old_hash_shape(self) -> None:
        provider_capabilities = capabilities("daily")
        request_payload = {
            "request_id": "legacy-request",
            "symbol": "NASDAQ:RKLB",
            "parent_candidate_id": "analysis_scope",
            "parent_degree": None,
            "parent_timeframe": None,
            "parent_start_utc": CONTEXT_START,
            "parent_end_utc": CUTOFF,
            "structural_question": "discover_root_structure",
            "expected_child_degree": None,
            "current_evidence_coverage": [],
            "desired_native_bar_minimum": 100,
            "desired_native_bar_maximum": 140,
            "provider_capability_set_hash": provider_capabilities.content_hash,
            "provider_supported_intervals": ["daily"],
            "requested_timeframe": None,
            "reason_code": "legacy",
            "cutoff_utc": CUTOFF,
            "maximum_request_budget": 8,
            "requests_already_used": 0,
            "maximum_bar_budget": 25000,
            "created_at_utc": STAMP,
            "schema_version": "adaptive-timeframe-request-1.1.0",
            "policy_version": "adaptive-timeframe-planner-policy-1.1.0",
        }
        request_payload["content_hash"] = canonical_sha256(request_payload)
        legacy_request = AdaptiveTimeframeRequest.from_dict(request_payload)
        self.assertEqual(legacy_request.to_dict(), request_payload)

        manifest_payload = {
            "request_id": "legacy-request",
            "planner_request_id": "legacy-request",
            "planner_request_hash": request_payload["content_hash"],
            "planner_decision_hash": "2" * 64,
            "symbol": "NASDAQ:RKLB",
            "provider": "fixture-feed",
            "source_interface": "legacy-fixture",
            "provider_capability_set_hash": provider_capabilities.content_hash,
            "canonical_timeframe": "daily",
            "provider_interval_alias": "fixture-daily",
            "parent_candidate_id": "analysis_scope",
            "parent_degree": None,
            "expected_child_degree": None,
            "structural_question": "discover_root_structure",
            "requested_start_utc": CONTEXT_START,
            "requested_end_utc": CUTOFF,
            "analysis_cutoff_utc": CUTOFF,
            "session_policy": "nasdaq_regular_session",
            "adjustment_policy": "split_adjusted_dividend_unadjusted",
            "require_native": True,
            "completed_candles_only": True,
            "required_metadata_fields": ["canonical_symbol"],
            "created_at_utc": STAMP,
            "schema_version": "adaptive-data-request-manifest-1.1.0",
        }
        manifest_payload["content_hash"] = canonical_sha256(manifest_payload)
        legacy_manifest = DataRequestManifest.from_dict(manifest_payload)
        self.assertEqual(legacy_manifest.to_dict(), manifest_payload)

    def test_version_1_2_manifest_keeps_its_structural_window_hash_shape(self) -> None:
        provider_capabilities = capabilities("daily")
        payload = {
            "request_id": "root-v12",
            "planner_request_id": "root-v12",
            "planner_request_hash": "1" * 64,
            "planner_decision_hash": "2" * 64,
            "symbol": "NASDAQ:RKLB",
            "provider": "fixture-feed",
            "source_interface": "fixture-v12",
            "provider_capability_set_hash": provider_capabilities.content_hash,
            "canonical_timeframe": "daily",
            "provider_interval_alias": "fixture-daily",
            "parent_candidate_id": "analysis_scope",
            "parent_degree": None,
            "expected_child_degree": None,
            "structural_question": "discover_root_structure",
            "requested_start_utc": CONTEXT_START,
            "requested_end_utc": CUTOFF,
            "analysis_cutoff_utc": CUTOFF,
            "session_policy": "nasdaq_regular_session",
            "adjustment_policy": "split_adjusted_dividend_unadjusted",
            "require_native": True,
            "completed_candles_only": True,
            "required_metadata_fields": ["canonical_symbol"],
            "created_at_utc": STAMP,
            "structural_scoring_start_utc": ANALYSIS_START,
            "structural_scoring_end_utc": CUTOFF,
            "schema_version": "adaptive-data-request-manifest-1.2.0",
        }
        payload["content_hash"] = canonical_sha256(payload)

        restored = DataRequestManifest.from_dict(payload)

        self.assertEqual(restored.to_dict(), payload)


class RecursiveDegreeCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _automatic_request(
        *timeframes: str,
        parent_degree: str = "Primary",
        parent_timeframe: str = "4h",
        child_degree: str = "Intermediate",
        maximum_bar_budget: int = 25000,
    ):
        provider_capabilities = capabilities(*timeframes)
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol="NASDAQ:RKLB",
            parent_candidate_id="primary:run-21-shaped-parent",
            parent_degree=parent_degree,
            parent_timeframe=parent_timeframe,
            parent_start_utc="2026-05-27T17:30:00+00:00",
            parent_end_utc="2026-07-29T17:30:00+00:00",
            structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
            expected_child_degree=child_degree,
            current_evidence_coverage=(),
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=provider_capabilities.content_hash,
            provider_supported_intervals=provider_capabilities.supported_timeframes,
            requested_timeframe=None,
            reason_code="proof-feasibility-regression",
            cutoff_utc=CUTOFF,
            maximum_request_budget=8,
            requests_already_used=1,
            maximum_bar_budget=maximum_bar_budget,
            created_at_utc=STAMP,
        )
        return request, provider_capabilities

    def test_primary_parent_accepts_finer_weekly_evidence_for_intermediate_child(self) -> None:
        request, provider_capabilities = recursive_request(
            parent_degree="Primary",
            parent_timeframe="monthly",
            expected_child_degree="Intermediate",
            requested_timeframe="weekly",
        )
        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "weekly")
        self.assertEqual(decision.proof_feasibility.expected_child_degree, "Intermediate")

    def test_intermediate_parent_accepts_one_hour_evidence_for_minor_child(self) -> None:
        request, provider_capabilities = recursive_request(
            parent_degree="Intermediate",
            parent_timeframe="daily",
            expected_child_degree="Minor",
            requested_timeframe="1h",
        )
        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        one_hour = next(item for item in decision.assessments if item.timeframe == "1h")
        self.assertTrue(one_hour.useful)
        self.assertEqual(one_hour.reason, "native useful candidate")

    def test_intermediate_four_hour_parent_resolves_minor_on_one_hour(self) -> None:
        request, provider_capabilities = self._automatic_request(
            "4h",
            "1h",
            "15m",
            parent_degree="Intermediate",
            parent_timeframe="4h",
            child_degree="Minor",
        )

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "1h")
        self.assertEqual(decision.proof_feasibility.parent_degree, "Intermediate")
        self.assertEqual(decision.proof_feasibility.expected_child_degree, "Minor")

    def test_minor_one_hour_parent_resolves_minute_on_fifteen_minutes(self) -> None:
        request, provider_capabilities = self._automatic_request(
            "1h",
            "15m",
            parent_degree="Minor",
            parent_timeframe="1h",
            child_degree="Minute",
        )

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "15m")
        self.assertEqual(decision.proof_feasibility.parent_degree, "Minor")
        self.assertEqual(decision.proof_feasibility.expected_child_degree, "Minute")

    def test_compatible_recursive_timeframe_remains_plannable(self) -> None:
        request, provider_capabilities = recursive_request(
            parent_degree="Intermediate",
            parent_timeframe="daily",
            expected_child_degree="Minor",
            requested_timeframe="4h",
        )
        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "4h")

    def test_coordinator_emits_manifest_for_relationally_valid_one_hour_request(self) -> None:
        coordinator, trace = planned_root(context_start_utc=CONTEXT_START)
        manifest = trace.data_manifests[-1]
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        trace = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=FakeRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        manifest_count = len(trace.data_manifests)

        trace = coordinator.plan_parent_subdivision(
            trace,
            role="primary",
            parent_wave_id="primary-root",
            bundles=(bundle,),
            created_at_utc=STAMP,
            requested_timeframe="1h",
            source_interface=trace.plan.provider_capabilities.source_interface,
        )

        self.assertEqual(len(trace.data_manifests), manifest_count + 1)
        self.assertEqual(
            trace.planner_decisions[-1].status,
            TimeframePlanningStatus.PLANNED,
        )
        self.assertEqual(trace.data_manifests[-1].canonical_timeframe, "1h")

    def test_coordinator_emits_manifest_for_compatible_recursive_request(self) -> None:
        coordinator, trace = planned_root(context_start_utc=CONTEXT_START)
        manifest = trace.data_manifests[-1]
        bundle = bundle_for_manifest(manifest, trace.plan.provider_capabilities)
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)
        trace = coordinator.generate_initial_candidates(
            trace,
            (bundle,),
            provider=FakeRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        manifest_count = len(trace.data_manifests)

        trace = coordinator.plan_parent_subdivision(
            trace,
            role="primary",
            parent_wave_id="primary-root",
            bundles=(bundle,),
            created_at_utc=STAMP,
            requested_timeframe="4h",
            source_interface=trace.plan.provider_capabilities.source_interface,
        )

        self.assertEqual(len(trace.data_manifests), manifest_count + 1)
        self.assertEqual(
            trace.planner_decisions[-1].status,
            TimeframePlanningStatus.PLANNED,
        )
        self.assertEqual(trace.data_manifests[-1].expected_child_degree, "Intermediate")

    def test_exact_primary_four_hour_case_selects_native_one_hour(self) -> None:
        request, provider_capabilities = self._automatic_request(
            "4h", "1h", "15m"
        )

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "1h")
        feasibility = decision.proof_feasibility
        self.assertIsNotNone(feasibility)
        self.assertEqual(feasibility.status, ParentProofFeasibilityStatus.FEASIBLE)
        self.assertEqual(feasibility.parent_degree, "Primary")
        self.assertEqual(feasibility.expected_child_degree, "Intermediate")
        self.assertEqual(feasibility.parent_evidence_timeframe, "4h")
        self.assertEqual(feasibility.viable_timeframes, ("1h", "15m"))
        self.assertEqual(
            ParentProofFeasibility.from_dict(feasibility.to_dict()),
            feasibility,
        )
        tampered = feasibility.to_dict()
        tampered["selected_timeframe"] = "15m"
        with self.assertRaisesRegex(ValueError, "content_hash"):
            ParentProofFeasibility.from_dict(tampered)

    def test_fifteen_minute_is_a_deterministic_fallback(self) -> None:
        request, provider_capabilities = self._automatic_request("4h", "15m")

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)
        self.assertEqual(decision.selected_timeframe, "15m")

    def test_no_finer_native_interval_is_not_covered(self) -> None:
        request, provider_capabilities = self._automatic_request("daily", "4h")

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.NOT_COVERED)
        self.assertEqual(
            decision.proof_feasibility.status,
            ParentProofFeasibilityStatus.NOT_COVERED,
        )

    def test_all_finer_intervals_over_hard_bar_budget_are_budget_exhausted(self) -> None:
        request, provider_capabilities = self._automatic_request(
            "4h", "1h", "15m", maximum_bar_budget=100
        )

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(decision.status, TimeframePlanningStatus.BUDGET_EXHAUSTED)
        self.assertEqual(decision.reason_code, TimeframePlanningReason.BAR_BUDGET_EXCEEDED)
        self.assertEqual(
            decision.proof_feasibility.status,
            ParentProofFeasibilityStatus.BUDGET_EXHAUSTED,
        )

    def test_valid_finer_interval_outside_preferred_range_remains_usable(self) -> None:
        request, provider_capabilities = recursive_request(
            parent_degree="Primary",
            parent_timeframe="4h",
            expected_child_degree="Intermediate",
            requested_timeframe="1h",
        )

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        one_hour = next(item for item in decision.assessments if item.timeframe == "1h")
        self.assertGreater(one_hour.estimated_native_bars, 140)
        self.assertEqual(decision.status, TimeframePlanningStatus.PLANNED)

    def test_same_degree_request_is_a_configuration_error(self) -> None:
        request, provider_capabilities = self._automatic_request(
            "4h", "1h", child_degree="Primary"
        )

        decision = AdaptiveTimeframePlanner().plan(request, provider_capabilities)

        self.assertEqual(
            decision.status,
            TimeframePlanningStatus.CONFIGURATION_ERROR,
        )
        self.assertIn(
            "direct_child_degree_descent_invalid",
            decision.proof_feasibility.blockers,
        )


if __name__ == "__main__":
    unittest.main()
