from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from elliott_ai.forecast_outcomes import (
    ForecastObservationSet,
    ForecastOutcomeEvaluation,
    ObservationCandle,
    OutcomeStatus,
    candle_manifest_hash,
    evaluate_forecast_observation_set,
)
from elliott_ai.forecast_records import ForecastRecord
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.mistake_memory import (
    FailureDiagnosis,
    FailureDiagnosisType,
    LessonScope,
    LessonScopeType,
    LessonStatus,
    ReviewDecision,
    build_lesson_event,
    create_forecast_outcome_review,
    propose_mistake_memory_lesson,
)
from elliott_ai.outcome_learning_agents import (
    FORECAST_OUTCOME_LEARNING_ORCHESTRATION_MIGRATION_SQL,
    MISTAKE_MEMORY_PROPOSAL_OUTPUT_SCHEMA,
    OUTCOME_REVIEWER_OUTPUT_SCHEMA,
    MistakeMemoryProposalRequest,
    OutcomeLearningAgentOrchestrator,
    OutcomeLearningAgentResultStatus,
    OutcomeLearningAgentRole,
    OutcomeLearningOrchestration,
    OutcomeLearningOrchestrationStatus,
    OutcomeReviewerRequest,
    build_mistake_memory_proposal_request,
    build_outcome_reviewer_request,
    canonical_outcome_learning_json,
    replay_outcome_learning_orchestration,
    validate_outcome_learning_orchestration,
)
from elliott_ai.providers import OllamaProvider, PacketProvider, ProviderError

try:
    from scripts.test_forecast_outcomes import _observation
    from scripts.test_forecast_records import _create_sources, _typed_record
except ModuleNotFoundError:  # Direct execution from scripts.
    from test_forecast_outcomes import _observation
    from test_forecast_records import _create_sources, _typed_record


def _after(timestamp: str, *, days: int = 1) -> str:
    return (
        datetime.fromisoformat(timestamp) + timedelta(days=days)
    ).astimezone(timezone.utc).isoformat(timespec="seconds")


def _review_output(packet: dict, *, mutation: str | None = None) -> dict:
    evaluation = packet["deterministic_evaluation"]
    main = evaluation["main_hypothesis_outcome"]
    alternatives = evaluation["alternative_hypothesis_outcomes"]
    status = OutcomeStatus(main["outcome_status"])
    if status in {
        OutcomeStatus.UNRESOLVED,
        OutcomeStatus.INSUFFICIENT_DATA,
        OutcomeStatus.INCOMPARABLE,
    }:
        decision = ReviewDecision.NEEDS_MORE_DATA.value
        diagnosis_type = FailureDiagnosisType.AMBIGUOUS_OR_INCOMPARABLE.value
        diagnoses = [
            {
                "diagnosis_type": diagnosis_type,
                "summary": "The deterministic result remains unavailable for a failure diagnosis.",
                "affected_hypothesis_ids": [main["hypothesis_id"]],
                "affected_claim_ids": [],
                "affected_wave_labels": [],
                "affected_degrees": [],
                "supporting_evidence_ids": [main["hypothesis_id"]],
                "contradictory_evidence_ids": [],
                "alternative_explanation": "More comparable post-cutoff evidence is required.",
            }
        ]
    elif status is OutcomeStatus.SUCCEEDED:
        decision = ReviewDecision.APPROVED.value
        diagnoses = []
    else:
        decision = ReviewDecision.APPROVED.value
        diagnoses = [
            {
                "diagnosis_type": FailureDiagnosisType.PREMATURE_COMPLETION.value,
                "summary": "The frozen count completed before price confirmed the structure.",
                "affected_hypothesis_ids": [main["hypothesis_id"]],
                "affected_claim_ids": [],
                "affected_wave_labels": [],
                "affected_degrees": [],
                "supporting_evidence_ids": [main["hypothesis_id"]],
                "contradictory_evidence_ids": [],
                "alternative_explanation": "A continuation interpretation remained open.",
            }
        ]
    result = {
        "reviewed_hypothesis_id": main["hypothesis_id"],
        "recommended_human_review_decision": decision,
        "diagnoses": diagnoses,
        "price_outcome_summary": "Price status is copied from deterministic evaluation evidence.",
        "timing_outcome_summary": "Timing remains separate from price accuracy.",
        "main_hypothesis_summary": "The main hypothesis remains separately identified.",
        "alternative_hypothesis_summaries": [
            {
                "hypothesis_id": item["hypothesis_id"],
                "summary": "This alternative remains separately reported.",
            }
            for item in alternatives
        ],
        "supporting_evidence_ids": [main["hypothesis_id"]],
        "contradictory_evidence_ids": [],
        "alternative_explanations": ["A different valid count may explain the move."],
        "unavailable_warnings": [],
        "incomparable_warnings": [],
        "concise_reasoning_summary": ["Draft only; explicit human review is required."],
    }
    if mutation == "invented_reference":
        result["supporting_evidence_ids"] = ["invented-evidence-id"]
    elif mutation == "missing_alternative":
        result["alternative_hypothesis_summaries"] = []
    elif mutation == "unknown_taxonomy":
        result["diagnoses"][0]["diagnosis_type"] = "invented_diagnosis"
    elif mutation == "rewrite_status":
        result["deterministic_outcome_status"] = OutcomeStatus.SUCCEEDED.value
    elif mutation == "force_failure_from_unresolved":
        result["recommended_human_review_decision"] = ReviewDecision.APPROVED.value
    return result


