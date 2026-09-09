from __future__ import annotations

import unittest
from dataclasses import replace

from elliott_ai.exposure_engine import (
    EXPOSURE_CONFIDENCE_VERSION,
    EXPOSURE_MAPPING_VERSION,
    ExposureMappingResult,
    aggregate_exposure_confidence,
    map_evidence_to_exposures,
)
from elliott_ai.market_scenario import (
    EXPOSURE_CATEGORY_COMPATIBILITY,
    EXPOSURE_TYPE_CATEGORY,
    EvidenceImplication,
    EvidenceItem,
    EvidenceStatus,
    ExposureCategory,
    ExposureCoverageState,
    ExposureItem,
    ExposureType,
)


CUTOFF = "2026-07-28T16:00:00+00:00"
RETRIEVED = "2026-07-29T09:00:00+00:00"


def _evidence(
    evidence_id: str,
    *,
    category: str = "revenue_growth_risk",
    implication: EvidenceImplication = EvidenceImplication.SUPPORTIVE,
    status: EvidenceStatus = EvidenceStatus.CONFIRMED,
    confidence: str = "moderate",
    claim: str | None = None,
    related_event_id: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        category=category,
        claim=claim or f"Revenue growth evidence for {evidence_id}.",
        source=f"https://example.test/{evidence_id}",
        source_type="primary_source",
        provider="Example Provider",
        publication_date="2026-07-20",
        retrieval_timestamp=RETRIEVED,
        applicable_cutoff=CUTOFF,
        status=status,
        implication=implication,
        confidence=confidence,
        relevance_to_technical_scenario="Could affect the frozen scenario.",
        related_event_id=related_event_id,
        warnings=(
            ("Stale evidence retained as qualified context.",)
            if status is EvidenceStatus.STALE
            else ()
        ),
    )


def _by_key(
    result: ExposureMappingResult,
) -> dict[tuple[ExposureCategory, ExposureType], ExposureItem]:
    return {
        (item.category, item.exposure_type): item
        for item in result.exposures
    }


def _coverage(
    result: ExposureMappingResult,
    category: ExposureCategory,
):
    return next(item for item in result.coverage if item.category is category)


class ExposureTaxonomyTests(unittest.TestCase):
    def test_category_vocabulary_is_separate_and_stable(self) -> None:
        self.assertEqual(
            tuple(item.value for item in ExposureCategory),
            (
                "company",
                "sector_and_competitors",
                "macro_and_cross_asset",
                "valuation_and_positioning",
                "event_calendar",
            ),
        )

    def test_mechanism_vocabulary_is_complete_and_stable(self) -> None:
        expected = {
            "revenue_growth_risk",
            "revenue_visibility",
            "earnings_risk",
            "guidance_risk",
            "margin_pressure",
            "cash_burn_risk",
            "liquidity_risk",
            "financing_risk",
            "dilution_risk",
            "debt_risk",
            "execution_risk",
            "operational_risk",
            "development_delay_risk",
            "supply_chain_risk",
            "customer_concentration_risk",
            "contract_risk",
            "regulatory_risk",
            "litigation_risk",
            "management_risk",
            "acquisition_integration_risk",
            "sector_demand_risk",
            "sector_valuation_risk",
            "competitive_pressure",
            "pricing_pressure",
            "market_share_risk",
            "competitor_execution_risk",
            "sector_regulatory_risk",
            "government_spending_exposure",
            "sector_supply_chain_risk",
            "sector_sentiment_risk",
            "interest_rate_sensitivity",
            "inflation_sensitivity",
            "recession_sensitivity",
            "liquidity_conditions",
            "credit_conditions",
            "equity_risk_appetite",
            "small_cap_sensitivity",
            "growth_stock_sensitivity",
            "currency_exposure",
            "commodity_exposure",
            "geopolitical_exposure",
            "valuation_compression",
            "valuation_expansion",
            "growth_expectation_risk",
            "analyst_revision_risk",
            "institutional_positioning",
            "insider_positioning",
            "short_interest_risk",
            "options_positioning",
            "crowded_trade_risk",
            "ownership_concentration_risk",
            "momentum_unwind_risk",
            "earnings_event_risk",
            "product_milestone_risk",
            "launch_event_risk",
            "regulatory_event_risk",
            "contract_award_risk",
            "financing_event_risk",
            "central_bank_event_risk",
            "economic_release_risk",
            "index_event_risk",
            "sell_the_news_risk",
            "pre_event_uncertainty",
        }
        self.assertEqual({item.value for item in ExposureType}, expected)
        self.assertTrue(EXPOSURE_MAPPING_VERSION)
        self.assertTrue(EXPOSURE_CONFIDENCE_VERSION)

    def test_compatibility_map_is_total_disjoint_and_reversible(self) -> None:
        self.assertEqual(
            set(EXPOSURE_CATEGORY_COMPATIBILITY),
            set(ExposureCategory),
        )
        flattened = [
            exposure_type
            for category in ExposureCategory
            for exposure_type in EXPOSURE_CATEGORY_COMPATIBILITY[category]
        ]
        self.assertEqual(set(flattened), set(ExposureType))
        self.assertEqual(len(flattened), len(set(flattened)))
        for exposure_type in ExposureType:
            category = EXPOSURE_TYPE_CATEGORY[exposure_type]
            self.assertIn(
                exposure_type,
                EXPOSURE_CATEGORY_COMPATIBILITY[category],
            )

    def test_old_broad_value_is_not_accepted_as_exposure_type(self) -> None:
        payload = {
            "exposure_id": "exposure-company",
            "category": "company",
            "exposure_type": "company",
            "summary": "Semantically invalid legacy payload.",
            "materiality": "unknown",
            "direction": "unknown",
            "possible_magnitude": None,
            "time_horizon": None,
            "status": "unknown",
            "supporting_evidence_ids": [],
            "contradicting_evidence_ids": [],
            "related_technical_outcomes": [],
        }
        with self.assertRaises(ValueError):
            ExposureItem.from_dict(payload)

    def test_invalid_enums_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ExposureCategory("not_a_category")
        with self.assertRaises(ValueError):
            ExposureType("not_a_mechanism")


