from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from elliott_ai.current_scenario_generator import (
    CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION,
)
from elliott_ai.current_state import CurrentStateBuilder
from elliott_ai.historical_market import HistoricalMoveType
from elliott_ai.historical_patterns import PatternDirection
from elliott_ai.market_scenario import ValidationIssue, ValidationResult
from elliott_ai.market_scenario_orchestrator import (
    ExecutionMode,
    MarketScenarioOrchestrationInput,
    MarketScenarioOrchestrationResult,
    MarketScenarioOrchestrator,
    MarketScenarioReplayPacket,
    MarketScenarioReplayResult,
    OrchestrationStage,
    OrchestrationStatus,
    PatternRetrievalConfiguration,
    ScenarioGenerationConfiguration,
    market_scenario_orchestration_input_content_hash,
    market_scenario_orchestration_result_content_hash,
    market_scenario_replay_packet_content_hash,
    market_scenario_replay_result_content_hash,
    replay_market_scenario_packet,
    validate_market_scenario_orchestration_input,
    validate_market_scenario_orchestration_result,
)
from elliott_ai.pattern_retrieval import (
    PatternRetrievalEngine,
    create_pattern_retrieval_query,
)
from elliott_ai.reasoning_audit import (
    AuditNodeType,
    reasoning_audit_graph_content_hash,
    validate_reasoning_audit_graph,
)
from scripts.test_current_scenario_generator import (
    FixtureProvider,
    _review_response,
    _scenario_set_response,
)
from scripts.test_current_state import _bullish_premise
from scripts.test_market_scenario import _draft_report, _premise
from scripts.test_pattern_retrieval import (
    _base_bundle,
    _library,
    _matching_regime,
    _standalone_variant,
)


FIXED_NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
CUTOFF = "2026-07-28T16:00:00+00:00"


def _patterns(*, bearish: bool, include_counter: bool):
    _, _, _, pattern, case_hashes = _base_bundle()
    bearish_profile = replace(
        pattern.expected_outcome_profile,
        expected_direction=PatternDirection.BEARISH,
        expected_move_types=(HistoricalMoveType.DOWNWARD_IMPULSE,),
    )
    bearish_pattern = _standalone_variant(
        pattern,
        pattern_id="bearish_counter_pattern",
        pattern_direction=PatternDirection.BEARISH,
        applicable_move_types=(
            HistoricalMoveType.DOWNWARD_IMPULSE,
        ),
        expected_outcome_profile=bearish_profile,
    )
    selected = [bearish_pattern if bearish else pattern]
    if include_counter:
        selected.append(pattern if bearish else bearish_pattern)
    return tuple(selected), case_hashes


