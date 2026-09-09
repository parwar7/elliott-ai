from __future__ import annotations

import json
import math
import unittest
from dataclasses import FrozenInstanceError, replace

from elliott_ai.historical_market import (
    HISTORICAL_CASE_SCHEMA_VERSION,
    HISTORICAL_CASE_VALIDATION_VERSION,
    HISTORICAL_CAUSAL_LEGACY_SCHEMA_VERSIONS,
    HISTORICAL_CAUSAL_SCHEMA_VERSION,
    HISTORICAL_SIMILARITY_SCHEMA_VERSION,
    DurationBucket,
    HistoricalAnalystType,
    HistoricalCaseExtractor,
    HistoricalCaseStatus,
    HistoricalCausalAnalysis,
    HistoricalEvidenceItem,
    HistoricalEvidencePacket,
    HistoricalEventRole,
    HistoricalMarketMove,
    HistoricalMaterialEvent,
    HistoricalMoveType,
    HistoricalSimilarityFeatures,
    HistoricalTemporalClassification,
    MagnitudeBucket,
    MarketRegime,
    MarketRegimeState,
    RelativePerformanceBucket,
    TransmissionMechanism,
    finalize_historical_causal_analysis,
    finalize_historical_evidence_packet,
    historical_causal_analysis_content_hash,
    historical_evidence_packet_content_hash,
    historical_market_move_content_hash,
    historical_similarity_features_content_hash,
    market_regime_content_hash,
    validate_historical_causal_analysis,
    validate_historical_evidence_packet,
    validate_historical_market_move,
    validate_historical_similarity_features,
)
from elliott_ai.market_scenario import (
    EvidenceImplication,
    EvidenceItem,
    EvidenceStatus,
    EventDatePrecision,
    ExposureCategory,
    ExposureItem,
    ExposureType,
    MaterialEvent,
    ScenarioDirection,
)


START = "2020-01-01T00:00:00+00:00"
END = "2020-01-11T00:00:00+00:00"
CUTOFF = "2020-02-01T00:00:00+00:00"
GENERATED = "2020-02-02T00:00:00+00:00"


def _move(
    *,
    case_id: str = "case-up",
    start_at: str = START,
    end_at: str = END,
    start_price: float = 100.0,
    end_price: float = 150.0,
    percentage_move: float | None = None,
    logarithmic_move: float | None = None,
    duration_days: float = 10.0,
    direction: ScenarioDirection = ScenarioDirection.UP,
    move_type: HistoricalMoveType = HistoricalMoveType.UPWARD_IMPULSE,
    source_hashes: dict[str, str] | None = None,
) -> HistoricalMarketMove:
    percentage = (
        percentage_move
        if percentage_move is not None
        else (end_price / start_price - 1.0) * 100.0
    )
    logarithmic = (
        logarithmic_move
        if logarithmic_move is not None
        else math.log(end_price / start_price)
    )
    return HistoricalMarketMove.create(
        case_id=case_id,
        symbol="NASDAQ:TEST",
        exchange="NASDAQ",
        move_type=move_type,
        direction=direction,
        start_at=start_at,
        end_at=end_at,
        start_price=start_price,
        end_price=end_price,
        percentage_move=percentage,
        logarithmic_move=logarithmic,
        duration_days=duration_days,
        peak_to_trough_or_trough_to_peak=abs(percentage),
        market_data_cutoff=CUTOFF,
        source_hashes=(
            source_hashes
            if source_hashes is not None
            else {"ohlcv": "a" * 64}
        ),
        case_status=HistoricalCaseStatus.VALIDATED,
        technical_structure={"structure": "five_wave_impulse"},
        technical_confidence={"structure": "moderate"},
        notes=("Deterministic test fixture.",),
    )


def _evidence(
    evidence_id: str,
    *,
    publication_date: str,
    applicable_cutoff: str,
    status: EvidenceStatus = EvidenceStatus.CONFIRMED,
    source: str | None = None,
    source_type: str = "issuer_filing",
    related_event_id: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        category="revenue_growth_risk",
        claim=f"Historical evidence claim for {evidence_id}.",
        source=source if source is not None else f"https://example.test/{evidence_id}",
        source_type=source_type,
        provider="Example Provider",
        publication_date=publication_date,
        retrieval_timestamp=GENERATED,
        applicable_cutoff=applicable_cutoff,
        status=status,
        implication=EvidenceImplication.SUPPORTIVE,
        confidence="moderate",
        relevance_to_technical_scenario="Historical contextual evidence.",
        related_event_id=related_event_id,
    )


