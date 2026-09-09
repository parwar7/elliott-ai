from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import elliott_ai
from elliott_ai.agent import ElliottAgent
from elliott_ai.forecast_outcomes import (
    ForecastOutcomeEvaluation,
    OutcomeStatus,
    evaluate_forecast_observation_set,
)
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.mistake_memory import (
    MISTAKE_MEMORY_MIGRATION_SQL,
    MISTAKE_MEMORY_TABLES,
    FailureDiagnosis,
    FailureDiagnosisType,
    ForecastOutcomeReview,
    LessonRetrievalQuery,
    LessonScope,
    LessonScopeType,
    LessonSourceRole,
    LessonStatus,
    ReviewDecision,
    approve_lesson,
    create_forecast_outcome_review,
    create_lesson_source,
    forecast_outcome_review_content_hash,
    lesson_current_status,
    propose_mistake_memory_lesson,
    reject_lesson,
    replay_lesson_retrieval,
    retrieve_applicable_lessons,
    retire_lesson,
    revise_forecast_outcome_review,
    supersede_lesson,
    validate_forecast_outcome_review,
)

try:
    from scripts.test_forecast_outcomes import (
        _daily_rows,
        _observation,
        _with_structural_invalidation,
    )
    from scripts.test_forecast_records import _create_sources, _typed_record
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from test_forecast_outcomes import (
        _daily_rows,
        _observation,
        _with_structural_invalidation,
    )
    from test_forecast_records import _create_sources, _typed_record


def _after(timestamp: str, *, days: int = 1) -> str:
    return (
        datetime.fromisoformat(timestamp) + timedelta(days=days)
    ).astimezone(timezone.utc).isoformat(timespec="seconds")


def _clone_evaluation(
    evaluation: ForecastOutcomeEvaluation,
    *,
    suffix: str,
) -> ForecastOutcomeEvaluation:
    payload = evaluation.to_dict()
    payload.pop("content_hash")
    payload["evaluation_id"] = f"evaluation-{suffix}"
    payload["evaluation_version"] = 1
    payload["supersedes_evaluation_id"] = None
    payload["forecast_id"] = f"forecast-{suffix}"
    payload["forecast_content_hash"] = canonical_sha256(
        {"forecast": suffix}
    )
    payload["source_hashes"]["forecast_record"] = payload[
        "forecast_content_hash"
    ]
    return ForecastOutcomeEvaluation.create(**payload)