def _proposal_output(
    packet: dict,
    *,
    scope_type: str = LessonScopeType.EXACT_CASE.value,
    possible_duplicates: list[str] | None = None,
    supporting_review_ids: list[str] | None = None,
    counterexample_review_ids: list[str] | None = None,
    existing_lesson_reference_id: str | None = None,
) -> dict:
    primary = packet["primary_bundle"]
    forecast = primary["forecast"]
    review = primary["approved_review"]
    hypotheses = [
        forecast["main_hypothesis"],
        *forecast["alternative_hypotheses"],
    ]
    hypothesis = next(
        item for item in hypotheses if item["hypothesis_id"] == review["reviewed_hypothesis_id"]
    )
    support = supporting_review_ids or [review["review_id"]]
    scope = {
        "scope_type": scope_type,
        "exact_forecast_ids": (
            [forecast["forecast_id"]]
            if scope_type == LessonScopeType.EXACT_CASE.value
            else []
        ),
        "symbols": [forecast["symbol"]],
        "asset_classes": [],
        "timeframes": [forecast["dataset_cutoffs"][0]["timeframe"]],
        "degrees": [hypothesis["degree"]],
        "wave_roles": [hypothesis["wave_label"]],
        "pattern_families": [hypothesis["pattern_family"]],
        "directions": [hypothesis["direction"]],
        "indicator_regimes": [],
        "market_regimes": [],
        "applicability_conditions": ["The same endpoint interpretation is under audit."],
        "non_applicability_conditions": ["A hard invalidation already removed the count."],
    }
    return {
        "recommended_scope": scope,
        "title": "Audit premature completion",
        "warning_text": "Recheck whether the final wave actually completed.",
        "recommended_audit_check": "Preserve a continuation alternate until price confirms completion.",
        "supporting_review_ids": support,
        "counterexample_review_ids": counterexample_review_ids or [],
        "possible_duplicate_lesson_ids": possible_duplicates or [],
        "existing_lesson_reference_id": existing_lesson_reference_id,
        "proposal_rationale": ["Reviewed evidence supports only the requested narrow scope."],
        "limitations": ["This draft cannot create, merge, or activate a lesson."],
        "concise_reasoning_summary": ["A human must decide whether to create a lesson."],
    }


class FixtureProvider:
    name = "fixture"
    model = "fixture-v1"

    def __init__(
        self,
        *,
        review_mutation: str | None = None,
        proposal_options: dict | None = None,
        fail_count: int = 0,
        malformed: bool = False,
    ) -> None:
        self.review_mutation = review_mutation
        self.proposal_options = proposal_options or {}
        self.fail_count = fail_count
        self.malformed = malformed
        self.calls: list[dict] = []

    def generate_strict_json(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) <= self.fail_count:
            raise ProviderError("transient fixture failure")
        if self.malformed:
            return {"malformed": True}
        packet = kwargs["packet"]
        if packet["agent_role"] == OutcomeLearningAgentRole.OUTCOME_REVIEWER.value:
            return _review_output(packet, mutation=self.review_mutation)
        return _proposal_output(packet, **self.proposal_options)

    def generate(self, **kwargs):
        return self.generate_strict_json(**kwargs)


def _evaluation_with_status(
    evaluation: ForecastOutcomeEvaluation, status: OutcomeStatus
) -> ForecastOutcomeEvaluation:
    payload = evaluation.to_dict()
    payload.pop("content_hash")
    main = dict(payload["main_hypothesis_outcome"])
    main["outcome_status"] = status.value
    main["price_status"] = status.value
    main["timing_status"] = status.value
    if status is OutcomeStatus.INCOMPARABLE:
        main["comparable"] = False
    payload["main_hypothesis_outcome"] = main
    payload["outcome_status"] = status.value
    payload["evaluation_id"] = f"{evaluation.evaluation_id}-{status.value}"
    return ForecastOutcomeEvaluation.create(**payload)


