from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import elliott_ai
from elliott_ai.agent import ElliottAgent
from elliott_ai.forecast_outcomes import (
    FORECAST_OUTCOME_MIGRATION_SQL,
    ClaimType,
    EvaluationPolicy,
    EventOrdering,
    ForecastObservationSet,
    ForecastOutcomeEvaluation,
    ObservationCandle,
    ObservationCompletenessState,
    ObservationScheduleMode,
    OutcomeStatus,
    SessionGap,
    build_forecast_observation_set,
    evaluate_forecast_observation_set,
    forecast_observation_set_content_hash,
    forecast_outcome_evaluation_content_hash,
    replay_forecast_observation_set,
    replay_forecast_outcome_evaluation,
    validate_forecast_observation_set,
)
from elliott_ai.forecast_records import (
    AlternativeHypothesis,
    ClaimEvaluationBasis,
    ConfirmationClaim,
    ConfirmationOperator,
    DatasetCutoff,
    DatasetHashScope,
    ExpectedCompletionWindow,
    ForecastDirection,
    ForecastRecord,
    InvalidationClaim,
    InvalidationOperator,
    MainHypothesis,
    TargetClaim,
    canonical_sha256,
)
from elliott_ai.knowledge import KnowledgeStore

try:
    from scripts.test_forecast_records import CUTOFF, SYMBOL, _create_sources, _typed_record
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from test_forecast_records import CUTOFF, SYMBOL, _create_sources, _typed_record


DAY = 86_400


def _rebuild_forecast(
    record: ForecastRecord,
    *,
    forecast_id: str,
    main_hypothesis: MainHypothesis | None = None,
    alternative_hypotheses: tuple[AlternativeHypothesis, ...] | None = None,
    target_claims: tuple[TargetClaim, ...] | None = None,
    invalidation_claims: tuple[InvalidationClaim, ...] | None = None,
    confirmation_claims: tuple[ConfirmationClaim, ...] | None = None,
    expected_completion_windows: tuple[ExpectedCompletionWindow, ...] | None = None,
    dataset_cutoffs: tuple[DatasetCutoff, ...] | None = None,
) -> ForecastRecord:
    payload = record.to_dict()
    payload.pop("content_hash")
    payload["forecast_id"] = forecast_id
    if main_hypothesis is not None:
        payload["main_hypothesis"] = main_hypothesis
    if alternative_hypotheses is not None:
        payload["alternative_hypotheses"] = alternative_hypotheses
    if target_claims is not None:
        payload["target_claims"] = target_claims
    if invalidation_claims is not None:
        payload["invalidation_claims"] = invalidation_claims
    if confirmation_claims is not None:
        payload["confirmation_claims"] = confirmation_claims
    if expected_completion_windows is not None:
        payload["expected_completion_windows"] = expected_completion_windows
    if dataset_cutoffs is not None:
        payload["dataset_cutoffs"] = dataset_cutoffs
    return ForecastRecord.create(**payload)


def _daily_schedule(record: ForecastRecord) -> tuple[str, ...]:
    start = datetime.fromisoformat(record.analysis_cutoff_utc)
    end = max(
        datetime.fromisoformat(item.end_utc)
        for item in record.expected_completion_windows
    )
    result: list[str] = []
    while start + timedelta(days=1) <= end:
        result.append(start.isoformat(timespec="seconds"))
        start += timedelta(days=1)
    return tuple(result)