def _historical_evidence() -> tuple[HistoricalEvidenceItem, ...]:
    return (
        HistoricalEvidenceItem(
            evidence=_evidence(
                "ev-before",
                publication_date="2019-12-15",
                applicable_cutoff=START,
                related_event_id="event-earnings",
            ),
            temporal_classification=(
                HistoricalTemporalClassification.KNOWN_BEFORE_MOVE
            ),
        ),
        HistoricalEvidenceItem(
            evidence=_evidence(
                "ev-during",
                publication_date="2020-01-05",
                applicable_cutoff="2020-01-05T23:59:00+00:00",
            ),
            temporal_classification=(
                HistoricalTemporalClassification.PUBLISHED_DURING_MOVE
            ),
        ),
        HistoricalEvidenceItem(
            evidence=_evidence(
                "ev-after",
                publication_date="2020-01-20",
                applicable_cutoff="2020-01-20T23:59:00+00:00",
            ),
            temporal_classification=(
                HistoricalTemporalClassification.KNOWN_ONLY_AFTER_MOVE
            ),
        ),
        HistoricalEvidenceItem(
            evidence=_evidence(
                "ev-retrospective",
                publication_date="2020-01-21",
                applicable_cutoff="2020-01-21T23:59:00+00:00",
                status=EvidenceStatus.INTERPRETATION,
                source_type="retrospective_research",
            ),
            temporal_classification=(
                HistoricalTemporalClassification.RETROSPECTIVE_INTERPRETATION
            ),
        ),
    )


def _exposure(
    exposure_id: str,
    evidence_id: str,
) -> ExposureItem:
    return ExposureItem(
        exposure_id=exposure_id,
        category=ExposureCategory.COMPANY,
        exposure_type=ExposureType.REVENUE_GROWTH_RISK,
        summary="Historical revenue-growth exposure.",
        materiality="unknown",
        direction="negative",
        possible_magnitude=None,
        time_horizon=None,
        status=EvidenceStatus.CONFIRMED,
        supporting_evidence_ids=(evidence_id,),
        contradicting_evidence_ids=(),
        related_technical_outcomes=(),
        confidence="moderate",
    )


def _event(
    *,
    event_id: str = "event-earnings",
    scheduled_at: str = "2020-01-05T20:00:00+00:00",
    role: HistoricalEventRole = HistoricalEventRole.INITIATING_CATALYST,
    lag_reasoning: str = "",
) -> HistoricalMaterialEvent:
    return HistoricalMaterialEvent(
        event=MaterialEvent(
            event_id=event_id,
            event_type="earnings",
            title="Historical earnings event",
            scheduled_at=scheduled_at,
            date_precision=EventDatePrecision.EXACT,
            status=EvidenceStatus.CONFIRMED,
            source="https://example.test/event",
            source_type="issuer_calendar",
            publication_date="2019-12-20",
            retrieval_timestamp=GENERATED,
            applicable_cutoff=START,
            related_evidence_ids=("ev-before",),
        ),
        role=role,
        lag_reasoning=lag_reasoning,
    )


def _regime(
    *,
    supporting_evidence_ids: tuple[str, ...] = ("ev-before",),
) -> MarketRegime:
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
        regime_start_at="2019-10-01T00:00:00+00:00",
        regime_end_at="2020-01-31T00:00:00+00:00",
        supporting_evidence_ids=supporting_evidence_ids,
        confidence="moderate",
    )


def _draft_packet(
    *,
    move: HistoricalMarketMove | None = None,
    evidence: tuple[HistoricalEvidenceItem, ...] | None = None,
    exposures_before_move: tuple[ExposureItem, ...] | None = None,
    exposures_during_move: tuple[ExposureItem, ...] | None = None,
    material_events: tuple[HistoricalMaterialEvent, ...] | None = None,
    regime: MarketRegime | None = None,
) -> HistoricalEvidencePacket:
    return HistoricalEvidencePacket(
        historical_move=move or _move(),
        evidence=evidence if evidence is not None else _historical_evidence(),
        exposures_before_move=(
            exposures_before_move
            if exposures_before_move is not None
            else (_exposure("exposure-before", "ev-before"),)
        ),
        exposures_during_move=(
            exposures_during_move
            if exposures_during_move is not None
            else (_exposure("exposure-during", "ev-during"),)
        ),
        material_events=(
            material_events if material_events is not None else (_event(),)
        ),
        market_regime=regime or _regime(),
        unresolved_unknowns=("Intraday positioning detail was unavailable.",),
        extraction_metadata={
            "extractor_id": "fixture-extractor",
            "extracted_at": GENERATED,
            "mode": "historical_cutoff",
        },
    )


