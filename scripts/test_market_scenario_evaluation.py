import copy
import unittest
from dataclasses import FrozenInstanceError, replace

from elliott_ai.current_scenarios import (
    CurrentScenarioSet,
    current_scenario_set_content_hash,
)
from elliott_ai.market_scenario import (
    EvidenceStatus,
    ScenarioDirection,
)
from elliott_ai.market_scenario_evaluation import (
    HARD_FAILURE_DANGLING_AUDIT,
    HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE,
    HARD_FAILURE_FUTURE_EVIDENCE,
    HARD_FAILURE_HASH_CONTINUITY,
    HARD_FAILURE_INVENTED_REFERENCE,
    HARD_FAILURE_MISSING_INVALIDATION,
    HARD_FAILURE_NON_REPRODUCIBLE,
    HARD_FAILURE_TECHNICAL_CONSTRAINT,
    HARD_FAILURE_TECHNICAL_PREMISE_MUTATION,
    EvaluationRun,
    EvaluationSuiteResult,
    EvaluationSuiteStatus,
    MarketScenarioEvaluationEngine,
    RegressionBaseline,
    SyntheticCaseType,
    SyntheticEvaluationCase,
    SyntheticEvaluationCaseGenerator,
    compare_regression_baseline,
    create_regression_baseline,
    evaluation_run_content_hash,
    evaluation_suite_result_content_hash,
    regression_baseline_content_hash,
    synthetic_evaluation_case_content_hash,
    validate_evaluation_run,
    validate_evaluation_suite_result,
)
from elliott_ai.market_scenario_orchestrator import (
    ExecutionMode,
    MarketScenarioReplayPacket,
    OrchestrationStage,
    OrchestrationStatus,
    market_scenario_orchestration_input_content_hash,
    market_scenario_replay_packet_content_hash,
)
from elliott_ai.reasoning_audit import AuditEdgeType
from scripts.test_market_scenario_orchestrator import (
    FIXED_NOW,
    _make_input,
    _responses,
    _run,
)


def _evaluation_run(
    *,
    recommendation="keep",
    bearish=False,
    replay=True,
    evaluation_run_id=None,
):
    execution_mode = (
        ExecutionMode.PACKET_REPLAY
        if replay
        else ExecutionMode.OFFLINE_FIXTURE
    )
    model_name = (
        "phase8-replay-fixture"
        if replay
        else "fixture-current-scenario-model"
    )
    orchestration_input = _make_input(
        bearish=bearish,
        execution_mode=execution_mode,
        model_name=model_name,
    )
    responses = _responses(
        orchestration_input,
        recommendation=recommendation,
    )
    result, _ = _run(
        orchestration_input,
        responses=responses,
    )
    packet = None
    if replay:
        packet = MarketScenarioReplayPacket.create(
            packet_id=(
                "phase9-packet-"
                + recommendation
                + ("-bearish" if bearish else "")
            ),
            orchestration_input=orchestration_input,
            expected_input_hash=orchestration_input.content_hash,
            fixture_provider_responses=responses,
            expected_stage_hashes={
                item.stage.value: item.analytical_hash
                for item in result.stage_records
            },
            expected_final_hash=result.analytical_hash,
            created_at=FIXED_NOW,
        )
    return EvaluationRun.create(
        orchestration_input=orchestration_input,
        orchestration_result=result,
        replay_packet=packet,
        fixture_tags=(
            recommendation,
            "bearish" if bearish else "bullish",
        ),
        created_at=FIXED_NOW,
        evaluation_run_id=evaluation_run_id,
    )


def _variant(
    source,
    *,
    orchestration_input=None,
    orchestration_result=None,
    replay_packet=None,
    evaluator_version=None,
    suffix="variant",
):
    return EvaluationRun.create(
        orchestration_input=(
            orchestration_input or source.orchestration_input
        ),
        orchestration_result=(
            orchestration_result or source.orchestration_result
        ),
        replay_packet=replay_packet,
        fixture_tags=source.fixture_tags + (suffix,),
        evaluator_version=(
            evaluator_version or source.evaluator_version
        ),
        created_at=source.created_at,
        evaluation_run_id=(
            source.evaluation_run_id + "__" + suffix
        ),
    )