class Phase11CFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "brain.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.run_id, self.resolution_id = _create_sources(self.store)
        self.record = _typed_record(self.store, self.run_id, self.resolution_id)
        self.store.create_forecast_record(self.record)
        self.observation = _observation(self.record)
        self.store.create_forecast_observation_set(self.observation)
        self.evaluation = evaluate_forecast_observation_set(
            self.record, self.observation
        )
        self.store.create_forecast_outcome_evaluation(self.evaluation)
        self.assertEqual(self.evaluation.outcome_status, OutcomeStatus.FAILED)
        self.reviewed_at = _after(self.evaluation.evaluated_through_utc)
        self.proposed_at = _after(self.reviewed_at)
        self.activated_at = _after(self.proposed_at)

    def diagnosis(self, suffix: str = "base") -> FailureDiagnosis:
        return FailureDiagnosis(
            diagnosis_id=f"diagnosis-{suffix}",
            diagnosis_type=FailureDiagnosisType.PREMATURE_COMPLETION,
            summary="Completion was called before the price structure confirmed it.",
            affected_hypothesis_ids=(
                self.evaluation.main_hypothesis_outcome.hypothesis_id,
            ),
        )

    def review(
        self,
        *,
        evaluation: ForecastOutcomeEvaluation | None = None,
        decision: ReviewDecision = ReviewDecision.APPROVED,
        reviewer: str = "human:reviewer-1",
        reviewed_at: str | None = None,
        diagnosis: FailureDiagnosis | None = None,
        corrected_interpretation: str | None = None,
        parent: ForecastOutcomeReview | None = None,
    ) -> ForecastOutcomeReview:
        selected = evaluation or self.evaluation
        return create_forecast_outcome_review(
            selected,
            reviewer_reference=reviewer,
            reviewed_at_utc=reviewed_at or self.reviewed_at,
            decision=decision,
            notes="Human review of the frozen deterministic result.",
            diagnoses=(diagnosis or self.diagnosis(selected.evaluation_id),),
            corrected_interpretation=corrected_interpretation,
            evidence_reference_ids=(
                selected.main_hypothesis_outcome.hypothesis_id,
            ),
            parent_review=parent,
        )

    def exact_scope(self, forecast_id: str | None = None, symbol: str | None = None) -> LessonScope:
        return LessonScope(
            scope_type=LessonScopeType.EXACT_CASE,
            exact_forecast_ids=(forecast_id or self.record.forecast_id,),
            symbols=(symbol or self.record.symbol,),
            timeframes=(self.record.dataset_cutoffs[0].timeframe,),
            degrees=(self.record.main_hypothesis.degree,),
            wave_roles=(self.record.main_hypothesis.wave_label,),
            pattern_families=(self.record.main_hypothesis.pattern_family,),
            directions=(self.record.main_hypothesis.direction.value,),
            applicability_conditions=("The same endpoint interpretation is under audit.",),
            non_applicability_conditions=("A hard price invalidation already removed the count.",),
        )

    def proposal(
        self,
        reviews: tuple[ForecastOutcomeReview, ...],
        evaluations: dict[str, ForecastOutcomeEvaluation],
        symbols: dict[str, str],
        *,
        scope: LessonScope | None = None,
        counterexamples: tuple[ForecastOutcomeReview, ...] = (),
        existing_lessons: tuple = (),
        supersedes_lesson_id: str | None = None,
        lesson_version: int = 1,
        proposed_at: str | None = None,
    ):
        return propose_mistake_memory_lesson(
            reviews,
            evaluations,
            forecast_symbols=symbols,
            scope=scope or self.exact_scope(),
            title="Audit premature completion",
            warning_text="Recheck whether the final wave actually completed.",
            recommended_audit_check="Preserve a continuation alternate until price confirms completion.",
            proposed_by="human:reviewer-1",
            proposed_at_utc=proposed_at or self.proposed_at,
            counterexample_reviews=counterexamples,
            existing_lessons=existing_lessons,
            supersedes_lesson_id=supersedes_lesson_id,
            lesson_version=lesson_version,
        )