def _packet(**kwargs) -> HistoricalEvidencePacket:
    return finalize_historical_evidence_packet(_draft_packet(**kwargs))


def _draft_causal(
    *,
    primary_drivers: tuple[str, ...] = (
        "Earnings revisions may have reduced expected cash flows.",
    ),
    secondary_drivers: tuple[str, ...] = (
        "Multiple expansion amplified the move.",
    ),
    mechanisms: tuple[TransmissionMechanism, ...] = (
        TransmissionMechanism.EARNINGS_REVISION,
        TransmissionMechanism.MULTIPLE_EXPANSION,
    ),
    contradicting_evidence_ids: tuple[str, ...] = ("ev-after",),
    triggering_events: tuple[str, ...] = ("event-earnings",),
) -> HistoricalCausalAnalysis:
    return HistoricalCausalAnalysis(
        case_id="case-up",
        initiating_conditions=("Revenue expectations were changing.",),
        primary_drivers=primary_drivers,
        secondary_drivers=secondary_drivers,
        amplifiers=("Risk appetite strengthened.",),
        dampeners=("Valuation remained elevated.",),
        triggering_events=triggering_events,
        transmission_mechanisms=mechanisms,
        exposure_interactions=("Earnings and valuation mechanisms interacted.",),
        market_regime_contribution="Liquidity may have amplified repricing.",
        company_specific_contribution="Earnings revisions were relevant.",
        sector_contribution="Sector conditions were supportive.",
        macro_contribution="Macro conditions were broadly stable.",
        positioning_contribution="Positioning may have accelerated the move.",
        no_single_catalyst=True,
        alternative_explanations=("Broad market beta could explain part of the move.",),
        contradicting_evidence_ids=contradicting_evidence_ids,
        missing_evidence=("Complete historical positioning was unavailable.",),
        confidence="moderate",
        analyst_type=HistoricalAnalystType.HUMAN,
        model_name="human-review",
        prompt_version="historical-causal-fixture-1.0.0",
        generated_at=GENERATED,
        applicable_cutoff=CUTOFF,
        supporting_evidence_ids=(
            "ev-before",
            "ev-during",
            "ev-after",
            "ev-retrospective",
        ),
        initiating_condition_evidence_ids=("ev-before",),
        retrospective_explanations=(
            "Later reporting provides retrospective context.",
        ),
        retrospective_evidence_ids=("ev-after", "ev-retrospective"),
    )


def _features(**overrides) -> HistoricalSimilarityFeatures:
    values = {
        "case_id": "case-up",
        "direction": ScenarioDirection.UP,
        "magnitude_bucket": MagnitudeBucket.FROM_40_TO_60_PERCENT,
        "duration_bucket": DurationBucket.WEEKS,
        "volatility_regime": MarketRegimeState.NORMAL,
        "volume_regime": MarketRegimeState.EXPANDING,
        "valuation_state": MarketRegimeState.HIGH,
        "liquidity_state": MarketRegimeState.EXPANDING,
        "positioning_state": MarketRegimeState.CROWDED,
        "company_exposure_types": (ExposureType.REVENUE_GROWTH_RISK,),
        "sector_exposure_types": (ExposureType.SECTOR_DEMAND_RISK,),
        "macro_exposure_types": (ExposureType.LIQUIDITY_CONDITIONS,),
        "event_types": ("earnings",),
        "transmission_mechanisms": (
            TransmissionMechanism.EARNINGS_REVISION,
        ),
        "technical_structure": "five_wave_impulse",
        "benchmark_relative_performance": (
            RelativePerformanceBucket.OUTPERFORMANCE
        ),
        "sector_relative_performance": RelativePerformanceBucket.IN_LINE,
    }
    values.update(overrides)
    return HistoricalSimilarityFeatures.create(**values)