def _make_input(
    *,
    bearish: bool = False,
    include_counter: bool = True,
    only_counter: bool = False,
    context_dependent: bool = False,
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_FIXTURE,
    model_name: str = "fixture-current-scenario-model",
):
    premise = _premise() if bearish else _bullish_premise()
    report = _draft_report(premise=premise)
    if bearish:
        events = report.material_events
        regime = None
    else:
        revision_event = replace(
            report.material_events[0],
            event_id="event-earnings-revision",
            event_type="earnings_revision",
        )
        events = report.material_events + (revision_event,)
        regime = _matching_regime()
    patterns, case_hashes = _patterns(
        bearish=bearish,
        include_counter=include_counter,
    )
    if context_dependent:
        _, _, _, base_pattern, case_hashes = _base_bundle()
        context_profile = replace(
            base_pattern.expected_outcome_profile,
            expected_direction=PatternDirection.CONTEXT_DEPENDENT,
        )
        context_pattern = _standalone_variant(
            base_pattern,
            pattern_id="context_dependent_pattern",
            pattern_direction=PatternDirection.CONTEXT_DEPENDENT,
            expected_outcome_profile=context_profile,
        )
        bearish_patterns, _ = _patterns(
            bearish=True,
            include_counter=False,
        )
        patterns = (context_pattern,) + (
            bearish_patterns if include_counter else ()
        )
    if only_counter:
        all_patterns, case_hashes = _patterns(
            bearish=not bearish,
            include_counter=False,
        )
        patterns = all_patterns
    library = _library(patterns, case_hashes)
    retrieval_config = PatternRetrievalConfiguration.create(
        requested_limit=10,
        counter_pattern_limit=3,
        include_context_dependent_patterns=context_dependent,
    )
    scenario_config = ScenarioGenerationConfiguration.create()
    orchestration_input = MarketScenarioOrchestrationInput.create(
        symbol=premise.symbol,
        applicable_cutoff=CUTOFF,
        frozen_technical_premise=premise,
        current_evidence=report.evidence,
        current_exposures=report.exposures,
        exposure_category_coverage=report.exposure_coverage,
        current_events=events,
        historical_pattern_library=library,
        current_regime_inputs=regime,
        valuation_inputs=None,
        liquidity_inputs=None,
        positioning_inputs=None,
        benchmark_relative_inputs=None,
        sector_relative_inputs=None,
        retrieval_configuration=retrieval_config,
        scenario_generation_configuration=scenario_config,
        execution_mode=execution_mode,
        created_at=FIXED_NOW,
        provider_configuration={
            "provider_name": "fixture",
            "model_name": model_name,
        },
    )
    return orchestration_input


def _preview(orchestration_input):
    state_result = CurrentStateBuilder(clock=lambda: FIXED_NOW).build(
        orchestration_input.frozen_technical_premise,
        orchestration_input.current_evidence,
        orchestration_input.current_exposures,
        orchestration_input.exposure_category_coverage,
        events=orchestration_input.current_events,
        market_regime=orchestration_input.current_regime_inputs,
        applicable_cutoff=orchestration_input.applicable_cutoff,
        as_of=orchestration_input.created_at,
    )
    if not state_result.success:
        raise AssertionError(state_result.errors)
    config = orchestration_input.retrieval_configuration
    query = create_pattern_retrieval_query(
        state_result.current_state,
        orchestration_input.historical_pattern_library,
        requested_limit=config.requested_limit,
        requested_counter_limit=config.counter_pattern_limit,
        minimum_match_quality=config.minimum_match_quality,
        include_context_dependent_patterns=(
            config.include_context_dependent_patterns
        ),
        include_bidirectional_patterns=(
            config.include_bidirectional_patterns
        ),
        allowed_pattern_statuses=config.allowed_pattern_statuses,
        generated_at=FIXED_NOW,
    )
    retrieval_execution = PatternRetrievalEngine(
        clock=lambda: FIXED_NOW
    ).retrieve(
        state_result.current_state,
        orchestration_input.historical_pattern_library,
        query,
    )
    if not retrieval_execution.success:
        raise AssertionError(retrieval_execution.errors)
    return (
        orchestration_input.frozen_technical_premise,
        state_result.current_state,
        retrieval_execution.retrieval_result,
        orchestration_input.current_evidence,
        orchestration_input.current_exposures,
        orchestration_input.current_events,
    )


def _responses(
    orchestration_input,
    *,
    recommendation: str = "keep",
):
    inputs = _preview(orchestration_input)
    draft = _scenario_set_response(inputs)
    replacement = draft if recommendation == "revise" else None
    review = _review_response(
        draft,
        recommendation=recommendation,
        replacement=replacement,
    )
    return [draft, review]


def _run(
    orchestration_input,
    *,
    responses=None,
    now: datetime = FIXED_NOW,
):
    provider = FixtureProvider(
        copy.deepcopy(
            responses
            if responses is not None
            else _responses(orchestration_input)
        ),
        model=orchestration_input.provider_configuration[
            "model_name"
        ],
    )
    result = MarketScenarioOrchestrator(
        provider,
        clock=lambda: now,
    ).execute(orchestration_input)
    return result, provider