def _clone_bundle(
    forecast: ForecastRecord,
    evaluation: ForecastOutcomeEvaluation,
    *,
    suffix: str,
    reviewed_at_utc: str,
) -> tuple[ForecastRecord, ForecastOutcomeEvaluation, object]:
    forecast_payload = forecast.to_dict()
    forecast_payload.pop("content_hash")
    forecast_payload["forecast_id"] = f"{forecast.forecast_id}-{suffix}"
    cloned_forecast = ForecastRecord.create(**forecast_payload)
    evaluation_payload = evaluation.to_dict()
    evaluation_payload.pop("content_hash")
    evaluation_payload["evaluation_id"] = f"{evaluation.evaluation_id}-{suffix}"
    evaluation_payload["forecast_id"] = cloned_forecast.forecast_id
    evaluation_payload["forecast_content_hash"] = cloned_forecast.content_hash
    evaluation_payload["source_hashes"]["forecast_record"] = cloned_forecast.content_hash
    cloned_evaluation = ForecastOutcomeEvaluation.create(**evaluation_payload)
    review = create_forecast_outcome_review(
        cloned_evaluation,
        reviewer_reference=f"human:{suffix}",
        reviewed_at_utc=reviewed_at_utc,
        decision=ReviewDecision.APPROVED,
        notes="Human approved the cloned fixture outcome.",
        diagnoses=(
            FailureDiagnosis(
                diagnosis_id=f"diagnosis-{suffix}",
                diagnosis_type=FailureDiagnosisType.PREMATURE_COMPLETION,
                summary="The endpoint was called too early.",
                affected_hypothesis_ids=(
                    cloned_evaluation.main_hypothesis_outcome.hypothesis_id,
                ),
            ),
        ),
        evidence_reference_ids=(
            cloned_evaluation.main_hypothesis_outcome.hypothesis_id,
        ),
    )
    return cloned_forecast, cloned_evaluation, review


class Phase11D2Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "brain.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.run_id, self.resolution_id = _create_sources(self.store)
        self.forecast = _typed_record(self.store, self.run_id, self.resolution_id)
        self.store.create_forecast_record(self.forecast)
        self.observation = _observation(self.forecast)
        self.store.create_forecast_observation_set(self.observation)
        self.evaluation = evaluate_forecast_observation_set(
            self.forecast, self.observation
        )
        self.store.create_forecast_outcome_evaluation(self.evaluation)
        self.reviewed_at = _after(self.evaluation.evaluated_through_utc)
        self.executed_at = _after(self.reviewed_at)

    def approved_review(
        self,
        *,
        decision: ReviewDecision = ReviewDecision.APPROVED,
        evaluation: ForecastOutcomeEvaluation | None = None,
    ):
        selected = evaluation or self.evaluation
        return create_forecast_outcome_review(
            selected,
            reviewer_reference="human:reviewer-1",
            reviewed_at_utc=self.reviewed_at,
            decision=decision,
            notes="Human reviewed the immutable deterministic outcome.",
            diagnoses=(
                FailureDiagnosis(
                    diagnosis_id=f"diagnosis-{selected.evaluation_id}",
                    diagnosis_type=FailureDiagnosisType.PREMATURE_COMPLETION,
                    summary="The endpoint was called before structural confirmation.",
                    affected_hypothesis_ids=(
                        selected.main_hypothesis_outcome.hypothesis_id,
                    ),
                ),
            ),
            evidence_reference_ids=(
                selected.main_hypothesis_outcome.hypothesis_id,
            ),
        )

    def reviewer_request(
        self,
        *,
        provider: FixtureProvider | PacketProvider | OllamaProvider | None = None,
        evaluation: ForecastOutcomeEvaluation | None = None,
        shadow_mode: bool = True,
    ) -> OutcomeReviewerRequest:
        selected_provider = provider or FixtureProvider()
        return build_outcome_reviewer_request(
            self.forecast,
            self.observation,
            evaluation or self.evaluation,
            provider=selected_provider.name,
            model=selected_provider.model,
            created_at_utc=self.reviewed_at,
            shadow_mode=shadow_mode,
        )

    def proposal_request(
        self,
        review=None,
        *,
        provider: FixtureProvider | None = None,
        shadow_mode: bool = True,
        additional=(),
        counterexamples=(),
        lessons=(),
        sources=(),
        events=(),
        existing_lesson_reference_id=None,
    ) -> MistakeMemoryProposalRequest:
        selected_provider = provider or FixtureProvider()
        return build_mistake_memory_proposal_request(
            self.forecast,
            self.evaluation,
            review or self.approved_review(),
            provider=selected_provider.name,
            model=selected_provider.model,
            created_at_utc=self.executed_at,
            additional_supporting_bundles=additional,
            counterexample_bundles=counterexamples,
            existing_lessons=lessons,
            existing_lesson_sources=sources,
            existing_lesson_events=events,
            existing_lesson_reference_id=existing_lesson_reference_id,
            shadow_mode=shadow_mode,
        )


