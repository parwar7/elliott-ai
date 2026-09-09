from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

from elliott_ai.current_state import (
    CurrentMarketState,
    CurrentStateBuilder,
    FeatureAvailability,
    finalize_current_market_state,
)
from elliott_ai.historical_market import (
    DurationBucket,
    HistoricalMoveType,
    MagnitudeBucket,
    MarketRegime,
    MarketRegimeState,
    RelativePerformanceBucket,
)
from elliott_ai.historical_patterns import (
    EvidenceIndependenceStatus,
    HistoricalPattern,
    HistoricalPatternLibrary,
    HistoricalPatternLibraryBuilder,
    PatternChange,
    PatternConfidence,
    PatternDirection,
    PatternStatus,
    finalize_historical_pattern,
    finalize_historical_pattern_library,
)
from elliott_ai.market_scenario import (
    ExposureCategory,
    ValidationResult,
)
from elliott_ai.pattern_retrieval import (
    DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE,
    MatchExplanationCode,
    MatchQuality,
    MatchRecommendation,
    PatternExclusionCode,
    PatternRetrievalEngine,
    PatternRetrievalExecutionResult,
    PatternRetrievalQuery,
    PatternRetrievalResult,
    RetrievalLane,
    RetrievedPatternMatch,
    create_pattern_retrieval_query,
    pattern_retrieval_execution_result_content_hash,
    pattern_retrieval_query_content_hash,
    pattern_retrieval_result_content_hash,
    retrieved_pattern_match_content_hash,
    validate_pattern_retrieval_query,
    validate_pattern_retrieval_result,
    validate_pattern_retrieval_scoring_profile,
    validate_retrieved_pattern_match,
)
from elliott_ai.current_state import TechnicalDirection
from scripts.test_current_state import (
    CUTOFF as CURRENT_CUTOFF,
    _bullish_premise,
)
from scripts.test_historical_patterns import (
    FIXED_NOW as HISTORICAL_NOW,
    _candidate_bundle,
    _pattern,
)
from scripts.test_market_scenario import _draft_report


FIXED_NOW = datetime(2026, 7, 29, 9, tzinfo=timezone.utc)


def _matching_regime() -> MarketRegime:
    return MarketRegime.create(
        interest_rate_regime=MarketRegimeState.STABLE,
        inflation_regime=MarketRegimeState.LOW,
        liquidity_regime=MarketRegimeState.EXPANDING,
        credit_regime=MarketRegimeState.EASING,
        equity_risk_appetite=MarketRegimeState.RISK_ON,
        volatility_regime=MarketRegimeState.NORMAL,
        small_cap_regime=MarketRegimeState.OUTPERFORMING,
        growth_stock_regime=MarketRegimeState.OUTPERFORMING,
        sector_regime=MarketRegimeState.ADVANCING,
        benchmark_trend=MarketRegimeState.ADVANCING,
        regime_start_at="2026-07-01T00:00:00+00:00",
        regime_end_at=CURRENT_CUTOFF,
        supporting_evidence_ids=("ev-macro",),
        confidence="moderate",
    )


def _matching_state() -> CurrentMarketState:
    report = _draft_report(premise=_bullish_premise())
    revision_event = replace(
        report.material_events[0],
        event_id="event-earnings-revision",
        event_type="earnings_revision",
    )
    result = CurrentStateBuilder(clock=lambda: FIXED_NOW).build(
        report.frozen_technical_premise,
        report.evidence,
        report.exposures,
        report.exposure_coverage,
        events=report.material_events + (revision_event,),
        market_regime=_matching_regime(),
    )
    if not result.success or result.current_state is None:
        raise AssertionError(
            "Current-state fixture failed: " + repr(result.errors)
        )
    return result.current_state


def _standalone_variant(
    pattern: HistoricalPattern,
    *,
    pattern_id: str,
    **changes,
) -> HistoricalPattern:
    draft = replace(
        pattern,
        pattern_id=pattern_id,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
        **changes,
    )
    result = finalize_historical_pattern(draft)
    if not result.validation_result.is_valid:
        raise AssertionError(
            "Pattern fixture failed: "
            + repr(result.validation_result.to_dict())
        )
    return result


