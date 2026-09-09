from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from elliott_ai.company_knowledge import (
    COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION,
    CandidateEligibilityStatus,
    CompanyEventArchetypeLibrary,
    CompanyEventArchetypeType,
    CompanyEventCandidateBuilder,
    CompanyKnowledgeEngine,
    CompanyKnowledgeInput,
    CompanyKnowledgeReplayPacket,
    CompanyKnowledgeSnapshot,
    CompanyScenarioContext,
    CompanyStage,
    CompanyStateValue,
    SpaceOperationalExtension,
    build_company_scenario_context,
    company_event_archetype_library_content_hash,
    company_event_candidate_content_hash,
    company_knowledge_replay_packet_content_hash,
    company_knowledge_snapshot_content_hash,
    company_scenario_context_content_hash,
    company_snapshot_to_market_inputs,
    create_company_knowledge_replay_packet,
    default_company_event_archetype_library,
    replay_company_knowledge,
    validate_company_event_archetype_library,
    validate_company_knowledge_snapshot,
    validate_company_scenario_usage,
)
from elliott_ai.current_scenario_generator import (
    CurrentMarketScenarioGenerator,
)
from elliott_ai.company_knowledge_bridge import (
    CompanyOrchestrationBridgeResult,
    company_orchestration_bridge_result_content_hash,
    run_company_orchestration_bridge,
)
from elliott_ai.current_state import (
    TechnicalDirection,
    finalize_current_market_state,
)
from elliott_ai.current_scenarios import (
    HypotheticalFutureEvent,
)
from elliott_ai.market_scenario_evaluation import (
    HARD_FAILURE_COMPANY_EVENT_CONFUSION,
    HARD_FAILURE_COMPANY_SNAPSHOT_MUTATION,
    HARD_FAILURE_HYPOTHETICAL_AS_FACT,
    evaluate_company_knowledge,
)
from elliott_ai.market_scenario import (
    ScenarioDirection,
    ValidationResult,
    frozen_technical_premise_content_hash,
)
from scripts.test_current_scenario_generator import (
    FIXED_NOW,
    FixtureProvider,
    _generator,
    _inputs,
    _scenario_set_response,
)
from scripts.test_market_scenario_orchestrator import (
    _make_input as _orchestration_input,
    _run as _run_orchestration,
)


def _profile(
    *,
    stage: str = "mature",
    industry: str = "Software",
) -> dict:
    return {
        "company_stage": stage,
        "revenue_model": "subscription",
        "primary_products": ["Core platform"],
        "primary_services": ["Support"],
        "geographic_exposure": ["United States"],
        "customer_types": ["enterprise"],
        "operating_history_class": "established",
        "cyclicality_class": "low",
        "capital_intensity_class": (
            "high" if industry == "Space" else "low"
        ),
        "regulatory_intensity_class": (
            "high" if industry in {"Space", "Biotechnology"} else "low"
        ),
    }


def _financial(**overrides) -> dict:
    result = {
        "revenue_state": "strong",
        "revenue_growth_state": "improving",
        "profitability_state": "profitable",
        "gross_margin_state": "strong",
        "operating_margin_state": "strong",
        "free_cash_flow_state": "positive",
        "cash_balance_state": "strong",
        "cash_runway_state": "strong",
        "debt_state": "low",
        "debt_maturity_state": "low",
        "interest_burden_state": "low",
        "liquidity_state": "strong",
        "working_capital_state": "strong",
        "capital_expenditure_state": "low",
        "financing_dependency_state": "low",
        "dilution_dependency_state": "low",
        "guidance_state": "improving",
        "estimate_dispersion_state": "low",
        "valuation_state": "moderate",
        "data_as_of": "2026-07-29",
        "quantitative_values": {"cash": 1000.0},
    }
    result.update(overrides)
    return result


def _operational(**overrides) -> dict:
    result = {
        "production_state": "strong",
        "service_delivery_state": "strong",
        "execution_state": "strong",
        "product_development_state": "strong",
        "launch_state": "unknown",
        "manufacturing_state": "unknown",
        "supply_chain_state": "stable",
        "backlog_state": "adequate",
        "customer_demand_state": "improving",
        "customer_concentration_state": "low",
        "contract_dependency_state": "low",
        "regulatory_approval_state": "adequate",
        "litigation_state": "absent",
        "management_execution_state": "strong",
        "hiring_state": "stable",
        "geographic_dependency_state": "low",
        "infrastructure_dependency_state": "low",
        "operational_bottlenecks": [],
        "data_as_of": "2026-07-29",
    }
    result.update(overrides)
    return result