def _replace_first_scenario(source, **changes):
    result = source.orchestration_result
    scenario_set = result.current_scenario_set
    scenario = replace(scenario_set.scenarios[0], **changes)
    changed_set = replace(
        scenario_set,
        scenarios=(scenario,) + scenario_set.scenarios[1:],
    )
    return replace(result, current_scenario_set=changed_set)


def _metric(suite, metric_id):
    return next(
        item for item in suite.metrics if item.metric_id == metric_id
    )


class Phase9Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = MarketScenarioEvaluationEngine()
        cls.base = _evaluation_run(evaluation_run_id="00_keep")
        cls.base_suite = cls.engine.evaluate(cls.base)


class ContractAndSerializationTests(Phase9Fixture):
    def test_valid_fixture_passes_every_hard_gate(self):
        self.assertIs(
            self.base_suite.status,
            EvaluationSuiteStatus.PASSED,
        )
        self.assertFalse(self.base_suite.issues)
        self.assertFalse(self.base_suite.hard_failure_codes)
        self.assertTrue(
            validate_evaluation_run(self.base).is_valid
        )
        self.assertTrue(
            validate_evaluation_suite_result(
                self.base_suite,
                run=self.base,
            ).is_valid
        )

    def test_all_typed_evaluation_contracts_round_trip(self):
        restored_run = EvaluationRun.from_dict(
            self.base.to_dict()
        )
        restored_suite = EvaluationSuiteResult.from_dict(
            self.base_suite.to_dict()
        )
        baseline = create_regression_baseline(
            self.base_suite,
            self.base,
        )
        restored_baseline = RegressionBaseline.from_dict(
            baseline.to_dict()
        )
        compared = self.engine.evaluate(
            self.base,
            baseline=baseline,
        )
        restored_compared = EvaluationSuiteResult.from_dict(
            compared.to_dict()
        )

        self.assertEqual(restored_run, self.base)
        self.assertEqual(restored_suite, self.base_suite)
        self.assertEqual(restored_baseline, baseline)
        self.assertEqual(restored_compared, compared)
        self.assertEqual(
            restored_run.content_hash,
            evaluation_run_content_hash(restored_run),
        )
        self.assertEqual(
            restored_suite.content_hash,
            evaluation_suite_result_content_hash(
                restored_suite
            ),
        )
        self.assertEqual(
            restored_baseline.content_hash,
            regression_baseline_content_hash(
                restored_baseline
            ),
        )
        self.assertTrue(
            validate_evaluation_suite_result(
                compared,
                run=self.base,
            ).is_valid
        )

    def test_contracts_are_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.base.evaluation_run_id = "changed"
        with self.assertRaises(FrozenInstanceError):
            self.base_suite.status = EvaluationSuiteStatus.FAILED

    def test_tampered_evaluation_wrapper_fails_validation(self):
        tampered = replace(self.base, content_hash="tampered")
        validation = validate_evaluation_run(tampered)
        self.assertFalse(validation.is_valid)
        self.assertIn(
            "evaluation_run_hash",
            validation.failed_rules,
        )

    def test_deterministic_repeated_evaluation_and_hashes(self):
        first = self.engine.evaluate(self.base)
        second = self.engine.evaluate(self.base)
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(
            tuple(item.content_hash for item in first.metrics),
            tuple(item.content_hash for item in second.metrics),
        )