def _base_bundle():
    packets, analyses, _, candidate, _ = _candidate_bundle()
    pattern = _pattern(
        packets,
        analyses,
        candidate,
        pattern_id="aligned_pattern",
    )
    case_hashes = {
        packet.historical_move.case_id: packet.content_hash
        for packet in packets
    }
    return packets, analyses, candidate, pattern, case_hashes


def _library(
    patterns: tuple[HistoricalPattern, ...],
    case_hashes: dict[str, str],
    *,
    clock=lambda: HISTORICAL_NOW,
) -> HistoricalPatternLibrary:
    result = HistoricalPatternLibraryBuilder(clock=clock).build(
        patterns,
        library_id="phase6_pattern_library",
        source_case_hashes=case_hashes,
    )
    if not result.success or result.library is None:
        raise AssertionError(
            "Library fixture failed: "
            + repr(result.validation_result.to_dict())
        )
    return result.library


def _library_allowing_invalid_member(
    valid_library: HistoricalPatternLibrary,
    patterns: tuple[HistoricalPattern, ...],
) -> HistoricalPatternLibrary:
    deprecated = tuple(
        sorted(
            item.pattern_ref
            for item in patterns
            if item.pattern_status is PatternStatus.DEPRECATED
        )
    )
    draft = replace(
        valid_library,
        patterns=tuple(
            sorted(
                patterns,
                key=lambda item: (
                    item.pattern_id,
                    item.pattern_version,
                ),
            )
        ),
        deprecated_pattern_refs=deprecated,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    return finalize_historical_pattern_library(draft)


def _query(
    state: CurrentMarketState,
    library: HistoricalPatternLibrary,
    **changes,
) -> PatternRetrievalQuery:
    values = {
        "generated_at": FIXED_NOW,
        "requested_limit": 10,
        "requested_counter_limit": 5,
    }
    values.update(changes)
    return create_pattern_retrieval_query(
        state,
        library,
        **values,
    )


def _retrieve(
    state: CurrentMarketState,
    library: HistoricalPatternLibrary,
    **query_changes,
) -> PatternRetrievalExecutionResult:
    return PatternRetrievalEngine(
        clock=lambda: FIXED_NOW
    ).retrieve(
        state,
        library,
        _query(state, library, **query_changes),
    )


def _without_required_exposure(
    state: CurrentMarketState,
    *,
    unavailable: bool,
) -> CurrentMarketState:
    category = next(
        category
        for category, value in state.exposure_category_availability.items()
        if value is FeatureAvailability.AVAILABLE
        and category
        == ExposureCategory.COMPANY.value
    )
    category_availability = dict(
        state.exposure_category_availability
    )
    category_availability[category] = (
        FeatureAvailability.UNAVAILABLE
        if unavailable
        else FeatureAvailability.RESEARCHED_NO_SIGNAL
    )
    unavailable_features = {
        item
        for item in state.unavailable_features
        if item != f"exposure_category:{category}"
    }
    if unavailable:
        unavailable_features.add(f"exposure_category:{category}")
    draft = replace(
        state,
        current_exposure_types=(),
        exposure_availability={},
        exposure_evidence_ids={},
        exposure_category_availability=category_availability,
        unavailable_features=tuple(sorted(unavailable_features)),
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    result = finalize_current_market_state(draft)
    if not result.validation_result.is_valid:
        raise AssertionError(
            "Partial-state fixture failed: "
            + repr(result.validation_result.to_dict())
        )
    return result


def _fully_aligned_state(
    state: CurrentMarketState,
) -> CurrentMarketState:
    draft = replace(
        state,
        expected_magnitude_bucket=MagnitudeBucket.FROM_40_TO_60_PERCENT,
        expected_duration_bucket=DurationBucket.WEEKS,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    result = finalize_current_market_state(draft)
    if not result.validation_result.is_valid:
        raise AssertionError(
            "Fully aligned state failed: "
            + repr(result.validation_result.to_dict())
        )
    return result


def _insufficient_state(
    state: CurrentMarketState,
) -> CurrentMarketState:
    feature_availability = {
        key: FeatureAvailability.UNAVAILABLE
        for key in state.feature_availability
    }
    category_availability = {
        key: FeatureAvailability.UNAVAILABLE
        for key in state.exposure_category_availability
    }
    unavailable = tuple(
        sorted(
            set(feature_availability)
            | {
                f"exposure_category:{key}"
                for key in category_availability
            }
        )
    )
    draft = replace(
        state,
        technical_direction=TechnicalDirection.UNAVAILABLE,
        technical_move_types=(),
        technical_structure="unavailable",
        expected_magnitude_bucket=MagnitudeBucket.UNAVAILABLE,
        expected_duration_bucket=DurationBucket.UNAVAILABLE,
        current_exposure_types=(),
        current_event_types=(),
        current_transmission_hypotheses=(),
        market_regime=None,
        valuation_state=MarketRegimeState.UNAVAILABLE,
        liquidity_state=MarketRegimeState.UNAVAILABLE,
        positioning_state=MarketRegimeState.UNAVAILABLE,
        benchmark_relative_state=(
            RelativePerformanceBucket.UNAVAILABLE
        ),
        sector_relative_state=RelativePerformanceBucket.UNAVAILABLE,
        unavailable_features=unavailable,
        feature_availability=feature_availability,
        exposure_category_availability=category_availability,
        exposure_availability={},
        exposure_evidence_ids={},
        transmission_source_refs={},
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    result = finalize_current_market_state(draft)
    if not result.validation_result.is_valid:
        raise AssertionError(
            "Insufficient-state fixture failed: "
            + repr(result.validation_result.to_dict())
        )
    return result


class PatternRetrievalCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.packets,
            self.analyses,
            self.candidate,
            self.pattern,
            self.case_hashes,
        ) = _base_bundle()
        self.state = _matching_state()

    def test_scoring_profile_is_valid_and_uses_required_weights(self) -> None:
        profile = DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
        self.assertTrue(
            validate_pattern_retrieval_scoring_profile(
                profile
            ).is_valid
        )
        self.assertEqual(
            dict(profile.component_weights),
            {
                "event": 10.0,
                "exposure": 30.0,
                "outcome": 10.0,
                "regime": 15.0,
                "technical": 15.0,
                "transmission": 20.0,
            },
        )
        self.assertEqual(
            sum(profile.component_weights.values()),
            100.0,
        )

    def test_exact_aligned_pattern_is_primary(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        execution = _retrieve(self.state, library)
        self.assertTrue(execution.success, execution.errors)
        retrieval = execution.retrieval_result
        self.assertIsNotNone(retrieval)
        self.assertEqual(len(retrieval.matches), 1)
        match = retrieval.matches[0]
        self.assertEqual(match.lane, RetrievalLane.PRIMARY)
        self.assertIn(
            MatchExplanationCode.REQUIRED_EXPOSURES_MATCH,
            match.explanation_codes,
        )
        self.assertIn(
            MatchExplanationCode.TRANSMISSION_MATCH,
            match.explanation_codes,
        )
        self.assertIn(
            MatchExplanationCode.REGIME_MATCH,
            match.explanation_codes,
        )
        self.assertIn(
            MatchExplanationCode.EVENT_MATCH,
            match.explanation_codes,
        )
        self.assertIn(
            MatchExplanationCode.TECHNICAL_DIRECTION_MATCH,
            match.explanation_codes,
        )
        self.assertGreater(match.total_score, 70.0)

    def test_researched_absence_is_missing_not_unknown(self) -> None:
        state = _without_required_exposure(
            self.state,
            unavailable=False,
        )
        library = _library((self.pattern,), self.case_hashes)
        execution = _retrieve(state, library)
        match = execution.retrieval_result.matches[0]
        self.assertIn(
            "exposure:revenue_growth_risk",
            match.missing_required_features,
        )
        self.assertNotIn(
            MatchExplanationCode.INSUFFICIENT_CURRENT_DATA,
            match.explanation_codes,
        )
        self.assertGreaterEqual(
            match.missing_data_penalty,
            DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE.penalties[
                "missing_required"
            ],
        )

    def test_unknown_required_exposure_reduces_completeness(self) -> None:
        state = _without_required_exposure(
            self.state,
            unavailable=True,
        )
        library = _library((self.pattern,), self.case_hashes)
        execution = _retrieve(state, library)
        match = (
            execution.retrieval_result.matches
            + execution.retrieval_result.counter_patterns
        )[0]
        self.assertIn(
            MatchExplanationCode.INSUFFICIENT_CURRENT_DATA,
            match.explanation_codes,
        )
        self.assertLess(match.information_completeness, 1.0)

    def test_unavailable_regime_is_unknown_not_incompatible(self) -> None:
        state = replace(
            self.state,
            market_regime=None,
            feature_availability={
                **dict(self.state.feature_availability),
                "market_regime": FeatureAvailability.UNAVAILABLE,
            },
            unavailable_features=tuple(
                sorted(
                    set(self.state.unavailable_features)
                    | {"market_regime"}
                )
            ),
            validation_result=ValidationResult.unvalidated(),
            content_hash="",
        )
        state = finalize_current_market_state(state)
        library = _library((self.pattern,), self.case_hashes)
        execution = _retrieve(state, library)
        all_matches = (
            execution.retrieval_result.matches
            + execution.retrieval_result.counter_patterns
        )
        self.assertTrue(all_matches)
        match = all_matches[0]
        self.assertEqual(match.incompatibility_penalty, 0.0)
        self.assertIn(
            MatchExplanationCode.INSUFFICIENT_CURRENT_DATA,
            match.explanation_codes,
        )

    def test_explicit_incompatible_regime_has_stronger_penalty(self) -> None:
        incompatible = _standalone_variant(
            self.pattern,
            pattern_id="incompatible_regime_pattern",
            required_market_regimes=(),
            incompatible_market_regimes=(
                "benchmark_trend=advancing",
            ),
        )
        library = _library((incompatible,), self.case_hashes)
        execution = _retrieve(self.state, library)
        match = execution.retrieval_result.matches[0]
        self.assertIn(
            MatchExplanationCode.INCOMPATIBLE_REGIME,
            match.explanation_codes,
        )
        self.assertGreater(match.incompatibility_penalty, 0.0)

    def test_technical_structure_mismatch_is_explicit(self) -> None:
        pattern = _standalone_variant(
            self.pattern,
            pattern_id="diagonal_structure_pattern",
            invariant_features=(
                self.pattern.invariant_features
                + ("technical_structure:diagonal",)
            ),
        )
        library = _library((pattern,), self.case_hashes)
        execution = _retrieve(self.state, library)
        match = execution.retrieval_result.matches[0]
        self.assertIn(
            MatchExplanationCode.TECHNICAL_STRUCTURE_MISMATCH,
            match.explanation_codes,
        )
        self.assertIn(
            "technical_structure:diagonal",
            match.contradicted_required_features,
        )

    def test_opposing_pattern_is_returned_only_in_counter_lane(self) -> None:
        bearish_profile = replace(
            self.pattern.expected_outcome_profile,
            expected_direction=PatternDirection.BEARISH,
            expected_move_types=(
                HistoricalMoveType.DOWNWARD_IMPULSE,
            ),
        )
        bearish = _standalone_variant(
            self.pattern,
            pattern_id="bearish_counter_pattern",
            pattern_direction=PatternDirection.BEARISH,
            applicable_move_types=(
                HistoricalMoveType.DOWNWARD_IMPULSE,
            ),
            expected_outcome_profile=bearish_profile,
        )
        library = _library(
            (self.pattern, bearish),
            self.case_hashes,
        )
        execution = _retrieve(self.state, library)
        retrieval = execution.retrieval_result
        self.assertEqual(
            tuple(item.pattern_ref for item in retrieval.matches),
            (self.pattern.pattern_ref,),
        )
        self.assertEqual(
            tuple(
                item.pattern_ref for item in retrieval.counter_patterns
            ),
            (bearish.pattern_ref,),
        )
        self.assertTrue(
            set(item.pattern_ref for item in retrieval.matches).isdisjoint(
                item.pattern_ref
                for item in retrieval.counter_patterns
            )
        )

    def test_context_dependent_pattern_requires_flag_and_context(self) -> None:
        profile = replace(
            self.pattern.expected_outcome_profile,
            expected_direction=PatternDirection.CONTEXT_DEPENDENT,
        )
        context = _standalone_variant(
            self.pattern,
            pattern_id="context_pattern",
            pattern_direction=PatternDirection.CONTEXT_DEPENDENT,
            expected_outcome_profile=profile,
        )
        library = _library((context,), self.case_hashes)
        excluded = _retrieve(self.state, library)
        self.assertIn(
            context.pattern_ref,
            excluded.retrieval_result.excluded_pattern_refs,
        )
        included = _retrieve(
            self.state,
            library,
            include_context_dependent_patterns=True,
        )
        self.assertEqual(
            included.retrieval_result.matches[0].pattern_ref,
            context.pattern_ref,
        )

    def test_bidirectional_pattern_requires_flag(self) -> None:
        profile = replace(
            self.pattern.expected_outcome_profile,
            expected_direction=PatternDirection.BIDIRECTIONAL,
        )
        bidirectional = _standalone_variant(
            self.pattern,
            pattern_id="bidirectional_pattern",
            pattern_direction=PatternDirection.BIDIRECTIONAL,
            expected_outcome_profile=profile,
        )
        library = _library((bidirectional,), self.case_hashes)
        excluded = _retrieve(self.state, library)
        self.assertIn(
            bidirectional.pattern_ref,
            excluded.retrieval_result.excluded_pattern_refs,
        )
        included = _retrieve(
            self.state,
            library,
            include_bidirectional_patterns=True,
        )
        self.assertEqual(
            included.retrieval_result.matches[0].pattern_ref,
            bidirectional.pattern_ref,
        )


class PatternRetrievalEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.packets,
            self.analyses,
            self.candidate,
            self.pattern,
            self.case_hashes,
        ) = _base_bundle()
        self.state = _matching_state()

    def test_concentration_and_weak_independence_are_penalized(self) -> None:
        metrics = replace(
            self.pattern.support_metrics,
            unique_symbol_count=1,
            unique_sector_count=1,
            independent_source_group_count=1,
            symbol_concentration=1.0,
            sector_concentration=1.0,
            evidence_independence_status=(
                EvidenceIndependenceStatus.CORRELATED
            ),
        )
        concentrated = _standalone_variant(
            self.pattern,
            pattern_id="concentrated_pattern",
            support_metrics=metrics,
            confidence=PatternConfidence.LOW,
            limitations=(
                "Symbol and sector concentration limit generalization.",
            ),
        )
        library = _library((concentrated,), self.case_hashes)
        match = _retrieve(
            self.state,
            library,
        ).retrieval_result.matches[0]
        profile = DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
        expected = (
            profile.penalties["one_symbol_concentration"]
            + profile.penalties["one_sector_concentration"]
            + profile.penalties["correlated_sources"]
        )
        self.assertEqual(match.concentration_penalty, expected)
        self.assertIn(
            MatchExplanationCode.CONCENTRATED_PATTERN_SUPPORT,
            match.explanation_codes,
        )
        self.assertIn(
            MatchExplanationCode.WEAK_SOURCE_INDEPENDENCE,
            match.explanation_codes,
        )

    def test_near_duplicate_overlap_keeps_one_deterministically(self) -> None:
        alpha = _standalone_variant(
            self.pattern,
            pattern_id="alpha_duplicate",
        )
        zeta = _standalone_variant(
            self.pattern,
            pattern_id="zeta_duplicate",
        )
        library = _library((zeta, alpha), self.case_hashes)
        retrieval = _retrieve(
            self.state,
            library,
        ).retrieval_result
        self.assertEqual(
            tuple(item.pattern_ref for item in retrieval.matches),
            (alpha.pattern_ref,),
        )
        self.assertEqual(
            retrieval.suppressed_pattern_refs,
            (zeta.pattern_ref,),
        )
        self.assertEqual(
            retrieval.exclusion_reasons[zeta.pattern_ref],
            (
                PatternExclusionCode
                .OVERLAP_NEAR_DUPLICATE_SUPPRESSED,
            ),
        )
        self.assertTrue(
            any(
                item.relationship.value == "near_duplicate"
                for item in retrieval.overlap_diagnostics
            )
        )

    def test_deprecated_pattern_is_excluded_unless_requested(self) -> None:
        deprecated = _standalone_variant(
            self.pattern,
            pattern_id="deprecated_pattern",
            pattern_status=PatternStatus.DEPRECATED,
        )
        library = _library((deprecated,), self.case_hashes)
        default = _retrieve(self.state, library).retrieval_result
        self.assertEqual(
            default.exclusion_reasons[deprecated.pattern_ref],
            (PatternExclusionCode.DEPRECATED_PATTERN,),
        )
        included = _retrieve(
            self.state,
            library,
            allowed_pattern_statuses=(
                PatternStatus.VALIDATED,
                PatternStatus.DEPRECATED,
            ),
        ).retrieval_result
        self.assertEqual(
            included.matches[0].pattern_ref,
            deprecated.pattern_ref,
        )

    def test_rejected_pattern_is_excluded_not_repaired(self) -> None:
        rejected = _standalone_variant(
            self.pattern,
            pattern_id="rejected_pattern",
            pattern_status=PatternStatus.REJECTED,
        )
        valid_library = _library((self.pattern,), self.case_hashes)
        library = _library_allowing_invalid_member(
            valid_library,
            (rejected,),
        )
        result = _retrieve(self.state, library)
        self.assertTrue(result.success)
        self.assertEqual(
            result.retrieval_result.exclusion_reasons[
                rejected.pattern_ref
            ],
            (PatternExclusionCode.REJECTED_PATTERN,),
        )

    def test_invalid_pattern_hash_is_excluded_not_repaired(self) -> None:
        valid = _standalone_variant(
            self.pattern,
            pattern_id="invalid_hash_pattern",
        )
        invalid = replace(valid, content_hash="0" * 64)
        valid_library = _library((self.pattern,), self.case_hashes)
        library = _library_allowing_invalid_member(
            valid_library,
            (invalid,),
        )
        result = _retrieve(self.state, library)
        self.assertTrue(result.success)
        self.assertEqual(
            result.retrieval_result.exclusion_reasons[
                invalid.pattern_ref
            ],
            (PatternExclusionCode.INVALID_PATTERN_HASH,),
        )
        self.assertEqual(invalid.content_hash, "0" * 64)

    def test_pattern_after_current_cutoff_is_excluded(self) -> None:
        future = _standalone_variant(
            self.pattern,
            pattern_id="future_pattern",
            applicable_cutoff="2027-01-01T00:00:00+00:00",
            generated_at="2027-01-02T00:00:00+00:00",
        )
        library = _library(
            (future,),
            self.case_hashes,
            clock=lambda: datetime(
                2027,
                1,
                3,
                tzinfo=timezone.utc,
            ),
        )
        result = _retrieve(self.state, library)
        self.assertEqual(
            result.retrieval_result.exclusion_reasons[
                future.pattern_ref
            ],
            (PatternExclusionCode.CUTOFF_INCOMPATIBILITY,),
        )

    def test_minimum_quality_gate_excludes_lower_quality_match(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        result = _retrieve(
            self.state,
            library,
            minimum_match_quality=MatchQuality.VERY_STRONG,
        )
        self.assertEqual(result.retrieval_result.matches, ())
        self.assertEqual(
            result.retrieval_result.exclusion_reasons[
                self.pattern.pattern_ref
            ],
            (PatternExclusionCode.BELOW_MINIMUM_QUALITY,),
        )

    def test_insufficient_current_data_has_separate_accounting(self) -> None:
        state = _insufficient_state(self.state)
        library = _library((self.pattern,), self.case_hashes)
        result = _retrieve(state, library)
        retrieval = result.retrieval_result
        self.assertEqual(retrieval.matches, ())
        self.assertEqual(
            retrieval.insufficient_data_pattern_refs,
            (self.pattern.pattern_ref,),
        )
        self.assertEqual(
            retrieval.exclusion_reasons[self.pattern.pattern_ref],
            (PatternExclusionCode.INSUFFICIENT_CURRENT_DATA,),
        )

    def test_fully_aligned_match_reaches_very_strong(self) -> None:
        state = _fully_aligned_state(self.state)
        library = _library((self.pattern,), self.case_hashes)
        match = _retrieve(state, library).retrieval_result.matches[0]
        self.assertEqual(match.match_quality, MatchQuality.VERY_STRONG)
        self.assertEqual(match.recommendation, MatchRecommendation.INCLUDE)
        self.assertGreaterEqual(match.total_score, 85.0)


class PatternRetrievalDeterminismTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.packets,
            self.analyses,
            self.candidate,
            self.pattern,
            self.case_hashes,
        ) = _base_bundle()
        self.state = _matching_state()

    def _tie_library(self) -> HistoricalPatternLibrary:
        alpha = _standalone_variant(
            self.pattern,
            pattern_id="alpha_tie",
        )
        changed_profile = replace(
            self.pattern.expected_outcome_profile,
            expected_volatility_change=PatternChange.EXPANDING,
        )
        zeta = _standalone_variant(
            self.pattern,
            pattern_id="zeta_tie",
            expected_outcome_profile=changed_profile,
        )
        return _library((zeta, alpha), self.case_hashes)

    def test_equal_scores_use_stable_pattern_id_tie_break(self) -> None:
        retrieval = _retrieve(
            self.state,
            self._tie_library(),
        ).retrieval_result
        self.assertEqual(
            tuple(item.pattern_id for item in retrieval.matches),
            ("alpha_tie", "zeta_tie"),
        )
        self.assertEqual(
            retrieval.matches[0].total_score,
            retrieval.matches[1].total_score,
        )

    def test_result_limit_is_applied_after_deterministic_ordering(self) -> None:
        library = self._tie_library()
        retrieval = _retrieve(
            self.state,
            library,
            requested_limit=1,
        ).retrieval_result
        self.assertEqual(
            tuple(item.pattern_id for item in retrieval.matches),
            ("alpha_tie",),
        )
        self.assertEqual(
            retrieval.exclusion_reasons["zeta_tie@1"],
            (PatternExclusionCode.RESULT_LIMIT,),
        )

    def test_repeated_runs_are_byte_stable(self) -> None:
        library = self._tie_library()
        query = _query(self.state, library)
        engine = PatternRetrievalEngine(clock=lambda: FIXED_NOW)
        first = engine.retrieve(self.state, library, query)
        second = engine.retrieve(self.state, library, query)
        self.assertEqual(first, second)
        self.assertEqual(
            first.content_hash,
            pattern_retrieval_execution_result_content_hash(first),
        )
        self.assertEqual(
            first.retrieval_result.content_hash,
            pattern_retrieval_result_content_hash(
                first.retrieval_result
            ),
        )

    def test_retrieval_does_not_mutate_state_library_or_patterns(self) -> None:
        library = self._tie_library()
        state_before = self.state.to_dict()
        library_before = library.to_dict()
        pattern_hashes = tuple(
            item.content_hash for item in library.patterns
        )
        result = _retrieve(self.state, library)
        self.assertTrue(result.success)
        self.assertEqual(self.state.to_dict(), state_before)
        self.assertEqual(library.to_dict(), library_before)
        self.assertEqual(
            tuple(item.content_hash for item in library.patterns),
            pattern_hashes,
        )

    def test_query_match_result_and_execution_round_trip(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        query = _query(self.state, library)
        execution = PatternRetrievalEngine(
            clock=lambda: FIXED_NOW
        ).retrieve(self.state, library, query)
        retrieval = execution.retrieval_result
        match = retrieval.matches[0]
        self.assertEqual(
            PatternRetrievalQuery.from_dict(query.to_dict()),
            query,
        )
        self.assertEqual(
            RetrievedPatternMatch.from_dict(match.to_dict()),
            match,
        )
        self.assertEqual(
            PatternRetrievalResult.from_dict(retrieval.to_dict()),
            retrieval,
        )
        self.assertEqual(
            PatternRetrievalExecutionResult.from_dict(
                execution.to_dict()
            ),
            execution,
        )

    def test_component_scores_reconcile_and_tampering_is_detected(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        match = _retrieve(
            self.state,
            library,
        ).retrieval_result.matches[0]
        self.assertTrue(
            validate_retrieved_pattern_match(
                match,
                pattern=self.pattern,
            ).is_valid
        )
        tampered = replace(
            match,
            total_score=match.total_score + 1.0,
            content_hash="",
        )
        tampered = replace(
            tampered,
            content_hash=retrieved_pattern_match_content_hash(
                tampered
            ),
        )
        validation = validate_retrieved_pattern_match(
            tampered,
            pattern=self.pattern,
        )
        self.assertFalse(validation.is_valid)
        self.assertIn(
            "match_score_reconciliation",
            validation.failed_rules,
        )

    def test_invalid_query_hash_returns_structured_failure(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        query = replace(
            _query(self.state, library),
            content_hash="bad",
        )
        result = PatternRetrievalEngine(
            clock=lambda: FIXED_NOW
        ).retrieve(self.state, library, query)
        self.assertFalse(result.success)
        self.assertIsNone(result.retrieval_result)
        self.assertIn("query_hash", result.validation_result.failed_rules)

    def test_state_hash_mismatch_returns_structured_failure(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        query = replace(
            _query(self.state, library),
            current_state_hash="f" * 64,
            content_hash="",
        )
        query = replace(
            query,
            content_hash=pattern_retrieval_query_content_hash(query),
        )
        self.assertFalse(
            validate_pattern_retrieval_query(
                query,
                state=self.state,
                library=library,
            ).is_valid
        )
        result = PatternRetrievalEngine(
            clock=lambda: FIXED_NOW
        ).retrieve(self.state, library, query)
        self.assertFalse(result.success)
        self.assertIn(
            "query_state_hash",
            result.validation_result.failed_rules,
        )

    def test_rank_gap_is_detected_by_result_validator(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        retrieval = _retrieve(
            self.state,
            library,
        ).retrieval_result
        match = replace(
            retrieval.matches[0],
            rank=2,
            content_hash="",
        )
        match = replace(
            match,
            content_hash=retrieved_pattern_match_content_hash(match),
        )
        tampered = replace(
            retrieval,
            matches=(match,),
            validation_result=ValidationResult.unvalidated(),
            content_hash="",
        )
        tampered = replace(
            tampered,
            content_hash=pattern_retrieval_result_content_hash(tampered),
        )
        validation = validate_pattern_retrieval_result(
            tampered,
            state=self.state,
            library=library,
        )
        self.assertFalse(validation.is_valid)
        self.assertIn("result_ranks", validation.failed_rules)

    def test_contracts_are_deeply_immutable(self) -> None:
        library = _library((self.pattern,), self.case_hashes)
        query = _query(self.state, library)
        retrieval = _retrieve(
            self.state,
            library,
        ).retrieval_result
        with self.assertRaises(FrozenInstanceError):
            query.requested_limit = 99
        with self.assertRaises(TypeError):
            DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE.component_weights[
                "exposure"
            ] = 0.0
        with self.assertRaises(FrozenInstanceError):
            retrieval.matches[0].rank = 3