class ReviewContractTests(Phase11CFixture):
    def test_immutable_review_round_trip_and_hash_tampering(self) -> None:
        review = self.review()
        with self.assertRaises(FrozenInstanceError):
            review.notes = "changed"  # type: ignore[misc]
        self.assertEqual(ForecastOutcomeReview.from_dict(review.to_dict()), review)
        payload = review.to_dict()
        payload["notes"] = "tampered"
        with self.assertRaisesRegex(ValueError, "content_hash"):
            ForecastOutcomeReview.from_dict(payload)

    def test_all_review_decisions_and_revision_chain(self) -> None:
        for decision in (
            ReviewDecision.APPROVED,
            ReviewDecision.REJECTED,
            ReviewDecision.NEEDS_MORE_DATA,
        ):
            self.assertEqual(self.review(decision=decision).decision, decision)
        first = self.review()
        revised = revise_forecast_outcome_review(
            first,
            self.evaluation,
            reviewer_reference="human:reviewer-2",
            reviewed_at_utc=_after(first.reviewed_at_utc),
            decision=ReviewDecision.REVISED,
            notes="The diagnosis was narrowed without changing the score.",
            diagnoses=(self.diagnosis("revision"),),
            corrected_interpretation="The endpoint was premature only at the stated degree.",
            evidence_reference_ids=(
                self.evaluation.main_hypothesis_outcome.hypothesis_id,
            ),
        )
        self.assertEqual(revised.parent_review_id, first.review_id)
        self.assertEqual(revised.review_version, 2)
        self.assertEqual(revised.evaluation_content_hash, first.evaluation_content_hash)

    def test_revised_requires_corrected_interpretation(self) -> None:
        with self.assertRaisesRegex(ValueError, "corrected_interpretation"):
            self.review(decision=ReviewDecision.REVISED)

    def test_deterministic_results_are_not_review_fields(self) -> None:
        review = self.review()
        review_fields = set(review.to_dict())
        self.assertFalse(
            review_fields
            & {
                "claim_outcomes",
                "main_hypothesis_outcome",
                "alternative_hypothesis_outcomes",
                "outcome_status",
            }
        )
        self.assertEqual(
            self.evaluation.content_hash,
            self.store.get_forecast_outcome_evaluation(
                self.evaluation.evaluation_id
            ).content_hash,
        )

    def test_scoring_error_requires_new_superseding_evaluation(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a superseding evaluation"):
            create_forecast_outcome_review(
                self.evaluation,
                reviewer_reference="human:reviewer-1",
                reviewed_at_utc=self.reviewed_at,
                decision=ReviewDecision.REJECTED,
                notes="The deterministic implementation needs correction.",
                deterministic_scoring_error=True,
            )
        successor = evaluate_forecast_observation_set(
            self.record,
            self.observation,
            evaluation_version=2,
            supersedes_evaluation_id=self.evaluation.evaluation_id,
        )
        review = create_forecast_outcome_review(
            self.evaluation,
            reviewer_reference="human:reviewer-1",
            reviewed_at_utc=self.reviewed_at,
            decision=ReviewDecision.REJECTED,
            notes="A superseding deterministic evaluation was created.",
            deterministic_scoring_error=True,
            superseding_evaluation=successor,
        )
        self.assertEqual(
            review.required_superseding_evaluation_id, successor.evaluation_id
        )
        self.assertEqual(self.evaluation.outcome_status, OutcomeStatus.FAILED)

    def test_unknown_evidence_reference_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            create_forecast_outcome_review(
                self.evaluation,
                reviewer_reference="human:reviewer-1",
                reviewed_at_utc=self.reviewed_at,
                decision=ReviewDecision.APPROVED,
                notes="Invalid reference test.",
                evidence_reference_ids=("invented-candle",),
            )


class LessonPolicyTests(Phase11CFixture):
    def test_unresolved_or_incomparable_cannot_support_lesson(self) -> None:
        incomparable_observation = _observation(
            self.record,
            rows=_daily_rows(
                self.record, target_index=4, invalidation_index=4
            ),
        )
        incomparable = evaluate_forecast_observation_set(
            self.record, incomparable_observation
        )
        self.assertEqual(incomparable.outcome_status, OutcomeStatus.INCOMPARABLE)
        review = self.review(evaluation=incomparable)
        with self.assertRaisesRegex(ValueError, "cannot support"):
            self.proposal(
                (review,),
                {incomparable.evaluation_id: incomparable},
                {incomparable.forecast_id: self.record.symbol},
            )
        unresolved_record = _with_structural_invalidation(self.record)
        unresolved_rows = _daily_rows(unresolved_record)[:5]
        unresolved_observation = _observation(
            unresolved_record,
            rows=unresolved_rows,
            actual_cutoff=unresolved_rows[-1]["close_time_utc"],
        )
        unresolved = evaluate_forecast_observation_set(
            unresolved_record, unresolved_observation
        )
        self.assertEqual(unresolved.outcome_status, OutcomeStatus.UNRESOLVED)
        unresolved_review = self.review(evaluation=unresolved)
        with self.assertRaisesRegex(ValueError, "cannot support"):
            self.proposal(
                (unresolved_review,),
                {unresolved.evaluation_id: unresolved},
                {unresolved.forecast_id: self.record.symbol},
            )

    def test_one_failure_is_proposed_but_not_generalized(self) -> None:
        review = self.review()
        general = LessonScope(
            scope_type=LessonScopeType.GENERAL,
            asset_classes=("equity",),
            wave_roles=("5",),
        )
        lesson, sources, proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
            scope=general,
        )
        self.assertEqual(proposed.to_status, LessonStatus.PROPOSED)
        with self.assertRaisesRegex(ValueError, "three independent forecasts"):
            approve_lesson(
                lesson,
                (proposed,),
                sources,
                actor_reference="human:reviewer-1",
                recorded_at_utc=self.activated_at,
                reason="Attempted over-generalization.",
            )

    def test_exact_case_warning_can_activate_with_one_human_approved_failure(self) -> None:
        review = self.review()
        lesson, sources, proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
        )
        active = approve_lesson(
            lesson,
            (proposed,),
            sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=self.activated_at,
            reason="Approved only as a warning for this exact forecast context.",
        )
        self.assertEqual(active.to_status, LessonStatus.ACTIVE)
        self.assertEqual(
            lesson_current_status(lesson, (proposed, active)), LessonStatus.ACTIVE
        )

    def test_general_threshold_and_explicit_human_exception(self) -> None:
        evaluations = {
            item.evaluation_id: item
            for item in (
                self.evaluation,
                _clone_evaluation(self.evaluation, suffix="two"),
                _clone_evaluation(self.evaluation, suffix="three"),
            )
        }
        reviews = tuple(
            self.review(
                evaluation=item,
                reviewer=f"human:{index}",
                diagnosis=FailureDiagnosis(
                    diagnosis_id=f"diagnosis-{index}",
                    diagnosis_type=FailureDiagnosisType.WRONG_DEGREE,
                    summary="The reviewed degree was too high.",
                    affected_hypothesis_ids=(
                        item.main_hypothesis_outcome.hypothesis_id,
                    ),
                ),
            )
            for index, item in enumerate(evaluations.values(), start=1)
        )
        symbols = {
            reviews[0].forecast_id: "NASDAQ:MSFT",
            reviews[1].forecast_id: "NASDAQ:MSFT",
            reviews[2].forecast_id: "NASDAQ:TSLA",
        }
        scope = LessonScope(
            scope_type=LessonScopeType.GENERAL,
            asset_classes=("equity",),
            degrees=("Primary",),
        )
        lesson, sources, proposed = self.proposal(
            reviews, evaluations, symbols, scope=scope
        )
        active = approve_lesson(
            lesson,
            (proposed,),
            sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=self.activated_at,
            reason="Three independent forecasts across two symbols support the audit.",
        )
        self.assertEqual(active.to_status, LessonStatus.ACTIVE)

        one_lesson, one_sources, one_proposed = self.proposal(
            (reviews[0],), evaluations, symbols, scope=scope,
            proposed_at=_after(self.proposed_at),
        )
        with self.assertRaisesRegex(ValueError, "exception reason"):
            approve_lesson(
                one_lesson,
                (one_proposed,),
                one_sources,
                actor_reference="human:reviewer-1",
                recorded_at_utc=_after(self.activated_at, days=2),
                reason="No documented exception.",
            )
        exception = approve_lesson(
            one_lesson,
            (one_proposed,),
            one_sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(self.activated_at, days=2),
            reason="Human explicitly approved a bounded methodological warning.",
            human_exception_reason=(
                "The reviewed data-quality defect is deterministic and the scope "
                "is retained for explicit audit, not as a statistical rule."
            ),
        )
        self.assertTrue(exception.human_exception_reason)

    def test_supporting_and_counterexample_sources_are_preserved(self) -> None:
        failed_review = self.review()
        success_observation = _observation(
            self.record, rows=_daily_rows(self.record, target_index=4)
        )
        success = evaluate_forecast_observation_set(
            self.record, success_observation
        )
        self.assertEqual(success.outcome_status, OutcomeStatus.SUCCEEDED)
        success_review = create_forecast_outcome_review(
            success,
            reviewer_reference="human:reviewer-2",
            reviewed_at_utc=self.reviewed_at,
            decision=ReviewDecision.APPROVED,
            notes="Reviewed counterexample with a successful outcome.",
        )
        lesson, sources, _ = self.proposal(
            (failed_review,),
            {
                self.evaluation.evaluation_id: self.evaluation,
                success.evaluation_id: success,
            },
            {self.record.forecast_id: self.record.symbol},
            counterexamples=(success_review,),
        )
        self.assertEqual(
            {item.source_role for item in sources},
            {LessonSourceRole.SUPPORTING, LessonSourceRole.COUNTEREXAMPLE},
        )
        self.assertEqual(len(lesson.counterexample_source_ids), 1)

    def test_duplicate_proposals_are_flagged_never_merged(self) -> None:
        review = self.review()
        first, _, _ = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
        )
        second, _, _ = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
            existing_lessons=(first,),
            proposed_at=_after(self.proposed_at),
        )
        self.assertNotEqual(first.lesson_id, second.lesson_id)
        self.assertIn(first.lesson_id, second.possible_duplicate_lesson_ids)
        self.assertEqual(first.deduplication_key, second.deduplication_key)

    def test_reject_retire_and_supersede_lifecycle(self) -> None:
        review = self.review()
        lesson, sources, proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
        )
        rejected = reject_lesson(
            lesson,
            (proposed,),
            sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=self.activated_at,
            reason="Human rejected the proposed lesson.",
        )
        self.assertEqual(rejected.to_status, LessonStatus.REJECTED)
        with self.assertRaisesRegex(ValueError, "Illegal lesson transition"):
            retire_lesson(
                lesson,
                (proposed, rejected),
                sources,
                actor_reference="human:reviewer-1",
                recorded_at_utc=_after(self.activated_at),
                reason="Terminal state cannot reopen.",
            )

        active_lesson, active_sources, active_proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
            proposed_at=_after(self.proposed_at, days=2),
        )
        active = approve_lesson(
            active_lesson,
            (active_proposed,),
            active_sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(self.activated_at, days=2),
            reason="Exact warning approved.",
        )
        replacement, _, _ = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
            supersedes_lesson_id=active_lesson.lesson_id,
            lesson_version=2,
            proposed_at=_after(self.proposed_at, days=3),
        )
        superseded = supersede_lesson(
            active_lesson,
            (active_proposed, active),
            active_sources,
            replacement_lesson=replacement,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(self.activated_at, days=3),
            reason="A narrower replacement preserves the original audit history.",
        )
        self.assertEqual(superseded.related_lesson_id, replacement.lesson_id)