class OrchestrationConfigurationTests(unittest.TestCase):
    def test_input_configuration_and_hashes_are_valid(self) -> None:
        orchestration_input = _make_input()
        provider = FixtureProvider([], model="fixture-current-scenario-model")
        validation = validate_market_scenario_orchestration_input(
            orchestration_input,
            provider=provider,
        )
        self.assertTrue(validation.is_valid, validation.to_dict())
        self.assertEqual(
            orchestration_input.content_hash,
            market_scenario_orchestration_input_content_hash(
                orchestration_input
            ),
        )

    def test_invalid_input_hash_stops_before_state(self) -> None:
        orchestration_input = replace(
            _make_input(),
            content_hash="tampered",
        )
        result, provider = _run(
            orchestration_input,
            responses=[],
        )
        self.assertFalse(result.success)
        self.assertIs(result.status, OrchestrationStatus.FAILED)
        self.assertIsNone(result.current_state)
        self.assertEqual(provider.strict_calls, 0)
        self.assertEqual(
            tuple(item.stage for item in result.stage_records),
            (
                OrchestrationStage.INPUT_VALIDATION,
                OrchestrationStage.FINAL_VALIDATION,
                OrchestrationStage.COMPLETED,
            ),
        )

    def test_cutoff_mismatch_stops_before_state(self) -> None:
        source = _make_input()
        invalid = replace(
            source,
            applicable_cutoff="2020-01-01T00:00:00+00:00",
            content_hash="",
        )
        invalid = replace(
            invalid,
            content_hash=(
                market_scenario_orchestration_input_content_hash(
                    invalid
                )
            ),
        )
        result, provider = _run(invalid, responses=[])
        self.assertFalse(result.success)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "cutoff_mismatch",
            result.stage_records[0].validation_result.failed_rules,
        )

    def test_invalid_historical_library_stops_before_state(self) -> None:
        source = _make_input()
        broken_library = replace(
            source.historical_pattern_library,
            content_hash="tampered",
        )
        invalid = replace(
            source,
            historical_pattern_library=broken_library,
            content_hash="",
        )
        invalid = replace(
            invalid,
            content_hash=(
                market_scenario_orchestration_input_content_hash(
                    invalid
                )
            ),
        )
        result, provider = _run(invalid, responses=[])
        self.assertFalse(result.success)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "invalid_historical_library",
            result.stage_records[0].validation_result.failed_rules,
        )

    def test_incompatible_scenario_configuration_rejected(self) -> None:
        source = _make_input()
        config = ScenarioGenerationConfiguration.create(
            minimum_scenarios=2
        )
        invalid = replace(
            source,
            scenario_generation_configuration=config,
            content_hash="",
        )
        invalid = replace(
            invalid,
            content_hash=(
                market_scenario_orchestration_input_content_hash(
                    invalid
                )
            ),
        )
        result, _ = _run(invalid, responses=[])
        self.assertFalse(result.success)
        self.assertIn(
            "unsupported_scenario_configuration",
            result.stage_records[0].validation_result.failed_rules,
        )

    def test_input_contract_is_frozen(self) -> None:
        orchestration_input = _make_input()
        with self.assertRaises(FrozenInstanceError):
            orchestration_input.symbol = "OTHER"