class ExposureMappingTests(unittest.TestCase):
    def test_specific_evidence_maps_to_specific_mechanism(self) -> None:
        result = map_evidence_to_exposures((_evidence("ev-revenue"),))
        self.assertEqual(len(result.exposures), 1)
        exposure = result.exposures[0]
        self.assertIs(exposure.category, ExposureCategory.COMPANY)
        self.assertIs(
            exposure.exposure_type,
            ExposureType.REVENUE_GROWTH_RISK,
        )

    def test_one_evidence_item_can_map_to_multiple_company_mechanisms(self) -> None:
        evidence = _evidence(
            "ev-runway",
            category="company",
            claim=(
                "Cash burn increased, creating financing risk and dilution risk."
            ),
        )
        result = map_evidence_to_exposures((evidence,))
        self.assertEqual(
            {item.exposure_type for item in result.exposures},
            {
                ExposureType.CASH_BURN_RISK,
                ExposureType.FINANCING_RISK,
                ExposureType.DILUTION_RISK,
            },
        )
        self.assertTrue(
            all(
                item.category is ExposureCategory.COMPANY
                for item in result.exposures
            )
        )
        self.assertTrue(
            all(
                item.supporting_evidence_ids == ("ev-runway",)
                for item in result.exposures
            )
        )

    def test_duplicate_mechanism_contributions_merge(self) -> None:
        support = _evidence(
            "ev-support",
            implication=EvidenceImplication.SUPPORTIVE,
            confidence="high",
        )
        oppose = _evidence(
            "ev-oppose",
            implication=EvidenceImplication.CONTRADICTORY,
            confidence="low",
        )
        result = map_evidence_to_exposures((support, oppose))
        self.assertEqual(len(result.exposures), 1)
        exposure = result.exposures[0]
        self.assertEqual(exposure.supporting_evidence_ids, ("ev-support",))
        self.assertEqual(exposure.contradicting_evidence_ids, ("ev-oppose",))
        self.assertEqual(exposure.direction, "mixed")
        self.assertEqual(exposure.status, EvidenceStatus.INTERPRETATION)
        self.assertEqual(exposure.confidence, "moderate")

    def test_different_mechanisms_in_one_category_do_not_merge(self) -> None:
        revenue = _evidence("ev-revenue")
        margin = _evidence(
            "ev-margin",
            category="margin",
            claim="Margin pressure increased.",
        )
        result = map_evidence_to_exposures((revenue, margin))
        self.assertEqual(len(result.exposures), 2)
        self.assertEqual(
            {item.exposure_type for item in result.exposures},
            {
                ExposureType.REVENUE_GROWTH_RISK,
                ExposureType.MARGIN_PRESSURE,
            },
        )

    def test_generic_broad_evidence_remains_unmapped(self) -> None:
        evidence = _evidence(
            "ev-generic",
            category="company",
            claim="The company disclosed a material update.",
        )
        result = map_evidence_to_exposures((evidence,))
        self.assertEqual(result.exposures, ())
        self.assertEqual(result.unmapped_evidence_ids, ("ev-generic",))
        self.assertIn(
            "ambiguous_exposure_mechanism",
            {item.code for item in result.warnings},
        )
        company = _coverage(result, ExposureCategory.COMPANY)
        self.assertEqual(company.exposure_ids, ())
        self.assertEqual(company.unmapped_evidence_ids, ("ev-generic",))

    def test_unsupported_evidence_category_is_warned_not_fabricated(self) -> None:
        evidence = _evidence(
            "ev-unsupported",
            category="miscellaneous",
            claim="A general disclosure was published.",
        )
        result = map_evidence_to_exposures((evidence,))
        self.assertEqual(result.exposures, ())
        self.assertEqual(result.unmapped_evidence_ids, ("ev-unsupported",))
        self.assertEqual(
            {item.code for item in result.warnings},
            {
                "unsupported_evidence_category",
                "ambiguous_exposure_mechanism",
            },
        )

    def test_explicit_compatible_mapping_is_supported(self) -> None:
        evidence = _evidence(
            "ev-explicit",
            category="company",
            claim="The disclosure requires explicit classification.",
        )
        result = map_evidence_to_exposures(
            (evidence,),
            exposure_types_by_evidence={
                "ev-explicit": (ExposureType.OPERATIONAL_RISK,)
            },
        )
        self.assertEqual(
            tuple(item.exposure_type for item in result.exposures),
            (ExposureType.OPERATIONAL_RISK,),
        )
        self.assertEqual(result.unmapped_evidence_ids, ())

    def test_incompatible_forced_mapping_is_rejected_with_warning(self) -> None:
        evidence = _evidence(
            "ev-forced",
            category="company",
            claim="A general company disclosure.",
        )
        result = map_evidence_to_exposures(
            (evidence,),
            exposure_types_by_evidence={
                "ev-forced": (ExposureType.INTEREST_RATE_SENSITIVITY,)
            },
        )
        self.assertEqual(result.exposures, ())
        self.assertEqual(result.unmapped_evidence_ids, ("ev-forced",))
        forced = next(
            item
            for item in result.warnings
            if item.code == "unsupported_forced_mapping"
        )
        self.assertEqual(
            forced.requested_exposure_type,
            ExposureType.INTEREST_RATE_SENSITIVITY.value,
        )

    def test_mapping_is_independent_of_input_order(self) -> None:
        first = _evidence(
            "ev-z",
            category="interest_rates",
            claim="Interest-rate sensitivity increased.",
        )
        second = _evidence(
            "ev-a",
            category="interest_rates",
            implication=EvidenceImplication.CONTRADICTORY,
            claim="Interest-rate sensitivity declined.",
        )
        forward = map_evidence_to_exposures((first, second))
        reverse = map_evidence_to_exposures((second, first))
        self.assertEqual(forward, reverse)
        exposure = forward.exposures[0]
        self.assertEqual(exposure.supporting_evidence_ids, ("ev-z",))
        self.assertEqual(exposure.contradicting_evidence_ids, ("ev-a",))

    def test_output_order_is_category_then_enum_order(self) -> None:
        evidence = (
            _evidence(
                "ev-event",
                category="economic_release",
                claim="Economic release risk is scheduled.",
            ),
            _evidence(
                "ev-margin",
                category="margin",
                claim="Margin pressure increased.",
            ),
            _evidence(
                "ev-rates",
                category="interest_rates",
                claim="Interest-rate sensitivity increased.",
            ),
            _evidence(
                "ev-revenue",
                category="revenue_growth",
                claim="Revenue growth slowed.",
            ),
        )
        result = map_evidence_to_exposures(evidence)
        self.assertEqual(
            tuple(
                (item.category, item.exposure_type)
                for item in result.exposures
            ),
            (
                (
                    ExposureCategory.COMPANY,
                    ExposureType.REVENUE_GROWTH_RISK,
                ),
                (ExposureCategory.COMPANY, ExposureType.MARGIN_PRESSURE),
                (
                    ExposureCategory.MACRO_AND_CROSS_ASSET,
                    ExposureType.INTEREST_RATE_SENSITIVITY,
                ),
                (
                    ExposureCategory.EVENT_CALENDAR,
                    ExposureType.ECONOMIC_RELEASE_RISK,
                ),
            ),
        )

    def test_hypotheses_and_explicit_assumptions_are_separate(self) -> None:
        confirmed = _evidence(
            "ev-confirmed",
            claim="Revenue growth was reported.",
        )
        hypothetical = _evidence(
            "ev-hypothesis",
            status=EvidenceStatus.HYPOTHETICAL,
            claim="Revenue growth could slow.",
        )
        result = map_evidence_to_exposures(
            (confirmed, hypothetical),
            assumptions_by_exposure={
                ExposureType.REVENUE_GROWTH_RISK: (
                    "Demand remains unchanged.",
                    "Demand remains unchanged.",
                )
            },
            related_technical_outcomes_by_exposure={
                ExposureType.REVENUE_GROWTH_RISK: (
                    "target range reached",
                    "target range reached",
                )
            },
        )
        exposure = result.exposures[0]
        self.assertEqual(
            exposure.assumptions,
            ("Demand remains unchanged.", "Revenue growth could slow."),
        )
        self.assertNotIn("Revenue growth was reported.", exposure.assumptions)
        self.assertEqual(
            exposure.related_technical_outcomes,
            ("target range reached",),
        )

    def test_neutral_and_incomparable_evidence_are_not_directional_ids(self) -> None:
        neutral = _evidence(
            "ev-neutral",
            implication=EvidenceImplication.NEUTRAL,
        )
        incomparable = _evidence(
            "ev-incomparable",
            implication=EvidenceImplication.INCOMPARABLE,
        )
        result = map_evidence_to_exposures((neutral, incomparable))
        exposure = result.exposures[0]
        self.assertEqual(exposure.supporting_evidence_ids, ())
        self.assertEqual(exposure.contradicting_evidence_ids, ())
        self.assertEqual(exposure.direction, "neutral")
        self.assertEqual(exposure.confidence, "insufficient_evidence")

    def test_duplicate_evidence_ids_are_rejected_before_merging(self) -> None:
        first = _evidence("ev-duplicate")
        second = _evidence(
            "ev-duplicate",
            implication=EvidenceImplication.CONTRADICTORY,
        )
        with self.assertRaisesRegex(ValueError, "Duplicate evidence IDs"):
            map_evidence_to_exposures((first, second))

    def test_mapper_does_not_mutate_evidence(self) -> None:
        evidence = _evidence("ev-stable")
        before = evidence.to_dict()
        map_evidence_to_exposures((evidence,))
        self.assertEqual(evidence.to_dict(), before)

    def test_annotations_reject_unknown_mechanism(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported exposure type"):
            map_evidence_to_exposures(
                (_evidence("ev-company"),),
                assumptions_by_exposure={
                    "not_an_exposure": ("Assumption.",)
                },
            )

    def test_mapping_result_round_trip_is_deterministic(self) -> None:
        original = map_evidence_to_exposures(
            (
                _evidence("ev-revenue"),
                _evidence(
                    "ev-ambiguous",
                    category="sector_and_competitors",
                    claim="Sector context was reviewed.",
                ),
            ),
            coverage_by_category={
                ExposureCategory.SECTOR_AND_COMPETITORS:
                    ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
                ExposureCategory.MACRO_AND_CROSS_ASSET:
                    ExposureCoverageState.UNAVAILABLE,
                ExposureCategory.VALUATION_AND_POSITIONING:
                    ExposureCoverageState.UNAVAILABLE,
                ExposureCategory.EVENT_CALENDAR:
                    ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
            },
        )
        restored = ExposureMappingResult.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.to_dict(), original.to_dict())


