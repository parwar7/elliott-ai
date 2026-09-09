from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

from elliott_ai.current_state import (
    CURRENT_MARKET_STATE_SCHEMA_VERSION,
    CURRENT_STATE_BUILDER_VERSION,
    CURRENT_TECHNICAL_NORMALIZATION_VERSION,
    CURRENT_TRANSMISSION_POLICY_VERSION,
    CurrentMarketState,
    CurrentStateBuildResult,
    CurrentStateBuilder,
    FeatureAvailability,
    TechnicalDirection,
    current_market_state_content_hash,
    current_state_build_result_content_hash,
    evidence_packet_content_hash,
    exposure_report_content_hash,
    validate_current_market_state,
)
from elliott_ai.exposure_engine import map_evidence_to_exposures
from elliott_ai.historical_market import (
    DurationBucket,
    HistoricalMoveType,
    MagnitudeBucket,
    MarketRegime,
    MarketRegimeState,
    RelativePerformanceBucket,
    TransmissionMechanism,
)
from elliott_ai.market_scenario import (
    EvidenceImplication,
    EvidenceStatus,
    ExposureCategory,
    ExposureCoverageState,
    ExposureType,
    FrozenTechnicalPremise,
)
from scripts.test_market_scenario import (
    CUTOFF,
    GENERATED_AT,
    _draft_report,
    _evidence,
    _premise,
)


FIXED_NOW = datetime(2026, 7, 29, 9, tzinfo=timezone.utc)


def _builder() -> CurrentStateBuilder:
    return CurrentStateBuilder(clock=lambda: FIXED_NOW)


def _bullish_premise() -> FrozenTechnicalPremise:
    raw = _premise(
        technical_structure={
            "retrieval_features": {
                "technical_structure": "five_wave_impulse",
                "projected_move_class": "upward_impulse",
                "expected_magnitude_bucket": "20_to_40_percent",
                "expected_duration_bucket": "months",
            },
            "preferred_count": ["Primary 3 active"],
        }
    ).to_dict()
    raw.pop("frozen_content_hash")
    raw["direction"] = "up"
    raw["current_wave_or_phase"] = "Primary 3 active"
    return FrozenTechnicalPremise.create(**raw)


def _current_regime() -> MarketRegime:
    return MarketRegime.create(
        interest_rate_regime=MarketRegimeState.RESTRICTIVE,
        inflation_regime=MarketRegimeState.STABLE,
        liquidity_regime=MarketRegimeState.CONTRACTING,
        credit_regime=MarketRegimeState.TIGHTENING,
        equity_risk_appetite=MarketRegimeState.RISK_OFF,
        volatility_regime=MarketRegimeState.HIGH,
        small_cap_regime=MarketRegimeState.UNDERPERFORMING,
        growth_stock_regime=MarketRegimeState.UNDERPERFORMING,
        sector_regime=MarketRegimeState.DECLINING,
        benchmark_trend=MarketRegimeState.DECLINING,
        regime_start_at="2026-07-01T00:00:00+00:00",
        regime_end_at=CUTOFF,
        supporting_evidence_ids=("ev-macro",),
        confidence="moderate",
    )


def _build(
    *,
    premise: FrozenTechnicalPremise | None = None,
    evidence=None,
    exposures=None,
    coverage=None,
    events=None,
    **kwargs,
) -> CurrentStateBuildResult:
    report = _draft_report(premise=premise)
    return _builder().build(
        report.frozen_technical_premise,
        evidence if evidence is not None else report.evidence,
        exposures if exposures is not None else report.exposures,
        coverage if coverage is not None else report.exposure_coverage,
        events=events if events is not None else report.material_events,
        **kwargs,
    )


