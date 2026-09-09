from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from elliott_ai.historical_market import (
    DurationBucket,
    HistoricalAnalystType,
    HistoricalCausalAnalysis,
    HistoricalEvidencePacket,
    HistoricalMarketMove,
    HistoricalMoveType,
    MagnitudeBucket,
    RelativePerformanceBucket,
    TransmissionMechanism,
    finalize_historical_causal_analysis,
)
from elliott_ai.historical_patterns import (
    DEFAULT_MINIMUM_PATTERN_CASES,
    HISTORICAL_PATTERN_CANDIDATE_SCHEMA_VERSION,
    HISTORICAL_PATTERN_LIBRARY_SCHEMA_VERSION,
    HISTORICAL_PATTERN_SCHEMA_VERSION,
    EvidenceIndependenceStatus,
    HistoricalPattern,
    HistoricalPatternCandidateBuildResult,
    HistoricalPatternCandidateBuilder,
    HistoricalPatternLibrary,
    HistoricalPatternLibraryBuildResult,
    HistoricalPatternLibraryBuilder,
    PatternChange,
    PatternConfidence,
    PatternDirection,
    PatternOutcomeProfile,
    PatternOverlapRelationship,
    PatternRecoveryProfile,
    PatternStatus,
    PatternTendency,
    analyze_pattern_overlaps,
    compute_pattern_support_metrics,
    finalize_historical_pattern,
    historical_pattern_content_hash,
    historical_pattern_library_content_hash,
    magnitude_bucket_for_move,
    pattern_candidate_content_hash,
    validate_historical_pattern,
    validate_historical_pattern_library,
    validate_pattern_candidate,
)
from elliott_ai.market_scenario import ExposureType, ScenarioDirection
from scripts.test_historical_market import (
    CUTOFF,
    _draft_causal,
    _features,
    _move,
    _packet,
)


FIXED_NOW = datetime(2020, 3, 1, tzinfo=timezone.utc)


def _case(
    index: int,
    *,
    direction: ScenarioDirection = ScenarioDirection.UP,
    move_type: HistoricalMoveType = HistoricalMoveType.UPWARD_IMPULSE,
    symbol: str | None = None,
    sector: str | None = None,
    source_group: str | None = None,
    end_price: float | None = None,
    mechanisms: tuple[TransmissionMechanism, ...] = (
        TransmissionMechanism.EARNINGS_REVISION,
    ),
) -> tuple[
    HistoricalEvidencePacket,
    HistoricalCausalAnalysis,
    object,
]:
    price = (
        end_price
        if end_price is not None
        else (
            145.0 + index * 2.0
            if direction is ScenarioDirection.UP
            else 55.0 - index * 2.0
        )
    )
    source_character = source_group or chr(96 + ((index - 1) % 20) + 1)
    base = _move(
        case_id=f"case-{index}",
        end_price=price,
        direction=direction,
        move_type=move_type,
        source_hashes={"ohlcv": source_character * 64},
    )
    raw_move = base.to_dict()
    raw_move.pop("content_hash")
    raw_move["symbol"] = symbol or f"NASDAQ:TEST{index}"
    raw_move["sector_symbol"] = sector or f"SECTOR:{index}"
    move = HistoricalMarketMove.create(**raw_move)
    packet = _packet(move=move)
    draft_analysis = replace(
        _draft_causal(mechanisms=mechanisms),
        case_id=move.case_id,
    )
    analysis = finalize_historical_causal_analysis(
        draft_analysis,
        packet=packet,
    )
    feature = _features(
        case_id=move.case_id,
        direction=direction,
        magnitude_bucket=magnitude_bucket_for_move(packet),
        transmission_mechanisms=mechanisms,
    )
    return packet, analysis, feature


def _bundle(
    count: int = 3,
    **case_kwargs,
) -> tuple[
    tuple[HistoricalEvidencePacket, ...],
    tuple[HistoricalCausalAnalysis, ...],
    tuple[object, ...],
]:
    values = [_case(index, **case_kwargs) for index in range(1, count + 1)]
    return (
        tuple(item[0] for item in values),
        tuple(item[1] for item in values),
        tuple(item[2] for item in values),
    )