class ExposureCoverageTests(unittest.TestCase):
    def test_coverage_distinguishes_all_three_meaningful_states(self) -> None:
        result = map_evidence_to_exposures(
            (_evidence("ev-company"),),
            coverage_by_category={
                ExposureCategory.SECTOR_AND_COMPETITORS:
                    ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
                ExposureCategory.MACRO_AND_CROSS_ASSET:
                    ExposureCoverageState.UNAVAILABLE,
                ExposureCategory.VALUATION_AND_POSITIONING:
                    ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
                ExposureCategory.EVENT_CALENDAR:
                    ExposureCoverageState.UNAVAILABLE,
            },
        )
        self.assertEqual(len(result.coverage), len(ExposureCategory))
        self.assertIs(
            _coverage(result, ExposureCategory.COMPANY).state,
            ExposureCoverageState.EXPOSURES_IDENTIFIED,
        )
        self.assertIs(
            _coverage(
                result, ExposureCategory.SECTOR_AND_COMPETITORS
            ).state,
            ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
        )
        self.assertIs(
            _coverage(
                result, ExposureCategory.MACRO_AND_CROSS_ASSET
            ).state,
            ExposureCoverageState.UNAVAILABLE,
        )

    def test_missing_coverage_is_explicitly_undeclared(self) -> None:
        result = map_evidence_to_exposures((_evidence("ev-company"),))
        self.assertIs(
            _coverage(
                result, ExposureCategory.SECTOR_AND_COMPETITORS
            ).state,
            ExposureCoverageState.UNDECLARED,
        )

    def test_empty_categories_do_not_create_fake_exposure_items(self) -> None:
        result = map_evidence_to_exposures(
            (),
            coverage_by_category={
                category: ExposureCoverageState.UNAVAILABLE
                for category in ExposureCategory
            },
        )
        self.assertEqual(result.exposures, ())
        self.assertEqual(len(result.coverage), 5)
        self.assertTrue(
            all(
                item.state is ExposureCoverageState.UNAVAILABLE
                for item in result.coverage
            )
        )

    def test_conflicting_coverage_is_derived_and_warned(self) -> None:
        result = map_evidence_to_exposures(
            (
                _evidence(
                    "ev-neutral",
                    implication=EvidenceImplication.NEUTRAL,
                ),
            ),
            coverage_by_category={
                ExposureCategory.COMPANY:
                    ExposureCoverageState.UNAVAILABLE
            },
        )
        self.assertIs(
            _coverage(result, ExposureCategory.COMPANY).state,
            ExposureCoverageState.EXPOSURES_IDENTIFIED,
        )
        warning = next(
            item
            for item in result.warnings
            if item.code == "coverage_declaration_conflict"
        )
        self.assertEqual(warning.evidence_id, "ev-neutral")