class HistoricalMoveTests(unittest.TestCase):
    def test_valid_upward_impulse_case(self) -> None:
        move = _move()
        validation = validate_historical_market_move(move)
        self.assertTrue(validation.is_valid)
        self.assertEqual(validation.errors, ())
        self.assertEqual(
            move.content_hash,
            historical_market_move_content_hash(move),
        )

    def test_valid_crash_case(self) -> None:
        move = _move(
            case_id="case-crash",
            end_price=50.0,
            direction=ScenarioDirection.DOWN,
            move_type=HistoricalMoveType.CRASH,
        )
        self.assertTrue(validate_historical_market_move(move).is_valid)
        self.assertEqual(move.percentage_move, -50.0)

    def test_move_object_is_deeply_immutable(self) -> None:
        move = _move()
        with self.assertRaises(FrozenInstanceError):
            move.symbol = "CHANGED"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            move.source_hashes["new"] = "b" * 64  # type: ignore[index]
        with self.assertRaises(TypeError):
            move.technical_structure["structure"] = "changed"  # type: ignore[index]

    def test_percentage_inconsistency_is_structured(self) -> None:
        move = _move(percentage_move=49.0)
        validation = validate_historical_market_move(move)
        self.assertFalse(validation.is_valid)
        self.assertIn(
            "percentage_move_inconsistency",
            validation.failed_rules,
        )

    def test_logarithmic_inconsistency_is_structured(self) -> None:
        move = _move(logarithmic_move=0.5)
        validation = validate_historical_market_move(move)
        self.assertIn(
            "logarithmic_move_inconsistency",
            validation.failed_rules,
        )

    def test_direction_mismatch_is_structured(self) -> None:
        move = _move(direction=ScenarioDirection.DOWN)
        validation = validate_historical_market_move(move)
        self.assertIn("direction_mismatch", validation.failed_rules)

    def test_invalid_dates_and_duration_are_structured(self) -> None:
        move = _move(
            start_at=END,
            end_at=START,
            duration_days=-10.0,
        )
        validation = validate_historical_market_move(move)
        self.assertIn("invalid_move_dates", validation.failed_rules)
        self.assertIn("invalid_duration", validation.failed_rules)

    def test_invalid_prices_are_structured(self) -> None:
        move = HistoricalMarketMove.create(
            case_id="case-invalid-price",
            symbol="NASDAQ:TEST",
            exchange="NASDAQ",
            move_type=HistoricalMoveType.CRASH,
            direction=ScenarioDirection.DOWN,
            start_at=START,
            end_at=END,
            start_price=-1.0,
            end_price=1.0,
            percentage_move=-200.0,
            logarithmic_move=0.0,
            duration_days=10.0,
            peak_to_trough_or_trough_to_peak=200.0,
            market_data_cutoff=CUTOFF,
            source_hashes={"ohlcv": "a" * 64},
            case_status=HistoricalCaseStatus.REJECTED,
        )
        self.assertIn(
            "invalid_prices",
            validate_historical_market_move(move).failed_rules,
        )

    def test_missing_provenance_is_structured(self) -> None:
        move = _move(source_hashes={})
        self.assertIn(
            "missing_provenance",
            validate_historical_market_move(move).failed_rules,
        )

    def test_move_round_trip_and_hash_are_deterministic(self) -> None:
        first = _move(source_hashes={"z": "z", "a": "a"})
        second = _move(source_hashes={"a": "a", "z": "z"})
        self.assertEqual(first.content_hash, second.content_hash)
        restored = HistoricalMarketMove.from_dict(
            json.loads(json.dumps(first.to_dict(), sort_keys=True))
        )
        self.assertEqual(restored, first)

    def test_move_hash_mismatch_is_structured(self) -> None:
        move = replace(_move(), end_price=151.0)
        validation = validate_historical_market_move(move)
        self.assertIn("content_hash", validation.failed_rules)