class StructuralHardGateTests(Phase9Fixture):
    def test_technical_premise_mutation_is_hard_failure(self):
        changed_result = replace(
            self.base.orchestration_result,
            frozen_technical_premise_hash="mutated",
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed_result,
                suffix="premise_mutation",
            )
        )
        self.assertIn(
            HARD_FAILURE_TECHNICAL_PREMISE_MUTATION,
            suite.hard_failure_codes,
        )
        self.assertFalse(
            _metric(
                suite,
                "technical_premise_immutability",
            ).value
        )

    def test_target_direction_duration_and_invalidation_constraints(self):
        premise = (
            self.base.orchestration_input.frozen_technical_premise
        )
        state = self.base.orchestration_result.current_state
        first = (
            self.base.orchestration_result.current_scenario_set.scenarios[
                0
            ]
        )
        opposite = (
            ScenarioDirection.DOWN
            if premise.direction is ScenarioDirection.UP
            else ScenarioDirection.UP
        )
        changed = _replace_first_scenario(
            self.base,
            linked_target_low=(
                (premise.target_low or 0.0) + 123.0
            ),
            direction=opposite,
            linked_duration_bucket=(
                state.expected_duration_bucket.__class__.YEARS
                if state.expected_duration_bucket.value != "years"
                else state.expected_duration_bucket.__class__.DAYS
            ),
            invalidation_conditions=(),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed,
                suffix="technical_constraints",
            )
        )
        self.assertIn(
            HARD_FAILURE_TECHNICAL_CONSTRAINT,
            suite.hard_failure_codes,
        )
        self.assertIn(
            HARD_FAILURE_MISSING_INVALIDATION,
            suite.hard_failure_codes,
        )
        for metric_id in (
            "target_consistency",
            "direction_consistency",
            "duration_consistency",
            "invalidation_condition_coverage",
        ):
            self.assertEqual(
                _metric(suite, metric_id).status.value,
                "failed",
            )
        self.assertTrue(first.invalidation_conditions)

    def test_future_evidence_beyond_cutoff_is_hard_failure(self):
        first = self.base.orchestration_input.current_evidence[0]
        changed_input = replace(
            self.base.orchestration_input,
            current_evidence=(
                replace(
                    first,
                    applicable_cutoff=(
                        "2999-01-01T00:00:00+00:00"
                    ),
                ),
            )
            + self.base.orchestration_input.current_evidence[1:],
            content_hash="",
        )
        changed_input = replace(
            changed_input,
            content_hash=(
                market_scenario_orchestration_input_content_hash(
                    changed_input
                )
            ),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_input=changed_input,
                suffix="future_evidence",
            )
        )
        self.assertIn(
            HARD_FAILURE_FUTURE_EVIDENCE,
            suite.hard_failure_codes,
        )
        self.assertGreater(
            _metric(
                suite,
                "applicable_cutoff_compliance",
            ).value,
            0,
        )

    def test_hash_continuity_failure_is_hard(self):
        changed_result = replace(
            self.base.orchestration_result,
            content_hash="tampered",
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed_result,
                suffix="hash_failure",
            )
        )
        self.assertIn(
            HARD_FAILURE_HASH_CONTINUITY,
            suite.hard_failure_codes,
        )

    def test_downstream_execution_after_failure_is_hard(self):
        records = list(
            self.base.orchestration_result.stage_records
        )
        records[0] = replace(
            records[0],
            status=OrchestrationStatus.FAILED,
            errors=("synthetic failure",),
        )
        changed_result = replace(
            self.base.orchestration_result,
            stage_records=tuple(records),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed_result,
                suffix="downstream_after_failure",
            )
        )
        self.assertIn(
            HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE,
            suite.hard_failure_codes,
        )
        self.assertTrue(
            _metric(
                suite,
                "downstream_execution_after_failure",
            ).value
        )

    def test_warning_and_failure_status_consistency(self):
        records = list(
            self.base.orchestration_result.stage_records
        )
        records[0] = replace(
            records[0],
            status=OrchestrationStatus.FAILED,
            errors=(),
            warnings=(),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=replace(
                    self.base.orchestration_result,
                    stage_records=tuple(records),
                ),
                suffix="status_inconsistent",
            )
        )
        self.assertFalse(
            _metric(
                suite,
                "validation_status_consistency",
            ).value
        )


