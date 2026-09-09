from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace

from elliott_ai.exposure_engine import map_evidence_to_exposures
from elliott_ai.market_scenario import (
    AdversarialRecommendation,
    AdversarialReview,
    EvidenceImplication,
    EvidenceItem,
    EvidenceStatus,
    EventDatePrecision,
    ExposureCategory,
    ExposureCoverageState,
    ExposureMappingWarning,
    ExposureType,
    FrozenTechnicalPremise,
    MARKET_SCENARIO_SCHEMA_VERSION,
    MarketScenarioReport,
    MaterialEvent,
    ScenarioDirection,
    ScenarioNarrative,
    TimelineBranch,
    TimelineBranches,
    finalize_market_scenario_report,
    frozen_technical_premise_content_hash,
    market_scenario_report_content_hash,
    validate_market_scenario_report,
)


CUTOFF = "2026-07-28T16:00:00+00:00"
GENERATED_AT = "2026-07-29T09:00:00+00:00"


def _premise(
    *,
    target_low: float | None = 300.0,
    target_high: float | None = 340.0,
    source_hashes: dict[str, str] | None = None,
    technical_structure: object | None = None,
) -> FrozenTechnicalPremise:
    return FrozenTechnicalPremise.create(
        resolution_id="resolution-001",
        symbol="NASDAQ:TEST",
        exchange="NASDAQ",
        analysed_at=CUTOFF,
        market_data_cutoff=CUTOFF,
        technical_structure=(
            technical_structure
            if technical_structure is not None
            else {
                "preferred_count": ["Cycle III", "Primary 4 active"],
                "invalidation": {"price": 275.0, "basis": "daily_close"},
            }
        ),
        current_wave_or_phase="Primary 4 active",
        direction=ScenarioDirection.DOWN,
        target_low=target_low,
        target_high=target_high,
        invalidation_level=375.0,
        expected_completion_start="2026-08-01",
        expected_completion_end="2026-10-31",
        technical_confidence={"structure": 0.72, "degree": 0.61},
        technical_readiness="provisional",
        supporting_technical_evidence=("Price structure validated.",),
        alternative_technical_hypotheses=("Primary 4 already complete.",),
        source_hashes=source_hashes
        or {
            "degree_resolution": "a" * 64,
            "market_data": "b" * 64,
        },
    )


def _evidence(
    evidence_id: str,
    *,
    implication: EvidenceImplication,
    category: str | None = None,
    status: EvidenceStatus = EvidenceStatus.CONFIRMED,
    source: str = "https://example.test/filing",
    source_type: str = "primary_filing",
    provider: str = "Example Exchange",
    publication_date: str | None = "2026-07-20",
    related_event_id: str | None = None,
    warnings: tuple[str, ...] = (),
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        category=category
        or (
            "interest_rate_sensitivity"
            if evidence_id == "ev-macro"
            else "revenue_growth_risk"
        ),
        claim=f"Conditional evidence claim for {evidence_id}.",
        source=source,
        source_type=source_type,
        provider=provider,
        publication_date=publication_date,
        retrieval_timestamp=GENERATED_AT,
        applicable_cutoff=CUTOFF,
        status=status,
        implication=implication,
        confidence="moderate",
        relevance_to_technical_scenario="Could affect the projected magnitude.",
        related_event_id=related_event_id,
        warnings=warnings,
    )


def _narrative(
    scenario_id: str,
    *,
    primary: bool = False,
    no_news: bool = False,
) -> ScenarioNarrative:
    return ScenarioNarrative(
        scenario_id=scenario_id,
        name=f"Scenario {scenario_id}",
        summary="One plausible combination could explain the frozen projection.",
        drivers=(
            ("valuation compression", "macro liquidity tightening")
            if primary
            else ("sector rotation", "positioning unwind")
        ),
        evidence_ids=("ev-support", "ev-macro"),
        contradicting_evidence_ids=("ev-oppose",),
        assumptions=("Market conditions remain comparable.",),
        causal_chain=(
            "A first driver could reduce valuation support.",
            "A second independent driver may amplify the move.",
        ),
        magnitude_explanation="The interacting drivers could support the projected range.",
        time_horizon="Within the frozen technical completion window.",
        confirmation_signals=("Evidence breadth increases.",),
        weakening_signals=("Liquidity improves.",),
        invalidation_signals=("The frozen technical invalidation is reached.",),
        is_no_company_specific_news_scenario=no_news,
        technical_consistency="moderate",
        fundamental_plausibility="moderate",
        confidence="moderate",
    )