class OrchestrationSuccessTests(unittest.TestCase):
    def test_successful_bullish_orchestration(self) -> None:
        orchestration_input = _make_input()
        result, provider = _run(orchestration_input)
        self.assertTrue(result.success, result.errors)
        self.assertIn(
            result.status,
            {
                OrchestrationStatus.COMPLETED,
                OrchestrationStatus.COMPLETED_WITH_WARNINGS,
            },
        )
        self.assertEqual(provider.strict_calls, 2)
        self.assertIsNotNone(result.current_state)
        self.assertIsNotNone(result.pattern_retrieval_result)
        self.assertIsNotNone(result.current_scenario_set)
        self.assertIsNotNone(result.reasoning_audit_graph)
        self.assertTrue(result.final_validation_result.is_valid)
        self.assertEqual(
            tuple(item.stage for item in result.stage_records),
            tuple(OrchestrationStage),
        )

    def test_successful_bearish_orchestration(self) -> None:
        orchestration_input = _make_input(bearish=True)
        result, _ = _run(orchestration_input)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(
            result.current_scenario_set.symbol,
            orchestration_input.symbol,
        )
        self.assertEqual(
            result.current_state.frozen_technical_premise_hash,
            orchestration_input.frozen_technical_premise.frozen_content_hash,
        )

    def test_successful_context_dependent_orchestration(self) -> None:
        orchestration_input = _make_input(
            context_dependent=True
        )
        preview = _preview(orchestration_input)
        self.assertTrue(preview[2].matches)
        result, _ = _run(orchestration_input)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(
            result.pattern_retrieval_result.matches[0].pattern_id,
            "context_dependent_pattern",
        )

    def test_primary_and_counter_patterns_retrieved(self) -> None:
        orchestration_input = _make_input()
        result, _ = _run(orchestration_input)
        self.assertTrue(result.pattern_retrieval_result.matches)
        self.assertTrue(
            result.pattern_retrieval_result.counter_patterns
        )
        self.assertGreater(
            result.audit_coverage_metrics.pattern_reference_coverage,
            0,
        )

    def test_adversarial_keep_is_recorded(self) -> None:
        result, _ = _run(_make_input())
        self.assertTrue(result.success)
        self.assertEqual(
            result.scenario_generation_result.adversarial_recommendation.value,
            "keep",
        )

    def test_adversarial_revise_is_supported(self) -> None:
        orchestration_input = _make_input()
        result, _ = _run(
            orchestration_input,
            responses=_responses(
                orchestration_input,
                recommendation="revise",
            ),
        )
        self.assertTrue(result.success, result.errors)
        self.assertEqual(
            result.scenario_generation_result.adversarial_recommendation.value,
            "revise",
        )

    def test_result_round_trip(self) -> None:
        result, _ = _run(_make_input())
        restored = MarketScenarioOrchestrationResult.from_dict(
            result.to_dict()
        )
        self.assertEqual(restored, result)
        self.assertEqual(
            restored.content_hash,
            market_scenario_orchestration_result_content_hash(
                restored
            ),
        )

    def test_deterministic_analytical_hash_ignores_operational_time(self) -> None:
        orchestration_input = _make_input()
        first, _ = _run(
            orchestration_input,
            now=FIXED_NOW,
        )
        second, _ = _run(
            orchestration_input,
            now=FIXED_NOW + timedelta(hours=7),
        )
        self.assertNotEqual(first.started_at, second.started_at)
        self.assertEqual(first.analytical_hash, second.analytical_hash)
        self.assertEqual(
            tuple(item.analytical_hash for item in first.stage_records),
            tuple(item.analytical_hash for item in second.stage_records),
        )
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_source_inputs_are_not_mutated(self) -> None:
        orchestration_input = _make_input()
        before = copy.deepcopy(orchestration_input.to_dict())
        result, _ = _run(orchestration_input)
        self.assertTrue(result.success)
        self.assertEqual(orchestration_input.to_dict(), before)