class OutcomeReviewerTests(Phase11D2Fixture):
    def test_shadow_mode_is_disabled_at_request_and_invocation_boundaries(self):
        provider = FixtureProvider()
        request = self.reviewer_request(provider=provider, shadow_mode=False)
        with self.assertRaises(PermissionError):
            OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
                request, shadow_mode=True
            )
        request = self.reviewer_request(provider=provider)
        with self.assertRaises(PermissionError):
            OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
                request
            )

    def test_exact_horizon_and_source_hashes_are_frozen(self):
        request = self.reviewer_request()
        self.assertEqual(
            request.observation_set.horizon_end_utc,
            self.observation.horizon_end_utc,
        )
        self.assertEqual(
            request.source_hashes["outcome_evaluation"], self.evaluation.content_hash
        )
        payload = request.to_dict()
        payload["observation_set"]["horizon_end_utc"] = _after(
            self.observation.horizon_end_utc
        )
        with self.assertRaises(ValueError):
            OutcomeReviewerRequest.from_dict(payload)

    def test_future_candle_beyond_horizon_is_rejected(self):
        horizon_end = datetime.fromisoformat(self.observation.horizon_end_utc)
        future = ObservationCandle.create(
            candle_id="",
            open_time_utc=(horizon_end + timedelta(hours=1)).isoformat(
                timespec="seconds"
            ),
            close_time_utc=(horizon_end + timedelta(days=1)).isoformat(
                timespec="seconds"
            ),
            open=400.0,
            high=410.0,
            low=390.0,
            close=405.0,
            volume=1_000_000.0,
            is_complete=True,
            source_sequence=len(self.observation.candles),
        )
        payload = self.observation.to_dict()
        payload.pop("content_hash")
        candles = (*self.observation.candles, future)
        manifest = candle_manifest_hash(candles)
        payload["candles"] = [item.to_dict() for item in candles]
        payload["candle_manifest_hash"] = manifest
        payload["source_hashes"]["candle_manifest"] = manifest
        payload["completeness"]["observed_candle_count"] += 1
        payload["completeness"]["complete_candle_count"] += 1
        payload["completeness"]["unexpected_candle_ids"].append(future.candle_id)
        contaminated = ForecastObservationSet.create(**payload)
        with self.assertRaisesRegex(ValueError, "beyond the frozen horizon"):
            build_outcome_reviewer_request(
                self.forecast,
                contaminated,
                self.evaluation,
                provider="fixture",
                model="fixture-v1",
                created_at_utc=self.reviewed_at,
                shadow_mode=True,
            )

    def test_successful_draft_preserves_deterministic_results_and_alternatives(self):
        provider = FixtureProvider()
        request = self.reviewer_request(provider=provider)
        before = self.evaluation.content_hash
        result = OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            request, shadow_mode=True, executed_at_utc=self.executed_at
        )
        self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.COMPLETED)
        self.assertTrue(result.human_action_required)
        self.assertEqual(self.evaluation.content_hash, before)
        draft = result.outcome_review_draft
        assert draft is not None
        self.assertEqual(
            draft.deterministic_outcome_status,
            self.evaluation.main_hypothesis_outcome.outcome_status,
        )
        self.assertEqual(
            set(draft.alternative_hypothesis_summaries),
            {
                item.hypothesis_id
                for item in self.evaluation.alternative_hypothesis_outcomes
            },
        )

    def test_all_deterministic_outcome_states_are_preserved(self):
        for status in OutcomeStatus:
            with self.subTest(status=status.value):
                evaluation = _evaluation_with_status(self.evaluation, status)
                provider = FixtureProvider()
                request = self.reviewer_request(
                    provider=provider, evaluation=evaluation
                )
                result = OutcomeLearningAgentOrchestrator(
                    provider=provider
                ).run_outcome_reviewer(
                    request, shadow_mode=True, executed_at_utc=self.executed_at
                )
                self.assertNotEqual(
                    result.status, OutcomeLearningOrchestrationStatus.FAILED
                )
                assert result.outcome_review_draft is not None
                self.assertEqual(
                    result.outcome_review_draft.deterministic_outcome_status,
                    status,
                )

    def test_invented_reference_and_missing_alternative_fail_closed(self):
        for mutation in ("invented_reference", "missing_alternative"):
            with self.subTest(mutation=mutation):
                provider = FixtureProvider(review_mutation=mutation)
                result = OutcomeLearningAgentOrchestrator(
                    provider=provider
                ).run_outcome_reviewer(
                    self.reviewer_request(provider=provider),
                    shadow_mode=True,
                    executed_at_utc=self.executed_at,
                )
                self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.FAILED)
                self.assertIsNone(result.outcome_review_draft)

    def test_unknown_diagnosis_and_status_rewrite_fail_closed(self):
        for mutation in ("unknown_taxonomy", "rewrite_status"):
            with self.subTest(mutation=mutation):
                provider = FixtureProvider(review_mutation=mutation)
                result = OutcomeLearningAgentOrchestrator(
                    provider=provider
                ).run_outcome_reviewer(
                    self.reviewer_request(provider=provider),
                    shadow_mode=True,
                    executed_at_utc=self.executed_at,
                )
                self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.FAILED)

    def test_unresolved_cannot_be_converted_to_failure(self):
        evaluation = _evaluation_with_status(self.evaluation, OutcomeStatus.UNRESOLVED)
        provider = FixtureProvider(review_mutation="force_failure_from_unresolved")
        result = OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            self.reviewer_request(provider=provider, evaluation=evaluation),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.FAILED)

    def test_provider_failure_malformed_output_and_partial_retry_are_recorded(self):
        failing = FixtureProvider(fail_count=2)
        failed = OutcomeLearningAgentOrchestrator(
            provider=failing, max_retries=1
        ).run_outcome_reviewer(
            self.reviewer_request(provider=failing),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.assertEqual(failed.result.status, OutcomeLearningAgentResultStatus.FAILED)
        self.assertEqual(len(failed.validation_errors), 2)
        malformed = FixtureProvider(malformed=True)
        malformed_result = OutcomeLearningAgentOrchestrator(
            provider=malformed
        ).run_outcome_reviewer(
            self.reviewer_request(provider=malformed),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.assertEqual(
            malformed_result.status, OutcomeLearningOrchestrationStatus.FAILED
        )
        transient = FixtureProvider(fail_count=1)
        recovered = OutcomeLearningAgentOrchestrator(
            provider=transient, max_retries=1
        ).run_outcome_reviewer(
            self.reviewer_request(provider=transient),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.assertEqual(
            recovered.status,
            OutcomeLearningOrchestrationStatus.COMPLETED_WITH_WARNINGS,
        )
        self.assertEqual(
            len({canonical_outcome_learning_json(item["packet"]) for item in transient.calls}),
            1,
        )

    def test_no_human_review_is_created_automatically(self):
        provider = FixtureProvider()
        before = self.store.list_forecast_outcome_reviews()
        OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            self.reviewer_request(provider=provider),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.assertEqual(self.store.list_forecast_outcome_reviews(), before)


class MistakeProposalTests(Phase11D2Fixture):
    def test_requires_explicitly_approved_human_review(self):
        rejected = self.approved_review(decision=ReviewDecision.REJECTED)
        with self.assertRaises(ValueError):
            self.proposal_request(rejected)

    def test_one_case_produces_exact_advisory_draft_only(self):
        review = self.approved_review()
        provider = FixtureProvider()
        result = OutcomeLearningAgentOrchestrator(
            provider=provider
        ).run_mistake_memory_proposal(
            self.proposal_request(review, provider=provider),
            shadow_mode=True,
            executed_at_utc=_after(self.executed_at),
        )
        self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.COMPLETED)
        draft = result.mistake_lesson_proposal_draft
        assert draft is not None
        self.assertEqual(draft.recommended_scope.scope_type, LessonScopeType.EXACT_CASE)
        self.assertTrue(draft.human_action_required)
        self.assertTrue(draft.advisory_only)

    def test_one_case_cannot_be_scoped_or_general(self):
        for scope_type in (LessonScopeType.SCOPED.value, LessonScopeType.GENERAL.value):
            with self.subTest(scope_type=scope_type):
                provider = FixtureProvider(
                    proposal_options={"scope_type": scope_type}
                )
                result = OutcomeLearningAgentOrchestrator(
                    provider=provider
                ).run_mistake_memory_proposal(
                    self.proposal_request(provider=provider),
                    shadow_mode=True,
                    executed_at_utc=_after(self.executed_at),
                )
                self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.FAILED)

    def test_scoped_requires_two_independent_forecasts(self):
        clone = _clone_bundle(
            self.forecast,
            self.evaluation,
            suffix="two",
            reviewed_at_utc=self.reviewed_at,
        )
        primary = self.approved_review()
        support_ids = [primary.review_id, clone[2].review_id]
        provider = FixtureProvider(
            proposal_options={
                "scope_type": LessonScopeType.SCOPED.value,
                "supporting_review_ids": support_ids,
            }
        )
        result = OutcomeLearningAgentOrchestrator(
            provider=provider
        ).run_mistake_memory_proposal(
            self.proposal_request(
                primary, provider=provider, additional=(clone,)
            ),
            shadow_mode=True,
            executed_at_utc=_after(self.executed_at),
        )
        self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.COMPLETED)

    def test_supporting_counterexample_and_duplicate_ids_are_verified(self):
        clone = _clone_bundle(
            self.forecast,
            self.evaluation,
            suffix="counter",
            reviewed_at_utc=self.reviewed_at,
        )
        provider = FixtureProvider(
            proposal_options={"counterexample_review_ids": ["invented-review"]}
        )
        result = OutcomeLearningAgentOrchestrator(
            provider=provider
        ).run_mistake_memory_proposal(
            self.proposal_request(provider=provider, counterexamples=(clone,)),
            shadow_mode=True,
            executed_at_utc=_after(self.executed_at),
        )
        self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.FAILED)

    def test_duplicate_is_detected_without_merge_or_activation(self):
        review = self.approved_review()
        self.store.create_forecast_outcome_review(review)
        scope = LessonScope(
            scope_type=LessonScopeType.EXACT_CASE,
            exact_forecast_ids=(self.forecast.forecast_id,),
            symbols=(self.forecast.symbol,),
            timeframes=(self.forecast.dataset_cutoffs[0].timeframe,),
            degrees=(self.forecast.main_hypothesis.degree,),
            wave_roles=(self.forecast.main_hypothesis.wave_label,),
            pattern_families=(self.forecast.main_hypothesis.pattern_family,),
            directions=(self.forecast.main_hypothesis.direction.value,),
            applicability_conditions=("The same endpoint interpretation is under audit.",),
            non_applicability_conditions=("A hard invalidation already removed the count.",),
        )
        lesson, sources, proposed = propose_mistake_memory_lesson(
            (review,),
            {self.evaluation.evaluation_id: self.evaluation},
            forecast_symbols={self.forecast.forecast_id: self.forecast.symbol},
            scope=scope,
            title="Audit premature completion",
            warning_text="Recheck whether the final wave actually completed.",
            recommended_audit_check="Preserve a continuation alternate until price confirms completion.",
            proposed_by="human:reviewer-1",
            proposed_at_utc=self.executed_at,
        )
        self.store.create_mistake_memory_lesson(lesson)
        for source in sources:
            self.store.create_mistake_memory_source(source)
        self.store.create_mistake_memory_event(proposed)
        activated = build_lesson_event(
            lesson,
            (proposed,),
            sources,
            to_status=LessonStatus.ACTIVE,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(self.executed_at),
            reason="Human activation for the fixture.",
        )
        self.store.create_mistake_memory_event(activated)
        request_time = _after(activated.recorded_at_utc)
        future_retirement = build_lesson_event(
            lesson,
            (proposed, activated),
            sources,
            to_status=LessonStatus.RETIRED,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(request_time),
            reason="A later event that must not leak into the earlier request.",
        )
        with self.assertRaisesRegex(ValueError, "after the request cutoff"):
            build_mistake_memory_proposal_request(
                self.forecast,
                self.evaluation,
                review,
                provider="fixture",
                model="fixture-v1",
                created_at_utc=request_time,
                existing_lessons=(lesson,),
                existing_lesson_sources=sources,
                existing_lesson_events=(proposed, activated, future_retirement),
                existing_lesson_reference_id=lesson.lesson_id,
                shadow_mode=True,
            )
        provider = FixtureProvider(
            proposal_options={
                "possible_duplicates": [lesson.lesson_id],
                "existing_lesson_reference_id": lesson.lesson_id,
            }
        )
        request = build_mistake_memory_proposal_request(
            self.forecast,
            self.evaluation,
            review,
            provider=provider.name,
            model=provider.model,
            created_at_utc=request_time,
            existing_lessons=(lesson,),
            existing_lesson_sources=sources,
            existing_lesson_events=(proposed, activated),
            existing_lesson_reference_id=lesson.lesson_id,
            shadow_mode=True,
        )
        before_lessons = self.store.list_mistake_memory_lessons()
        result = OutcomeLearningAgentOrchestrator(
            provider=provider
        ).run_mistake_memory_proposal(
            request,
            shadow_mode=True,
            executed_at_utc=_after(request_time),
        )
        draft = result.mistake_lesson_proposal_draft
        assert draft is not None
        self.assertEqual(draft.possible_duplicate_lesson_ids, (lesson.lesson_id,))
        self.assertTrue(draft.duplicate_candidates[0].deduplication_key_match)
        persisted = self.store.create_forecast_outcome_learning_orchestration(result)
        self.assertTrue(persisted["inserted"])
        self.assertEqual(self.store.list_mistake_memory_lessons(), before_lessons)

    def test_no_lesson_or_event_is_created_automatically(self):
        provider = FixtureProvider()
        before_lessons = self.store.list_mistake_memory_lessons()
        before_events = self.store.list_mistake_memory_events()
        OutcomeLearningAgentOrchestrator(provider=provider).run_mistake_memory_proposal(
            self.proposal_request(provider=provider),
            shadow_mode=True,
            executed_at_utc=_after(self.executed_at),
        )
        self.assertEqual(self.store.list_mistake_memory_lessons(), before_lessons)
        self.assertEqual(self.store.list_mistake_memory_events(), before_events)