def _branch() -> TimelineBranch:
    return TimelineBranch(
        summary="The move could occur in this timing branch.",
        mechanisms=("valuation repricing", "positioning adjustment"),
        related_event_ids=("event-earnings",),
        supporting_evidence_ids=("ev-support",),
        contradicting_evidence_ids=("ev-oppose",),
        assumptions=("Event timing remains unchanged.",),
        confidence="moderate",
    )


def _draft_report(
    *,
    premise: FrozenTechnicalPremise | None = None,
) -> MarketScenarioReport:
    evidence = (
        _evidence(
            "ev-support",
            implication=EvidenceImplication.SUPPORTIVE,
            related_event_id="event-earnings",
        ),
        _evidence("ev-oppose", implication=EvidenceImplication.CONTRADICTORY),
        _evidence("ev-macro", implication=EvidenceImplication.SUPPORTIVE),
    )
    mapping = map_evidence_to_exposures(
        evidence,
        related_technical_outcomes_by_exposure={
            ExposureType.REVENUE_GROWTH_RISK: (
                "target range is reached",
            ),
        },
        coverage_by_category={
            ExposureCategory.SECTOR_AND_COMPETITORS:
                ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
            ExposureCategory.VALUATION_AND_POSITIONING:
                ExposureCoverageState.UNAVAILABLE,
            ExposureCategory.EVENT_CALENDAR:
                ExposureCoverageState.UNAVAILABLE,
        },
    )
    return MarketScenarioReport(
        schema_version=MARKET_SCENARIO_SCHEMA_VERSION,
        report_id="scenario-report-001",
        generated_at=GENERATED_AT,
        research_cutoff=CUTOFF,
        evidence_availability_statement="The fixture supplies all required lanes.",
        frozen_technical_premise=premise or _premise(),
        evidence=evidence,
        exposures=mapping.exposures,
        material_events=(
            MaterialEvent(
                event_id="event-earnings",
                event_type="earnings",
                title="Scheduled quarterly report",
                scheduled_at="2026-08-15T20:00:00+00:00",
                date_precision=EventDatePrecision.EXACT,
                status=EvidenceStatus.SCHEDULED,
                source="https://example.test/calendar",
                source_type="issuer_calendar",
                publication_date="2026-07-10",
                retrieval_timestamp=GENERATED_AT,
                applicable_cutoff=CUTOFF,
                related_evidence_ids=("ev-support",),
            ),
        ),
        narratives=(
            _narrative("scenario-primary", primary=True),
            _narrative("scenario-no-news", no_news=True),
            _narrative("scenario-opposing"),
        ),
        primary_scenario_id="scenario-primary",
        timeline_branches=TimelineBranches(
            before_events=_branch(),
            around_events=_branch(),
            after_events=_branch(),
        ),
        no_company_specific_news_scenario_id="scenario-no-news",
        adversarial_review=AdversarialReview(
            strongest_counterargument=(
                "Improving liquidity could make the primary narrative unnecessary."
            ),
            missing_evidence=("Current positioning detail is limited.",),
            opposing_evidence_ids=("ev-oppose",),
            unsupported_assumptions=("Market regime persistence.",),
            simpler_explanations=("Ordinary volatility could explain part of the move.",),
            timing_conflicts=(),
            technical_invalidation_signals=("Price closes above 375.",),
            fundamental_contradiction_signals=("Material guidance improvement.",),
            recommendation=AdversarialRecommendation.KEEP,
            revised_primary_scenario_id=None,
            confidence="moderate",
        ),
        unresolved_unknowns=("Future unscheduled information is unknown.",),
        final_synthesis=(
            "The frozen technical move could be consistent with interacting "
            "valuation and liquidity forces; this is contextual evidence, not a forecast."
        ),
        confidence="moderate",
        explicit_limitations=(
            "The technical premise remains frozen and independent.",
        ),
        exposure_coverage=mapping.coverage,
        exposure_mapping_warnings=mapping.warnings,
    )