class GroundingAndQualityTests(Phase9Fixture):
    def test_unknown_evidence_reference_is_hard_failure(self):
        changed = _replace_first_scenario(
            self.base,
            supporting_current_evidence_ids=(
                "unknown-evidence-id",
            ),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed,
                suffix="unknown_evidence",
            )
        )
        self.assertIn(
            HARD_FAILURE_INVENTED_REFERENCE,
            suite.hard_failure_codes,
        )
        self.assertIn(
            "evidence:unknown-evidence-id",
            suite.grounding_evaluation.unknown_reference_ids,
        )

    def test_unknown_exposure_event_and_pattern_references(self):
        result = self.base.orchestration_result
        scenario_set = result.current_scenario_set
        first = scenario_set.scenarios[0]
        first_step = first.timeline_sequence[0]
        changed_step = replace(
            first_step,
            event_ids=("unknown-event",),
        )
        changed_scenario = replace(
            first,
            timeline_sequence=(changed_step,)
            + first.timeline_sequence[1:],
            supporting_pattern_refs=("unknown-pattern",),
        )
        changed_state = replace(
            result.current_state,
            exposure_ids=result.current_state.exposure_ids
            + ("unknown-exposure",),
        )
        changed_set = replace(
            scenario_set,
            scenarios=(changed_scenario,)
            + scenario_set.scenarios[1:],
        )
        changed_result = replace(
            result,
            current_state=changed_state,
            current_scenario_set=changed_set,
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed_result,
                suffix="unknown_reference_families",
            )
        )
        unknown = set(
            suite.grounding_evaluation.unknown_reference_ids
        )
        self.assertIn("event:unknown-event", unknown)
        self.assertIn(
            "state_exposure:unknown-exposure",
            unknown,
        )
        self.assertIn("pattern:unknown-pattern", unknown)

    def test_primary_and_counter_patterns_are_grounded(self):
        retrieval = self.base_suite.retrieval_quality_evaluation
        self.assertTrue(retrieval.primary_pattern_refs)
        self.assertTrue(retrieval.counter_pattern_refs)
        self.assertEqual(
            _metric(
                self.base_suite,
                "primary_pattern_grounding",
            ).status.value,
            "passed",
        )
        self.assertEqual(
            _metric(
                self.base_suite,
                "counter_pattern_grounding",
            ).status.value,
            "passed",
        )

    def test_duplicate_causal_chain_is_detected(self):
        scenario_set = (
            self.base.orchestration_result.current_scenario_set
        )
        duplicate = replace(
            scenario_set.scenarios[0],
            scenario_id="duplicate-chain",
        )
        changed_result = replace(
            self.base.orchestration_result,
            current_scenario_set=replace(
                scenario_set,
                scenarios=scenario_set.scenarios + (duplicate,),
            ),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed_result,
                suffix="duplicate_chain",
            )
        )
        self.assertGreater(
            _metric(
                suite,
                "scenario_chain_distinctness",
            ).value,
            0,
        )
        self.assertTrue(
            any(
                item.code == "duplicate_causal_chain"
                for item in suite.issues
            )
        )

    def test_weak_causal_chain_is_detected(self):
        scenario = (
            self.base.orchestration_result.current_scenario_set.scenarios[
                0
            ]
        )
        changed = _replace_first_scenario(
            self.base,
            timeline_sequence=scenario.timeline_sequence[:1],
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed,
                suffix="weak_chain",
            )
        )
        self.assertLess(
            _metric(
                suite,
                "causal_chain_completeness",
            ).value,
            1.0,
        )
        self.assertTrue(
            any(
                item.code == "weak_causal_chain"
                for item in suite.issues
            )
        )

    def test_contradiction_and_missing_evidence_disclosure(self):
        changed = _replace_first_scenario(
            self.base,
            contradicting_current_evidence_ids=(),
            missing_evidence=(),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed,
                suffix="missing_disclosures",
            )
        )
        self.assertLess(
            _metric(
                suite,
                "contradiction_coverage",
            ).value,
            1.0,
        )
        self.assertLess(
            _metric(
                suite,
                "missing_evidence_disclosure",
            ).value,
            1.0,
        )

    def test_fact_assumption_scheduled_and_hypothetical_separation(self):
        scenario_set = (
            self.base.orchestration_result.current_scenario_set
        )
        index = next(
            index
            for index, item in enumerate(scenario_set.scenarios)
            if item.hypothetical_future_events
        )
        scenario = scenario_set.scenarios[index]
        hypothetical = replace(
            scenario.hypothetical_future_events[0],
            explicitly_hypothetical=False,
        )
        changed_scenario = replace(
            scenario,
            hypothetical_future_events=(hypothetical,)
            + scenario.hypothetical_future_events[1:],
        )
        changed_scenarios = list(scenario_set.scenarios)
        changed_scenarios[index] = changed_scenario
        changed_result = replace(
            self.base.orchestration_result,
            current_scenario_set=replace(
                scenario_set,
                scenarios=tuple(changed_scenarios),
            ),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed_result,
                suffix="evidence_separation",
            )
        )
        self.assertGreater(
            _metric(
                suite,
                "evidence_type_separation",
            ).value,
            0,
        )