def _daily_rows(
    record: ForecastRecord,
    *,
    target_index: int | None = None,
    invalidation_index: int | None = None,
    confirmation_close: bool = True,
    omit_indexes: frozenset[int] = frozenset(),
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, open_time in enumerate(_daily_schedule(record)):
        if index in omit_indexes:
            continue
        opened = datetime.fromisoformat(open_time)
        high = 410.0
        low = 390.0
        close = 400.0
        if target_index == index:
            high = 445.0
            close = 425.0 if confirmation_close else 410.0
        if invalidation_index == index:
            low = 345.0
        rows.append(
            {
                "open_time_utc": open_time,
                "close_time_utc": (opened + timedelta(days=1)).isoformat(
                    timespec="seconds"
                ),
                "open": 400.0,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1_000_000.0 + index,
                "source_sequence": index,
            }
        )
    return rows


def _observation(
    record: ForecastRecord,
    *,
    rows: list[dict[str, object]] | None = None,
    actual_cutoff: str | None = None,
    expected_opens: tuple[str, ...] | None = None,
    source_dataset_id: str | None = None,
    policy: EvaluationPolicy | None = None,
    version: int = 1,
    supersedes: str | None = None,
    **metadata: object,
) -> ForecastObservationSet:
    dataset = (
        next(
            item
            for item in record.dataset_cutoffs
            if item.dataset_id == source_dataset_id
        )
        if source_dataset_id is not None
        else record.dataset_cutoffs[0]
    )
    selected_policy = policy or EvaluationPolicy.create(
        expected_interval_seconds=DAY,
        schedule_mode=ObservationScheduleMode.EXPLICIT_EXPECTED_OPENS,
    )
    return build_forecast_observation_set(
        record,
        rows if rows is not None else _daily_rows(record),
        source_dataset_id=dataset.dataset_id,
        actual_evaluation_cutoff_utc=actual_cutoff
        or max(item.end_utc for item in record.expected_completion_windows),
        evaluation_policy=selected_policy,
        expected_open_times_utc=(
            expected_opens
            if expected_opens is not None
            else (
                _daily_schedule(record)
                if selected_policy.schedule_mode
                is ObservationScheduleMode.EXPLICIT_EXPECTED_OPENS
                else None
            )
        ),
        observation_set_version=version,
        supersedes_observation_set_id=supersedes,
        **metadata,
    )


def _with_reference(record: ForecastRecord) -> ForecastRecord:
    main_payload = record.main_hypothesis.to_dict()
    main_payload["count"]["evaluation_reference"] = {
        "price": 400.0,
        "timestamp_utc": record.analysis_cutoff_utc,
        "price_basis": "ohlc",
    }
    return _rebuild_forecast(
        record,
        forecast_id="forecast-with-evaluation-reference",
        main_hypothesis=MainHypothesis.from_dict(main_payload),
    )


def _with_multiple_targets(record: ForecastRecord) -> ForecastRecord:
    second = TargetClaim(
        claim_id="target-main-2",
        hypothesis_id="main",
        target_low=470.0,
        target_high=480.0,
        direction=ForecastDirection.UP,
        timeframe="daily",
        price_basis="ohlc",
        rationale="Second frozen target zone.",
    )
    main_payload = record.main_hypothesis.to_dict()
    main_payload["target_claim_ids"] = ["target-main", "target-main-2"]
    return _rebuild_forecast(
        record,
        forecast_id="forecast-multiple-targets",
        main_hypothesis=MainHypothesis.from_dict(main_payload),
        target_claims=record.target_claims + (second,),
    )


def _with_scored_alternative(record: ForecastRecord) -> ForecastRecord:
    alternate = AlternativeHypothesis(
        hypothesis_id="alternate-down",
        label="Downward alternate",
        direction=ForecastDirection.DOWN,
        degree="Primary",
        wave_label="C",
        pattern_family="zigzag",
        completion_status="active",
        summary="A separate downward alternative.",
        count={"wave_ids": ["ALT-C"]},
        target_claim_ids=("target-alt",),
        invalidation_claim_ids=("invalidate-alt",),
        confirmation_claim_ids=("confirm-alt",),
        completion_window_ids=("window-alt",),
        original_uncalibrated_confidence={"structure": 0.25},
    )
    target = TargetClaim(
        claim_id="target-alt",
        hypothesis_id="alternate-down",
        target_low=355.0,
        target_high=365.0,
        direction=ForecastDirection.DOWN,
        timeframe="daily",
        price_basis="ohlc",
        rationale="Frozen alternate target.",
    )
    invalidation = InvalidationClaim(
        claim_id="invalidate-alt",
        hypothesis_id="alternate-down",
        operator=InvalidationOperator.AT_OR_ABOVE,
        condition="Price reaches 430.",
        timeframe="daily",
        price_basis="ohlc",
        price_level=430.0,
    )
    confirmation = ConfirmationClaim(
        claim_id="confirm-alt",
        hypothesis_id="alternate-down",
        operator=ConfirmationOperator.AT_OR_BELOW,
        condition="A daily candle closes at or below 380.",
        timeframe="daily",
        price_basis="close",
        price_level=380.0,
    )
    base_window = record.expected_completion_windows[0]
    window = ExpectedCompletionWindow(
        window_id="window-alt",
        hypothesis_id="alternate-down",
        start_utc=base_window.start_utc,
        end_utc=base_window.end_utc,
        timeframe="daily",
        start_rule="First completed post-cutoff candle.",
        end_rule="Frozen alternate horizon end.",
        rationale="Stored alternate timing estimate.",
    )
    return _rebuild_forecast(
        record,
        forecast_id="forecast-scored-alternative",
        alternative_hypotheses=(alternate,),
        target_claims=record.target_claims + (target,),
        invalidation_claims=record.invalidation_claims + (invalidation,),
        confirmation_claims=record.confirmation_claims + (confirmation,),
        expected_completion_windows=record.expected_completion_windows + (window,),
    )


def _with_structural_invalidation(record: ForecastRecord) -> ForecastRecord:
    structural = InvalidationClaim(
        claim_id="invalidate-main",
        hypothesis_id="main",
        operator=InvalidationOperator.STRUCTURAL_CONDITION,
        condition="A later reviewed overlap invalidates the motive count.",
        timeframe="daily",
        price_basis="ohlc",
        evaluation_basis=ClaimEvaluationBasis.HUMAN_STRUCTURAL_REVIEW,
    )
    return _rebuild_forecast(
        record,
        forecast_id="forecast-structural-invalidation",
        invalidation_claims=(structural,),
    )


def _with_hourly_dataset(record: ForecastRecord) -> ForecastRecord:
    daily = record.dataset_cutoffs[0]
    hourly = DatasetCutoff(
        dataset_id="dataset-hourly-post-order",
        source_reference="fixture_msft_hourly.json",
        timeframe="1h",
        cutoff_utc=record.analysis_cutoff_utc,
        dataset_hash=canonical_sha256(
            {"source": "fixture_msft_hourly.json", "cutoff": record.analysis_cutoff_utc}
        ),
        hash_scope=DatasetHashScope.STORED_DATASET_SUMMARY,
        provider=daily.provider,
        feed_identity=daily.feed_identity,
        requested_symbol=daily.requested_symbol,
        resolved_symbol=daily.resolved_symbol,
        exchange=daily.exchange,
        session=daily.session,
        timezone=daily.timezone,
        adjustment=daily.adjustment,
        price_basis=daily.price_basis,
        completed_candles_only=True,
        bar_count=100,
    )
    return _rebuild_forecast(
        record,
        forecast_id="forecast-with-hourly-dataset",
        dataset_cutoffs=record.dataset_cutoffs + (hourly,),
    )


class ForecastOutcomeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "brain.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.run_id, self.resolution_id = _create_sources(self.store)
        self.record = _typed_record(self.store, self.run_id, self.resolution_id)


class ObservationContractTests(ForecastOutcomeFixture):
    def test_contracts_are_frozen_and_round_trip(self) -> None:
        candle = ObservationCandle.create(
            open_time_utc=CUTOFF,
            close_time_utc="2024-02-01T20:00:00Z",
            open=400,
            high=410,
            low=390,
            close=405,
        )
        with self.assertRaises(FrozenInstanceError):
            candle.close = 999  # type: ignore[misc]
        restored = ObservationCandle.from_dict(candle.to_dict())
        self.assertEqual(restored, candle)

    def test_exact_cutoff_open_is_accepted(self) -> None:
        observation = _observation(self.record)
        self.assertEqual(observation.candles[0].open_time_utc, self.record.analysis_cutoff_utc)
        self.assertEqual(
            validate_forecast_observation_set(observation, self.record), ()
        )

    def test_candle_overlapping_cutoff_is_rejected(self) -> None:
        rows = _daily_rows(self.record)
        rows[0]["open_time_utc"] = "2024-01-31T19:00:00Z"
        rows[0]["close_time_utc"] = "2024-02-01T19:00:00Z"
        with self.assertRaisesRegex(ValueError, "overlaps decision-time"):
            _observation(self.record, rows=rows)

    def test_partial_candle_is_preserved_but_not_scored(self) -> None:
        opened = datetime.fromisoformat(self.record.analysis_cutoff_utc)
        rows = [
            {
                "open_time_utc": opened.isoformat(timespec="seconds"),
                "close_time_utc": (opened + timedelta(days=1)).isoformat(timespec="seconds"),
                "open": 400,
                "high": 405,
                "low": 395,
                "close": 402,
                "is_complete": False,
            }
        ]
        observation = _observation(
            self.record,
            rows=rows,
            actual_cutoff=(opened + timedelta(hours=12)).isoformat(timespec="seconds"),
        )
        self.assertEqual(
            observation.completeness.state, ObservationCompletenessState.PARTIAL
        )
        evaluation = evaluate_forecast_observation_set(self.record, observation)
        self.assertEqual(evaluation.outcome_status, OutcomeStatus.INSUFFICIENT_DATA)

    def test_duplicate_and_out_of_order_timestamps_are_rejected(self) -> None:
        rows = _daily_rows(self.record)
        duplicate = dict(rows[0])
        rows.insert(1, duplicate)
        with self.assertRaisesRegex(ValueError, "duplicate|overlap"):
            _observation(self.record, rows=rows)
        rows = _daily_rows(self.record)
        rows[0], rows[1] = rows[1], rows[0]
        with self.assertRaisesRegex(ValueError, "out of order|overlap"):
            _observation(self.record, rows=rows)

    def test_missing_candle_is_detected(self) -> None:
        observation = _observation(
            self.record, rows=_daily_rows(self.record, omit_indexes=frozenset({3}))
        )
        self.assertEqual(
            observation.completeness.state, ObservationCompletenessState.INCOMPLETE
        )
        self.assertEqual(len(observation.completeness.missing_open_times_utc), 1)

    def test_explicit_session_gap_is_not_a_missing_candle(self) -> None:
        full_schedule = _daily_schedule(self.record)
        shortened_schedule = tuple(
            item for index, item in enumerate(full_schedule) if index != 2
        )
        rows = _daily_rows(self.record, omit_indexes=frozenset({2}))
        gap_start = full_schedule[2]
        gap_end = (
            datetime.fromisoformat(gap_start) + timedelta(days=1)
        ).isoformat(timespec="seconds")
        observation = build_forecast_observation_set(
            self.record,
            rows,
            source_dataset_id=self.record.dataset_cutoffs[0].dataset_id,
            actual_evaluation_cutoff_utc=max(
                item.end_utc for item in self.record.expected_completion_windows
            ),
            evaluation_policy=EvaluationPolicy.create(
                expected_interval_seconds=DAY,
                schedule_mode=ObservationScheduleMode.EXPLICIT_EXPECTED_OPENS,
            ),
            expected_open_times_utc=shortened_schedule,
            session_gaps=(
                SessionGap(
                    start_utc=gap_start,
                    end_utc=gap_end,
                    reason="Exchange was closed; no candle was expected.",
                ),
            ),
        )
        self.assertEqual(
            observation.completeness.state, ObservationCompletenessState.COMPLETE
        )
        self.assertEqual(observation.completeness.missing_open_times_utc, ())
        self.assertEqual(len(observation.completeness.session_gaps), 1)

    def test_horizon_censoring_is_explicit(self) -> None:
        rows = _daily_rows(self.record)[:5]
        observation = _observation(
            self.record,
            rows=rows,
            actual_cutoff=rows[-1]["close_time_utc"],  # type: ignore[arg-type]
        )
        self.assertEqual(
            observation.completeness.state, ObservationCompletenessState.CENSORED
        )
        self.assertTrue(observation.completeness.censored)
        self.assertFalse(observation.completeness.horizon_complete)

    def test_metadata_mismatches_are_rejected(self) -> None:
        cases = (
            {"provider": "other-provider"},
            {"feed": "other-feed"},
            {"adjustment": {"splits_adjusted": False}},
            {"price_basis": "adjusted_close"},
        )
        for metadata in cases:
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(ValueError, "incompatible"):
                    _observation(self.record, **metadata)

    def test_manifest_hash_tampering_is_detected(self) -> None:
        observation = _observation(self.record)
        payload = observation.to_dict()
        payload["candles"][0]["high"] = 999.0
        with self.assertRaisesRegex(ValueError, "content_hash"):
            ForecastObservationSet.from_dict(payload)
        payload = observation.to_dict()
        payload["candle_manifest_hash"] = "0" * 64
        payload["content_hash"] = forecast_observation_set_content_hash(payload)
        tampered = ForecastObservationSet.from_dict(payload)
        errors = validate_forecast_observation_set(tampered, self.record)
        self.assertTrue(any("manifest" in item for item in errors))

    def test_replay_is_deterministic(self) -> None:
        first = _observation(self.record)
        second = replay_forecast_observation_set(self.record, first.to_dict())
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)