class OrchestrationFailureTests(unittest.TestCase):
    def test_no_primary_pattern_stops_before_provider(self) -> None:
        orchestration_input = _make_input(only_counter=True)
        result, provider = _run(
            orchestration_input,
            responses=[],
        )
        self.assertFalse(result.success)
        self.assertIs(
            result.status,
            OrchestrationStatus.INSUFFICIENT_EVIDENCE,
        )
        self.assertIsNotNone(result.current_state)
        self.assertIsNotNone(result.pattern_retrieval_result)
        self.assertIsNone(result.current_scenario_set)
        self.assertEqual(provider.strict_calls, 0)
        self.assertNotIn(
            OrchestrationStage.SCENARIO_GENERATION,
            tuple(item.stage for item in result.stage_records),
        )

    def test_state_builder_failure_preserves_no_downstream_output(
        self,
    ) -> None:
        orchestration_input = _make_input()

        class BrokenBuilder:
            def __init__(self, **kwargs):
                del kwargs

            def build(self, *args, **kwargs):
                del args, kwargs
                raise RuntimeError("fixture state failure")

        with patch(
            "elliott_ai.market_scenario_orchestrator.CurrentStateBuilder",
            BrokenBuilder,
        ):
            result, provider = _run(
                orchestration_input,
                responses=[],
            )
        self.assertFalse(result.success)
        self.assertIsNone(result.current_state)
        self.assertIsNone(result.pattern_retrieval_result)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "current_state_stage_exception",
            result.stage_records[1].validation_result.failed_rules,
        )

    def test_retrieval_failure_preserves_state(self) -> None:
        orchestration_input = _make_input()

        class BrokenRetrievalEngine:
            def __init__(self, **kwargs):
                del kwargs

            def retrieve(self, *args, **kwargs):
                del args, kwargs
                raise RuntimeError("fixture retrieval failure")

        with patch(
            "elliott_ai.market_scenario_orchestrator.PatternRetrievalEngine",
            BrokenRetrievalEngine,
        ):
            result, provider = _run(
                orchestration_input,
                responses=[],
            )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.current_state)
        self.assertIsNone(result.pattern_retrieval_result)
        self.assertIsNone(result.current_scenario_set)
        self.assertEqual(provider.strict_calls, 0)

    def test_scenario_generation_failure_preserves_state_and_retrieval(
        self,
    ) -> None:
        orchestration_input = _make_input()
        result, provider = _run(
            orchestration_input,
            responses=[{"invalid": "response"}],
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.current_state)
        self.assertIsNotNone(result.pattern_retrieval_result)
        self.assertIsNone(result.current_scenario_set)
        self.assertGreaterEqual(provider.strict_calls, 1)

    def test_adversarial_reject_is_structured_failure(self) -> None:
        orchestration_input = _make_input()
        result, _ = _run(
            orchestration_input,
            responses=_responses(
                orchestration_input,
                recommendation="reject",
            ),
        )
        self.assertFalse(result.success)
        self.assertIs(result.status, OrchestrationStatus.FAILED)
        self.assertIsNotNone(result.current_state)
        self.assertIsNotNone(result.pattern_retrieval_result)

    def test_final_continuity_failure_marks_orchestration_failed(
        self,
    ) -> None:
        orchestration_input = _make_input()
        invalid = ValidationResult(
            is_valid=False,
            score=0,
            errors=(
                ValidationIssue(
                    code="hash_continuity_failure",
                    severity="error",
                    path="fixture",
                    message="Forced fixture mismatch.",
                ),
            ),
            failed_rules=("hash_continuity_failure",),
            validation_version=(
                "market-scenario-orchestration-validation-1.0.0"
            ),
        )
        with patch(
            "elliott_ai.market_scenario_orchestrator._validate_pipeline_continuity",
            return_value=invalid,
        ):
            result, _ = _run(orchestration_input)
        self.assertFalse(result.success)
        self.assertIs(result.status, OrchestrationStatus.FAILED)
        self.assertIsNotNone(result.current_scenario_set)
        final_record = next(
            item
            for item in result.stage_records
            if item.stage is OrchestrationStage.FINAL_VALIDATION
        )
        self.assertFalse(final_record.validation_result.is_valid)

    def test_tampered_final_hash_rejected(self) -> None:
        result, _ = _run(_make_input())
        tampered = replace(result, content_hash="tampered")
        validation = validate_market_scenario_orchestration_result(
            tampered
        )
        self.assertFalse(validation.is_valid)
        self.assertIn(
            "final_content_hash_mismatch",
            validation.failed_rules,
        )

    def test_duplicate_and_out_of_order_stage_records_rejected(
        self,
    ) -> None:
        result, _ = _run(_make_input())
        duplicate = replace(
            result,
            stage_records=(
                result.stage_records[0],
                result.stage_records[0],
            )
            + result.stage_records[1:],
            analytical_hash="tampered",
            content_hash="tampered",
        )
        validation = validate_market_scenario_orchestration_result(
            duplicate
        )
        self.assertIn(
            "duplicate_stage_record",
            validation.failed_rules,
        )
        reordered = replace(
            result,
            stage_records=(
                result.stage_records[1],
                result.stage_records[0],
            )
            + result.stage_records[2:],
            analytical_hash="tampered",
            content_hash="tampered",
        )
        validation = validate_market_scenario_orchestration_result(
            reordered
        )
        self.assertIn(
            "stage_order_violation",
            validation.failed_rules,
        )

    def test_missing_terminal_stage_rejected(self) -> None:
        result, _ = _run(_make_input())
        missing = replace(
            result,
            stage_records=result.stage_records[:-1],
            analytical_hash="tampered",
            content_hash="tampered",
        )
        validation = validate_market_scenario_orchestration_result(
            missing
        )
        self.assertIn(
            "missing_stage_record",
            validation.failed_rules,
        )