class AuditGraphTests(Phase9Fixture):
    def test_dangling_audit_reference_is_hard_failure(self):
        graph = self.base.orchestration_result.reasoning_audit_graph
        edge = replace(
            graph.edges[0],
            target_node_id="missing-audit-node",
        )
        changed_graph = replace(
            graph,
            edges=(edge,) + graph.edges[1:],
        )
        changed_result = replace(
            self.base.orchestration_result,
            reasoning_audit_graph=changed_graph,
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=changed_result,
                suffix="dangling_audit",
            )
        )
        self.assertIn(
            HARD_FAILURE_DANGLING_AUDIT,
            suite.hard_failure_codes,
        )
        self.assertEqual(
            _metric(suite, "audit_dangling_count").value,
            1,
        )

    def test_orphan_node_declaration_is_reported(self):
        graph = self.base.orchestration_result.reasoning_audit_graph
        changed_graph = replace(
            graph,
            orphan_node_ids=(graph.nodes[-1].node_id,),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=replace(
                    self.base.orchestration_result,
                    reasoning_audit_graph=changed_graph,
                ),
                suffix="audit_orphan",
            )
        )
        self.assertEqual(
            _metric(suite, "audit_orphan_count").value,
            1,
        )
        self.assertTrue(
            any(
                item.code == "orphan_audit_node"
                for item in suite.issues
            )
        )

    def test_missing_required_edge_type_reduces_completeness(self):
        graph = self.base.orchestration_result.reasoning_audit_graph
        edges = tuple(
            item
            for item in graph.edges
            if item.edge_type is not AuditEdgeType.FOLLOWS
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                orchestration_result=replace(
                    self.base.orchestration_result,
                    reasoning_audit_graph=replace(
                        graph,
                        edges=edges,
                    ),
                ),
                suffix="audit_edge_incomplete",
            )
        )
        self.assertLess(
            _metric(
                suite,
                "audit_edge_completeness",
            ).value,
            1.0,
        )


class ReproducibilityTests(Phase9Fixture):
    def test_provider_fixture_replay_is_reproducible(self):
        reproducibility = (
            self.base_suite.reproducibility_evaluation
        )
        self.assertEqual(
            reproducibility.replay_attempt_count,
            2,
        )
        self.assertEqual(
            len(set(reproducibility.observed_final_hashes)),
            1,
        )
        for metric_id in (
            "replay_packet_validity",
            "provider_fixture_reproducibility",
            "repeated_run_stability",
            "evaluated_result_replay_match",
        ):
            self.assertEqual(
                _metric(
                    self.base_suite,
                    metric_id,
                ).status.value,
                "passed",
            )

    def test_replay_mismatch_is_hard_failure(self):
        packet_seed = replace(
            self.base.replay_packet,
            expected_final_hash="f" * 64,
            content_hash="",
        )
        packet = replace(
            packet_seed,
            content_hash=market_scenario_replay_packet_content_hash(
                packet_seed
            ),
        )
        suite = self.engine.evaluate(
            _variant(
                self.base,
                replay_packet=packet,
                suffix="replay_mismatch",
            )
        )
        self.assertIn(
            HARD_FAILURE_NON_REPRODUCIBLE,
            suite.hard_failure_codes,
        )

    def test_run_without_packet_is_explicitly_unavailable(self):
        run = _evaluation_run(
            replay=False,
            evaluation_run_id="without_replay",
        )
        suite = self.engine.evaluate(run)
        self.assertEqual(
            _metric(
                suite,
                "provider_fixture_reproducibility",
            ).status.value,
            "unavailable",
        )
        self.assertNotIn(
            HARD_FAILURE_NON_REPRODUCIBLE,
            suite.hard_failure_codes,
        )

    def test_partial_upstream_result_does_not_fabricate_scenarios(self):
        orchestration_input = _make_input(
            only_counter=True,
            execution_mode=ExecutionMode.OFFLINE_FIXTURE,
        )
        result, _ = _run(
            orchestration_input,
            responses=[],
        )
        run = EvaluationRun.create(
            orchestration_input=orchestration_input,
            orchestration_result=result,
            created_at=FIXED_NOW,
            evaluation_run_id="no-primary-pattern",
        )
        suite = self.engine.evaluate(run)
        self.assertFalse(
            suite.scenario_quality_evaluation.evaluated_scenario_ids
        )
        self.assertNotIn(
            HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE,
            suite.hard_failure_codes,
        )