class RetrievalTests(Phase11CFixture):
    def active_exact_lesson(self):
        review = self.review()
        lesson, sources, proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
        )
        active = approve_lesson(
            lesson,
            (proposed,),
            sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=self.activated_at,
            reason="Exact-case audit warning approved.",
        )
        return lesson, sources, (proposed, active)

    def query(self, cutoff: str, *, blind: bool = False) -> LessonRetrievalQuery:
        return LessonRetrievalQuery.create(
            analysis_cutoff_utc=cutoff,
            blind_mode=blind,
            current_forecast_id=self.record.forecast_id,
            symbol=self.record.symbol,
            asset_class="equity",
            timeframe=self.record.dataset_cutoffs[0].timeframe,
            degree=self.record.main_hypothesis.degree,
            wave_role=self.record.main_hypothesis.wave_label,
            pattern_family=self.record.main_hypothesis.pattern_family,
            direction=self.record.main_hypothesis.direction.value,
        )

    def test_point_in_time_and_future_lesson_leakage(self) -> None:
        lesson, sources, events = self.active_exact_lesson()
        before = self.query(_after(self.proposed_at, days=0))
        after = self.query(_after(self.activated_at))
        self.assertEqual(
            retrieve_applicable_lessons(
                before, lessons=(lesson,), sources=sources, events=events
            ),
            (),
        )
        retrieved = retrieve_applicable_lessons(
            after, lessons=(lesson,), sources=sources, events=events
        )
        self.assertEqual(len(retrieved), 1)
        self.assertTrue(retrieved[0].audit_only)
        self.assertEqual(retrieved[0].source_ids, tuple(item.source_id for item in sources))

    def test_blind_mode_isolation(self) -> None:
        lesson, sources, events = self.active_exact_lesson()
        result = retrieve_applicable_lessons(
            self.query(_after(self.activated_at), blind=True),
            lessons=(lesson,),
            sources=sources,
            events=events,
        )
        self.assertEqual(result, ())

    def test_counterexample_added_after_activation_remains_visible(self) -> None:
        lesson, sources, events = self.active_exact_lesson()
        success_observation = _observation(
            self.record, rows=_daily_rows(self.record, target_index=4)
        )
        success = evaluate_forecast_observation_set(
            self.record, success_observation
        )
        success_review = create_forecast_outcome_review(
            success,
            reviewer_reference="human:reviewer-2",
            reviewed_at_utc=self.reviewed_at,
            decision=ReviewDecision.APPROVED,
            notes="This reviewed case contradicts the proposed warning.",
        )
        counterexample = create_lesson_source(
            lesson,
            success_review,
            success,
            symbol=self.record.symbol,
            source_role=LessonSourceRole.COUNTEREXAMPLE,
            added_by="human:reviewer-2",
            added_at_utc=_after(self.activated_at),
        )
        result = retrieve_applicable_lessons(
            self.query(_after(counterexample.added_at_utc)),
            lessons=(lesson,),
            sources=(*sources, counterexample),
            events=events,
        )
        self.assertEqual(
            set(result[0].source_ids),
            {sources[0].source_id, counterexample.source_id},
        )
        self.assertIn(
            "counterexample",
            {item["source_role"] for item in result[0].source_provenance},
        )

    def test_deterministic_ranking_stable_tie_and_replay(self) -> None:
        exact, exact_sources, exact_events = self.active_exact_lesson()
        review = self.review()
        general_scope = LessonScope(
            scope_type=LessonScopeType.GENERAL,
            asset_classes=("equity",),
        )
        general, general_sources, general_proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
            scope=general_scope,
            proposed_at=_after(self.proposed_at, days=2),
        )
        general_active = approve_lesson(
            general,
            (general_proposed,),
            general_sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(self.activated_at, days=2),
            reason="Explicit bounded exception for retrieval-order testing.",
            human_exception_reason="Test fixture exercises the approved human-exception path.",
        )
        query = self.query(_after(self.activated_at, days=3))
        lessons = (general, exact)
        sources = general_sources + exact_sources
        events = (general_proposed, general_active, *exact_events)
        first = retrieve_applicable_lessons(
            query, lessons=lessons, sources=sources, events=events
        )
        second = replay_lesson_retrieval(
            query.to_dict(),
            lessons=tuple(item.to_dict() for item in lessons),
            sources=tuple(item.to_dict() for item in sources),
            events=tuple(item.to_dict() for item in events),
            expected_results=tuple(item.to_dict() for item in first),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [item.retrieval_scope for item in first],
            [LessonScopeType.EXACT_CASE, LessonScopeType.GENERAL],
        )
        self.assertEqual(
            [item.content_hash for item in first],
            [item.content_hash for item in second],
        )

    def test_equal_scope_uses_stable_lesson_id_tie_break(self) -> None:
        review = self.review()
        first, first_sources, first_proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
            proposed_at=self.proposed_at,
        )
        second, second_sources, second_proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
            proposed_at=_after(self.proposed_at),
        )
        first_active = approve_lesson(
            first,
            (first_proposed,),
            first_sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(self.activated_at, days=2),
            reason="First exact warning approved.",
        )
        second_active = approve_lesson(
            second,
            (second_proposed,),
            second_sources,
            actor_reference="human:reviewer-1",
            recorded_at_utc=_after(self.activated_at, days=2),
            reason="Second exact warning approved independently.",
        )
        query = self.query(_after(self.activated_at, days=3))
        result = retrieve_applicable_lessons(
            query,
            lessons=(second, first),
            sources=second_sources + first_sources,
            events=(second_proposed, second_active, first_proposed, first_active),
        )
        self.assertEqual(
            [item.lesson_id for item in result],
            sorted((first.lesson_id, second.lesson_id)),
        )

    def test_scope_mismatch_returns_nothing(self) -> None:
        lesson, sources, events = self.active_exact_lesson()
        query = LessonRetrievalQuery.create(
            analysis_cutoff_utc=_after(self.activated_at),
            blind_mode=False,
            current_forecast_id="different-forecast",
            symbol="NASDAQ:TSLA",
            timeframe="1D",
            degree="Primary",
            wave_role="5",
            pattern_family="impulse",
            direction="up",
        )
        self.assertEqual(
            retrieve_applicable_lessons(
                query, lessons=(lesson,), sources=sources, events=events
            ),
            (),
        )