class DeterministicEvaluationTests(ForecastOutcomeFixture):
    def test_touch_target_and_close_confirmation(self) -> None:
        observation = _observation(
            self.record, rows=_daily_rows(self.record, target_index=4)
        )
        result = evaluate_forecast_observation_set(self.record, observation)
        target = next(
            item
            for item in result.main_hypothesis_outcome.claim_outcomes
            if item.claim_type is ClaimType.TARGET
        )
        confirmation = next(
            item
            for item in result.main_hypothesis_outcome.claim_outcomes
            if item.claim_type is ClaimType.CONFIRMATION
        )
        self.assertTrue(target.target_touched)
        self.assertTrue(confirmation.confirmation_met)
        self.assertEqual(result.outcome_status, OutcomeStatus.SUCCEEDED)

    def test_confirmation_uses_close_not_intrabar_high(self) -> None:
        observation = _observation(
            self.record,
            rows=_daily_rows(
                self.record, target_index=4, confirmation_close=False
            ),
        )
        result = evaluate_forecast_observation_set(self.record, observation)
        confirmation = next(
            item
            for item in result.main_hypothesis_outcome.claim_outcomes
            if item.claim_type is ClaimType.CONFIRMATION
        )
        self.assertFalse(confirmation.confirmation_met)
        self.assertEqual(
            result.main_hypothesis_outcome.price_status, OutcomeStatus.PARTIAL
        )

    def test_structural_invalidation_remains_unresolved(self) -> None:
        record = _with_structural_invalidation(self.record)
        result = evaluate_forecast_observation_set(
            record,
            _observation(record, rows=_daily_rows(record, target_index=4)),
        )
        invalidation = next(
            item
            for item in result.main_hypothesis_outcome.claim_outcomes
            if item.claim_type is ClaimType.INVALIDATION
        )
        self.assertIsNone(invalidation.invalidation_breached)
        self.assertFalse(invalidation.comparable)
        self.assertEqual(invalidation.outcome_status, OutcomeStatus.UNRESOLVED)

    def test_multiple_targets_are_aggregated_without_blended_score(self) -> None:
        record = _with_multiple_targets(self.record)
        result = evaluate_forecast_observation_set(
            record,
            _observation(record, rows=_daily_rows(record, target_index=4)),
        )
        targets = [
            item
            for item in result.main_hypothesis_outcome.claim_outcomes
            if item.claim_type is ClaimType.TARGET
        ]
        self.assertEqual([item.target_touched for item in targets], [True, False])
        self.assertEqual(result.outcome_status, OutcomeStatus.PARTIAL)

    def test_main_and_alternative_are_independent(self) -> None:
        record = _with_scored_alternative(self.record)
        result = evaluate_forecast_observation_set(
            record,
            _observation(record, rows=_daily_rows(record, target_index=4)),
        )
        self.assertEqual(result.main_hypothesis_outcome.outcome_status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(len(result.alternative_hypothesis_outcomes), 1)
        self.assertEqual(
            result.alternative_hypothesis_outcomes[0].outcome_status,
            OutcomeStatus.FAILED,
        )
        self.assertEqual(result.outcome_status, OutcomeStatus.SUCCEEDED)

    def test_same_candle_target_and_invalidation_is_incomparable(self) -> None:
        rows = _daily_rows(self.record, target_index=4, invalidation_index=4)
        result = evaluate_forecast_observation_set(
            self.record, _observation(self.record, rows=rows)
        )
        self.assertEqual(
            result.main_hypothesis_outcome.event_order, EventOrdering.INCOMPARABLE
        )
        self.assertEqual(result.outcome_status, OutcomeStatus.INCOMPARABLE)

    def test_registered_lower_timeframe_set_resolves_order(self) -> None:
        record = _with_hourly_dataset(self.record)
        daily_rows = _daily_rows(record, target_index=0, invalidation_index=0)
        parent = _observation(record, rows=daily_rows)
        opened = datetime.fromisoformat(record.analysis_cutoff_utc)
        hourly_rows: list[dict[str, object]] = []
        for index in range(24):
            high = 445.0 if index == 2 else 410.0
            low = 345.0 if index == 10 else 390.0
            hourly_rows.append(
                {
                    "open_time_utc": (opened + timedelta(hours=index)).isoformat(
                        timespec="seconds"
                    ),
                    "close_time_utc": (
                        opened + timedelta(hours=index + 1)
                    ).isoformat(timespec="seconds"),
                    "open": 400,
                    "high": high,
                    "low": low,
                    "close": 425 if index == 2 else 400,
                    "source_sequence": index,
                }
            )
        hourly_dataset = record.dataset_cutoffs[-1]
        lower = _observation(
            record,
            rows=hourly_rows,
            actual_cutoff=(opened + timedelta(days=1)).isoformat(timespec="seconds"),
            source_dataset_id=hourly_dataset.dataset_id,
            policy=EvaluationPolicy.create(
                expected_interval_seconds=3600,
                schedule_mode=ObservationScheduleMode.CONTINUOUS_INTERVAL,
            ),
        )
        result = evaluate_forecast_observation_set(
            record, parent, lower_timeframe_observation_sets=(lower,)
        )
        self.assertEqual(
            result.main_hypothesis_outcome.event_order,
            EventOrdering.TARGET_BEFORE_INVALIDATION,
        )
        self.assertEqual(
            result.main_hypothesis_outcome.lower_timeframe_resolution_observation_set_id,
            lower.observation_set_id,
        )

    def test_mfe_and_mae_require_frozen_reference(self) -> None:
        observation = _observation(
            self.record, rows=_daily_rows(self.record, target_index=4)
        )
        without = evaluate_forecast_observation_set(self.record, observation)
        self.assertFalse(without.main_hypothesis_outcome.excursion_available)
        self.assertIsNone(without.main_hypothesis_outcome.mfe)
        record = _with_reference(self.record)
        rows = _daily_rows(record, target_index=4)
        rows[2]["low"] = 380.0
        rows[4]["high"] = 450.0
        with_reference = evaluate_forecast_observation_set(
            record, _observation(record, rows=rows)
        )
        outcome = with_reference.main_hypothesis_outcome
        self.assertTrue(outcome.excursion_available)
        self.assertEqual(outcome.mfe, 50.0)
        self.assertEqual(outcome.mae, 20.0)
        self.assertEqual(outcome.mfe_pct, 12.5)
        self.assertEqual(outcome.mae_pct, 5.0)

    def test_price_and_timing_are_separate(self) -> None:
        rows = _daily_rows(self.record, target_index=0)
        result = evaluate_forecast_observation_set(
            self.record, _observation(self.record, rows=rows)
        )
        self.assertEqual(
            result.main_hypothesis_outcome.price_status, OutcomeStatus.SUCCEEDED
        )
        self.assertEqual(
            result.main_hypothesis_outcome.timing_status, OutcomeStatus.SUCCEEDED
        )

    def test_incomplete_sequence_cannot_invent_success(self) -> None:
        rows = _daily_rows(
            self.record, target_index=4, omit_indexes=frozenset({2})
        )
        result = evaluate_forecast_observation_set(
            self.record, _observation(self.record, rows=rows)
        )
        self.assertEqual(result.outcome_status, OutcomeStatus.INSUFFICIENT_DATA)

    def test_repeated_evaluation_has_stable_hash(self) -> None:
        observation = _observation(
            self.record, rows=_daily_rows(self.record, target_index=4)
        )
        first = evaluate_forecast_observation_set(self.record, observation)
        second = replay_forecast_outcome_evaluation(self.record, observation)
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_evaluation_tampering_is_detected(self) -> None:
        observation = _observation(self.record)
        result = evaluate_forecast_observation_set(self.record, observation)
        payload = result.to_dict()
        payload["outcome_status"] = "succeeded"
        if result.outcome_status is OutcomeStatus.SUCCEEDED:
            payload["outcome_status"] = "failed"
        with self.assertRaisesRegex(ValueError, "content_hash"):
            ForecastOutcomeEvaluation.from_dict(payload)
        self.assertRegex(forecast_outcome_evaluation_content_hash(result), r"^[0-9a-f]{64}$")


class OutcomePersistenceTests(ForecastOutcomeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.store.create_forecast_record(self.record)

    def test_migration_adds_exactly_two_phase11b_tables(self) -> None:
        with self.store.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('forecast_observation_sets', 'forecast_outcome_evaluations')"
                )
            }
        self.assertEqual(
            tables, {"forecast_observation_sets", "forecast_outcome_evaluations"}
        )

    def test_create_get_list_and_append_only_triggers(self) -> None:
        observation = _observation(self.record)
        first = self.store.create_forecast_observation_set(observation)
        second = self.store.create_forecast_observation_set(observation)
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(
            self.store.get_forecast_observation_set(observation.observation_set_id),
            observation,
        )
        evaluation = evaluate_forecast_observation_set(self.record, observation)
        self.store.create_forecast_outcome_evaluation(evaluation)
        self.assertEqual(
            self.store.get_forecast_outcome_evaluation(evaluation.evaluation_id),
            evaluation,
        )
        self.assertEqual(self.store.list_forecast_observation_sets(), [observation])
        self.assertEqual(self.store.list_forecast_outcome_evaluations(), [evaluation])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE forecast_observation_sets SET symbol='OTHER' "
                    "WHERE observation_set_id=?",
                    (observation.observation_set_id,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "DELETE FROM forecast_outcome_evaluations WHERE evaluation_id=?",
                    (evaluation.evaluation_id,),
                )

    def test_observation_and_evaluation_supersession(self) -> None:
        first_rows = _daily_rows(self.record)[:5]
        first = _observation(
            self.record,
            rows=first_rows,
            actual_cutoff=first_rows[-1]["close_time_utc"],  # type: ignore[arg-type]
        )
        self.store.create_forecast_observation_set(first)
        second = _observation(
            self.record,
            rows=_daily_rows(self.record, target_index=4),
            version=2,
            supersedes=first.observation_set_id,
        )
        self.store.create_forecast_observation_set(second)
        current = self.store.list_forecast_observation_sets(include_superseded=False)
        self.assertEqual(current, [second])
        evaluation_one = evaluate_forecast_observation_set(self.record, first)
        self.store.create_forecast_outcome_evaluation(evaluation_one)
        evaluation_two = evaluate_forecast_observation_set(
            self.record,
            second,
            evaluation_version=2,
            supersedes_evaluation_id=evaluation_one.evaluation_id,
        )
        self.store.create_forecast_outcome_evaluation(evaluation_two)
        self.assertEqual(
            self.store.list_forecast_outcome_evaluations(include_superseded=False),
            [evaluation_two],
        )
        self.assertEqual(len(self.store.list_forecast_outcome_evaluations()), 2)

    def test_foreign_keys_and_integrity(self) -> None:
        observation = _observation(self.record)
        self.store.create_forecast_observation_set(observation)
        evaluation = evaluate_forecast_observation_set(self.record, observation)
        self.store.create_forecast_outcome_evaluation(evaluation)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "DELETE FROM forecast_records WHERE forecast_id=?",
                    (self.record.forecast_id,),
                )
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unregistered_lower_timeframe_reference_is_rejected(self) -> None:
        observation = _observation(self.record)
        self.store.create_forecast_observation_set(observation)
        evaluation = evaluate_forecast_observation_set(self.record, observation)
        payload = evaluation.to_dict()
        payload["lower_timeframe_observation_set_ids"] = ["not-registered"]
        payload["lower_timeframe_observation_set_hashes"] = {
            "not-registered": "a" * 64
        }
        payload["source_hashes"]["lower_observation:not-registered"] = "a" * 64
        payload["content_hash"] = forecast_outcome_evaluation_content_hash(payload)
        altered = ForecastOutcomeEvaluation.from_dict(payload)
        with self.assertRaisesRegex(KeyError, "not registered"):
            self.store.create_forecast_outcome_evaluation(altered)

    def test_no_mutation_or_delete_methods_exist(self) -> None:
        for name in (
            "update_forecast_observation_set",
            "delete_forecast_observation_set",
            "update_forecast_outcome_evaluation",
            "delete_forecast_outcome_evaluation",
        ):
            self.assertFalse(hasattr(self.store, name))

    def test_migration_rollback_leaves_no_phase11b_tables(self) -> None:
        database = Path(self.temporary.name) / "rollback.sqlite3"
        initial = KnowledgeStore(database)
        with initial.connect() as connection:
            for trigger in (
                "forecast_outcome_evaluations_no_update",
                "forecast_outcome_evaluations_no_delete",
                "forecast_observation_sets_no_update",
                "forecast_observation_sets_no_delete",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute("DROP TABLE forecast_outcome_evaluations")
            connection.execute("DROP TABLE forecast_observation_sets")
        broken_sql = (FORECAST_OUTCOME_MIGRATION_SQL[0], "CREATE TABLE broken(")
        with patch("elliott_ai.knowledge.FORECAST_OUTCOME_MIGRATION_SQL", broken_sql):
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
        self.assertNotIn("forecast_observation_sets", tables)
        self.assertNotIn("forecast_outcome_evaluations", tables)

    def test_preexisting_database_gets_backup_and_audit(self) -> None:
        database = Path(self.temporary.name) / "preexisting.sqlite3"
        initial = KnowledgeStore(database)
        with initial.connect() as connection:
            for trigger in (
                "forecast_outcome_evaluations_no_update",
                "forecast_outcome_evaluations_no_delete",
                "forecast_observation_sets_no_update",
                "forecast_observation_sets_no_delete",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute("DROP TABLE forecast_outcome_evaluations")
            connection.execute("DROP TABLE forecast_observation_sets")
        migrated = KnowledgeStore(database)
        self.assertTrue(migrated.forecast_outcome_migration_applied)
        self.assertIsNotNone(migrated.forecast_outcome_backup_path)
        self.assertTrue(migrated.forecast_outcome_backup_path.exists())
        self.assertTrue(migrated.forecast_outcome_migration_audit_path.exists())

    def test_legacy_and_existing_public_interfaces_remain_readable(self) -> None:
        self.assertEqual(self.store.get_forecast_record(self.record.forecast_id), self.record)
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


if __name__ == "__main__":
    unittest.main()