def _capital(**overrides) -> dict:
    result = {
        "share_count_state": "stable",
        "recent_dilution_state": "absent",
        "authorized_share_capacity": 100.0,
        "equity_raise_history": [],
        "debt_instruments": [],
        "convertible_instruments": [],
        "warrant_overhang": "absent",
        "employee_compensation_dilution": "low",
        "refinancing_requirements": [],
        "covenant_constraints": [],
        "maturity_schedule": [],
        "capital_access_state": "strong",
        "financing_optionalities": ["cash flow"],
        "data_as_of": "2026-07-29",
    }
    result.update(overrides)
    return result


def _ownership(**overrides) -> dict:
    result = {
        "insider_ownership_state": "moderate",
        "institutional_ownership_state": "high",
        "concentrated_holder_state": "low",
        "short_interest_state": "low",
        "borrow_availability_state": "strong",
        "option_positioning_state": "moderate",
        "lockup_state": "absent",
        "insider_transaction_state": "stable",
        "index_membership_state": "present",
        "passive_flow_exposure": "moderate",
        "known_positioning_events": [],
        "data_as_of": "2026-07-29",
    }
    result.update(overrides)
    return result


def _event(
    event_id: str,
    event_type: str,
    *,
    start: str = "2026-08-01",
    announced_at: str = "2026-07-01T00:00:00Z",
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "title": event_type.replace("_", " ").title(),
        "announced_at": announced_at,
        "scheduled_start": start,
        "scheduled_end": None,
        "date_precision": "date_only",
        "status": "scheduled",
        "recurrence": "one_time",
        "source_type": "company_ir",
        "source_reference": "fixture://company-event",
        "known_at_cutoff": True,
        "expected_information_type": ["company update"],
    }


def _risk(
    item_id: str,
    risk_type: str,
    supporting_field: str,
    **extra,
) -> dict:
    result = {
        "item_id": item_id,
        "type": risk_type,
        "description": f"Supplied {risk_type} condition.",
        "supporting_fields": [supporting_field],
        "dependency_ids": [],
        "scheduled_event_ids": [],
        "preconditions": ["supplied condition remains present"],
        "invalidating_conditions": ["supplied condition resolves"],
        "status": "active",
    }
    result.update(extra)
    return result


def _opportunity(
    item_id: str,
    opportunity_type: str,
    supporting_field: str,
    **extra,
) -> dict:
    result = {
        "item_id": item_id,
        "type": opportunity_type,
        "description": f"Supplied {opportunity_type} condition.",
        "supporting_fields": [supporting_field],
        "dependency_ids": [],
        "scheduled_event_ids": [],
        "preconditions": ["supplied condition remains present"],
        "invalidating_conditions": ["supplied condition resolves"],
        "status": "active",
    }
    result.update(extra)
    return result


def _company_input(
    *,
    profile=None,
    financial=None,
    operational=None,
    capital=None,
    ownership=None,
    events=(),
    dependencies=(),
    historical=(),
    risks=(),
    opportunities=(),
    input_id: str = "phase10_fixture",
) -> CompanyKnowledgeInput:
    premise, state, _, _, _, _ = _inputs()
    profile_values = profile if profile is not None else _profile()
    return CompanyKnowledgeInput.create(
        input_id=input_id,
        symbol=state.symbol,
        company_name="Fixture Company",
        exchange=state.exchange,
        jurisdiction="United States",
        sector="Technology",
        industry=(
            "Software"
            if profile is None
            else str(profile_values.get("industry", "Software"))
        ),
        business_model="supplied fixture model",
        applicable_cutoff=state.applicable_cutoff,
        company_profile_inputs=profile_values,
        financial_state_inputs=(
            _financial() if financial is None else financial
        ),
        operational_state_inputs=(
            _operational() if operational is None else operational
        ),
        capital_structure_inputs=(
            _capital() if capital is None else capital
        ),
        ownership_and_positioning_inputs=(
            _ownership() if ownership is None else ownership
        ),
        scheduled_event_inputs=tuple(events),
        dependency_inputs=tuple(dependencies),
        historical_company_event_inputs=tuple(historical),
        known_risk_inputs=tuple(risks),
        known_opportunity_inputs=tuple(opportunities),
        source_metadata={
            "source_id": "phase10_fixture",
            "source_type": "offline_structured_fixture",
            "source": "unit-test fixture",
            "retrieval_cutoff": state.applicable_cutoff,
        },
        created_at=FIXED_NOW,
    )