def _candidate_bundle(
    count: int = 3,
    **case_kwargs,
):
    packets, analyses, features = _bundle(count, **case_kwargs)
    result = HistoricalPatternCandidateBuilder(
        clock=lambda: FIXED_NOW
    ).build(packets, analyses, features)
    if not result.candidates:
        raise AssertionError("Fixture did not produce a candidate.")
    return packets, analyses, features, result.candidates[0], result


def _profile(
    packets: tuple[HistoricalEvidencePacket, ...],
) -> PatternOutcomeProfile:
    directions = {
        packet.historical_move.direction for packet in packets
    }
    direction = (
        PatternDirection.BULLISH
        if directions == {ScenarioDirection.UP}
        else (
            PatternDirection.BEARISH
            if directions == {ScenarioDirection.DOWN}
            else PatternDirection.BIDIRECTIONAL
        )
    )
    magnitude_values = {
        magnitude_bucket_for_move(packet) for packet in packets
    }
    magnitude = (
        next(iter(magnitude_values))
        if len(magnitude_values) == 1
        else MagnitudeBucket.UNAVAILABLE
    )
    return PatternOutcomeProfile(
        expected_move_types=tuple(
            sorted(
                {
                    packet.historical_move.move_type
                    for packet in packets
                },
                key=lambda item: item.value,
            )
        ),
        expected_direction=direction,
        magnitude_bucket=magnitude,
        duration_bucket=DurationBucket.WEEKS,
        expected_volatility_change=PatternChange.UNAVAILABLE,
        expected_volume_change=PatternChange.UNAVAILABLE,
        expected_benchmark_relative_behavior=(
            RelativePerformanceBucket.UNAVAILABLE
        ),
        expected_sector_relative_behavior=(
            RelativePerformanceBucket.UNAVAILABLE
        ),
        continuation_tendency=PatternTendency.CONTEXT_DEPENDENT,
        reversal_tendency=PatternTendency.CONTEXT_DEPENDENT,
        typical_recovery_profile=PatternRecoveryProfile.UNKNOWN,
        uncertainty_notes=(
            "Recovery evidence was not consistently available.",
        ),
    )