def _final(report: MarketScenarioReport) -> MarketScenarioReport:
    return finalize_market_scenario_report(report)


class MarketScenarioContractTests(unittest.TestCase):
    def test_valid_minimal_report(self) -> None:
        report = _final(_draft_report())
        self.assertTrue(report.validation_result.is_valid)
        self.assertEqual(report.validation_result.errors, ())
        self.assertEqual(
            report.content_hash, market_scenario_report_content_hash(report)
        )
        self.assertEqual(
            report.frozen_technical_premise.frozen_content_hash,
            frozen_technical_premise_content_hash(
                report.frozen_technical_premise
            ),
        )

    def test_frozen_premise_is_deeply_immutable(self) -> None:
        premise = _premise()
        with self.assertRaises(FrozenInstanceError):
            premise.symbol = "CHANGED"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            premise.source_hashes["new"] = "c" * 64  # type: ignore[index]
        with self.assertRaises(TypeError):
            premise.technical_structure["invalidation"]["price"] = 0  # type: ignore[index]

    def test_hashing_is_deterministic_and_mapping_order_independent(self) -> None:
        first = _premise(
            source_hashes={"z": "1" * 64, "a": "2" * 64},
            technical_structure={"z": {"b": 2, "a": 1}, "a": [3, 4]},
        )
        second = _premise(
            source_hashes={"a": "2" * 64, "z": "1" * 64},
            technical_structure={"a": [3, 4], "z": {"a": 1, "b": 2}},
        )
        self.assertEqual(first.frozen_content_hash, second.frozen_content_hash)

        report = _final(_draft_report(premise=first))
        repeated = _final(MarketScenarioReport.from_dict(report.to_dict()))
        self.assertEqual(report.content_hash, repeated.content_hash)

    def test_json_dictionary_round_trip_preserves_values(self) -> None:
        original = _final(_draft_report())
        encoded = json.dumps(original.to_dict(), sort_keys=True)
        restored = MarketScenarioReport.from_dict(json.loads(encoded))
        self.assertEqual(restored, original)
        self.assertEqual(restored.to_dict(), original.to_dict())

    def test_invalid_enum_is_not_silently_coerced(self) -> None:
        raw = _draft_report().evidence[0].to_dict()
        raw["status"] = "probably_confirmed"
        with self.assertRaises(ValueError):
            EvidenceItem.from_dict(raw)

    def test_requested_enum_vocabularies_are_complete(self) -> None:
        self.assertEqual(
            {item.value for item in EvidenceStatus},
            {
                "confirmed",
                "scheduled",
                "interpretation",
                "hypothetical",
                "unknown",
                "unavailable",
                "stale",
            },
        )
        self.assertEqual(
            {item.value for item in EvidenceImplication},
            {"supportive", "contradictory", "neutral", "incomparable"},
        )
        self.assertEqual(
            {item.value for item in ScenarioDirection},
            {"up", "down", "sideways", "unknown"},
        )
        self.assertEqual(
            {item.value for item in AdversarialRecommendation},
            {"keep", "revise", "downgrade", "reject", "insufficient_evidence"},
        )
        self.assertEqual(
            {item.value for item in EventDatePrecision},
            {"exact", "date_only", "month", "quarter", "window", "unknown"},
        )

    def test_optional_premise_fields_round_trip_as_explicit_nulls(self) -> None:
        premise = FrozenTechnicalPremise.create(
            resolution_id="resolution-minimal",
            symbol="TEST",
            analysed_at=CUTOFF,
            market_data_cutoff=CUTOFF,
            technical_structure="Primary 4 active",
            direction=ScenarioDirection.UNKNOWN,
            technical_confidence="unavailable",
            technical_readiness="unavailable",
            source_hashes={"resolution": "c" * 64},
        )
        payload = premise.to_dict()
        self.assertIsNone(payload["exchange"])
        self.assertIsNone(payload["target_low"])
        self.assertIsNone(payload["expected_completion_end"])
        self.assertEqual(FrozenTechnicalPremise.from_dict(payload), premise)

    def test_missing_required_model_field_is_malformed_input(self) -> None:
        raw = _draft_report().evidence[0].to_dict()
        raw.pop("claim")
        with self.assertRaises(TypeError):
            EvidenceItem.from_dict(raw)

    def test_report_hash_tampering_is_detected_without_exception(self) -> None:
        report = _final(_draft_report())
        tampered = replace(report, final_synthesis="Content changed after hashing.")
        validation = validate_market_scenario_report(tampered)
        self.assertFalse(validation.is_valid)
        self.assertIn("report_content_hash", validation.failed_rules)