class CurrentStateBuilderTests(unittest.TestCase):
    def test_valid_bearish_current_state(self) -> None:
        result = _build()
        self.assertTrue(result.success)
        state = result.current_state
        self.assertEqual(
            state.technical_direction,
            TechnicalDirection.BEARISH,
        )
        self.assertEqual(state.schema_version, CURRENT_MARKET_STATE_SCHEMA_VERSION)
        self.assertEqual(
            state.content_hash,
            current_market_state_content_hash(state),
        )
        self.assertEqual(
            state.provenance["builder_version"],
            CURRENT_STATE_BUILDER_VERSION,
        )

    def test_valid_bullish_state_and_explicit_technical_normalization(
        self,
    ) -> None:
        result = _build(premise=_bullish_premise())
        self.assertTrue(result.success)
        state = result.current_state
        self.assertEqual(
            state.technical_direction,
            TechnicalDirection.BULLISH,
        )
        self.assertEqual(
            state.technical_move_types,
            (HistoricalMoveType.UPWARD_IMPULSE,),
        )
        self.assertEqual(state.technical_structure, "five_wave_impulse")
        self.assertEqual(
            state.expected_magnitude_bucket,
            MagnitudeBucket.FROM_20_TO_40_PERCENT,
        )
        self.assertEqual(
            state.expected_duration_bucket,
            DurationBucket.MONTHS,
        )

    def test_target_size_does_not_infer_move_or_magnitude(self) -> None:
        result = _build()
        self.assertTrue(result.success)
        state = result.current_state
        self.assertEqual(state.technical_move_types, ())
        self.assertEqual(
            state.expected_magnitude_bucket,
            MagnitudeBucket.UNAVAILABLE,
        )
        self.assertIn(
            "technical_move_types",
            state.unavailable_features,
        )

    def test_partial_data_is_explicit(self) -> None:
        result = _build()
        state = result.current_state
        self.assertTrue(result.success)
        self.assertEqual(
            state.exposure_category_availability[
                ExposureCategory.VALUATION_AND_POSITIONING.value
            ],
            FeatureAvailability.UNAVAILABLE,
        )
        self.assertIn(
            "exposure_category:valuation_and_positioning",
            state.unavailable_features,
        )

    def test_unavailable_regime_is_not_inferred(self) -> None:
        result = _build(market_regime=None)
        self.assertTrue(result.success)
        self.assertIsNone(result.current_state.market_regime)
        self.assertEqual(
            result.current_state.feature_availability["market_regime"],
            FeatureAvailability.UNAVAILABLE,
        )

    def test_valid_regime_and_normalized_context_fields(self) -> None:
        result = _build(
            market_regime=_current_regime(),
            valuation_state=MarketRegimeState.HIGH,
            liquidity_state=MarketRegimeState.CONTRACTING,
            positioning_state=MarketRegimeState.CROWDED,
            benchmark_relative_state=(
                RelativePerformanceBucket.UNDERPERFORMANCE
            ),
            sector_relative_state=(
                RelativePerformanceBucket.STRONG_UNDERPERFORMANCE
            ),
        )
        self.assertTrue(result.success)
        self.assertEqual(
            result.current_state.market_regime.liquidity_regime,
            MarketRegimeState.CONTRACTING,
        )
        self.assertEqual(
            result.current_state.positioning_state,
            MarketRegimeState.CROWDED,
        )

    def test_current_cutoff_leakage_is_rejected(self) -> None:
        report = _draft_report()
        leaked = replace(
            report.evidence[0],
            publication_date="2026-08-01",
        )
        evidence = (leaked,) + report.evidence[1:]
        result = _build(evidence=evidence)
        self.assertFalse(result.success)
        self.assertIn(
            "cutoff_ordering",
            result.validation_result.failed_rules,
        )

    def test_future_scheduled_event_is_calendar_context(self) -> None:
        result = _build()
        self.assertTrue(result.success)
        self.assertIn("earnings", result.current_state.current_event_types)

    def test_future_event_cannot_be_marked_completed(self) -> None:
        report = _draft_report()
        confirmed = replace(
            report.material_events[0],
            status=EvidenceStatus.CONFIRMED,
        )
        result = _build(events=(confirmed,))
        self.assertFalse(result.success)
        self.assertIn(
            "future_event_status",
            result.validation_result.failed_rules,
        )

    def test_transmission_hypothesis_uses_controlled_mapping(self) -> None:
        evidence = (
            _evidence(
                "ev-valuation",
                implication=EvidenceImplication.SUPPORTIVE,
                category="valuation_compression",
            ),
        )
        mapping = map_evidence_to_exposures(
            evidence,
            coverage_by_category={
                category: ExposureCoverageState.UNAVAILABLE
                for category in ExposureCategory
                if category
                is not ExposureCategory.VALUATION_AND_POSITIONING
            },
        )
        result = _build(
            evidence=evidence,
            exposures=mapping.exposures,
            coverage=mapping.coverage,
            events=(),
        )
        self.assertTrue(result.success)
        self.assertEqual(
            result.current_state.current_transmission_hypotheses,
            (TransmissionMechanism.MULTIPLE_COMPRESSION,),
        )
        self.assertEqual(
            result.current_state.transmission_source_refs[
                TransmissionMechanism.MULTIPLE_COMPRESSION.value
            ],
            (
                "exposure-valuation_and_positioning-"
                "valuation_compression",
            ),
        )

    def test_unmapped_exposure_produces_warning_without_forcing(self) -> None:
        result = _build()
        self.assertTrue(result.success)
        self.assertIn(
            "unmapped_transmission_exposure:revenue_growth_risk",
            result.warnings,
        )
        self.assertNotIn(
            TransmissionMechanism.EARNINGS_REVISION,
            result.current_state.current_transmission_hypotheses,
        )

    def test_confirmed_fact_cannot_be_relabelled_as_assumption(self) -> None:
        report = _draft_report()
        result = _build(
            assumptions=(report.evidence[0].claim,),
        )
        self.assertFalse(result.success)
        self.assertIn(
            "assumption_separation",
            result.validation_result.failed_rules,
        )

    def test_dangling_exposure_reference_is_rejected(self) -> None:
        report = _draft_report()
        invalid = replace(
            report.exposures[0],
            supporting_evidence_ids=("missing-evidence",),
        )
        exposures = (invalid,) + report.exposures[1:]
        result = _build(exposures=exposures)
        self.assertFalse(result.success)
        self.assertIn(
            "exposure_references",
            result.validation_result.failed_rules,
        )

    def test_hashes_are_order_independent_and_inputs_are_not_mutated(
        self,
    ) -> None:
        report = _draft_report()
        before = (
            tuple(item.to_dict() for item in report.evidence),
            tuple(item.to_dict() for item in report.exposures),
            tuple(item.to_dict() for item in report.exposure_coverage),
        )
        first = _build()
        second = _build(
            evidence=tuple(reversed(report.evidence)),
            exposures=tuple(reversed(report.exposures)),
            coverage=tuple(reversed(report.exposure_coverage)),
        )
        self.assertEqual(
            first.current_state.evidence_packet_hash,
            second.current_state.evidence_packet_hash,
        )
        self.assertEqual(
            first.current_state.exposure_report_hash,
            second.current_state.exposure_report_hash,
        )
        self.assertEqual(first.current_state, second.current_state)
        after = (
            tuple(item.to_dict() for item in report.evidence),
            tuple(item.to_dict() for item in report.exposures),
            tuple(item.to_dict() for item in report.exposure_coverage),
        )
        self.assertEqual(before, after)

    def test_state_is_deeply_immutable_and_standalone_valid(self) -> None:
        state = _build().current_state
        with self.assertRaises(FrozenInstanceError):
            state.symbol = "CHANGED"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            state.provenance["new"] = "value"
        self.assertTrue(validate_current_market_state(state).is_valid)

    def test_state_and_build_result_round_trip(self) -> None:
        result = _build()
        state = CurrentMarketState.from_dict(
            result.current_state.to_dict()
        )
        restored = CurrentStateBuildResult.from_dict(result.to_dict())
        self.assertEqual(state, result.current_state)
        self.assertEqual(restored, result)
        self.assertEqual(
            restored.content_hash,
            current_state_build_result_content_hash(restored),
        )
        self.assertEqual(
            result.current_state.provenance[
                "technical_normalization_version"
            ],
            CURRENT_TECHNICAL_NORMALIZATION_VERSION,
        )
        self.assertEqual(
            result.current_state.provenance[
                "transmission_policy_version"
            ],
            CURRENT_TRANSMISSION_POLICY_VERSION,
        )

    def test_bundle_hash_helpers_are_deterministic(self) -> None:
        report = _draft_report()
        self.assertEqual(
            evidence_packet_content_hash(report.evidence),
            evidence_packet_content_hash(
                tuple(reversed(report.evidence))
            ),
        )
        self.assertEqual(
            exposure_report_content_hash(
                report.exposures,
                report.exposure_coverage,
            ),
            exposure_report_content_hash(
                tuple(reversed(report.exposures)),
                tuple(reversed(report.exposure_coverage)),
            ),
        )


if __name__ == "__main__":
    unittest.main()