def _built_context(value: CompanyKnowledgeInput, *, bearish: bool = False):
    premise, state, _, _, exposures, _ = _inputs()
    if bearish:
        premise_seed = replace(
            premise,
            direction=ScenarioDirection.DOWN,
            frozen_content_hash="",
        )
        premise = replace(
            premise_seed,
            frozen_content_hash=frozen_technical_premise_content_hash(
                premise_seed
            ),
        )
        state_seed = replace(
            state,
            technical_direction=TechnicalDirection.BEARISH,
            frozen_technical_premise_hash=premise.frozen_content_hash,
            validation_result=ValidationResult.unvalidated(),
            content_hash="",
        )
        state = finalize_current_market_state(
            state_seed,
            premise=premise,
        )
    build = CompanyKnowledgeEngine().build(value)
    if build.snapshot is None:
        raise AssertionError(build.errors)
    library = default_company_event_archetype_library()
    context = build_company_scenario_context(
        build.snapshot,
        premise,
        state,
        exposures,
        library=library,
    )
    return premise, state, exposures, build, library, context


def _candidate(context, event_type: CompanyEventArchetypeType):
    return next(
        item
        for item in (
            context.eligible_event_candidates
            + context.excluded_event_candidates
        )
        if item.event_type is event_type
    )


def _response_with_company_candidate(inputs, candidate):
    raw = _scenario_set_response(inputs)
    scenario = raw["scenarios"][0]
    scenario["hypothetical_future_events"] = [
        {
            "hypothetical_event_id": candidate.candidate_id,
            "event_type": candidate.event_type.value,
            "description": (
                f"A hypothetical {candidate.event_type.value} could occur "
                "only if the supplied preconditions persist."
            ),
            "timing_window": (
                "It may occur within the frozen technical duration window."
            ),
            "assumptions": [
                "The event is a conditional pathway, not known future news."
            ],
            "uncertainty": "The event may never occur.",
            "explicitly_hypothetical": True,
        }
    ]
    scenario["timeline_sequence"][1]["hypothetical_event_ids"] = [
        candidate.candidate_id
    ]
    references = (
        candidate.supporting_company_facts
        + candidate.supporting_dependencies
        + candidate.supporting_structural_risks
        + candidate.supporting_structural_opportunities
        + candidate.related_scheduled_events
    )
    tokens = [
        (
            f"company_classification:{candidate.candidate_id}:"
            f"{candidate.event_classification.value}"
        ),
        f"company_ref:{references[0]}",
    ]
    tokens.extend(
        f"company_contradiction:{item}"
        for item in candidate.contradictory_conditions
    )
    tokens.extend(
        f"company_missing:{item}"
        for item in candidate.missing_preconditions
    )
    scenario["assumptions"].extend(tokens)
    return raw