class ContractReplayAndProviderTests(Phase11D2Fixture):
    def test_contracts_are_frozen_hash_stable_and_tamper_evident(self):
        provider = FixtureProvider()
        first = OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            self.reviewer_request(provider=provider),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        second_provider = FixtureProvider()
        second = OutcomeLearningAgentOrchestrator(
            provider=second_provider
        ).run_outcome_reviewer(
            self.reviewer_request(provider=second_provider),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.assertEqual(first.content_hash, second.content_hash)
        with self.assertRaises(FrozenInstanceError):
            first.status = OutcomeLearningOrchestrationStatus.FAILED
        payload = first.to_dict()
        payload["warnings"] = ["tampered"]
        with self.assertRaises(ValueError):
            OutcomeLearningOrchestration.from_dict(payload)

    def test_deterministic_replay_never_calls_provider(self):
        provider = FixtureProvider()
        original = OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            self.reviewer_request(provider=provider),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        replayed = replay_outcome_learning_orchestration(
            original.to_dict(), expected_content_hash=original.content_hash
        )
        self.assertEqual(replayed, original)
        self.assertEqual(validate_outcome_learning_orchestration(replayed), ())

    def test_packet_provider_fails_closed(self):
        provider = PacketProvider()
        request = self.reviewer_request(provider=provider)
        result = OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            request, shadow_mode=True, executed_at_utc=self.executed_at
        )
        self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.FAILED)

    def test_ollama_strict_json_compatibility_without_network(self):
        provider = OllamaProvider(model="fixture-model")
        request = self.reviewer_request(provider=provider)
        packet = {
            "deterministic_evaluation": self.evaluation.to_dict(),
        }
        # Build the exact fixture response from the runtime packet supplied by the orchestrator.
        def fake_post(url, payload, **kwargs):
            supplied = json.loads(payload["prompt"].split("FROZEN INPUT:\n", 1)[1])
            return {"response": json.dumps(_review_output(supplied))}

        with patch("elliott_ai.providers._post_json", side_effect=fake_post):
            result = OutcomeLearningAgentOrchestrator(
                provider=provider
            ).run_outcome_reviewer(
                request, shadow_mode=True, executed_at_utc=self.executed_at
            )
        self.assertEqual(result.status, OutcomeLearningOrchestrationStatus.COMPLETED)

    def test_output_schemas_forbid_unknown_fields(self):
        self.assertFalse(OUTCOME_REVIEWER_OUTPUT_SCHEMA["additionalProperties"])
        self.assertFalse(MISTAKE_MEMORY_PROPOSAL_OUTPUT_SCHEMA["additionalProperties"])