class RegressionTests(Phase9Fixture):
    def test_identical_run_has_no_regression(self):
        baseline = create_regression_baseline(
            self.base_suite,
            self.base,
        )
        suite = self.engine.evaluate(
            self.base,
            baseline=baseline,
        )
        comparison = suite.regression_comparison
        self.assertFalse(
            comparison.newly_introduced_hard_failures
        )
        self.assertFalse(comparison.resolved_hard_failures)
        self.assertFalse(comparison.deteriorated_metrics)
        self.assertFalse(comparison.improved_metrics)
        self.assertFalse(comparison.changed_scenario_count)
        self.assertFalse(comparison.changed_retrieval_order)
        self.assertFalse(comparison.changed_pattern_usage)
        self.assertFalse(comparison.changed_audit_coverage)
        self.assertFalse(comparison.changed_content_hashes)
        self.assertFalse(comparison.version_differences)

    def test_new_and_resolved_hard_failures(self):
        baseline = create_regression_baseline(
            self.base_suite,
            self.base,
        )
        bad_result = _replace_first_scenario(
            self.base,
            invalidation_conditions=(),
        )
        bad_run = _variant(
            self.base,
            orchestration_result=bad_result,
            suffix="regression_bad",
        )
        bad_suite = self.engine.evaluate(bad_run)
        compared_bad = compare_regression_baseline(
            baseline,
            bad_suite,
            bad_run,
        )
        self.assertIn(
            HARD_FAILURE_MISSING_INVALIDATION,
            compared_bad.newly_introduced_hard_failures,
        )
        self.assertTrue(compared_bad.deteriorated_metrics)

        bad_baseline = create_regression_baseline(
            bad_suite,
            bad_run,
        )
        compared_good = compare_regression_baseline(
            bad_baseline,
            self.base_suite,
            self.base,
        )
        self.assertIn(
            HARD_FAILURE_MISSING_INVALIDATION,
            compared_good.resolved_hard_failures,
        )
        self.assertTrue(compared_good.improved_metrics)

    def test_regression_reports_version_and_hash_differences(self):
        baseline = create_regression_baseline(
            self.base_suite,
            self.base,
        )
        changed_run = _variant(
            self.base,
            replay_packet=self.base.replay_packet,
            evaluator_version="phase9-evaluator-next",
            suffix="version_change",
        )
        changed_suite = self.engine.evaluate(changed_run)
        comparison = compare_regression_baseline(
            baseline,
            changed_suite,
            changed_run,
        )
        self.assertTrue(comparison.version_differences)
        self.assertTrue(comparison.changed_content_hashes)

    def test_regression_reports_scenario_order_pattern_and_audit_changes(
        self,
    ):
        baseline = create_regression_baseline(
            self.base_suite,
            self.base,
        )
        result = self.base.orchestration_result
        scenario_set = result.current_scenario_set
        duplicate = replace(
            scenario_set.scenarios[0],
            scenario_id="regression-extra",
            supporting_pattern_refs=(),
        )
        retrieval = replace(
            result.pattern_retrieval_result,
            matches=(
                result.pattern_retrieval_result.matches
                + result.pattern_retrieval_result.counter_patterns[:1]
            ),
            counter_patterns=(
                result.pattern_retrieval_result.counter_patterns[1:]
            ),
        )
        graph = replace(
            result.reasoning_audit_graph,
            orphan_node_ids=(
                result.reasoning_audit_graph.nodes[-1].node_id,
            ),
        )
        changed_result = replace(
            result,
            current_scenario_set=replace(
                scenario_set,
                scenarios=scenario_set.scenarios + (duplicate,),
            ),
            pattern_retrieval_result=retrieval,
            reasoning_audit_graph=graph,
        )
        changed_run = _variant(
            self.base,
            orchestration_result=changed_result,
            suffix="regression_artifacts",
        )
        changed_suite = self.engine.evaluate(changed_run)
        comparison = compare_regression_baseline(
            baseline,
            changed_suite,
            changed_run,
        )
        self.assertTrue(comparison.changed_scenario_count)
        self.assertTrue(comparison.changed_retrieval_order)
        self.assertTrue(comparison.changed_pattern_usage)
        self.assertTrue(comparison.changed_audit_coverage)