class HistoricalPacketTests(unittest.TestCase):
    def assert_failed(
        self,
        packet: HistoricalEvidencePacket,
        rule: str,
    ) -> None:
        finalized = finalize_historical_evidence_packet(packet)
        self.assertFalse(finalized.validation_result.is_valid)
        self.assertIn(rule, finalized.validation_result.failed_rules)

    def test_valid_temporally_separated_packet(self) -> None:
        packet = _packet()
        self.assertTrue(packet.validation_result.is_valid)
        classes = {
            item.temporal_classification for item in packet.evidence
        }
        self.assertEqual(classes, set(HistoricalTemporalClassification) - {
            HistoricalTemporalClassification.UNAVAILABLE
        })
        self.assertEqual(
            packet.content_hash,
            historical_evidence_packet_content_hash(packet),
        )

    def test_historical_cutoff_violation(self) -> None:
        evidence = list(_historical_evidence())
        changed = replace(
            evidence[2].evidence,
            applicable_cutoff="2020-01-15T00:00:00+00:00",
        )
        evidence[2] = replace(evidence[2], evidence=changed)
        self.assert_failed(
            _draft_packet(evidence=tuple(evidence)),
            "historical_cutoff_violation",
        )

    def test_post_outcome_evidence_cannot_enter_pre_move_features(self) -> None:
        leaked = _exposure("exposure-leaked", "ev-after")
        self.assert_failed(
            _draft_packet(exposures_before_move=(leaked,)),
            "future_leakage",
        )

    def test_during_move_evidence_cannot_be_known_before(self) -> None:
        evidence = list(_historical_evidence())
        evidence[1] = replace(
            evidence[1],
            temporal_classification=(
                HistoricalTemporalClassification.KNOWN_BEFORE_MOVE
            ),
        )
        self.assert_failed(
            _draft_packet(evidence=tuple(evidence)),
            "temporal_classification",
        )

    def test_retrospective_interpretation_cannot_be_confirmed_fact(self) -> None:
        evidence = list(_historical_evidence())
        evidence[3] = replace(
            evidence[3],
            evidence=replace(
                evidence[3].evidence,
                status=EvidenceStatus.CONFIRMED,
            ),
        )
        finalized = finalize_historical_evidence_packet(
            _draft_packet(evidence=tuple(evidence))
        )
        self.assertIn(
            "retrospective_pre_move",
            finalized.validation_result.failed_rules,
        )
        self.assertIn(
            "temporal_classification",
            finalized.validation_result.failed_rules,
        )

    def test_dangling_evidence_reference(self) -> None:
        dangling = _exposure("exposure-dangling", "ev-missing")
        self.assert_failed(
            _draft_packet(exposures_before_move=(dangling,)),
            "dangling_evidence_references",
        )

    def test_dangling_event_reference(self) -> None:
        evidence = list(_historical_evidence())
        evidence[0] = replace(
            evidence[0],
            evidence=replace(
                evidence[0].evidence,
                related_event_id="event-missing",
            ),
        )
        self.assert_failed(
            _draft_packet(evidence=tuple(evidence)),
            "dangling_event_references",
        )

    def test_duplicate_evidence_ids_are_invalid(self) -> None:
        evidence = _historical_evidence()
        duplicate = replace(
            evidence[1],
            evidence=replace(
                evidence[1].evidence,
                evidence_id="ev-before",
            ),
        )
        self.assert_failed(
            _draft_packet(evidence=(evidence[0], duplicate) + evidence[2:]),
            "duplicate_ids",
        )

    def test_post_move_initiating_event_requires_lag_reasoning(self) -> None:
        event = _event(scheduled_at="2020-01-20T20:00:00+00:00")
        self.assert_failed(
            _draft_packet(material_events=(event,)),
            "event_lag_reasoning",
        )
        explained = replace(
            event,
            lag_reasoning=(
                "The later event is retained only as lagged confirmation, "
                "not contemporaneous knowledge."
            ),
        )
        finalized = finalize_historical_evidence_packet(
            _draft_packet(material_events=(explained,))
        )
        self.assertNotIn(
            "event_lag_reasoning",
            finalized.validation_result.failed_rules,
        )

    def test_available_evidence_requires_provenance(self) -> None:
        evidence = list(_historical_evidence())
        evidence[0] = replace(
            evidence[0],
            evidence=replace(evidence[0].evidence, source=""),
        )
        self.assert_failed(
            _draft_packet(evidence=tuple(evidence)),
            "missing_provenance",
        )

    def test_post_move_evidence_cannot_define_market_regime(self) -> None:
        self.assert_failed(
            _draft_packet(regime=_regime(
                supporting_evidence_ids=("ev-after",)
            )),
            "future_leakage",
        )

    def test_dangling_market_regime_reference_is_explicit(self) -> None:
        self.assert_failed(
            _draft_packet(regime=_regime(
                supporting_evidence_ids=("ev-missing",)
            )),
            "dangling_evidence_references",
        )

    def test_packet_round_trip_and_repeated_finalization_are_stable(self) -> None:
        packet = _packet()
        repeated = finalize_historical_evidence_packet(packet)
        restored = HistoricalEvidencePacket.from_dict(packet.to_dict())
        self.assertEqual(repeated, packet)
        self.assertEqual(restored, packet)

    def test_packet_hash_tampering_is_detected(self) -> None:
        packet = _packet()
        tampered = replace(
            packet,
            unresolved_unknowns=("Changed after hashing.",),
        )
        self.assertIn(
            "content_hash",
            validate_historical_evidence_packet(tampered).failed_rules,
        )

    def test_market_regime_hash_round_trip(self) -> None:
        regime = _regime()
        restored = MarketRegime.from_dict(regime.to_dict())
        self.assertEqual(restored, regime)
        self.assertEqual(regime.content_hash, market_regime_content_hash(regime))