class ReasoningAuditTests(unittest.TestCase):
    def test_graph_contains_required_nodes_and_edges(self) -> None:
        result, _ = _run(_make_input())
        graph = result.reasoning_audit_graph
        node_types = {item.node_type for item in graph.nodes}
        self.assertIn(
            AuditNodeType.FROZEN_TECHNICAL_PREMISE,
            node_types,
        )
        self.assertIn(AuditNodeType.SCENARIO, node_types)
        self.assertIn(AuditNodeType.SCENARIO_STEP, node_types)
        self.assertIn(
            AuditNodeType.INVALIDATION_CONDITION,
            node_types,
        )
        self.assertTrue(graph.validation_result.is_valid)
        self.assertEqual(
            result.audit_coverage_metrics.orphan_node_count,
            0,
        )
        self.assertEqual(
            result.audit_coverage_metrics.dangling_reference_count,
            0,
        )

    def test_audit_orphan_detection(self) -> None:
        result, _ = _run(_make_input())
        graph = result.reasoning_audit_graph
        assumption = next(
            item
            for item in graph.nodes
            if item.node_type is AuditNodeType.ASSUMPTION
        )
        edges = tuple(
            item
            for item in graph.edges
            if assumption.node_id
            not in (item.source_node_id, item.target_node_id)
        )
        changed = replace(
            graph,
            edges=edges,
            orphan_node_ids=(assumption.node_id,),
            content_hash="",
        )
        changed = replace(
            changed,
            content_hash=reasoning_audit_graph_content_hash(
                changed
            ),
        )
        validation = validate_reasoning_audit_graph(
            changed,
            scenario_set=result.current_scenario_set,
        )
        self.assertTrue(validation.is_valid, validation.to_dict())
        self.assertTrue(
            any(
                item.code == "audit_orphans"
                for item in validation.warnings
            )
        )

    def test_audit_dangling_reference_rejected(self) -> None:
        result, _ = _run(_make_input())
        graph = result.reasoning_audit_graph
        broken_edge = replace(
            graph.edges[0],
            target_node_id="missing-node",
        )
        changed = replace(
            graph,
            edges=(broken_edge,) + graph.edges[1:],
            content_hash="",
        )
        changed = replace(
            changed,
            content_hash=reasoning_audit_graph_content_hash(
                changed
            ),
        )
        validation = validate_reasoning_audit_graph(
            changed,
            scenario_set=result.current_scenario_set,
        )
        self.assertFalse(validation.is_valid)
        self.assertIn(
            "audit_no_dangling_edges",
            validation.failed_rules,
        )