def _pattern(
    packets: tuple[HistoricalEvidencePacket, ...],
    analyses: tuple[HistoricalCausalAnalysis, ...],
    candidate,
    *,
    pattern_id: str = "earnings_revision_pattern",
    pattern_version: int = 1,
    supporting_case_ids: tuple[str, ...] | None = None,
    contradicting_case_ids: tuple[str, ...] = (),
    exception_case_ids: tuple[str, ...] = (),
    status: PatternStatus = PatternStatus.VALIDATED,
    confidence: PatternConfidence = PatternConfidence.MODERATE,
    trigger_event_types: tuple[str, ...] = ("earnings",),
    no_single_trigger: bool = False,
    limitations: tuple[str, ...] = (
        "The short sample limits regime and temporal generalization.",
    ),
    description: str = (
        "Earnings revision and valuation transmission may interact across "
        "historical cases."
    ),
    replacement_pattern_ref: str | None = None,
) -> HistoricalPattern:
    packet_map = {
        packet.historical_move.case_id: packet for packet in packets
    }
    analysis_map = {analysis.case_id: analysis for analysis in analyses}
    support = supporting_case_ids or tuple(
        sorted(packet_map)
    )
    support_packets = tuple(packet_map[case_id] for case_id in support)
    support_analyses = tuple(analysis_map[case_id] for case_id in support)
    all_ids = set(support) | set(contradicting_case_ids) | set(
        exception_case_ids
    )
    shared_regime = candidate.shared_market_regime_features
    draft = HistoricalPattern(
        pattern_id=pattern_id,
        pattern_version=pattern_version,
        name="Earnings revision transmission",
        description=description,
        invariant_features=(
            "Earnings-revision transmission appeared in every support case.",
        ),
        common_features=(
            "Revenue-growth exposure appeared across the support set.",
        ),
        optional_features=(
            "Positioning amplification appeared in only some records.",
        ),
        disqualifying_features=(
            "Independent evidence that offsets revisions weakens applicability.",
        ),
        pattern_status=status,
        pattern_direction=_profile(support_packets).expected_direction,
        applicable_move_types=_profile(
            support_packets
        ).expected_move_types,
        initiating_exposure_types=(
            ExposureType.REVENUE_GROWTH_RISK,
        ),
        initiating_conditions=(
            "Revenue expectations were already changing before repricing.",
        ),
        trigger_event_types=trigger_event_types,
        no_single_trigger=no_single_trigger,
        transmission_mechanisms=(
            TransmissionMechanism.EARNINGS_REVISION,
        ),
        amplifying_exposure_types=(),
        amplifiers=(
            "Positioning and risk appetite may have amplified the move.",
        ),
        dampening_exposure_types=(),
        dampeners=("Contrary valuation evidence may have slowed repricing.",),
        required_market_regimes=(
            tuple(shared_regime[:1]) if shared_regime else ()
        ),
        optional_market_regimes=(),
        incompatible_market_regimes=(),
        failure_conditions=(
            "The hypothesis may fail when revisions are offset by stronger "
            "independent cash-flow evidence.",
        ),
        expected_outcome_profile=_profile(support_packets),
        supporting_case_ids=support,
        contradicting_case_ids=contradicting_case_ids,
        exception_case_ids=exception_case_ids,
        no_contradicting_cases_explanation=(
            ""
            if contradicting_case_ids
            else "No contradicting case was available in this bounded candidate."
        ),
        source_analysis_hashes={
            case_id: analysis_map[case_id].content_hash
            for case_id in all_ids
        },
        support_metrics=compute_pattern_support_metrics(
            support_packets,
            support_analyses,
            contradicting_case_count=len(contradicting_case_ids),
            exception_case_count=len(exception_case_ids),
        ),
        confidence=confidence,
        limitations=limitations,
        missing_evidence=(
            "Independent positioning history was incomplete.",
        ),
        analyst_type=HistoricalAnalystType.DETERMINISTIC,
        model_name="fixture-pattern-analyst",
        prompt_version="fixture-pattern-prompt-1.0.0",
        generated_at=FIXED_NOW,
        applicable_cutoff=CUTOFF,
        replacement_pattern_ref=replacement_pattern_ref,
    )
    return finalize_historical_pattern(
        draft,
        candidate=candidate,
        packets=packets,
        analyses=analyses,
    )