class HistoricalCausalAnalysisTests(unittest.TestCase):
    def assert_failed(
        self,
        analysis: HistoricalCausalAnalysis,
        rule: str,
    ) -> None:
        finalized = finalize_historical_causal_analysis(
            analysis,
            packet=_packet(),
        )
        self.assertFalse(finalized.validation_result.is_valid)
        self.assertIn(rule, finalized.validation_result.failed_rules)

    def test_valid_causal_analysis(self) -> None:
        packet = _packet()
        analysis = finalize_historical_causal_analysis(
            _draft_causal(),
            packet=packet,
        )
        self.assertTrue(analysis.validation_result.is_valid)
        self.assertEqual(
            analysis.content_hash,
            historical_causal_analysis_content_hash(analysis),
        )

    def test_missing_causal_drivers(self) -> None:
        self.assert_failed(
            _draft_causal(primary_drivers=(), secondary_drivers=()),
            "missing_causal_drivers",
        )

    def test_missing_transmission_mechanism(self) -> None:
        self.assert_failed(
            _draft_causal(mechanisms=()),
            "no_transmission_mechanism",
        )

    def test_unsupported_causal_certainty(self) -> None:
        self.assert_failed(
            _draft_causal(
                primary_drivers=("Earnings definitely caused the move.",)
            ),
            "unsupported_causal_certainty",
        )

    def test_missing_contradicting_evidence(self) -> None:
        self.assert_failed(
            _draft_causal(contradicting_evidence_ids=()),
            "causal_contradicting_evidence",
        )

    def test_current_causal_schema_requires_supporting_evidence(self) -> None:
        self.assert_failed(
            replace(
                _draft_causal(),
                supporting_evidence_ids=(),
                initiating_conditions=(),
                initiating_condition_evidence_ids=(),
                retrospective_explanations=(),
                retrospective_evidence_ids=(),
            ),
            "causal_evidence_linkage",
        )

    def test_post_move_support_must_be_labeled_retrospective(self) -> None:
        self.assert_failed(
            replace(
                _draft_causal(),
                retrospective_explanations=(),
                retrospective_evidence_ids=(),
            ),
            "causal_temporal_labeling",
        )

    def test_invalid_causal_confidence(self) -> None:
        self.assert_failed(
            replace(_draft_causal(), confidence="certain"),
            "invalid_confidence",
        )

    def test_dangling_causal_references(self) -> None:
        analysis = finalize_historical_causal_analysis(
            _draft_causal(
                contradicting_evidence_ids=("ev-missing",),
                triggering_events=("event-missing",),
            ),
            packet=_packet(),
        )
        self.assertIn(
            "dangling_evidence_references",
            analysis.validation_result.failed_rules,
        )
        self.assertIn(
            "dangling_event_references",
            analysis.validation_result.failed_rules,
        )

    def test_causal_round_trip_and_hash_are_deterministic(self) -> None:
        packet = _packet()
        original = finalize_historical_causal_analysis(
            _draft_causal(),
            packet=packet,
        )
        repeated = finalize_historical_causal_analysis(
            original,
            packet=packet,
        )
        restored = HistoricalCausalAnalysis.from_dict(original.to_dict())
        self.assertEqual(repeated, original)
        self.assertEqual(restored, original)
        self.assertTrue(
            validate_historical_causal_analysis(
                restored,
                packet=packet,
            ).is_valid
        )

    def test_legacy_causal_schema_remains_readable(self) -> None:
        legacy_version = next(iter(HISTORICAL_CAUSAL_LEGACY_SCHEMA_VERSIONS))
        legacy = finalize_historical_causal_analysis(
            replace(
                _draft_causal(),
                supporting_evidence_ids=(),
                initiating_condition_evidence_ids=(),
                retrospective_explanations=(),
                retrospective_evidence_ids=(),
                schema_version=legacy_version,
            ),
            packet=_packet(),
        )
        payload = legacy.to_dict()
        for field_name in (
            "supporting_evidence_ids",
            "initiating_condition_evidence_ids",
            "retrospective_explanations",
            "retrospective_evidence_ids",
        ):
            payload.pop(field_name)
        restored = HistoricalCausalAnalysis.from_dict(payload)
        self.assertEqual(restored, legacy)
        self.assertTrue(
            validate_historical_causal_analysis(
                restored,
                packet=_packet(),
            ).is_valid
        )