class MarketScenarioValidationTests(unittest.TestCase):
    def assert_failed(self, report: MarketScenarioReport, rule: str) -> None:
        finalized = _final(report)
        self.assertFalse(finalized.validation_result.is_valid)
        self.assertIn(rule, finalized.validation_result.failed_rules)

    def test_too_few_narratives(self) -> None:
        report = _draft_report()
        self.assert_failed(
            replace(report, narratives=report.narratives[:2]),
            "narrative_minimum",
        )

    def test_too_many_narratives(self) -> None:
        report = _draft_report()
        extras = (
            _narrative("scenario-extra-1"),
            _narrative("scenario-extra-2"),
            _narrative("scenario-extra-3"),
        )
        self.assert_failed(
            replace(report, narratives=report.narratives + extras),
            "narrative_maximum",
        )

    def test_single_driver_primary_narrative(self) -> None:
        report = _draft_report()
        primary = replace(report.narratives[0], drivers=("valuation compression",))
        self.assert_failed(
            replace(report, narratives=(primary,) + report.narratives[1:]),
            "primary_independent_drivers",
        )

    def test_primary_requires_contradictory_evidence(self) -> None:
        report = _draft_report()
        primary = replace(report.narratives[0], contradicting_evidence_ids=())
        self.assert_failed(
            replace(report, narratives=(primary,) + report.narratives[1:]),
            "primary_contradictory_evidence",
        )

    def test_missing_primary_and_missing_no_company_news_are_detected(self) -> None:
        report = _draft_report()
        finalized = _final(
            replace(
                report,
                primary_scenario_id="",
                no_company_specific_news_scenario_id="",
            )
        )
        self.assertIn(
            "primary_scenario_required", finalized.validation_result.failed_rules
        )
        self.assertIn(
            "no_company_news_required", finalized.validation_result.failed_rules
        )

    def test_unknown_primary_and_no_company_news_ids_are_detected(self) -> None:
        report = _draft_report()
        finalized = _final(
            replace(
                report,
                primary_scenario_id="scenario-missing",
                no_company_specific_news_scenario_id="scenario-also-missing",
            )
        )
        self.assertIn(
            "primary_scenario_reference", finalized.validation_result.failed_rules
        )
        self.assertIn(
            "no_company_news_reference", finalized.validation_result.failed_rules
        )

    def test_missing_timeline_branch(self) -> None:
        report = _draft_report()
        branches = replace(report.timeline_branches, around_events=None)
        self.assert_failed(
            replace(report, timeline_branches=branches),
            "timeline_around_events",
        )

    def test_missing_strongest_counterargument(self) -> None:
        report = _draft_report()
        review = replace(report.adversarial_review, strongest_counterargument="")
        self.assert_failed(
            replace(report, adversarial_review=review),
            "strongest_counterargument",
        )

    def test_missing_invalidation_and_contradiction_signals(self) -> None:
        report = _draft_report()
        review = replace(
            report.adversarial_review,
            technical_invalidation_signals=(),
            fundamental_contradiction_signals=(),
        )
        finalized = _final(replace(report, adversarial_review=review))
        self.assertIn(
            "technical_invalidation_signals",
            finalized.validation_result.failed_rules,
        )
        self.assertIn(
            "fundamental_contradiction_signals",
            finalized.validation_result.failed_rules,
        )

    def test_factual_evidence_requires_provenance(self) -> None:
        report = _draft_report()
        first = replace(report.evidence[0], source="", provider="")
        self.assert_failed(
            replace(report, evidence=(first,) + report.evidence[1:]),
            "factual_evidence_provenance",
        )

    def test_hypothetical_evidence_cannot_be_confirmed(self) -> None:
        report = _draft_report()
        first = replace(report.evidence[0], source_type="hypothetical")
        self.assert_failed(
            replace(report, evidence=(first,) + report.evidence[1:]),
            "hypothetical_not_confirmed",
        )

    def test_stale_evidence_is_warned_and_requires_explicit_qualification(self) -> None:
        report = _draft_report()
        stale = replace(
            report.evidence[0],
            status=EvidenceStatus.STALE,
            warnings=("Stale evidence retained for qualified context.",),
        )
        qualified = _final(
            replace(report, evidence=(stale,) + report.evidence[1:])
        )
        self.assertTrue(qualified.validation_result.is_valid)
        self.assertIn(
            "stale_evidence_retained",
            {item.code for item in qualified.validation_result.warnings},
        )

        unqualified = replace(stale, warnings=())
        self.assert_failed(
            replace(report, evidence=(unqualified,) + report.evidence[1:]),
            "stale_evidence_warning",
        )

    def test_historical_cutoff_violation(self) -> None:
        report = _draft_report()
        future = replace(report.evidence[0], publication_date="2026-08-01")
        self.assert_failed(
            replace(report, evidence=(future,) + report.evidence[1:]),
            "historical_cutoff",
        )

    def test_duplicate_ids(self) -> None:
        report = _draft_report()
        duplicate = replace(
            report.evidence[1],
            evidence_id=report.evidence[0].evidence_id,
        )
        self.assert_failed(
            replace(report, evidence=(report.evidence[0], duplicate, report.evidence[2])),
            "unique_ids",
        )

    def test_dangling_evidence_event_and_scenario_references(self) -> None:
        report = _draft_report()
        primary = replace(report.narratives[0], evidence_ids=("ev-missing",))
        branch = replace(_branch(), related_event_ids=("event-missing",))
        review = replace(
            report.adversarial_review,
            revised_primary_scenario_id="scenario-missing",
        )
        finalized = _final(
            replace(
                report,
                narratives=(primary,) + report.narratives[1:],
                timeline_branches=replace(
                    report.timeline_branches, before_events=branch
                ),
                adversarial_review=review,
            )
        )
        self.assertIn(
            "evidence_references", finalized.validation_result.failed_rules
        )
        self.assertIn("event_references", finalized.validation_result.failed_rules)
        self.assertIn(
            "scenario_references", finalized.validation_result.failed_rules
        )

    def test_invalid_exposure_reference_is_reported_separately(self) -> None:
        report = _draft_report()
        company = next(
            item
            for item in report.exposures
            if item.exposure_type is ExposureType.REVENUE_GROWTH_RISK
        )
        changed = replace(
            company,
            supporting_evidence_ids=company.supporting_evidence_ids
            + ("ev-not-present",),
        )
        exposures = tuple(
            (
                changed
                if item.exposure_type
                is ExposureType.REVENUE_GROWTH_RISK
                else item
            )
            for item in report.exposures
        )
        self.assert_failed(
            replace(report, exposures=exposures),
            "exposure_references",
        )

    def test_exposure_reference_cannot_support_and_contradict(self) -> None:
        report = _draft_report()
        company = next(
            item
            for item in report.exposures
            if item.exposure_type is ExposureType.REVENUE_GROWTH_RISK
        )
        overlap_id = company.supporting_evidence_ids[0]
        changed = replace(
            company,
            contradicting_evidence_ids=company.contradicting_evidence_ids
            + (overlap_id,),
        )
        exposures = tuple(
            (
                changed
                if item.exposure_type
                is ExposureType.REVENUE_GROWTH_RISK
                else item
            )
            for item in report.exposures
        )
        self.assert_failed(
            replace(report, exposures=exposures),
            "exposure_reference_overlap",
        )

    def test_duplicate_mechanism_and_missing_coverage_are_invalid(self) -> None:
        report = _draft_report()
        company = next(
            item
            for item in report.exposures
            if item.exposure_type is ExposureType.REVENUE_GROWTH_RISK
        )
        duplicate = replace(
            company,
            exposure_id="exposure-company-revenue-growth-duplicate",
        )
        without_events = tuple(
            item
            for item in report.exposure_coverage
            if item.category is not ExposureCategory.EVENT_CALENDAR
        )
        finalized = _final(
            replace(
                report,
                exposures=report.exposures + (duplicate,),
                exposure_coverage=without_events,
            )
        )
        self.assertIn(
            "unique_exposure_keys",
            finalized.validation_result.failed_rules,
        )
        self.assertIn(
            "exposure_coverage_required",
            finalized.validation_result.failed_rules,
        )

    def test_category_and_mechanism_must_be_compatible(self) -> None:
        report = _draft_report()
        changed = replace(
            report.exposures[0],
            exposure_type=ExposureType.INTEREST_RATE_SENSITIVITY,
        )
        self.assert_failed(
            replace(
                report,
                exposures=(changed,) + report.exposures[1:],
            ),
            "category_exposure_compatibility",
        )

    def test_undeclared_empty_category_is_invalid(self) -> None:
        report = _draft_report()
        event_coverage = next(
            item
            for item in report.exposure_coverage
            if item.category is ExposureCategory.EVENT_CALENDAR
        )
        changed = replace(
            event_coverage,
            state=ExposureCoverageState.UNDECLARED,
        )
        coverage = tuple(
            changed
            if item.category is ExposureCategory.EVENT_CALENDAR
            else item
            for item in report.exposure_coverage
        )
        self.assert_failed(
            replace(report, exposure_coverage=coverage),
            "exposure_coverage_state",
        )

    def test_duplicate_coverage_declaration_is_invalid(self) -> None:
        report = _draft_report()
        self.assert_failed(
            replace(
                report,
                exposure_coverage=report.exposure_coverage
                + (report.exposure_coverage[0],),
            ),
            "unique_exposure_coverage",
        )

    def test_unsupported_forced_mapping_blocks_final_validation(self) -> None:
        report = _draft_report()
        warning = ExposureMappingWarning(
            code="unsupported_forced_mapping",
            evidence_id="ev-support",
            category=ExposureCategory.COMPANY,
            requested_exposure_type=(
                ExposureType.INTEREST_RATE_SENSITIVITY.value
            ),
            message=(
                "The requested mechanism is incompatible with the evidence "
                "category."
            ),
        )
        self.assert_failed(
            replace(
                report,
                exposure_mapping_warnings=(
                    report.exposure_mapping_warnings + (warning,)
                ),
            ),
            "unsupported_forced_mappings",
        )

    def test_invalid_exposure_confidence_is_rejected(self) -> None:
        report = _draft_report()
        changed = replace(report.exposures[0], confidence="certain")
        self.assert_failed(
            replace(report, exposures=(changed,) + report.exposures[1:]),
            "qualitative_confidence",
        )

    def test_invalid_confidence(self) -> None:
        self.assert_failed(
            replace(_draft_report(), confidence="certain"),
            "qualitative_confidence",
        )

    def test_invalid_target_range(self) -> None:
        premise = _premise(target_low=350.0, target_high=300.0)
        self.assert_failed(
            _draft_report(premise=premise),
            "target_range",
        )

    def test_frozen_premise_hash_mismatch(self) -> None:
        report = _draft_report()
        changed = replace(
            report.frozen_technical_premise,
            technical_structure={"silently": "changed"},
        )
        self.assert_failed(
            replace(report, frozen_technical_premise=changed),
            "frozen_premise_hash",
        )

    def test_prohibited_certainty_language(self) -> None:
        self.assert_failed(
            replace(
                _draft_report(),
                final_synthesis="This will happen because valuation is high.",
            ),
            "prohibited_certainty_language",
        )

    def test_chart_cannot_predict_a_future_event(self) -> None:
        self.assert_failed(
            replace(
                _draft_report(),
                final_synthesis=(
                    "The Elliott Wave count predicts an earnings announcement."
                ),
            ),
            "technical_event_prediction",
        )


if __name__ == "__main__":
    unittest.main()