class PatternCandidateBuilderTests(unittest.TestCase):
    def test_valid_deterministic_candidate_generation(self) -> None:
        packets, analyses, _, candidate, result = _candidate_bundle()
        self.assertTrue(result.success)
        self.assertTrue(candidate.validation_result.is_valid)
        self.assertEqual(len(candidate.case_ids), 3)
        self.assertEqual(
            candidate.content_hash,
            pattern_candidate_content_hash(candidate),
        )
        self.assertEqual(
            candidate.schema_version,
            HISTORICAL_PATTERN_CANDIDATE_SCHEMA_VERSION,
        )
        self.assertTrue(
            validate_pattern_candidate(
                candidate,
                packets=packets,
                analyses=analyses,
            ).is_valid
        )

    def test_default_minimum_prevents_one_case_pattern(self) -> None:
        packets, analyses, features = _bundle(1)
        result = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(packets, analyses, features)
        self.assertTrue(result.success)
        self.assertEqual(result.candidates, ())

    def test_invalid_packet_is_rejected(self) -> None:
        packets, analyses, features = _bundle()
        invalid = replace(packets[0], content_hash="bad")
        result = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (invalid,) + packets[1:],
            analyses,
            features,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.candidates, ())

    def test_duplicate_case_id_is_rejected(self) -> None:
        packets, analyses, features = _bundle()
        result = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            packets + (packets[0],),
            analyses,
            features,
        )
        self.assertFalse(result.success)

    def test_duplicate_historical_move_is_excluded(self) -> None:
        first = _case(1, symbol="NASDAQ:SAME", end_price=150.0)
        second = _case(2, symbol="NASDAQ:SAME", end_price=150.0)
        third = _case(3)
        fourth = _case(4)
        values = (first, second, third, fourth)
        result = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            tuple(item[0] for item in values),
            tuple(item[1] for item in values),
            tuple(item[2] for item in values),
        )
        self.assertTrue(result.success)
        self.assertEqual(len(result.rejected_case_ids), 1)
        self.assertTrue(
            all(
                "duplicate_historical_move_excluded"
                in candidate.candidate_warnings
                for candidate in result.candidates
            )
        )

    def test_one_symbol_and_sector_concentration_warnings(self) -> None:
        packets, analyses, features = _bundle(
            3,
            symbol="NASDAQ:SAME",
            sector="SECTOR:SAME",
        )
        result = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(packets, analyses, features)
        candidate = result.candidates[0]
        self.assertIn(
            "one_symbol_concentration",
            candidate.candidate_warnings,
        )
        self.assertIn(
            "one_sector_concentration",
            candidate.candidate_warnings,
        )

    def test_overlapping_source_group_warning(self) -> None:
        packets, analyses, features = _bundle(3, source_group="z")
        result = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(packets, analyses, features)
        self.assertIn(
            "overlapping_source_groups",
            result.candidates[0].candidate_warnings,
        )
        self.assertIs(
            result.candidates[
                0
            ].candidate_support_metrics.evidence_independence_status,
            EvidenceIndependenceStatus.CORRELATED,
        )
        self.assertEqual(
            result.candidates[
                0
            ].candidate_support_metrics.independent_source_group_count,
            1,
        )

    def test_partial_source_overlap_uses_connected_source_groups(self) -> None:
        values = (
            _case(1, source_group="z"),
            _case(2, source_group="z"),
            _case(3, source_group="y"),
        )
        metrics = compute_pattern_support_metrics(
            tuple(item[0] for item in values),
            tuple(item[1] for item in values),
        )
        self.assertEqual(metrics.independent_source_group_count, 2)
        self.assertIs(
            metrics.evidence_independence_status,
            EvidenceIndependenceStatus.MIXED,
        )

    def test_candidate_build_is_deterministic(self) -> None:
        packets, analyses, features = _bundle()
        first = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(packets, analyses, features)
        second = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            tuple(reversed(packets)),
            tuple(reversed(analyses)),
            tuple(reversed(features)),
        )
        self.assertEqual(first, second)
        restored = HistoricalPatternCandidateBuildResult.from_dict(
            first.to_dict()
        )
        self.assertEqual(restored, first)