class HistoricalSimilarityTests(unittest.TestCase):
    def test_valid_similarity_contract(self) -> None:
        features = _features()
        validation = validate_historical_similarity_features(features)
        self.assertTrue(validation.is_valid)
        self.assertEqual(
            features.content_hash,
            historical_similarity_features_content_hash(features),
        )

    def test_wrong_category_mechanism_is_invalid(self) -> None:
        features = _features(
            company_exposure_types=(ExposureType.LIQUIDITY_CONDITIONS,)
        )
        validation = validate_historical_similarity_features(features)
        self.assertIn(
            "similarity_feature_compatibility",
            validation.failed_rules,
        )

    def test_duplicate_similarity_features_are_invalid(self) -> None:
        features = _features(event_types=("earnings", "earnings"))
        self.assertIn(
            "similarity_feature_duplicates",
            validate_historical_similarity_features(features).failed_rules,
        )

    def test_non_normalized_event_type_is_invalid(self) -> None:
        features = _features(event_types=("Earnings Event",))
        self.assertIn(
            "similarity_feature_values",
            validate_historical_similarity_features(features).failed_rules,
        )

    def test_similarity_round_trip(self) -> None:
        original = _features()
        restored = HistoricalSimilarityFeatures.from_dict(original.to_dict())
        self.assertEqual(restored, original)


class HistoricalVocabularyTests(unittest.TestCase):
    def test_required_move_types_and_statuses(self) -> None:
        self.assertEqual(
            {item.value for item in HistoricalMoveType},
            {
                "upward_impulse",
                "downward_impulse",
                "crash",
                "correction",
                "capitulation",
                "short_squeeze",
                "melt_up",
                "gap_repricing",
                "post_earnings_repricing",
                "post_event_repricing",
                "valuation_rerating",
                "valuation_derating",
                "failed_breakout",
                "failed_breakdown",
                "reversal",
                "recovery",
                "prolonged_decline",
                "prolonged_advance",
            },
        )
        self.assertEqual(
            {item.value for item in HistoricalCaseStatus},
            {
                "draft",
                "extracted",
                "reviewed",
                "validated",
                "rejected",
                "insufficient_evidence",
            },
        )

    def test_required_transmission_mechanisms(self) -> None:
        self.assertEqual(len(TransmissionMechanism), 25)
        self.assertIn(
            TransmissionMechanism.REFLEXIVE_PRICE_FEEDBACK,
            TransmissionMechanism,
        )

    def test_schema_versions_are_independent(self) -> None:
        self.assertEqual(
            HISTORICAL_CASE_SCHEMA_VERSION,
            "historical-market-case-1.0.0",
        )
        self.assertNotEqual(
            HISTORICAL_CASE_SCHEMA_VERSION,
            HISTORICAL_CAUSAL_SCHEMA_VERSION,
        )
        self.assertNotEqual(
            HISTORICAL_CASE_SCHEMA_VERSION,
            HISTORICAL_SIMILARITY_SCHEMA_VERSION,
        )
        self.assertEqual(
            HISTORICAL_CASE_VALIDATION_VERSION,
            "historical-market-case-validation-1.0.0",
        )
        self.assertEqual(
            HISTORICAL_CAUSAL_SCHEMA_VERSION,
            "historical-causal-analysis-1.1.0",
        )

    def test_extractor_is_protocol_only(self) -> None:
        class FixtureExtractor:
            def extract(self, historical_move, supplied_context):
                return _packet(move=historical_move)

        self.assertIsInstance(FixtureExtractor(), HistoricalCaseExtractor)


if __name__ == "__main__":
    unittest.main()