class ReplayTests(unittest.TestCase):
    def _replay_source(self):
        orchestration_input = _make_input(
            execution_mode=ExecutionMode.PACKET_REPLAY,
            model_name="phase8-replay-fixture",
        )
        responses = _responses(orchestration_input)
        baseline, _ = _run(
            orchestration_input,
            responses=responses,
        )
        self.assertTrue(baseline.success, baseline.errors)
        return orchestration_input, responses, baseline

    def test_replay_packet_success(self) -> None:
        orchestration_input, responses, baseline = (
            self._replay_source()
        )
        packet = MarketScenarioReplayPacket.create(
            packet_id="packet-success",
            orchestration_input=orchestration_input,
            expected_input_hash=orchestration_input.content_hash,
            fixture_provider_responses=responses,
            expected_stage_hashes={
                item.stage.value: item.analytical_hash
                for item in baseline.stage_records
            },
            expected_final_hash=baseline.analytical_hash,
            created_at=FIXED_NOW,
        )
        replay = replay_market_scenario_packet(
            packet,
            clock=lambda: FIXED_NOW + timedelta(days=1),
        )
        self.assertTrue(replay.success, replay.mismatch_codes)
        self.assertFalse(replay.mismatch_codes)
        self.assertEqual(replay.remaining_fixture_responses, 0)
        self.assertEqual(
            replay.actual_final_hash,
            baseline.analytical_hash,
        )

    def test_replay_expected_hash_mismatch(self) -> None:
        orchestration_input, responses, baseline = (
            self._replay_source()
        )
        packet = MarketScenarioReplayPacket.create(
            packet_id="packet-mismatch",
            orchestration_input=orchestration_input,
            expected_input_hash=orchestration_input.content_hash,
            fixture_provider_responses=responses,
            expected_stage_hashes={},
            expected_final_hash="wrong",
            created_at=FIXED_NOW,
        )
        replay = replay_market_scenario_packet(
            packet,
            clock=lambda: FIXED_NOW,
        )
        self.assertFalse(replay.success)
        self.assertIn(
            "final_hash_mismatch",
            replay.mismatch_codes,
        )
        self.assertEqual(
            replay.orchestration_result.analytical_hash,
            baseline.analytical_hash,
        )

    def test_replay_fixture_response_mismatch(self) -> None:
        orchestration_input, responses, _ = self._replay_source()
        packet = MarketScenarioReplayPacket.create(
            packet_id="packet-response-mismatch",
            orchestration_input=orchestration_input,
            expected_input_hash=orchestration_input.content_hash,
            fixture_provider_responses=responses[:1],
            expected_stage_hashes={},
            expected_final_hash=None,
            created_at=FIXED_NOW,
        )
        replay = replay_market_scenario_packet(
            packet,
            clock=lambda: FIXED_NOW,
        )
        self.assertFalse(replay.success)
        self.assertIn(
            "fixture_response_mismatch",
            replay.mismatch_codes,
        )

    def test_replay_serialization_round_trip(self) -> None:
        orchestration_input, responses, baseline = (
            self._replay_source()
        )
        packet = MarketScenarioReplayPacket.create(
            packet_id="packet-round-trip",
            orchestration_input=orchestration_input,
            expected_input_hash=orchestration_input.content_hash,
            fixture_provider_responses=responses,
            expected_stage_hashes={
                item.stage.value: item.analytical_hash
                for item in baseline.stage_records
            },
            expected_final_hash=baseline.analytical_hash,
            created_at=FIXED_NOW,
        )
        restored_packet = MarketScenarioReplayPacket.from_dict(
            packet.to_dict()
        )
        self.assertEqual(restored_packet, packet)
        self.assertEqual(
            packet.content_hash,
            market_scenario_replay_packet_content_hash(packet),
        )
        replay = replay_market_scenario_packet(
            restored_packet,
            clock=lambda: FIXED_NOW,
        )
        restored_result = MarketScenarioReplayResult.from_dict(
            replay.to_dict()
        )
        self.assertEqual(restored_result, replay)
        self.assertEqual(
            replay.content_hash,
            market_scenario_replay_result_content_hash(replay),
        )


if __name__ == "__main__":
    unittest.main()