class HistoricalPatternValidationTests(unittest.TestCase):
    def test_valid_bullish_impulse_pattern(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        pattern = _pattern(packets, analyses, candidate)
        self.assertTrue(pattern.validation_result.is_valid)
        self.assertEqual(
            pattern.schema_version,
            HISTORICAL_PATTERN_SCHEMA_VERSION,
        )

    def test_valid_bearish_crash_pattern(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle(
            direction=ScenarioDirection.DOWN,
            move_type=HistoricalMoveType.CRASH,
        )
        pattern = _pattern(packets, analyses, candidate)
        self.assertTrue(pattern.validation_result.is_valid)
        self.assertIs(pattern.pattern_direction, PatternDirection.BEARISH)

    def test_valid_no_single_trigger_pattern(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        pattern = _pattern(
            packets,
            analyses,
            candidate,
            trigger_event_types=(),
            no_single_trigger=True,
        )
        self.assertTrue(pattern.validation_result.is_valid)

    def test_contradicting_and_exception_cases_are_preserved(self) -> None:
        values = [
            _case(index) for index in range(1, 4)
        ] + [
            _case(
                4,
                direction=ScenarioDirection.DOWN,
                move_type=HistoricalMoveType.CRASH,
            ),
            _case(5),
        ]
        packets = tuple(item[0] for item in values)
        analyses = tuple(item[1] for item in values)
        features = tuple(item[2] for item in values)
        build = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(packets, analyses, features)
        candidate = next(
            item for item in build.candidates if len(item.case_ids) == 5
        )
        pattern = _pattern(
            packets,
            analyses,
            candidate,
            supporting_case_ids=("case-1", "case-2", "case-3"),
            contradicting_case_ids=("case-4",),
            exception_case_ids=("case-5",),
        )
        self.assertTrue(pattern.validation_result.is_valid)
        self.assertEqual(
            set(
                pattern.supporting_case_ids
                + pattern.contradicting_case_ids
                + pattern.exception_case_ids
            ),
            set(candidate.case_ids),
        )

    def test_below_minimum_support_is_invalid(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        pattern = _pattern(packets, analyses, candidate)
        invalid = finalize_historical_pattern(
            replace(
                pattern,
                supporting_case_ids=("case-1", "case-2"),
                exception_case_ids=("case-3",),
                support_metrics=compute_pattern_support_metrics(
                    packets[:2],
                    analyses[:2],
                    exception_case_count=1,
                ),
            ),
            candidate=candidate,
            packets=packets,
            analyses=analyses,
        )
        self.assertIn(
            "pattern_case_support",
            invalid.validation_result.failed_rules,
        )

    def test_missing_limitations_is_invalid(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        pattern = _pattern(packets, analyses, candidate)
        invalid = finalize_historical_pattern(
            replace(pattern, limitations=()),
            candidate=candidate,
            packets=packets,
            analyses=analyses,
        )
        self.assertIn(
            "pattern_falsifiability",
            invalid.validation_result.failed_rules,
        )

    def test_unsupported_certainty_is_invalid(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        pattern = _pattern(
            packets,
            analyses,
            candidate,
            description="Earnings definitely caused every historical move.",
        )
        self.assertIn(
            "pattern_certainty_language",
            pattern.validation_result.failed_rules,
        )

    def test_circular_pattern_is_invalid(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle(
            direction=ScenarioDirection.DOWN,
            move_type=HistoricalMoveType.CRASH,
        )
        pattern = _pattern(
            packets,
            analyses,
            candidate,
            description="Bad news causes crashes.",
        )
        self.assertIn(
            "pattern_causal_law_language",
            pattern.validation_result.failed_rules,
        )

    def test_candidate_case_omission_is_invalid(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        pattern = _pattern(packets, analyses, candidate)
        invalid = finalize_historical_pattern(
            replace(
                pattern,
                supporting_case_ids=("case-1", "case-2"),
                support_metrics=compute_pattern_support_metrics(
                    packets[:2],
                    analyses[:2],
                ),
                source_analysis_hashes={
                    "case-1": analyses[0].content_hash,
                    "case-2": analyses[1].content_hash,
                },
            ),
            candidate=candidate,
            packets=packets,
            analyses=analyses,
            minimum_supporting_cases=2,
        )
        self.assertIn(
            "pattern_candidate_accounting",
            invalid.validation_result.failed_rules,
        )

    def test_one_symbol_general_pattern_requires_low_confidence_disclosure(
        self,
    ) -> None:
        packets, analyses, features = _bundle(
            3,
            symbol="NASDAQ:SAME",
        )
        build = HistoricalPatternCandidateBuilder(
            clock=lambda: FIXED_NOW
        ).build(packets, analyses, features)
        candidate = build.candidates[0]
        pattern = _pattern(
            packets,
            analyses,
            candidate,
            confidence=PatternConfidence.MODERATE,
            limitations=("The sample is temporally narrow.",),
        )
        self.assertIn(
            "pattern_concentration",
            pattern.validation_result.failed_rules,
        )

    def test_pattern_round_trip_and_hash(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        pattern = _pattern(packets, analyses, candidate)
        restored = HistoricalPattern.from_dict(pattern.to_dict())
        self.assertEqual(restored, pattern)
        self.assertEqual(
            pattern.content_hash,
            historical_pattern_content_hash(pattern),
        )


class HistoricalPatternLibraryTests(unittest.TestCase):
    def _fixture(self):
        packets, analyses, _, candidate, _ = _candidate_bundle()
        first = _pattern(
            packets,
            analyses,
            candidate,
            pattern_id="zeta_pattern",
        )
        second = _pattern(
            packets,
            analyses,
            candidate,
            pattern_id="alpha_pattern",
        )
        case_hashes = {
            packet.historical_move.case_id: packet.content_hash
            for packet in packets
        }
        return packets, analyses, candidate, first, second, case_hashes

    def test_unreviewed_extracted_pattern_cannot_enter_library(self) -> None:
        packets, analyses, candidate, _, _, case_hashes = self._fixture()
        extracted = _pattern(
            packets,
            analyses,
            candidate,
            status=PatternStatus.EXTRACTED,
        )
        result = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (extracted,),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
        )
        self.assertFalse(result.success)
        self.assertEqual(
            result.rejected_pattern_refs,
            (extracted.pattern_ref,),
        )

    def test_deterministic_library_ordering_and_hash(self) -> None:
        _, _, _, first, second, case_hashes = self._fixture()
        result = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (first, second),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
        )
        self.assertTrue(result.success)
        self.assertEqual(
            tuple(item.pattern_id for item in result.library.patterns),
            ("alpha_pattern", "zeta_pattern"),
        )
        self.assertEqual(
            result.library.library_schema_version,
            HISTORICAL_PATTERN_LIBRARY_SCHEMA_VERSION,
        )
        self.assertEqual(
            result.library.content_hash,
            historical_pattern_library_content_hash(result.library),
        )

    def test_duplicate_pattern_reference_is_rejected(self) -> None:
        _, _, _, first, _, case_hashes = self._fixture()
        result = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (first, first),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.library)

    def test_new_version_preserves_existing_version(self) -> None:
        packets, analyses, candidate, first, _, case_hashes = self._fixture()
        initial = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (first,),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
        ).library
        second_version = _pattern(
            packets,
            analyses,
            candidate,
            pattern_id=first.pattern_id,
            pattern_version=2,
        )
        updated = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (second_version,),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
            existing_library=initial,
        )
        self.assertTrue(updated.success)
        self.assertEqual(
            tuple(item.pattern_version for item in updated.library.patterns),
            (1, 2),
        )
        self.assertEqual(updated.library.patterns[0], first)

    def test_deprecation_does_not_overwrite_history(self) -> None:
        packets, analyses, candidate, first, second, case_hashes = (
            self._fixture()
        )
        deprecated = _pattern(
            packets,
            analyses,
            candidate,
            pattern_id=first.pattern_id,
            pattern_version=2,
            status=PatternStatus.DEPRECATED,
            replacement_pattern_ref=second.pattern_ref,
        )
        result = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (first, deprecated, second),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
        )
        self.assertTrue(result.success)
        self.assertIn(
            deprecated.pattern_ref,
            result.library.deprecated_pattern_refs,
        )
        self.assertIn(first, result.library.patterns)

    def test_overlap_diagnostics_do_not_merge(self) -> None:
        _, _, _, first, second, case_hashes = self._fixture()
        result = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (first, second),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
        )
        self.assertEqual(len(result.library.patterns), 2)
        self.assertEqual(len(result.overlap_diagnostics), 1)
        self.assertIn(
            result.overlap_diagnostics[0].relationship,
            {
                PatternOverlapRelationship.NEAR_DUPLICATE,
                PatternOverlapRelationship.PARENT_CHILD,
            },
        )

    def test_library_serialization_round_trip(self) -> None:
        _, _, _, first, second, case_hashes = self._fixture()
        result = HistoricalPatternLibraryBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            (first, second),
            library_id="main_pattern_library",
            source_case_hashes=case_hashes,
        )
        restored_library = HistoricalPatternLibrary.from_dict(
            result.library.to_dict()
        )
        restored_result = HistoricalPatternLibraryBuildResult.from_dict(
            result.to_dict()
        )
        self.assertEqual(restored_library, result.library)
        self.assertEqual(restored_result, result)
        self.assertTrue(
            validate_historical_pattern_library(restored_library).is_valid
        )


if __name__ == "__main__":
    unittest.main()