class ExposureConfidenceTests(unittest.TestCase):
    def test_high_confirmed_support_without_opposition_stays_high(self) -> None:
        self.assertEqual(
            aggregate_exposure_confidence(
                (_evidence("ev-high", confidence="high"),)
            ),
            "high",
        )

    def test_any_real_opposition_caps_stronger_lane_at_moderate(self) -> None:
        evidence = (
            _evidence("ev-high", confidence="high"),
            _evidence(
                "ev-low-opposition",
                implication=EvidenceImplication.CONTRADICTORY,
                confidence="low",
            ),
        )
        self.assertEqual(
            aggregate_exposure_confidence(evidence),
            "moderate",
        )

    def test_balanced_directional_strength_returns_low(self) -> None:
        evidence = (
            _evidence("ev-support", confidence="high"),
            _evidence(
                "ev-oppose",
                implication=EvidenceImplication.CONTRADICTORY,
                confidence="high",
            ),
        )
        self.assertEqual(aggregate_exposure_confidence(evidence), "low")

    def test_evidence_count_does_not_increase_confidence(self) -> None:
        one = (_evidence("ev-one", confidence="low"),)
        many = tuple(
            _evidence(f"ev-{index}", confidence="low")
            for index in range(10)
        )
        self.assertEqual(aggregate_exposure_confidence(one), "low")
        self.assertEqual(aggregate_exposure_confidence(many), "low")

    def test_hypothetical_and_stale_evidence_are_capped_at_low(self) -> None:
        hypothetical = _evidence(
            "ev-hypothetical",
            status=EvidenceStatus.HYPOTHETICAL,
            confidence="high",
        )
        stale = _evidence(
            "ev-stale",
            status=EvidenceStatus.STALE,
            confidence="high",
        )
        self.assertEqual(
            aggregate_exposure_confidence((hypothetical,)),
            "low",
        )
        self.assertEqual(
            aggregate_exposure_confidence((stale,)),
            "low",
        )

    def test_empty_unavailable_and_nondirectional_states_are_explicit(self) -> None:
        self.assertEqual(
            aggregate_exposure_confidence(()),
            "unavailable",
        )
        unavailable = _evidence(
            "ev-unavailable",
            status=EvidenceStatus.UNAVAILABLE,
        )
        neutral = _evidence(
            "ev-neutral",
            implication=EvidenceImplication.NEUTRAL,
        )
        self.assertEqual(
            aggregate_exposure_confidence((unavailable,)),
            "unavailable",
        )
        self.assertEqual(
            aggregate_exposure_confidence((neutral,)),
            "insufficient_evidence",
        )

    def test_invalid_confidence_is_not_silently_aggregated(self) -> None:
        invalid = replace(_evidence("ev-invalid"), confidence="certain")
        with self.assertRaisesRegex(ValueError, "unsupported values"):
            aggregate_exposure_confidence((invalid,))


if __name__ == "__main__":
    unittest.main()