class CompanyKnowledgeContractTests(unittest.TestCase):
    def test_default_library_is_versioned_hash_stable_and_complete(self):
        first = default_company_event_archetype_library()
        second = default_company_event_archetype_library()
        self.assertEqual(first, second)
        self.assertEqual(
            first.library_version,
            COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION,
        )
        self.assertEqual(
            first.content_hash,
            company_event_archetype_library_content_hash(first),
        )
        self.assertTrue(
            validate_company_event_archetype_library(first).is_valid
        )
        required = {
            CompanyEventArchetypeType.EARNINGS_MISS,
            CompanyEventArchetypeType.EARNINGS_BEAT,
            CompanyEventArchetypeType.LAUNCH_DELAY,
            CompanyEventArchetypeType.LAUNCH_SUCCESS,
            CompanyEventArchetypeType.CAPITAL_RAISE,
            CompanyEventArchetypeType.DEBT_REFINANCING,
            CompanyEventArchetypeType.CONTRACT_AWARD,
            CompanyEventArchetypeType.CONTRACT_CANCELLATION,
        }
        self.assertTrue(
            required <= {item.archetype_type for item in first.archetypes}
        )

    def test_snapshot_is_immutable_hash_stable_and_serializable(self):
        _, _, _, build, _, _ = _built_context(_company_input())
        snapshot = build.snapshot
        self.assertTrue(snapshot.validation_result.is_valid)
        self.assertEqual(
            snapshot.content_hash,
            company_knowledge_snapshot_content_hash(snapshot),
        )
        self.assertEqual(
            CompanyKnowledgeSnapshot.from_dict(snapshot.to_dict()),
            snapshot,
        )
        with self.assertRaises(TypeError):
            snapshot.source_metadata["new"] = "value"

    def test_sparse_input_stays_unknown_without_hidden_inference(self):
        sparse = _company_input(
            profile={"industry": "Software"},
            financial={},
            operational={},
            capital={},
            ownership={},
            input_id="sparse_fixture",
        )
        build = CompanyKnowledgeEngine().build(sparse)
        self.assertTrue(build.snapshot.validation_result.is_valid)
        self.assertIs(
            build.snapshot.financial_state.cash_runway_state,
            CompanyStateValue.UNKNOWN,
        )
        self.assertIs(
            build.snapshot.company_profile.company_stage,
            CompanyStage.UNKNOWN,
        )
        self.assertIn(
            "financial.cash_runway_state",
            build.snapshot.known_missing_information,
        )

    def test_contradictory_state_is_unknown_and_warned(self):
        financial = _financial(
            liquidity_state=["strong", "constrained"]
        )
        build = CompanyKnowledgeEngine().build(
            _company_input(
                financial=financial,
                input_id="contradictory_fixture",
            )
        )
        self.assertIs(
            build.snapshot.financial_state.liquidity_state,
            CompanyStateValue.UNKNOWN,
        )
        self.assertIn(
            "contradictory_state:liquidity_state",
            build.warnings,
        )

    def test_space_extension_is_typed(self):
        operational = _operational(
            space_extension={
                "launch_cadence_state": "adequate",
                "launch_reliability_state": "strong",
                "mission_backlog_state": "strong",
                "launch_site_dependency_state": "high",
                "payload_concentration_state": "moderate",
                "government_contract_exposure_state": "high",
            }
        )
        build = CompanyKnowledgeEngine().build(
            _company_input(
                profile={
                    **_profile(stage="early_revenue", industry="Space"),
                    "industry": "Space",
                },
                operational=operational,
                input_id="space_extension_fixture",
            )
        )
        self.assertIsInstance(
            build.snapshot.operational_state.space_extension,
            SpaceOperationalExtension,
        )

    def test_scheduled_event_after_cutoff_is_rejected(self):
        value = _company_input(
            events=(
                _event(
                    "late_event",
                    "earnings_release",
                    announced_at="2026-08-02T00:00:00Z",
                ),
            ),
            input_id="late_event_fixture",
        )
        build = CompanyKnowledgeEngine().build(value)
        self.assertFalse(build.snapshot.validation_result.is_valid)
        self.assertIn(
            "scheduled_event_cutoff",
            build.snapshot.validation_result.failed_rules,
        )

    def test_invalid_structural_reference_is_rejected(self):
        value = _company_input(
            risks=(
                {
                    **_risk(
                        "bad_ref",
                        "liquidity",
                        "financial.liquidity_state",
                    ),
                    "dependency_ids": ["missing_dependency"],
                },
            ),
            input_id="invalid_reference_fixture",
        )
        build = CompanyKnowledgeEngine().build(value)
        self.assertFalse(build.snapshot.validation_result.is_valid)
        self.assertIn(
            "snapshot_references",
            build.snapshot.validation_result.failed_rules,
        )

    def test_market_input_bridge_is_neutral_and_invents_no_exposure(self):
        _, _, _, build, _, _ = _built_context(
            _company_input(
                events=(_event("earnings", "earnings_release"),),
            )
        )
        bridge = company_snapshot_to_market_inputs(build.snapshot)
        self.assertTrue(bridge.evidence)
        self.assertEqual(len(bridge.material_events), 1)
        self.assertEqual(bridge.invented_exposures, ())
        self.assertTrue(
            all(item.implication.value == "neutral" for item in bridge.evidence)
        )