class PersistenceAndMigrationTests(Phase11D2Fixture):
    def _persisted_outcome_orchestration(self):
        provider = FixtureProvider()
        orchestration = OutcomeLearningAgentOrchestrator(
            provider=provider
        ).run_outcome_reviewer(
            self.reviewer_request(provider=provider),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.store.create_forecast_outcome_learning_orchestration(orchestration)
        return orchestration

    def test_create_get_list_round_trip_and_idempotency(self):
        orchestration = self._persisted_outcome_orchestration()
        stored = self.store.get_forecast_outcome_learning_orchestration(
            orchestration.orchestration_id
        )
        self.assertEqual(stored, orchestration)
        self.assertEqual(
            self.store.list_forecast_outcome_learning_orchestrations(
                orchestration_kind=OutcomeLearningAgentRole.OUTCOME_REVIEWER.value
            ),
            [orchestration],
        )
        self.assertFalse(
            self.store.create_forecast_outcome_learning_orchestration(orchestration)[
                "inserted"
            ]
        )

    def test_mistake_persistence_verifies_review_linkage(self):
        review = self.approved_review()
        self.store.create_forecast_outcome_review(review)
        provider = FixtureProvider()
        orchestration = OutcomeLearningAgentOrchestrator(
            provider=provider
        ).run_mistake_memory_proposal(
            self.proposal_request(review, provider=provider),
            shadow_mode=True,
            executed_at_utc=_after(self.executed_at),
        )
        inserted = self.store.create_forecast_outcome_learning_orchestration(
            orchestration
        )
        self.assertTrue(inserted["inserted"])

    def test_append_only_triggers_and_restrict_foreign_keys(self):
        orchestration = self._persisted_outcome_orchestration()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE forecast_outcome_learning_orchestrations "
                    "SET status = 'failed' WHERE orchestration_id = ?",
                    (orchestration.orchestration_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM forecast_outcome_learning_orchestrations "
                    "WHERE orchestration_id = ?",
                    (orchestration.orchestration_id,),
                )
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(forecast_outcome_learning_orchestrations)"
            ).fetchall()
            self.assertTrue(foreign_keys)
            self.assertTrue(all(row[6].upper() == "RESTRICT" for row in foreign_keys))
        finally:
            connection.close()

    def test_linear_supersession_and_branch_rejection(self):
        first = self._persisted_outcome_orchestration()
        provider = FixtureProvider()
        second = OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            self.reviewer_request(provider=provider),
            shadow_mode=True,
            executed_at_utc=_after(self.executed_at),
            orchestration_version=2,
            supersedes_orchestration_id=first.orchestration_id,
        )
        self.store.create_forecast_outcome_learning_orchestration(second)
        self.assertEqual(
            self.store.list_forecast_outcome_learning_orchestrations(
                include_superseded=False
            ),
            [second],
        )
        branch_provider = FixtureProvider()
        branch = OutcomeLearningAgentOrchestrator(
            provider=branch_provider
        ).run_outcome_reviewer(
            self.reviewer_request(provider=branch_provider),
            shadow_mode=True,
            executed_at_utc=_after(self.executed_at, days=2),
            orchestration_version=2,
            supersedes_orchestration_id=first.orchestration_id,
        )
        with self.assertRaises(ValueError):
            self.store.create_forecast_outcome_learning_orchestration(branch)

    def test_schema_is_one_new_table_and_d1_layout_is_unchanged(self):
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("forecast_outcome_learning_orchestrations", tables)
            d1_columns = tuple(
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(forecast_agent_orchestrations)"
                )
            )
            self.assertEqual(len(d1_columns), 26)
            self.assertNotIn("orchestration_kind", d1_columns)
        finally:
            connection.close()

    def test_transactional_migration_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rollback.sqlite3"
            broken = FORECAST_OUTCOME_LEARNING_ORCHESTRATION_MIGRATION_SQL + (
                "THIS IS NOT VALID SQL",
            )
            with patch(
                "elliott_ai.knowledge.FORECAST_OUTCOME_LEARNING_ORCHESTRATION_MIGRATION_SQL",
                broken,
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    KnowledgeStore(database)
            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='forecast_outcome_learning_orchestrations'"
                ).fetchone()
                self.assertIsNone(row)
            finally:
                connection.close()

    def test_preexisting_database_gets_backup_and_migration_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "migration.sqlite3"
            initial = KnowledgeStore(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE forecast_outcome_learning_orchestrations")
                connection.commit()
            finally:
                connection.close()
            migrated = KnowledgeStore(database)
            self.assertTrue(migrated.outcome_learning_migration_applied)
            self.assertIsNotNone(migrated.outcome_learning_backup_path)
            self.assertTrue(migrated.outcome_learning_backup_path.exists())
            self.assertTrue(
                migrated.outcome_learning_pre_migration_audit_path.exists()
            )
            self.assertTrue(migrated.outcome_learning_migration_audit_path.exists())
            audit = json.loads(
                migrated.outcome_learning_migration_audit_path.read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(audit["pre_migration"]["integrity_check"], "ok")
            self.assertEqual(audit["post_migration"]["integrity_check"], "ok")
            self.assertTrue(audit["d1_table_unchanged"])
            self.assertEqual(
                audit["protected_phase11d1_schema_hashes"],
                audit["post_phase11d1_schema_hashes"],
            )

    def test_sqlite_integrity_and_foreign_keys(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()


class CompatibilityBoundaryTests(Phase11D2Fixture):
    def test_public_analysis_and_cli_are_not_integrated(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "elliott_ai/agent.py",
            "elliott_ai/cli.py",
            "elliott_ai/telegram_bot.py",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("OutcomeLearningAgentOrchestrator", text)

    def test_existing_public_method_signatures_are_unchanged(self):
        from elliott_ai.agent import ElliottAgent

        self.assertNotIn("outcome", inspect.signature(ElliottAgent.analyze).parameters)
        self.assertNotIn(
            "outcome", inspect.signature(ElliottAgent.resolve_degrees).parameters
        )

    def test_frozen_forecast_confidence_and_hard_rules_are_untouched(self):
        provider = FixtureProvider()
        confidence = canonical_outcome_learning_json(
            self.forecast.original_uncalibrated_confidence
        )
        forecast_hash = self.forecast.content_hash
        OutcomeLearningAgentOrchestrator(provider=provider).run_outcome_reviewer(
            self.reviewer_request(provider=provider),
            shadow_mode=True,
            executed_at_utc=self.executed_at,
        )
        self.assertEqual(self.forecast.content_hash, forecast_hash)
        self.assertEqual(
            canonical_outcome_learning_json(
                self.forecast.original_uncalibrated_confidence
            ),
            confidence,
        )


if __name__ == "__main__":
    unittest.main()
