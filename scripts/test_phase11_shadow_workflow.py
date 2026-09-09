from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from pathlib import Path

from elliott_ai.agent import ElliottAgent
from elliott_ai.forecast_outcomes import (
    EvaluationPolicy,
    ObservationScheduleMode,
    OutcomeStatus,
    build_forecast_observation_set,
)
from elliott_ai.forecast_records import (
    ClaimEvaluationBasis,
    ConfirmationClaim,
    ConfirmationOperator,
    ExpectedCompletionWindow,
    ForecastDirection,
    InvalidationClaim,
    InvalidationOperator,
    TargetClaim,
    canonical_sha256,
)
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.mistake_memory import (
    FailureDiagnosis,
    FailureDiagnosisType,
    LessonScopeType,
    ReviewDecision,
    approve_lesson,
    create_forecast_outcome_review,
    propose_mistake_memory_lesson,
)
from elliott_ai.outcome_learning_agents import OutcomeLearningAgentOrchestrator
from elliott_ai.phase11_shadow_workflow import (
    OperatorApproval,
    Phase11ShadowWorkflow,
    ShadowWorkflowResult,
    ShadowWorkflowStage,
    ShadowWorkflowStatus,
    replay_shadow_workflow_result,
    validate_shadow_workflow_result,
)
from elliott_ai.technical_agent_orchestrator import (
    TechnicalAgentOrchestrator,
    TechnicalAgentRole,
    build_technical_agent_request_from_stored_records,
)

try:
    from scripts.test_outcome_learning_agents import FixtureProvider as D2FixtureProvider
    from scripts.test_technical_agent_orchestrator import (
        CUTOFF,
        EXECUTED,
        FixtureProvider as D1FixtureProvider,
        _resolution,
        _source_records,
        _technical_evidence,
    )
except ModuleNotFoundError:  # Direct execution from scripts.
    from test_outcome_learning_agents import FixtureProvider as D2FixtureProvider
    from test_technical_agent_orchestrator import (
        CUTOFF,
        EXECUTED,
        FixtureProvider as D1FixtureProvider,
        _resolution,
        _source_records,
        _technical_evidence,
    )


APPROVED_AT = "2026-01-05T21:06:00+00:00"
HORIZON_END = "2026-01-10T21:00:00+00:00"
OBSERVED_AT = "2026-01-10T21:01:00+00:00"
EVALUATED_AT = "2026-01-10T21:02:00+00:00"
DRAFTED_AT = "2026-01-10T21:03:00+00:00"
REVIEWED_AT = "2026-01-10T21:04:00+00:00"
PROPOSED_AT = "2026-01-10T21:05:00+00:00"
FUTURE_CUTOFF = "2026-01-20T21:00:00+00:00"


class FutureD1FixtureProvider(D1FixtureProvider):
    def generate_strict_json(self, **kwargs):
        result = super().generate_strict_json(**kwargs)
        if kwargs["packet"]["agent_role"] in {
            TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value,
            TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value,
        }:
            result["candidate"]["degree_resolution"]["data_cutoff"] = kwargs[
                "packet"
            ]["request_reference"]["analysis_cutoff_utc"]
        return result