class SyntheticGenerationTests(Phase9Fixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bearish = _evaluation_run(
            bearish=True,
            replay=False,
            evaluation_run_id="01_bearish",
        )
        cls.revise = _evaluation_run(
            recommendation="revise",
            replay=False,
            evaluation_run_id="02_revise",
        )
        cls.reject = _evaluation_run(
            recommendation="reject",
            replay=False,
            evaluation_run_id="03_reject",
        )
        cls.source_runs = (
            cls.base,
            cls.bearish,
            cls.revise,
            cls.reject,
        )
        cls.generator = SyntheticEvaluationCaseGenerator()
        cls.cases = cls.generator.generate(cls.source_runs)

    def test_generation_covers_required_case_families(self):
        present = {item.case_type for item in self.cases}
        expected = {
            SyntheticCaseType.BULLISH_PREMISE,
            SyntheticCaseType.BEARISH_PREMISE,
            SyntheticCaseType.SHORT_DURATION,
            SyntheticCaseType.MEDIUM_DURATION,
            SyntheticCaseType.LONG_DURATION,
            SyntheticCaseType.PRIMARY_COUNTER_COMBINATION,
            SyntheticCaseType.SPARSE_EVIDENCE,
            SyntheticCaseType.CONTRADICTORY_EVIDENCE,
            SyntheticCaseType.MISSING_OPTIONAL_STATE,
            SyntheticCaseType.INVALID_CUTOFF,
            SyntheticCaseType.DUPLICATE_SCENARIO,
            SyntheticCaseType.WEAK_CAUSAL_CHAIN,
            SyntheticCaseType.MISSING_INVALIDATION,
            SyntheticCaseType.INVENTED_REFERENCE,
            SyntheticCaseType.REPLAY_MISMATCH,
            SyntheticCaseType.STAGE_FAILURE,
            SyntheticCaseType.ADVERSARIAL_KEEP,
            SyntheticCaseType.ADVERSARIAL_REVISE,
            SyntheticCaseType.ADVERSARIAL_REJECT,
            SyntheticCaseType.COMPATIBLE_EVIDENCE_COMBINATION,
        }
        self.assertTrue(expected <= present, expected - present)

    def test_generation_is_deterministic_and_preserves_sources(self):
        before = copy.deepcopy(
            tuple(item.to_dict() for item in self.source_runs)
        )
        second = self.generator.generate(self.source_runs)
        self.assertEqual(second, self.cases)
        self.assertEqual(
            tuple(item.to_dict() for item in self.source_runs),
            before,
        )

    def test_generation_rejects_invalid_source_fixtures(self):
        with self.assertRaisesRegex(
            ValueError,
            "requires valid source fixtures",
        ):
            self.generator.generate(
                (replace(self.base, content_hash="tampered"),)
            )

    def test_synthetic_cases_round_trip_and_hash(self):
        for case in self.cases:
            with self.subTest(case_type=case.case_type.value):
                restored = SyntheticEvaluationCase.from_dict(
                    case.to_dict()
                )
                self.assertEqual(restored, case)
                self.assertEqual(
                    restored.content_hash,
                    synthetic_evaluation_case_content_hash(
                        restored
                    ),
                )

    def test_negative_synthetic_cases_trigger_their_metrics(self):
        by_type = {}
        for case in self.cases:
            by_type.setdefault(case.case_type, case)
        expected_hard = {
            SyntheticCaseType.INVALID_CUTOFF: (
                HARD_FAILURE_FUTURE_EVIDENCE
            ),
            SyntheticCaseType.INVENTED_REFERENCE: (
                HARD_FAILURE_INVENTED_REFERENCE
            ),
            SyntheticCaseType.MISSING_INVALIDATION: (
                HARD_FAILURE_MISSING_INVALIDATION
            ),
            SyntheticCaseType.REPLAY_MISMATCH: (
                HARD_FAILURE_NON_REPRODUCIBLE
            ),
            SyntheticCaseType.STAGE_FAILURE: (
                HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE
            ),
        }
        for case_type, hard_code in expected_hard.items():
            with self.subTest(case_type=case_type.value):
                suite = self.engine.evaluate(
                    by_type[case_type].evaluation_run
                )
                self.assertIn(
                    hard_code,
                    suite.hard_failure_codes,
                )

        duplicate_suite = self.engine.evaluate(
            by_type[
                SyntheticCaseType.DUPLICATE_SCENARIO
            ].evaluation_run
        )
        weak_suite = self.engine.evaluate(
            by_type[
                SyntheticCaseType.WEAK_CAUSAL_CHAIN
            ].evaluation_run
        )
        self.assertGreater(
            _metric(
                duplicate_suite,
                "scenario_chain_distinctness",
            ).value,
            0,
        )
        self.assertLess(
            _metric(
                weak_suite,
                "causal_chain_completeness",
            ).value,
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