class PersistenceAndCompatibilityTests(Phase11CFixture):
    def persisted_lesson_bundle(self):
        review = self.review()
        lesson, sources, proposed = self.proposal(
            (review,),
            {review.evaluation_id: self.evaluation},
            {review.forecast_id: self.record.symbol},
        )
        self.store.create_forecast_outcome_review(review)
        self.store.create_mistake_memory_lesson(lesson)
        for source in sources:
            self.store.create_mistake_memory_source(source)
        self.store.create_mistake_memory_event(proposed)
        return review, lesson, sources, proposed

    def test_create_get_list_round_trips_and_idempotency(self) -> None:
        review, lesson, sources, proposed = self.persisted_lesson_bundle()
        self.assertFalse(self.store.create_forecast_outcome_review(review)["inserted"])
        self.assertFalse(self.store.create_mistake_memory_lesson(lesson)["inserted"])
        self.assertFalse(self.store.create_mistake_memory_source(sources[0])["inserted"])
        self.assertFalse(self.store.create_mistake_memory_event(proposed)["inserted"])
        self.assertEqual(self.store.get_forecast_outcome_review(review.review_id), review)
        self.assertEqual(self.store.get_mistake_memory_lesson(lesson.lesson_id), lesson)
        self.assertEqual(self.store.get_mistake_memory_source(sources[0].source_id), sources[0])
        self.assertEqual(self.store.get_mistake_memory_event(proposed.event_id), proposed)
        self.assertEqual(self.store.list_forecast_outcome_reviews(), [review])
        self.assertEqual(self.store.list_mistake_memory_lessons(), [lesson])
        self.assertEqual(self.store.list_mistake_memory_sources(), list(sources))
        self.assertEqual(self.store.list_mistake_memory_events(lesson_id=lesson.lesson_id), [proposed])

    def test_persisted_human_review_chain_and_active_event(self) -> None:
        review, lesson, sources, proposed = self.persisted_lesson_bundle()
        revised = revise_forecast_outcome_review(
            review,
            self.evaluation,
            reviewer_reference="human:reviewer-2",
            reviewed_at_utc=_after(review.reviewed_at_utc),
            decision=ReviewDecision.REVISED,
            notes="Narrowed diagnosis.",
            diagnoses=(self.diagnosis("stored-revision"),),
            corrected_interpretation="Only the Primary-degree completion was premature.",
            evidence_reference_ids=(
                self.evaluation.main_hypothesis_outcome.hypothesis_id,
            ),
        )
        self.store.create_forecast_outcome_review(revised)
        active = approve_lesson(
            lesson,
            (proposed,),
            sources,
            actor_reference="human:reviewer-2",
            recorded_at_utc=self.activated_at,
            reason="Named human approved the exact-case audit warning.",
        )
        self.store.create_mistake_memory_event(active)
        self.assertEqual(
            self.store.list_forecast_outcome_reviews(include_superseded=False),
            [revised],
        )
        self.assertEqual(
            self.store.list_mistake_memory_events(lesson_id=lesson.lesson_id),
            [proposed, active],
        )

    def test_append_only_restrict_and_integrity(self) -> None:
        review, lesson, sources, proposed = self.persisted_lesson_bundle()
        for table, key, value in (
            ("forecast_outcome_reviews", "review_id", review.review_id),
            ("mistake_memory_lessons", "lesson_id", lesson.lesson_id),
            ("mistake_memory_sources", "source_id", sources[0].source_id),
            ("mistake_memory_events", "event_id", proposed.event_id),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.connect() as connection:
                    connection.execute(
                        f"UPDATE {table} SET content_hash=? WHERE {key}=?",
                        ("0" * 64, value),
                    )
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.connect() as connection:
                    connection.execute(f"DELETE FROM {table} WHERE {key}=?", (value,))
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_database_tampering_is_detected(self) -> None:
        review, _, _, _ = self.persisted_lesson_bundle()
        with self.store.connect() as connection:
            connection.execute("DROP TRIGGER forecast_outcome_reviews_no_update")
            payload = review.to_dict()
            payload["notes"] = "tampered"
            connection.execute(
                "UPDATE forecast_outcome_reviews SET record_json=? WHERE review_id=?",
                (json.dumps(payload), review.review_id),
            )
        with self.assertRaisesRegex(RuntimeError, "immutable validation"):
            self.store.get_forecast_outcome_review(review.review_id)

    def test_only_four_tables_and_no_mutation_methods(self) -> None:
        with self.store.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if row[0] in MISTAKE_MEMORY_TABLES
            }
        self.assertEqual(tables, set(MISTAKE_MEMORY_TABLES))
        for stem in (
            "forecast_outcome_review",
            "mistake_memory_lesson",
            "mistake_memory_source",
            "mistake_memory_event",
        ):
            self.assertFalse(hasattr(self.store, f"update_{stem}"))
            self.assertFalse(hasattr(self.store, f"delete_{stem}"))

    def test_migration_rollback_leaves_no_phase11c_tables(self) -> None:
        database = Path(self.temporary.name) / "rollback.sqlite3"
        initial = KnowledgeStore(database)
        with initial.connect() as connection:
            for table in (
                "mistake_memory_events",
                "mistake_memory_sources",
                "mistake_memory_lessons",
                "forecast_outcome_reviews",
            ):
                connection.execute(f"DROP TABLE {table}")
        broken_sql = (MISTAKE_MEMORY_MIGRATION_SQL[0], "CREATE TABLE broken(")
        with patch("elliott_ai.knowledge.MISTAKE_MEMORY_MIGRATION_SQL", broken_sql):
            with self.assertRaises(sqlite3.OperationalError):
                KnowledgeStore(database)
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(set(MISTAKE_MEMORY_TABLES).isdisjoint(tables))

    def test_preexisting_database_gets_backup_and_audit(self) -> None:
        database = Path(self.temporary.name) / "preexisting.sqlite3"
        initial = KnowledgeStore(database)
        with initial.connect() as connection:
            for table in (
                "mistake_memory_events",
                "mistake_memory_sources",
                "mistake_memory_lessons",
                "forecast_outcome_reviews",
            ):
                connection.execute(f"DROP TABLE {table}")
        migrated = KnowledgeStore(database)
        self.assertTrue(migrated.mistake_memory_migration_applied)
        self.assertTrue(migrated.mistake_memory_backup_path.exists())
        self.assertTrue(migrated.mistake_memory_migration_audit_path.exists())

    def test_legacy_and_public_interfaces_remain_unchanged(self) -> None:
        self.assertEqual(
            elliott_ai.__all__, ["AnalysisRequest", "ElliottAgent", "KnowledgeStore"]
        )
        self.assertEqual(
            list(inspect.signature(ElliottAgent.analyze).parameters),
            ["self", "request"],
        )
        self.assertEqual(
            list(inspect.signature(ElliottAgent.resolve_degrees).parameters),
            ["self", "source_run_id", "question"],
        )
        self.assertEqual(
            self.store.get_forecast_record(self.record.forecast_id), self.record
        )


if __name__ == "__main__":
    unittest.main()