class Phase11E1Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "brain.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.run, self.resolution = _source_records(self.store)
        self.d1_provider = D1FixtureProvider()
        self.d2_provider = D2FixtureProvider()
        self.workflow = self.make_workflow()

    def make_workflow(
        self,
        *,
        d1_provider=None,
        d2_provider=None,
    ) -> Phase11ShadowWorkflow:
        selected_d1 = d1_provider or self.d1_provider
        selected_d2 = d2_provider or self.d2_provider
        return Phase11ShadowWorkflow(
            store=self.store,
            technical_orchestrator=TechnicalAgentOrchestrator(
                provider=selected_d1,
                lesson_store=self.store,
            ),
            outcome_learning_orchestrator=OutcomeLearningAgentOrchestrator(
                provider=selected_d2
            ),
        )

    def request(self, *, shadow: bool = True, run=None, resolution=None):
        selected_run = run or self.run
        selected_resolution = resolution or self.resolution
        return build_technical_agent_request_from_stored_records(
            selected_run,
            selected_resolution,
            provider=self.d1_provider.name,
            model=self.d1_provider.model,
            created_at_utc=(
                FUTURE_CUTOFF if selected_run is not self.run else EXECUTED
            ),
            shadow_mode=shadow,
        )

    def start(self, *, workflow=None, request=None, executed_at_utc=None):
        selected = workflow or self.workflow
        return selected.start_decision_time_analysis(
            request or self.request(),
            executed_at_utc=executed_at_utc or (
                FUTURE_CUTOFF if request is not None and request.analysis_cutoff_utc == FUTURE_CUTOFF else EXECUTED
            ),
            shadow_mode=True,
        )

    def approval(
        self,
        result,
        *,
        selected_candidate_id: str | None = None,
        include_scored_alternative: bool = False,
        structural_invalidation: bool = False,
        proposal_hash: str | None = None,
    ) -> OperatorApproval:
        proposal = result.proposal
        assert proposal is not None
        selected = selected_candidate_id or proposal.proposed_selected_candidate_id
        assert selected is not None
        targets = [
            TargetClaim(
                claim_id=f"target-{selected}",
                hypothesis_id=selected,
                target_low=140.0,
                target_high=145.0,
                direction=ForecastDirection.UP,
                timeframe="daily",
                price_basis="ohlc",
                rationale="Frozen operator target fixture.",
            )
        ]
        invalidations = [
            InvalidationClaim(
                claim_id=f"invalidate-{selected}",
                hypothesis_id=selected,
                operator=(
                    InvalidationOperator.STRUCTURAL_CONDITION
                    if structural_invalidation
                    else InvalidationOperator.AT_OR_BELOW
                ),
                condition=(
                    "A later human-reviewed structural overlap invalidates the count."
                    if structural_invalidation
                    else "Price reaches or breaches 90."
                ),
                timeframe="daily",
                price_basis="ohlc",
                price_level=(None if structural_invalidation else 90.0),
                evaluation_basis=(
                    ClaimEvaluationBasis.HUMAN_STRUCTURAL_REVIEW
                    if structural_invalidation
                    else ClaimEvaluationBasis.INTRABAR_TOUCH_OR_BREACH
                ),
            )
        ]
        windows = [
            ExpectedCompletionWindow(
                window_id=f"window-{selected}",
                hypothesis_id=selected,
                start_utc=CUTOFF,
                end_utc=HORIZON_END,
                timeframe="daily",
                start_rule="First completed post-cutoff candle.",
                end_rule="Frozen five-session horizon.",
                rationale="Deterministic fixture horizon.",
            )
        ]
        confirmations = []
        if structural_invalidation:
            confirmations.append(
                ConfirmationClaim(
                    claim_id=f"confirm-{selected}",
                    hypothesis_id=selected,
                    operator=ConfirmationOperator.STRUCTURAL_CONDITION,
                    condition="A human must confirm the terminal structure.",
                    timeframe="daily",
                    price_basis="ohlc",
                    evaluation_basis=ClaimEvaluationBasis.HUMAN_STRUCTURAL_REVIEW,
                )
            )
        if include_scored_alternative:
            alternate = next(
                item for item in proposal.eligible_candidate_ids if item != selected
            )
            targets.append(
                TargetClaim(
                    claim_id=f"target-{alternate}",
                    hypothesis_id=alternate,
                    target_low=115.0,
                    target_high=120.0,
                    direction=ForecastDirection.UP,
                    timeframe="daily",
                    price_basis="ohlc",
                    rationale="Frozen alternative target fixture.",
                )
            )
            invalidations.append(
                InvalidationClaim(
                    claim_id=f"invalidate-{alternate}",
                    hypothesis_id=alternate,
                    operator=InvalidationOperator.AT_OR_BELOW,
                    condition="Price reaches or breaches 80.",
                    timeframe="daily",
                    price_basis="ohlc",
                    price_level=80.0,
                )
            )
            windows.append(
                ExpectedCompletionWindow(
                    window_id=f"window-{alternate}",
                    hypothesis_id=alternate,
                    start_utc=CUTOFF,
                    end_utc=HORIZON_END,
                    timeframe="daily",
                    start_rule="First completed post-cutoff candle.",
                    end_rule="Frozen five-session horizon.",
                    rationale="Deterministic alternative horizon.",
                )
            )
        return OperatorApproval.create(
            approval_id="",
            human_actor="human:operator-1",
            approved_at_utc=APPROVED_AT,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal_hash or proposal.content_hash,
            selected_candidate_id=selected,
            direction=ForecastDirection.UP,
            target_claims=tuple(targets),
            invalidation_claims=tuple(invalidations),
            confirmation_claims=tuple(confirmations),
            expected_completion_windows=tuple(windows),
            approval_note="I reviewed the frozen candidates and typed claims.",
        )

    def approve(self, result=None, **approval_options):
        selected = result or self.start()
        return self.workflow.approve_forecast(
            selected, self.approval(selected, **approval_options)
        )

    def observation(self, result, *, mode: str = "success"):
        forecast = self.store.get_forecast_record(result.forecast_id)
        assert forecast is not None
        start = datetime.fromisoformat(forecast.analysis_cutoff_utc)
        expected_opens = tuple(
            (start + timedelta(days=index)).isoformat(timespec="seconds")
            for index in range(5)
        )
        rows = []
        count = 2 if mode == "insufficient" else 5
        for index in range(count):
            opened = start + timedelta(days=index)
            high = 110.0
            low = 95.0
            if mode == "success" and index == 1:
                high = 142.0
            elif mode == "failure" and index == 1:
                low = 85.0
            elif mode == "same_candle" and index == 1:
                high, low = 142.0, 85.0
            elif mode == "failed_main_alt_success":
                if index == 0:
                    high = 118.0
                if index == 1:
                    low = 85.0
            elif mode == "structural_unresolved" and index == 1:
                high = 142.0
            rows.append(
                {
                    "open_time_utc": opened.isoformat(timespec="seconds"),
                    "close_time_utc": (opened + timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                    "open": 100.0,
                    "high": high,
                    "low": low,
                    "close": 102.0,
                    "volume": 1_000.0 + index,
                    "source_sequence": index,
                }
            )
        policy = EvaluationPolicy.create(
            expected_interval_seconds=86_400,
            schedule_mode=ObservationScheduleMode.EXPLICIT_EXPECTED_OPENS,
        )
        actual_cutoff = HORIZON_END
        return build_forecast_observation_set(
            forecast,
            rows,
            source_dataset_id=forecast.dataset_cutoffs[0].dataset_id,
            actual_evaluation_cutoff_utc=actual_cutoff,
            evaluation_policy=policy,
            expected_open_times_utc=expected_opens,
        )

    def to_evaluation(
        self,
        *,
        mode: str,
        include_scored_alternative: bool = False,
        structural_invalidation: bool = False,
    ):
        result = self.approve(
            include_scored_alternative=include_scored_alternative,
            structural_invalidation=structural_invalidation,
        )
        observation = self.observation(result, mode=mode)
        result = self.workflow.accept_observations(
            result, observation, accepted_at_utc=OBSERVED_AT
        )
        result = self.workflow.evaluate(result, evaluated_at_utc=EVALUATED_AT)
        evaluation = self.store.get_forecast_outcome_evaluation(result.evaluation_id)
        assert evaluation is not None
        return result, evaluation

    def to_draft(self, *, mode: str, **options):
        result, evaluation = self.to_evaluation(mode=mode, **options)
        result = self.workflow.draft_outcome_review(
            result, drafted_at_utc=DRAFTED_AT, shadow_mode=True
        )
        return result, evaluation

    def human_review(
        self,
        evaluation,
        *,
        decision: ReviewDecision = ReviewDecision.APPROVED,
        parent=None,
    ):
        diagnoses = ()
        if evaluation.main_hypothesis_outcome.outcome_status in {
            OutcomeStatus.FAILED,
            OutcomeStatus.PARTIAL,
        }:
            diagnoses = (
                FailureDiagnosis(
                    diagnosis_id="diagnosis-" + evaluation.evaluation_id,
                    diagnosis_type=FailureDiagnosisType.PREMATURE_COMPLETION,
                    summary="The frozen main count failed its deterministic claims.",
                    affected_hypothesis_ids=(
                        evaluation.main_hypothesis_outcome.hypothesis_id,
                    ),
                ),
            )
        review = create_forecast_outcome_review(
            evaluation,
            reviewer_reference="human:reviewer-1",
            reviewed_at_utc=(
                "2026-01-10T21:04:30+00:00" if parent is not None else REVIEWED_AT
            ),
            decision=decision,
            notes="Human reviewed the immutable deterministic outcome.",
            diagnoses=diagnoses,
            corrected_interpretation=(
                "The corrected human interpretation remains advisory."
                if decision is ReviewDecision.REVISED
                else None
            ),
            evidence_reference_ids=(
                evaluation.main_hypothesis_outcome.hypothesis_id,
            ),
            parent_review=parent,
        )
        self.store.create_forecast_outcome_review(review)
        return review

    def failed_to_proposal(self, *, decision=ReviewDecision.APPROVED, d2_provider=None):
        if d2_provider is not None:
            self.workflow = self.make_workflow(d2_provider=d2_provider)
        result, evaluation = self.to_draft(mode="failure")
        parent = None
        if decision is ReviewDecision.REVISED:
            parent = self.human_review(evaluation)
        review = self.human_review(evaluation, decision=decision, parent=parent)
        result = self.workflow.accept_human_outcome_review(result, review)
        result = self.workflow.propose_mistake_memory(
            result,
            proposed_at_utc=PROPOSED_AT,
            shadow_mode=True,
        )
        return result, evaluation, review


class ShadowContractAndTransitionTests(Phase11E1Fixture):
    def test_immutable_contracts_replay_and_hash_tampering(self) -> None:
        result = self.start()
        with self.assertRaises(FrozenInstanceError):
            result.status = ShadowWorkflowStatus.COMPLETED  # type: ignore[misc]
        restored = ShadowWorkflowResult.from_dict(result.to_dict())
        self.assertEqual(restored, result)
        self.assertEqual(validate_shadow_workflow_result(result), ())
        self.assertEqual(
            replay_shadow_workflow_result(result, expected_content_hash=result.content_hash),
            result,
        )
        payload = result.to_dict()
        payload["proposal"]["eligible_candidate_ids"] = []
        with self.assertRaisesRegex(ValueError, "hash|eligibility|classify"):
            ShadowWorkflowResult.from_dict(payload)

    def test_skipped_repeated_and_out_of_order_transitions_are_rejected(self) -> None:
        first = self.start()
        with self.assertRaisesRegex(RuntimeError, "Out-of-order"):
            self.workflow.evaluate(first, evaluated_at_utc=EVALUATED_AT)
        approved = self.workflow.approve_forecast(first, self.approval(first))
        with self.assertRaisesRegex(RuntimeError, "Out-of-order"):
            self.workflow.approve_forecast(approved, self.approval(first))

    def test_repeated_identical_calls_are_deterministic_and_idempotent(self) -> None:
        first = self.start()
        second = self.start()
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(len(self.store.list_forecast_agent_orchestrations()), 1)
        approval = self.approval(first)
        frozen_one = self.workflow.approve_forecast(first, approval)
        frozen_two = self.workflow.approve_forecast(first, approval)
        self.assertEqual(frozen_one.content_hash, frozen_two.content_hash)
        self.assertEqual(len(self.store.list_forecast_records()), 1)

    def test_wrong_proposal_hash_and_hard_invalid_candidate_are_blocked(self) -> None:
        first = self.start()
        with self.assertRaisesRegex(ValueError, "proposal ID or hash"):
            self.workflow.approve_forecast(
                first,
                self.approval(first, proposal_hash="0" * 64),
            )

        hard_provider = D1FixtureProvider(
            hard_invalid_roles={TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value}
        )
        workflow = self.make_workflow(d1_provider=hard_provider)
        request = build_technical_agent_request_from_stored_records(
            self.run,
            self.resolution,
            provider=hard_provider.name,
            model=hard_provider.model,
            created_at_utc=EXECUTED,
            shadow_mode=True,
        )
        hard = self.start(
            workflow=workflow,
            request=request,
            executed_at_utc="2026-01-05T21:05:01+00:00",
        )
        primary_id = next(
            item.candidate_id
            for item in self.store.get_forecast_agent_orchestration(
                hard.proposal.technical_orchestration_id
            ).candidates
            if item.role is TechnicalAgentRole.PRIMARY_WAVE_COUNTER
        )
        with self.assertRaisesRegex(ValueError, "hard-rule-invalid|ineligible"):
            workflow.approve_forecast(
                hard,
                self.approval(hard, selected_candidate_id=primary_id),
            )


class EndToEndLifecycleTests(Phase11E1Fixture):
    def test_successful_main_finishes_without_lesson(self) -> None:
        result, evaluation = self.to_draft(mode="success")
        self.assertEqual(evaluation.main_hypothesis_outcome.outcome_status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(self.store.list_forecast_outcome_reviews(), [])
        review = self.human_review(evaluation)
        completed = self.workflow.accept_human_outcome_review(result, review)
        self.assertEqual(completed.status, ShadowWorkflowStatus.COMPLETED)
        self.assertEqual(completed.current_stage, ShadowWorkflowStage.COMPLETED)
        self.assertEqual(self.store.list_mistake_memory_lessons(), [])

    def test_failed_main_and_successful_alternative_remain_separate(self) -> None:
        result, evaluation = self.to_draft(
            mode="failed_main_alt_success", include_scored_alternative=True
        )
        self.assertEqual(evaluation.main_hypothesis_outcome.outcome_status, OutcomeStatus.FAILED)
        self.assertEqual(len(evaluation.alternative_hypothesis_outcomes), 1)
        self.assertEqual(
            evaluation.alternative_hypothesis_outcomes[0].outcome_status,
            OutcomeStatus.SUCCEEDED,
        )
        self.assertIn(
            evaluation.alternative_hypothesis_outcomes[0].hypothesis_id,
            result.outcome_review_draft.alternative_hypothesis_summaries,
        )

    def test_failed_forecast_reaches_reviewed_lesson_draft_only(self) -> None:
        result, _, review = self.failed_to_proposal()
        self.assertEqual(result.status, ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED)
        self.assertIsNotNone(result.mistake_lesson_proposal_draft)
        self.assertTrue(result.mistake_lesson_proposal_draft.human_action_required)
        self.assertEqual(result.human_review_id, review.review_id)
        self.assertEqual(self.store.list_mistake_memory_lessons(), [])
        self.assertEqual(self.store.list_mistake_memory_events(), [])

    def test_revised_human_review_is_eligible_for_draft(self) -> None:
        result, _, review = self.failed_to_proposal(decision=ReviewDecision.REVISED)
        self.assertEqual(review.decision, ReviewDecision.REVISED)
        self.assertEqual(result.status, ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED)
        stored = self.store.list_forecast_outcome_learning_orchestrations(
            orchestration_kind="mistake_memory_proposal"
        )
        self.assertEqual(len(stored), 1)

    def test_unresolved_stops_before_mistake_learning(self) -> None:
        result, evaluation = self.to_draft(
            mode="structural_unresolved", structural_invalidation=True
        )
        self.assertEqual(
            evaluation.main_hypothesis_outcome.outcome_status,
            OutcomeStatus.UNRESOLVED,
        )
        review = self.human_review(evaluation)
        stopped = self.workflow.accept_human_outcome_review(result, review)
        self.assertEqual(stopped.status, ShadowWorkflowStatus.STOPPED_NO_LEARNING)
        with self.assertRaisesRegex(RuntimeError, "Out-of-order"):
            self.workflow.propose_mistake_memory(
                stopped, proposed_at_utc=PROPOSED_AT, shadow_mode=True
            )

    def test_insufficient_data_stops_safely(self) -> None:
        result, evaluation = self.to_draft(mode="insufficient")
        self.assertEqual(evaluation.outcome_status, OutcomeStatus.INSUFFICIENT_DATA)
        review = self.human_review(evaluation)
        stopped = self.workflow.accept_human_outcome_review(result, review)
        self.assertEqual(stopped.status, ShadowWorkflowStatus.STOPPED_NO_LEARNING)
        self.assertIsNone(stopped.mistake_lesson_proposal_draft)

    def test_same_candle_collision_stops_as_incomparable(self) -> None:
        result, evaluation = self.to_draft(mode="same_candle")
        self.assertEqual(evaluation.outcome_status, OutcomeStatus.INCOMPARABLE)
        review = self.human_review(evaluation)
        stopped = self.workflow.accept_human_outcome_review(result, review)
        self.assertEqual(stopped.status, ShadowWorkflowStatus.STOPPED_NO_LEARNING)

    def test_no_automatic_forecast_observations_reviews_or_lessons(self) -> None:
        first = self.start()
        self.assertEqual(self.store.list_forecast_records(), [])
        approved = self.workflow.approve_forecast(first, self.approval(first))
        self.assertEqual(len(self.store.list_forecast_records()), 1)
        self.assertEqual(self.store.list_forecast_observation_sets(), [])
        observation = self.observation(approved, mode="failure")
        observed = self.workflow.accept_observations(
            approved, observation, accepted_at_utc=OBSERVED_AT
        )
        evaluated = self.workflow.evaluate(observed, evaluated_at_utc=EVALUATED_AT)
        drafted = self.workflow.draft_outcome_review(
            evaluated, drafted_at_utc=DRAFTED_AT, shadow_mode=True
        )
        self.assertIsNotNone(drafted.outcome_review_draft)
        self.assertEqual(self.store.list_forecast_outcome_reviews(), [])
        self.assertEqual(self.store.list_mistake_memory_lessons(), [])


class SafetyFailureAndIsolationTests(Phase11E1Fixture):
    def test_future_candle_and_metadata_leakage_are_rejected(self) -> None:
        approved = self.approve()
        forecast = self.store.get_forecast_record(approved.forecast_id)
        observation = self.observation(approved, mode="success")
        payload = observation.to_dict()
        payload["candles"][-1]["close_time_utc"] = "2026-01-11T21:00:00+00:00"
        payload["candles"][-1]["open_time_utc"] = HORIZON_END
        payload["content_hash"] = "0" * 64
        with self.assertRaises(ValueError):
            self.workflow.accept_observations(
                approved,
                type(observation).from_dict(payload, verify_hash=False),
                accepted_at_utc=OBSERVED_AT,
            )
        self.assertIsNotNone(forecast)
        with self.assertRaisesRegex(ValueError, "incompatible"):
            build_forecast_observation_set(
                forecast,
                [item.to_dict() for item in observation.candles],
                source_dataset_id=forecast.dataset_cutoffs[0].dataset_id,
                actual_evaluation_cutoff_utc=HORIZON_END,
                evaluation_policy=observation.evaluation_policy,
                expected_open_times_utc=observation.completeness.expected_open_times_utc,
                feed="wrong-feed",
            )

    def test_wrong_human_review_lineage_is_rejected(self) -> None:
        result, evaluation = self.to_draft(mode="failure")
        review = self.human_review(evaluation)
        unregistered = replace(
            review,
            review_id="forecast_review_other_lineage",
            content_hash="0" * 64,
        )
        with self.assertRaisesRegex(ValueError, "already exist|unchanged"):
            self.workflow.accept_human_outcome_review(result, unregistered)

    def test_general_scope_threshold_remains_enforced(self) -> None:
        provider = D2FixtureProvider(
            proposal_options={"scope_type": LessonScopeType.GENERAL.value}
        )
        result, _, _ = self.failed_to_proposal(d2_provider=provider)
        self.assertEqual(result.status, ShadowWorkflowStatus.FAILED)
        self.assertIsNone(result.mistake_lesson_proposal_draft)
        self.assertTrue(any("three independent forecasts" in item for item in result.errors))

    def test_provider_failure_and_partial_agent_failure_fail_closed(self) -> None:
        failed_provider = D1FixtureProvider(
            fail_roles={
                TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value,
                TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value,
            }
        )
        failed_workflow = self.make_workflow(d1_provider=failed_provider)
        failed_request = build_technical_agent_request_from_stored_records(
            self.run,
            self.resolution,
            provider=failed_provider.name,
            model=failed_provider.model,
            created_at_utc=EXECUTED,
            shadow_mode=True,
        )
        failed = self.start(workflow=failed_workflow, request=failed_request)
        self.assertEqual(failed.status, ShadowWorkflowStatus.FAILED)
        self.assertIsNone(failed.human_gate)

        partial_provider = D1FixtureProvider(
            fail_roles={TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value}
        )
        partial_workflow = self.make_workflow(d1_provider=partial_provider)
        partial_request = build_technical_agent_request_from_stored_records(
            self.run,
            self.resolution,
            provider=partial_provider.name,
            model=partial_provider.model,
            created_at_utc=EXECUTED,
            shadow_mode=True,
        )
        partial = self.start(
            workflow=partial_workflow,
            request=partial_request,
            executed_at_utc="2026-01-05T21:05:01+00:00",
        )
        self.assertEqual(partial.status, ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED)
        self.assertEqual(len(partial.proposal.eligible_candidate_ids), 1)

    def test_outcome_provider_failure_stops_without_human_review(self) -> None:
        provider = D2FixtureProvider(fail_count=1)
        self.workflow = self.make_workflow(d2_provider=provider)
        result, _ = self.to_evaluation(mode="failure")
        stopped = self.workflow.draft_outcome_review(
            result, drafted_at_utc=DRAFTED_AT, shadow_mode=True
        )
        self.assertEqual(stopped.status, ShadowWorkflowStatus.FAILED)
        self.assertIsNone(stopped.outcome_review_draft)
        self.assertEqual(self.store.list_forecast_outcome_reviews(), [])

    def test_no_live_pipeline_cli_report_telegram_or_tradingview_integration(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow_name = "phase11_shadow_workflow"
        for relative in (
            "elliott_ai/agent.py",
            "elliott_ai/cli.py",
            "elliott_ai/reporting.py",
            "elliott_ai/telegram_bot.py",
        ):
            path = root / relative
            if path.exists():
                self.assertNotIn(workflow_name, path.read_text(encoding="utf-8"))
        self.assertEqual(
            str(inspect.signature(ElliottAgent.analyze)),
            "(self, request: 'AnalysisRequest') -> 'dict[str, Any]'",
        )

    def test_temporary_database_only_and_sqlite_integrity(self) -> None:
        self.start()
        self.assertTrue(
            str(self.store.database_path).startswith(self.temporary.name)
        )
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertNotIn("phase11_shadow_workflows", tables)
        self.assertNotIn("shadow_workflow_checkpoints", tables)


class FutureAnalysisProofTests(Phase11E1Fixture):
    def _activate_exact_lesson(self, result, evaluation, review):
        draft = result.mistake_lesson_proposal_draft
        lesson, sources, proposed = propose_mistake_memory_lesson(
            (review,),
            {evaluation.evaluation_id: evaluation},
            forecast_symbols={review.forecast_id: self.store.get_forecast_record(review.forecast_id).symbol},
            scope=draft.recommended_scope,
            title=draft.title,
            warning_text=draft.warning_text,
            recommended_audit_check=draft.recommended_audit_check,
            proposed_by="human:reviewer-1",
            proposed_at_utc="2026-01-10T21:06:00+00:00",
        )
        active = approve_lesson(
            lesson,
            (proposed,),
            sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc="2026-01-10T21:07:00+00:00",
            reason="Human approved the exact-case audit warning.",
        )
        self.store.create_mistake_memory_lesson(lesson)
        for source in sources:
            self.store.create_mistake_memory_source(source)
        self.store.create_mistake_memory_event(proposed)
        self.store.create_mistake_memory_event(active)
        return lesson

    def _future_sources(self):
        evidence = _technical_evidence(end_date=FUTURE_CUTOFF)
        run_id = self.store.save_run(
            symbol=self.run["symbol"],
            provider="fixture",
            model="fixture-v1",
            question="Future fixture analysis",
            request={"symbol": self.run["symbol"]},
            evidence=evidence,
            response={"data_cutoff": FUTURE_CUTOFF},
            validation_errors=[],
        )
        response = _resolution(run_id=run_id)
        response["data_cutoff"] = FUTURE_CUTOFF
        resolution_id = self.store.save_degree_resolution(
            run_id=run_id,
            provider="fixture",
            model="fixture-v1",
            request={"source_run_id": run_id},
            response=response,
            validation_errors=[],
            readiness={"final_report_ready": False},
        )
        return self.store.get_run(run_id), self.store.get_degree_resolution(resolution_id)

    def test_active_exact_lesson_is_retrieved_only_after_future_counts_freeze(self) -> None:
        self.d1_provider = FutureD1FixtureProvider()
        self.workflow = self.make_workflow(d1_provider=self.d1_provider)
        result, evaluation, review = self.failed_to_proposal()
        lesson = self._activate_exact_lesson(result, evaluation, review)
        run, resolution = self._future_sources()
        future_request = self.request(run=run, resolution=resolution)
        call_start = len(self.d1_provider.calls)
        phase5_hash = canonical_sha256(
            {"ordered_analogue_ids": ["case-a", "case-b"], "policy": "fixture"}
        )
        completed = self.workflow.prove_future_analysis_isolation(
            result,
            future_request,
            expected_lesson_id=lesson.lesson_id,
            phase5_ranking_hash_before=phase5_hash,
            phase5_ranking_hash_after=phase5_hash,
            executed_at_utc=FUTURE_CUTOFF,
            shadow_mode=True,
        )
        calls = self.d1_provider.calls[call_start:]
        counter_calls = calls[:2]
        self.assertEqual(
            [item["role"] for item in counter_calls],
            [
                TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value,
                TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value,
            ],
        )
        for call in counter_calls:
            serialized = json.dumps(call["packet"])
            self.assertNotIn(lesson.lesson_id, serialized)
            self.assertNotIn(lesson.warning_text, serialized)
            self.assertNotIn("retrieved_lessons", serialized)
        auditor_call = next(
            item
            for item in calls
            if item["role"] == TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR.value
        )
        self.assertIn(lesson.lesson_id, json.dumps(auditor_call["packet"]))
        self.assertEqual(completed.status, ShadowWorkflowStatus.COMPLETED)
        self.assertIsNotNone(completed.future_technical_orchestration_id)
        future = self.store.get_forecast_agent_orchestration(
            completed.future_technical_orchestration_id
        )
        self.assertIn(lesson.lesson_id, {item.lesson_id for item in future.retrieved_lessons})
        self.assertTrue(all(item.structural_validity_unchanged for item in future.evidence_audits))

    def test_future_proof_rejects_phase5_ranking_change_and_future_lesson(self) -> None:
        self.d1_provider = FutureD1FixtureProvider()
        self.workflow = self.make_workflow(d1_provider=self.d1_provider)
        result, evaluation, review = self.failed_to_proposal()
        lesson = self._activate_exact_lesson(result, evaluation, review)
        run, resolution = self._future_sources()
        future_request = self.request(run=run, resolution=resolution)
        with self.assertRaisesRegex(ValueError, "Phase 5 analogue ranking changed"):
            self.workflow.prove_future_analysis_isolation(
                result,
                future_request,
                expected_lesson_id=lesson.lesson_id,
                phase5_ranking_hash_before="1" * 64,
                phase5_ranking_hash_after="2" * 64,
                executed_at_utc=FUTURE_CUTOFF,
                shadow_mode=True,
            )


if __name__ == "__main__":
    unittest.main()