class CompanyCandidateFixtureTests(unittest.TestCase):
    def assertEligible(self, value, event_type, *, bearish=False):
        *_, context = _built_context(value, bearish=bearish)
        candidate = _candidate(context, event_type)
        self.assertIs(
            candidate.eligibility_status,
            CandidateEligibilityStatus.ELIGIBLE,
            candidate.exclusion_reasons,
        )
        return candidate

    def test_valid_earnings_beat_candidate(self):
        self.assertEligible(
            _company_input(input_id="earnings_beat_fixture"),
            CompanyEventArchetypeType.EARNINGS_BEAT,
        )

    def test_valid_earnings_miss_candidate(self):
        self.assertEligible(
            _company_input(
                financial=_financial(
                    revenue_growth_state="weak",
                    guidance_state="deteriorating",
                ),
                risks=(
                    _risk(
                        "demand_risk",
                        "demand_weakness",
                        "financial.revenue_growth_state",
                    ),
                ),
                input_id="earnings_miss_fixture",
            ),
            CompanyEventArchetypeType.EARNINGS_MISS,
            bearish=True,
        )

    def test_valid_launch_delay_and_launch_success_candidates(self):
        delay = _company_input(
            profile={
                **_profile(stage="early_revenue", industry="Space"),
                "industry": "Space",
            },
            operational=_operational(launch_state="constrained"),
            events=(_event("launch", "space_launch"),),
            risks=(
                _risk(
                    "launch_delay_risk",
                    "launch_delay",
                    "operational.launch_state",
                ),
            ),
            input_id="launch_delay_fixture",
        )
        self.assertEligible(
            delay,
            CompanyEventArchetypeType.LAUNCH_DELAY,
            bearish=True,
        )
        success = _company_input(
            profile={
                **_profile(stage="early_revenue", industry="Space"),
                "industry": "Space",
            },
            operational=_operational(launch_state="strong"),
            events=(_event("launch", "space_launch"),),
            opportunities=(
                _opportunity(
                    "launch_success_opportunity",
                    "launch_success",
                    "operational.launch_state",
                ),
            ),
            input_id="launch_success_fixture",
        )
        self.assertEligible(
            success,
            CompanyEventArchetypeType.LAUNCH_SUCCESS,
        )

    def test_valid_capital_raise_and_strong_liquidity_rejection(self):
        constrained = _company_input(
            profile={
                **_profile(stage="growth"),
                "industry": "Software",
            },
            financial=_financial(
                liquidity_state="constrained",
                financing_dependency_state="high",
            ),
            capital=_capital(
                capital_access_state="constrained",
                recent_dilution_state="present",
                equity_raise_history=["2025 equity raise"],
            ),
            risks=(
                _risk(
                    "financing_risk",
                    "financing",
                    "financial.liquidity_state",
                ),
            ),
            input_id="capital_raise_fixture",
        )
        self.assertEligible(
            constrained,
            CompanyEventArchetypeType.CAPITAL_RAISE,
            bearish=True,
        )
        strong = _company_input(
            profile={
                **_profile(stage="growth"),
                "industry": "Software",
            },
            risks=(
                _risk(
                    "financing_risk",
                    "financing",
                    "financial.liquidity_state",
                ),
            ),
            input_id="capital_raise_invalid_fixture",
        )
        *_, context = _built_context(strong)
        candidate = _candidate(
            context,
            CompanyEventArchetypeType.CAPITAL_RAISE,
        )
        self.assertIsNot(
            candidate.eligibility_status,
            CandidateEligibilityStatus.ELIGIBLE,
        )
        self.assertIn(
            "contradictory_conditions_present",
            candidate.exclusion_reasons,
        )

    def test_refinancing_contract_loss_and_contract_award(self):
        refinancing = _company_input(
            financial=_financial(
                debt_state="high",
                debt_maturity_state="constrained",
                interest_burden_state="high",
            ),
            events=(_event("maturity", "debt_maturity"),),
            risks=(
                _risk(
                    "refinancing_risk",
                    "refinancing",
                    "financial.debt_maturity_state",
                ),
            ),
            input_id="refinancing_fixture",
        )
        self.assertEligible(
            refinancing,
            CompanyEventArchetypeType.DEBT_REFINANCING,
        )
        contract_loss = _company_input(
            operational=_operational(contract_dependency_state="high"),
            dependencies=(
                {
                    "dependency_id": "major_contract",
                    "dependency_type": "contract",
                    "subject": "Fixture Company",
                    "dependency_target": "Major contract",
                    "concentration_class": "high",
                    "substitutability_class": "difficult",
                    "time_to_replace_class": "long",
                    "operational_impact_class": "high",
                    "financial_impact_class": "high",
                    "known_mitigants": [],
                },
            ),
            risks=(
                _risk(
                    "contract_loss_risk",
                    "contract_loss",
                    "operational.contract_dependency_state",
                    dependency_ids=["major_contract"],
                ),
            ),
            input_id="contract_loss_fixture",
        )
        self.assertEligible(
            contract_loss,
            CompanyEventArchetypeType.CONTRACT_CANCELLATION,
            bearish=True,
        )
        contract_award = _company_input(
            events=(_event("contract", "contract_milestone"),),
            opportunities=(
                _opportunity(
                    "contract_award_opportunity",
                    "contract_award",
                    "operational.backlog_state",
                    scheduled_event_ids=["contract"],
                ),
            ),
            input_id="contract_award_fixture",
        )
        self.assertEligible(
            contract_award,
            CompanyEventArchetypeType.CONTRACT_AWARD,
        )

    def test_industry_and_stage_incompatibility(self):
        value = _company_input(
            profile={
                **_profile(stage="mature", industry="Software"),
                "industry": "Software",
            },
            operational=_operational(launch_state="constrained"),
            risks=(
                _risk(
                    "launch_delay_risk",
                    "launch_delay",
                    "operational.launch_state",
                ),
            ),
            input_id="industry_incompatible_fixture",
        )
        *_, context = _built_context(value)
        launch = _candidate(
            context,
            CompanyEventArchetypeType.LAUNCH_DELAY,
        )
        self.assertIn(
            "incompatible_industry",
            launch.exclusion_reasons,
        )
        capital = _candidate(
            context,
            CompanyEventArchetypeType.CAPITAL_RAISE,
        )
        self.assertIn(
            "incompatible_company_stage",
            capital.exclusion_reasons,
        )

    def test_duplicate_candidate_causal_structure_is_suppressed(self):
        base = default_company_event_archetype_library()
        original = next(
            item
            for item in base.archetypes
            if item.archetype_type
            is CompanyEventArchetypeType.EARNINGS_BEAT
        )
        duplicate = replace(
            original,
            archetype_id="company_archetype_earnings_beat_duplicate",
            content_hash="",
        )
        from elliott_ai.company_knowledge import (
            company_event_archetype_content_hash,
        )

        duplicate = replace(
            duplicate,
            content_hash=company_event_archetype_content_hash(duplicate),
        )
        library = CompanyEventArchetypeLibrary.create(
            library_id="duplicate_fixture_library",
            library_version=COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION,
            archetypes=base.archetypes + (duplicate,),
        )
        premise, state, exposures, build, _, _ = _built_context(
            _company_input(input_id="duplicate_candidate_fixture")
        )
        candidates = CompanyEventCandidateBuilder(library).build(
            build.snapshot,
            premise,
            state,
            exposures,
        )
        beats = [
            item
            for item in candidates
            if item.event_type is CompanyEventArchetypeType.EARNINGS_BEAT
        ]
        self.assertEqual(
            sum(
                item.eligibility_status
                is CandidateEligibilityStatus.ELIGIBLE
                for item in beats
            ),
            1,
        )
        self.assertEqual(
            sum(
                item.eligibility_status
                is CandidateEligibilityStatus.DUPLICATE
                for item in beats
            ),
            1,
        )

    def test_fixture_company_classes(self):
        fixtures = {
            "early_stage_space": _company_input(
                profile={
                    **_profile(stage="early_revenue", industry="Space"),
                    "industry": "Space",
                },
                operational=_operational(launch_state="constrained"),
                input_id="fixture_early_stage_space",
            ),
            "mature_technology": _company_input(
                input_id="fixture_mature_technology"
            ),
            "pre_revenue_biotech": _company_input(
                profile={
                    **_profile(
                        stage="pre_revenue",
                        industry="Biotechnology",
                    ),
                    "industry": "Biotechnology",
                },
                financial=_financial(
                    revenue_state="absent",
                    profitability_state="unprofitable",
                    cash_runway_state="constrained",
                ),
                events=(_event("regulatory", "regulatory_decision"),),
                opportunities=(
                    _opportunity(
                        "approval_opportunity",
                        "regulatory_approval",
                        "operational.regulatory_approval_state",
                    ),
                ),
                input_id="fixture_pre_revenue_biotech",
            ),
            "leveraged_industrial": _company_input(
                profile={
                    **_profile(stage="mature", industry="Industrial"),
                    "industry": "Industrial",
                },
                financial=_financial(
                    debt_state="high",
                    debt_maturity_state="constrained",
                ),
                risks=(
                    _risk(
                        "refi",
                        "refinancing",
                        "financial.debt_maturity_state",
                    ),
                ),
                input_id="fixture_leveraged_industrial",
            ),
            "profitable_low_debt": _company_input(
                input_id="fixture_profitable_low_debt"
            ),
            "cash_constrained_growth": _company_input(
                profile={
                    **_profile(stage="growth"),
                    "industry": "Software",
                },
                financial=_financial(liquidity_state="constrained"),
                risks=(
                    _risk(
                        "liquidity",
                        "liquidity",
                        "financial.liquidity_state",
                    ),
                ),
                input_id="fixture_cash_constrained_growth",
            ),
            "high_customer_concentration": _company_input(
                operational=_operational(
                    customer_concentration_state="high"
                ),
                dependencies=(
                    {
                        "dependency_id": "largest_customer",
                        "dependency_type": "customer",
                        "subject": "Fixture Company",
                        "dependency_target": "Largest customer",
                        "concentration_class": "high",
                        "substitutability_class": "difficult",
                        "time_to_replace_class": "long",
                        "operational_impact_class": "high",
                        "financial_impact_class": "high",
                        "known_mitigants": [],
                    },
                ),
                risks=(
                    _risk(
                        "customer_concentration",
                        "customer_concentration",
                        "operational.customer_concentration_state",
                        dependency_ids=["largest_customer"],
                    ),
                ),
                input_id="fixture_high_customer_concentration",
            ),
            "launch_provider_dependency": _company_input(
                profile={
                    **_profile(stage="early_revenue", industry="Space"),
                    "industry": "Space",
                },
                dependencies=(
                    {
                        "dependency_id": "launch_provider",
                        "dependency_type": "launch_provider",
                        "subject": "Fixture Company",
                        "dependency_target": "External launch provider",
                        "concentration_class": "single_source",
                        "substitutability_class": "difficult",
                        "time_to_replace_class": "long",
                        "operational_impact_class": "critical",
                        "financial_impact_class": "high",
                        "known_mitigants": [],
                    },
                ),
                input_id="fixture_launch_dependency",
            ),
            "debt_maturity": _company_input(
                financial=_financial(
                    debt_state="high",
                    debt_maturity_state="constrained",
                ),
                capital=_capital(
                    debt_instruments=["term loan"],
                    maturity_schedule=["2026-09-30"],
                    refinancing_requirements=["term loan maturity"],
                ),
                events=(_event("debt_maturity", "debt_maturity"),),
                input_id="fixture_debt_maturity",
            ),
            "dilution_history": _company_input(
                profile={
                    **_profile(stage="growth"),
                    "industry": "Software",
                },
                capital=_capital(
                    recent_dilution_state="present",
                    equity_raise_history=["2025 equity raise"],
                ),
                input_id="fixture_dilution_history",
            ),
            "no_scheduled_event": _company_input(
                events=(),
                input_id="fixture_no_scheduled_event",
            ),
        }
        for name, value in fixtures.items():
            with self.subTest(name=name):
                build = CompanyKnowledgeEngine().build(value)
                self.assertIsNotNone(build.snapshot)
                self.assertTrue(
                    build.snapshot.validation_result.is_valid,
                    build.errors,
                )


class CompanyReplayGeneratorEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.inputs = _inputs()
        self.value = _company_input(
            events=(_event("earnings", "earnings_release"),),
            input_id="integration_fixture",
        )
        (
            self.premise,
            self.state,
            self.exposures,
            self.build,
            self.library,
            self.context,
        ) = _built_context(self.value)
        self.candidate = _candidate(
            self.context,
            CompanyEventArchetypeType.EARNINGS_BEAT,
        )
        self.assertIs(
            self.candidate.eligibility_status,
            CandidateEligibilityStatus.ELIGIBLE,
        )

    def _scenario_result(self):
        raw = _response_with_company_candidate(
            self.inputs,
            self.candidate,
        )
        provider = FixtureProvider([raw])
        result = _generator(
            provider,
            adversarial=False,
        ).generate(*self.inputs, company_context=self.context)
        self.assertTrue(result.success, result.errors)
        return result, provider

    def test_replay_and_serialization_are_deterministic(self):
        packet = create_company_knowledge_replay_packet(
            replay_id="company_replay_fixture",
            input=self.value,
            frozen_technical_premise=self.premise,
            current_market_state=self.state,
            relevant_exposures=self.exposures,
            snapshot=self.build.snapshot,
            context=self.context,
            archetype_library=self.library,
            created_at=FIXED_NOW,
        )
        restored = CompanyKnowledgeReplayPacket.from_dict(
            packet.to_dict()
        )
        self.assertEqual(packet, restored)
        self.assertEqual(
            packet.content_hash,
            company_knowledge_replay_packet_content_hash(packet),
        )
        first = replay_company_knowledge(packet)
        second = replay_company_knowledge(restored)
        self.assertTrue(first.success, first.errors)
        self.assertEqual(first, second)

    def test_context_serialization_round_trip(self):
        restored = CompanyScenarioContext.from_dict(
            self.context.to_dict()
        )
        self.assertEqual(restored, self.context)
        self.assertEqual(
            restored.content_hash,
            company_scenario_context_content_hash(restored),
        )

    def test_generator_accepts_grounded_company_context(self):
        result, provider = self._scenario_result()
        self.assertEqual(
            result.input_hashes["company_scenario_context"],
            self.context.content_hash,
        )
        packet = provider.calls[0]["packet"]
        self.assertEqual(
            packet["company_scenario_context"]["content_hash"],
            self.context.content_hash,
        )
        self.assertIn(
            "OPTIONAL OFFLINE COMPANY-CONTEXT RULES",
            provider.calls[0]["system_prompt"],
        )
        usage = validate_company_scenario_usage(
            result.scenario_set,
            self.context,
        )
        self.assertTrue(usage.is_valid, usage.errors)

    def test_standalone_orchestrator_bridge_reuses_frozen_artifacts(self):
        orchestration_input = _orchestration_input()
        orchestration_result, _ = _run_orchestration(
            orchestration_input
        )
        self.assertTrue(orchestration_result.success)
        snapshot = self.build.snapshot
        context = build_company_scenario_context(
            snapshot,
            orchestration_input.frozen_technical_premise,
            orchestration_result.current_state,
            orchestration_input.current_exposures,
            library=self.library,
        )
        candidate = _candidate(
            context,
            CompanyEventArchetypeType.EARNINGS_BEAT,
        )
        bridge_inputs = (
            orchestration_input.frozen_technical_premise,
            orchestration_result.current_state,
            orchestration_result.pattern_retrieval_result,
            orchestration_input.current_evidence,
            orchestration_input.current_exposures,
            orchestration_input.current_events,
        )
        raw = _response_with_company_candidate(
            bridge_inputs,
            candidate,
        )
        generator = _generator(
            FixtureProvider([raw]),
            adversarial=False,
        )
        bridge = run_company_orchestration_bridge(
            orchestration_input,
            orchestration_result,
            context,
            generator,
        )
        self.assertTrue(
            bridge.scenario_generation_result.success,
            bridge.errors,
        )
        self.assertEqual(
            bridge.company_context_hash,
            context.content_hash,
        )
        self.assertEqual(
            bridge.content_hash,
            company_orchestration_bridge_result_content_hash(bridge),
        )
        self.assertEqual(
            CompanyOrchestrationBridgeResult.from_dict(bridge.to_dict()),
            bridge,
        )

    def test_generator_without_context_preserves_original_behavior(self):
        raw_one = _scenario_set_response(self.inputs)
        raw_two = copy.deepcopy(raw_one)
        provider_one = FixtureProvider([raw_one])
        provider_two = FixtureProvider([raw_two])
        first = CurrentMarketScenarioGenerator(
            provider_one,
            use_adversarial_pass=False,
            clock=lambda: FIXED_NOW,
        ).generate(*self.inputs)
        second = CurrentMarketScenarioGenerator(
            provider_two,
            use_adversarial_pass=False,
            clock=lambda: FIXED_NOW,
        ).generate(*self.inputs, company_context=None)
        self.assertEqual(first, second)
        self.assertNotIn(
            "company_scenario_context",
            provider_one.calls[0]["packet"],
        )
        self.assertNotIn(
            "OPTIONAL OFFLINE COMPANY-CONTEXT RULES",
            provider_one.calls[0]["system_prompt"],
        )

    def test_hypothetical_stated_as_fact_fails_usage_validation(self):
        result, _ = self._scenario_result()
        scenario = result.scenario_set.scenarios[0]
        bad_event = replace(
            scenario.hypothetical_future_events[0],
            description="The company will report an earnings beat.",
        )
        bad_scenario = replace(
            scenario,
            hypothetical_future_events=(bad_event,),
        )
        bad_set = replace(
            result.scenario_set,
            scenarios=(bad_scenario,)
            + result.scenario_set.scenarios[1:],
        )
        validation = validate_company_scenario_usage(
            bad_set,
            self.context,
        )
        self.assertIn(
            "company_candidate_no_certainty",
            validation.failed_rules,
        )

    def test_phase9_company_evaluation_and_hard_gates(self):
        result, _ = self._scenario_result()
        evaluation = evaluate_company_knowledge(
            self.build.snapshot,
            context=self.context,
            scenario_set=result.scenario_set,
            archetype_library=self.library,
        )
        self.assertTrue(evaluation.validation_result.is_valid)
        self.assertEqual(evaluation.hard_failure_codes, ())

        mutated_snapshot = replace(
            self.build.snapshot,
            content_hash="0" * 64,
        )
        mutation = evaluate_company_knowledge(mutated_snapshot)
        self.assertIn(
            HARD_FAILURE_COMPANY_SNAPSHOT_MUTATION,
            mutation.hard_failure_codes,
        )

        scenario = result.scenario_set.scenarios[0]
        known_as_hypothetical = replace(
            scenario.hypothetical_future_events[0],
            hypothetical_event_id="earnings",
        )
        confused_scenario = replace(
            scenario,
            hypothetical_future_events=(known_as_hypothetical,),
        )
        confused_set = replace(
            result.scenario_set,
            scenarios=(confused_scenario,)
            + result.scenario_set.scenarios[1:],
        )
        confused = evaluate_company_knowledge(
            self.build.snapshot,
            context=self.context,
            scenario_set=confused_set,
            archetype_library=self.library,
        )
        self.assertIn(
            HARD_FAILURE_COMPANY_EVENT_CONFUSION,
            confused.hard_failure_codes,
        )

        certain_event = replace(
            scenario.hypothetical_future_events[0],
            description="The company will report an earnings beat.",
        )
        certain_scenario = replace(
            scenario,
            hypothetical_future_events=(certain_event,),
        )
        certain_set = replace(
            result.scenario_set,
            scenarios=(certain_scenario,)
            + result.scenario_set.scenarios[1:],
        )
        certain = evaluate_company_knowledge(
            self.build.snapshot,
            context=self.context,
            scenario_set=certain_set,
            archetype_library=self.library,
        )
        self.assertIn(
            HARD_FAILURE_HYPOTHETICAL_AS_FACT,
            certain.hard_failure_codes,
        )


if __name__ == "__main__":
    unittest.main()
