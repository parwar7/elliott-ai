from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from elliott_ai.current_scenario_generator import (
    CURRENT_SCENARIO_ADVERSARIAL_PROMPT_VERSION,
    CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION,
    CurrentMarketScenarioGenerator,
)
from elliott_ai.current_state import CurrentStateBuilder
from elliott_ai.current_scenarios import (
    CURRENT_SCENARIO_GENERATION_RESULT_SCHEMA_VERSION,
    CURRENT_SCENARIO_SET_SCHEMA_VERSION,
    CurrentScenarioGenerationResult,
    ScenarioAdversarialRecommendation,
    ScenarioType,
    current_scenario_generation_result_content_hash,
    current_scenario_set_content_hash,
    validate_current_scenario_generation_result,
)
from elliott_ai.historical_market import HistoricalMoveType
from elliott_ai.historical_patterns import PatternDirection
from elliott_ai.providers import ProviderError
from scripts.test_current_state import _bullish_premise
from scripts.test_market_scenario import _draft_report, _premise
from scripts.test_pattern_retrieval import (
    _base_bundle,
    _library,
    _matching_state,
    _retrieve,
    _standalone_variant,
)


FIXED_NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)


@dataclass
class FixtureProvider:
    responses: list[Any]
    name: str = "fixture"
    model: str | None = "fixture-current-scenario-model"
    calls: list[dict[str, Any]] = field(default_factory=list)
    strict_calls: int = 0

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema": copy.deepcopy(schema),
                "packet": copy.deepcopy(packet),
            }
        )
        if not self.responses:
            raise AssertionError("Fixture provider has no queued response.")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def generate_strict_json(self, **kwargs: Any) -> dict[str, Any]:
        self.strict_calls += 1
        return self.generate(**kwargs)


def _inputs(
    *,
    include_counter: bool = True,
    bearish: bool = False,
):
    premise = _premise() if bearish else _bullish_premise()
    report = _draft_report(premise=premise)
    if bearish:
        events = report.material_events
        state_result = CurrentStateBuilder(
            clock=lambda: FIXED_NOW
        ).build(
            premise,
            report.evidence,
            report.exposures,
            report.exposure_coverage,
            events=events,
        )
        if not state_result.success:
            raise AssertionError(state_result.errors)
        state = state_result.current_state
    else:
        revision_event = replace(
            report.material_events[0],
            event_id="event-earnings-revision",
            event_type="earnings_revision",
        )
        events = report.material_events + (revision_event,)
        state = _matching_state()
    _, _, _, pattern, case_hashes = _base_bundle()
    bearish_profile = replace(
        pattern.expected_outcome_profile,
        expected_direction=PatternDirection.BEARISH,
        expected_move_types=(
            HistoricalMoveType.DOWNWARD_IMPULSE,
        ),
    )
    bearish_pattern = _standalone_variant(
        pattern,
        pattern_id="bearish_counter_pattern",
        pattern_direction=PatternDirection.BEARISH,
        applicable_move_types=(
            HistoricalMoveType.DOWNWARD_IMPULSE,
        ),
        expected_outcome_profile=bearish_profile,
    )
    patterns = [bearish_pattern if bearish else pattern]
    if include_counter:
        patterns.append(
            pattern if bearish else bearish_pattern
        )
    retrieval = _retrieve(
        state,
        _library(tuple(patterns), case_hashes),
    ).retrieval_result
    return (
        premise,
        state,
        retrieval,
        report.evidence,
        report.exposures,
        events,
    )


def _grounding(match) -> dict[str, Any]:
    matched = (
        match.matched_required_features
        + match.matched_optional_features
    )
    differences = (
        match.missing_required_features
        + match.contradicted_required_features
        + match.incompatible_features
        + match.contradictory_current_features
    )
    return {
        "pattern_ref": match.pattern_ref,
        "lane": match.lane.value,
        "recommendation": match.recommendation.value,
        "match_quality": match.match_quality.value,
        "reused_causal_features": list(matched[:2]),
        "acknowledged_differences": list(differences),
        "acknowledged_warnings": list(match.warnings),
    }


def _step(
    number: int,
    step_type: str,
    *,
    evidence_ids: tuple[str, ...],
    exposure_types: tuple[str, ...],
    mechanisms: tuple[str, ...],
    event_ids: tuple[str, ...] = (),
    hypothetical_event_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "step_number": number,
        "step_type": step_type,
        "description": (
            "The supplied conditions could interact at this stage."
        ),
        "current_evidence_ids": list(evidence_ids),
        "current_exposure_types": list(exposure_types),
        "event_ids": list(event_ids),
        "hypothetical_event_ids": list(
            hypothetical_event_ids
        ),
        "transmission_mechanisms": list(mechanisms),
        "expected_market_effect": (
            "This could conditionally alter market repricing."
        ),
        "timing_relation": "This may occur after the prior step.",
        "assumptions": [],
        "uncertainty": "The strength and timing remain uncertain.",
    }


def _scenario(
    inputs,
    *,
    scenario_id: str,
    title: str,
    scenario_type: str,
    required_exposures: tuple[str, ...],
    mechanisms: tuple[str, ...],
    support_ids: tuple[str, ...],
    use_event: bool = False,
    use_hypothetical: bool = False,
    invalidation: bool = False,
) -> dict[str, Any]:
    premise, state, retrieval, _, _, _ = inputs
    direction = premise.direction.value
    if invalidation:
        direction = (
            "down" if premise.direction.value == "up" else "up"
        )
    hypothetical = []
    hypothetical_ids: tuple[str, ...] = ()
    if use_hypothetical:
        hypothetical = [
            {
                "hypothetical_event_id": f"{scenario_id}_rate_shock",
                "event_type": "rate_shock",
                "description": (
                    "A rate shock could occur, but it is not known."
                ),
                "timing_window": (
                    "It might occur within the technical duration window."
                ),
                "assumptions": [
                    "Policy expectations would need to change."
                ],
                "uncertainty": "The event may never occur.",
                "explicitly_hypothetical": True,
            }
        ]
        hypothetical_ids = (
            hypothetical[0]["hypothetical_event_id"],
        )
    event_ids = ("event-earnings",) if use_event else ()
    evidence_ids = support_ids[:1]
    steps = (
        _step(
            1,
            "precondition",
            evidence_ids=evidence_ids,
            exposure_types=required_exposures,
            mechanisms=mechanisms,
        ),
        _step(
            2,
            "trigger",
            evidence_ids=evidence_ids,
            exposure_types=required_exposures,
            mechanisms=mechanisms,
            event_ids=event_ids,
            hypothetical_event_ids=hypothetical_ids,
        ),
        _step(
            3,
            "invalidation" if invalidation else "initial_repricing",
            evidence_ids=evidence_ids,
            exposure_types=required_exposures,
            mechanisms=mechanisms,
        ),
    )
    if invalidation and retrieval.counter_patterns:
        pattern_matches = retrieval.counter_patterns[:1]
        supporting_refs: list[str] = []
        counter_refs = [pattern_matches[0].pattern_ref]
    else:
        pattern_matches = retrieval.matches[:1]
        supporting_refs = [pattern_matches[0].pattern_ref]
        counter_refs = []
    invalidation_conditions = (
        ["price_invalidation_level:375"]
        if invalidation
        else ["The supplied supporting conditions may fail to persist."]
    )
    return {
        "scenario_id": scenario_id,
        "title": title,
        "scenario_type": scenario_type,
        "direction": direction,
        "summary": (
            "Multiple supplied conditions could plausibly interact."
        ),
        "initiating_conditions": [
            "Current exposures may remain active.",
            "A separate trigger could emerge.",
        ],
        "required_current_exposure_types": list(
            required_exposures
        ),
        "optional_current_exposure_types": [],
        "triggering_events": (
            ["Scheduled earnings could act as a trigger."]
            if use_event
            else ["A conditional repricing trigger may emerge."]
        ),
        "hypothetical_future_events": hypothetical,
        "transmission_mechanisms": list(mechanisms),
        "exposure_interactions": [
            "The supplied exposures could reinforce one another."
        ],
        "amplifiers": ["Positioning may amplify repricing."],
        "dampeners": ["Opposing evidence may dampen the move."],
        "market_regime_requirements": (
            ["Liquidity conditions may need to remain supportive."]
            if "interest_rate_sensitivity" in required_exposures
            else []
        ),
        "timeline_sequence": list(steps),
        "target_linkage": (
            "This combination could be consistent with the frozen "
            "target range without claiming exact causation."
        ),
        "duration_linkage": (
            "The sequence may unfold over the frozen duration bucket."
        ),
        "linked_target_low": premise.target_low,
        "linked_target_high": premise.target_high,
        "linked_magnitude_bucket": (
            state.expected_magnitude_bucket.value
        ),
        "linked_duration_bucket": (
            state.expected_duration_bucket.value
        ),
        "supporting_current_evidence_ids": list(support_ids),
        "contradicting_current_evidence_ids": ["ev-oppose"],
        "supporting_pattern_refs": supporting_refs,
        "counter_pattern_refs": counter_refs,
        "pattern_grounding": [
            _grounding(item) for item in pattern_matches
        ],
        "assumptions": [
            "Transmission could remain active through the horizon."
        ],
        "missing_evidence": [
            "Future transmission strength remains unavailable."
        ],
        "invalidation_conditions": invalidation_conditions,
        "technical_premise_compatibility": (
            "contradictory" if invalidation else "consistent"
        ),
        "confidence": "moderate",
        "limitations": [
            "This is conditional contextual evidence, not a forecast."
        ],
    }


def _scenario_set_response(inputs) -> dict[str, Any]:
    scenarios = [
        _scenario(
            inputs,
            scenario_id="company_event",
            title="Company and scheduled event path",
            scenario_type="earnings_and_guidance",
            required_exposures=("revenue_growth_risk",),
            mechanisms=("earnings_revision",),
            support_ids=("ev-support",),
            use_event=True,
        ),
        _scenario(
            inputs,
            scenario_id="macro_liquidity",
            title="Macro and liquidity repricing path",
            scenario_type="macro_and_liquidity",
            required_exposures=("interest_rate_sensitivity",),
            mechanisms=("macro_shock",),
            support_ids=("ev-macro",),
            use_hypothetical=True,
        ),
        _scenario(
            inputs,
            scenario_id="multi_factor",
            title="Multi-factor reinforcement path",
            scenario_type="multi_factor",
            required_exposures=(
                "interest_rate_sensitivity",
                "revenue_growth_risk",
            ),
            mechanisms=(
                "earnings_revision",
                "multiple_expansion",
            ),
            support_ids=("ev-support", "ev-macro"),
            use_event=True,
        ),
        _scenario(
            inputs,
            scenario_id="technical_failure",
            title="Technical premise failure path",
            scenario_type="technical_invalidation",
            required_exposures=("interest_rate_sensitivity",),
            mechanisms=("risk_premium_expansion",),
            support_ids=("ev-macro",),
            invalidation=True,
        ),
    ]
    return {
        "scenarios": scenarios,
        "scenario_relationships": [
            {
                "source_scenario_id": "technical_failure",
                "target_scenario_id": "company_event",
                "relationship_type": "counter_scenario",
                "rationale": (
                    "The failure path may oppose the company-event path."
                ),
            },
            {
                "source_scenario_id": "company_event",
                "target_scenario_id": "multi_factor",
                "relationship_type": "compatible",
                "rationale": (
                    "The company path may coexist with broader forces."
                ),
            },
        ],
        "overall_missing_evidence": [
            "Future trigger realization remains unavailable."
        ],
        "overall_limitations": [
            "Historical structures are contextual and non-predictive."
        ],
        "insufficient_evidence_reason": None,
    }


def _review_response(
    draft: dict[str, Any],
    *,
    recommendation: str = "keep",
    replacement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scenario_ids = [
        item["scenario_id"] for item in draft["scenarios"]
    ]
    pattern_refs = sorted(
        {
            item
            for scenario in draft["scenarios"]
            for item in (
                scenario["supporting_pattern_refs"]
                + scenario["counter_pattern_refs"]
            )
        }
    )
    response = {
        "recommendation": recommendation,
        "summary": (
            "The scenarios were checked against the frozen inputs."
        ),
        "duplicated_scenario_findings": [],
        "unsupported_future_event_findings": [],
        "invented_fact_findings": [],
        "dangling_reference_findings": [],
        "hindsight_leakage_findings": [],
        "historical_analogue_overreliance_findings": [],
        "ignored_counter_pattern_findings": [],
        "missing_contradicting_evidence_findings": [],
        "weak_causal_transmission_findings": [],
        "target_magnitude_mismatch_findings": [],
        "duration_mismatch_findings": [],
        "timing_contradiction_findings": [],
        "excessive_certainty_findings": [],
        "single_catalyst_findings": [],
        "scheduled_hypothetical_confusion_findings": [],
        "simpler_explanations": [
            "Broad risk appetite could explain part of the move."
        ],
        "strongest_counterargument": (
            "The supporting conditions may dissipate before repricing."
        ),
        "referenced_scenario_ids": scenario_ids,
        "referenced_current_evidence_ids": [
            "ev-support",
            "ev-oppose",
            "ev-macro",
        ],
        "referenced_pattern_refs": pattern_refs,
        "replacement_scenario_set": replacement,
    }
    return response


def _generator(
    provider: Any,
    *,
    adversarial: bool = True,
    attempts: int = 2,
) -> CurrentMarketScenarioGenerator:
    return CurrentMarketScenarioGenerator(
        provider,
        max_attempts=attempts,
        use_adversarial_pass=adversarial,
        clock=lambda: FIXED_NOW,
    )


class CurrentScenarioSuccessTests(unittest.TestCase):
    def test_valid_bullish_competing_scenario_set(self) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        provider = FixtureProvider(
            [draft, _review_response(draft)]
        )
        result = _generator(provider).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(len(result.scenario_set.scenarios), 4)
        self.assertEqual(provider.strict_calls, 2)
        self.assertTrue(result.used_adversarial_pass)
        self.assertIs(
            result.adversarial_recommendation,
            ScenarioAdversarialRecommendation.KEEP,
        )
        self.assertEqual(
            result.scenario_set.schema_version,
            CURRENT_SCENARIO_SET_SCHEMA_VERSION,
        )
        self.assertEqual(
            result.scenario_set.content_hash,
            current_scenario_set_content_hash(result.scenario_set),
        )

    def test_valid_bearish_scenario_set(self) -> None:
        inputs = _inputs(bearish=True)
        raw = _scenario_set_response(inputs)
        result = _generator(
            FixtureProvider([raw]),
            adversarial=False,
        ).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        normal_directions = {
            item.direction.value
            for item in result.scenario_set.scenarios
            if item.scenario_type
            is not ScenarioType.TECHNICAL_INVALIDATION
        }
        self.assertEqual(normal_directions, {"down"})

    def test_three_distinct_company_macro_multi_factor_and_invalidation_lanes(
        self,
    ) -> None:
        inputs = _inputs()
        result = _generator(
            FixtureProvider([_scenario_set_response(inputs)]),
            adversarial=False,
        ).generate(*inputs)
        types = {
            item.scenario_type for item in result.scenario_set.scenarios
        }
        self.assertIn(ScenarioType.EARNINGS_AND_GUIDANCE, types)
        self.assertIn(ScenarioType.MACRO_AND_LIQUIDITY, types)
        self.assertIn(ScenarioType.MULTI_FACTOR, types)
        self.assertIn(ScenarioType.TECHNICAL_INVALIDATION, types)
        self.assertTrue(result.validation_result.is_valid)

    def test_hypothetical_event_is_explicit_and_has_no_source_or_date(
        self,
    ) -> None:
        inputs = _inputs()
        result = _generator(
            FixtureProvider([_scenario_set_response(inputs)]),
            adversarial=False,
        ).generate(*inputs)
        macro = next(
            item
            for item in result.scenario_set.scenarios
            if item.scenario_type is ScenarioType.MACRO_AND_LIQUIDITY
        )
        event = macro.hypothetical_future_events[0]
        self.assertTrue(event.explicitly_hypothetical)
        self.assertNotIn("source", event.to_dict())
        self.assertNotIn("date", event.to_dict())

    def test_pattern_and_counter_pattern_grounding_is_preserved(self) -> None:
        inputs = _inputs()
        result = _generator(
            FixtureProvider([_scenario_set_response(inputs)]),
            adversarial=False,
        ).generate(*inputs)
        failure = next(
            item
            for item in result.scenario_set.scenarios
            if item.scenario_type
            is ScenarioType.TECHNICAL_INVALIDATION
        )
        self.assertTrue(failure.counter_pattern_refs)
        self.assertEqual(
            failure.pattern_grounding[0].pattern_ref,
            failure.counter_pattern_refs[0],
        )

    def test_application_controls_provenance_and_hash_fields(self) -> None:
        inputs = _inputs()
        raw = _scenario_set_response(inputs)
        raw["model_name"] = "provider-invented-model"
        raw["content_hash"] = "bad"
        for scenario in raw["scenarios"]:
            scenario["model_name"] = "provider-invented-model"
            scenario["content_hash"] = "bad"
        result = _generator(
            FixtureProvider([raw]),
            adversarial=False,
        ).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(
            result.scenario_set.model_name,
            "fixture-current-scenario-model",
        )
        self.assertEqual(
            result.scenario_set.scenarios[0].prompt_version,
            CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION,
        )
        self.assertNotEqual(result.scenario_set.content_hash, "bad")

    def test_positioning_scenario_fixture(self) -> None:
        inputs = _inputs()
        raw = _scenario_set_response(inputs)
        positioning = raw["scenarios"][1]
        positioning["scenario_type"] = "positioning_unwind"
        positioning["title"] = "Positioning unwind path"
        positioning["transmission_mechanisms"] = [
            "crowded_positioning_unwind"
        ]
        for step in positioning["timeline_sequence"]:
            step["transmission_mechanisms"] = [
                "crowded_positioning_unwind"
            ]
        result = _generator(
            FixtureProvider([raw]),
            adversarial=False,
        ).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertIn(
            ScenarioType.POSITIONING_UNWIND,
            {
                item.scenario_type
                for item in result.scenario_set.scenarios
            },
        )

    def test_prompts_contain_frozen_boundary_and_adversarial_checks(
        self,
    ) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        provider = FixtureProvider(
            [draft, _review_response(draft)]
        )
        result = _generator(provider).generate(*inputs)
        self.assertTrue(result.success)
        self.assertIn(
            "immutable",
            provider.calls[0]["system_prompt"].casefold(),
        )
        self.assertIn(
            "technical_invalidation_tokens",
            provider.calls[0]["user_prompt"],
        )
        self.assertIn(
            "ignored counter-patterns",
            provider.calls[1]["system_prompt"],
        )

    def test_execution_round_trip_and_hash(self) -> None:
        inputs = _inputs()
        result = _generator(
            FixtureProvider([_scenario_set_response(inputs)]),
            adversarial=False,
        ).generate(*inputs)
        restored = CurrentScenarioGenerationResult.from_dict(
            result.to_dict()
        )
        self.assertEqual(restored, result)
        self.assertEqual(
            result.content_hash,
            current_scenario_generation_result_content_hash(result),
        )
        self.assertEqual(
            result.schema_version,
            CURRENT_SCENARIO_GENERATION_RESULT_SCHEMA_VERSION,
        )
        envelope = validate_current_scenario_generation_result(
            result,
            premise=inputs[0],
            state=inputs[1],
            retrieval=inputs[2],
        )
        self.assertTrue(envelope.is_valid, envelope.to_dict())

    def test_generation_does_not_mutate_frozen_inputs(self) -> None:
        inputs = _inputs()
        before = [
            item.to_dict()
            if hasattr(item, "to_dict")
            else [value.to_dict() for value in item]
            for item in inputs
        ]
        result = _generator(
            FixtureProvider([_scenario_set_response(inputs)]),
            adversarial=False,
        ).generate(*inputs)
        after = [
            item.to_dict()
            if hasattr(item, "to_dict")
            else [value.to_dict() for value in item]
            for item in inputs
        ]
        self.assertTrue(result.success)
        self.assertEqual(before, after)


class CurrentScenarioValidationTests(unittest.TestCase):
    def _run_invalid(
        self,
        mutate,
        *,
        include_counter: bool = True,
    ) -> CurrentScenarioGenerationResult:
        inputs = _inputs(include_counter=include_counter)
        raw = _scenario_set_response(inputs)
        mutate(raw)
        return _generator(
            FixtureProvider([raw]),
            adversarial=False,
            attempts=1,
        ).generate(*inputs)

    def test_hypothetical_event_with_source_field_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][1][
                "hypothetical_future_events"
            ][0].update({"source": "invented"})
        )
        self.assertFalse(result.success)
        self.assertTrue(
            any("unknown fields" in item for item in result.errors)
        )

    def test_hypothetical_event_presented_as_confirmed_is_rejected(
        self,
    ) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][1][
                "hypothetical_future_events"
            ][0].update({"explicitly_hypothetical": False})
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_hypothetical_events",
            result.scenario_set.scenarios[
                1
            ].validation_result.failed_rules,
        )

    def test_hypothetical_event_with_exact_fabricated_date_is_rejected(
        self,
    ) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][1][
                "hypothetical_future_events"
            ][0].update({"timing_window": "It may occur on 2026-09-15."})
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_hypothetical_events",
            result.scenario_set.scenarios[
                1
            ].validation_result.failed_rules,
        )

    def test_excluded_or_unknown_pattern_reference_is_rejected(self) -> None:
        def mutate(raw):
            scenario = raw["scenarios"][0]
            scenario["supporting_pattern_refs"] = ["excluded@1"]
            scenario["pattern_grounding"][0][
                "pattern_ref"
            ] = "excluded@1"

        result = self._run_invalid(mutate)
        self.assertFalse(result.success)
        self.assertIn(
            "set_scenario_validation",
            result.validation_result.failed_rules,
        )

    def test_counter_pattern_omission_is_rejected(self) -> None:
        def mutate(raw):
            failure = raw["scenarios"][3]
            failure["counter_pattern_refs"] = []
            failure["pattern_grounding"] = []

        result = self._run_invalid(mutate)
        self.assertFalse(result.success)
        self.assertIn(
            "set_counter_patterns",
            result.validation_result.failed_rules,
        )

    def test_dangling_evidence_reference_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0][
                "supporting_current_evidence_ids"
            ].append("invented-evidence")
        )
        self.assertFalse(result.success)
        nested = result.scenario_set.scenarios[0].validation_result
        self.assertIn(
            "scenario_evidence_references",
            nested.failed_rules,
        )

    def test_dangling_event_reference_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0]["timeline_sequence"][
                1
            ]["event_ids"].append("invented-event")
        )
        self.assertFalse(result.success)
        nested = result.scenario_set.scenarios[0].validation_result
        self.assertIn(
            "scenario_event_references",
            nested.failed_rules,
        )

    def test_target_mismatch_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {"linked_target_low": 999.0}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_target_linkage",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_duration_mismatch_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {"linked_duration_bucket": "years"}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_duration_linkage",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_structurally_duplicated_scenarios_are_rejected(self) -> None:
        def mutate(raw):
            duplicate = copy.deepcopy(raw["scenarios"][0])
            duplicate["scenario_id"] = "duplicate_company"
            duplicate["title"] = "Renamed duplicate company path"
            raw["scenarios"][1] = duplicate

        result = self._run_invalid(mutate)
        self.assertFalse(result.success)
        self.assertIn(
            "set_diversity",
            result.validation_result.failed_rules,
        )

    def test_unsupported_certainty_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {"summary": "This will inevitably happen."}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_prohibited_language",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_trade_recommendation_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {"summary": "Buy the stock before the event."}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_prohibited_language",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_pattern_rank_without_causal_feature_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0]["pattern_grounding"][
                0
            ].update({"reused_causal_features": []})
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_pattern_grounding",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_pattern_warning_omission_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][3]["pattern_grounding"][
                0
            ].update({"acknowledged_warnings": []})
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_pattern_grounding",
            result.scenario_set.scenarios[
                3
            ].validation_result.failed_rules,
        )

    def test_missing_invalidation_token_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][3].update(
                {"invalidation_conditions": ["A different level."]}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_invalidation",
            result.scenario_set.scenarios[
                3
            ].validation_result.failed_rules,
        )

    def test_normal_scenario_direction_drift_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {"direction": "down"}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_direction",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_numeric_probability_language_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {"summary": "There is a 90% chance of this path."}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_prohibited_language",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_claim_that_chart_predicts_event_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {
                    "summary": (
                        "The Elliott count predicts an earnings event."
                    )
                }
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_prohibited_language",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_noncontiguous_causal_steps_are_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0]["timeline_sequence"][
                1
            ].update({"step_number": 4})
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_causal_sequence",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_missing_contradicting_evidence_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][0].update(
                {"contradicting_current_evidence_ids": []}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "scenario_contradicting_evidence",
            result.scenario_set.scenarios[
                0
            ].validation_result.failed_rules,
        )

    def test_missing_multi_factor_lane_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenarios"][2].update(
                {"scenario_type": "company_execution"}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "set_diversity",
            result.validation_result.failed_rules,
        )

    def test_dangling_relationship_is_rejected(self) -> None:
        result = self._run_invalid(
            lambda raw: raw["scenario_relationships"][0].update(
                {"target_scenario_id": "invented_scenario"}
            )
        )
        self.assertFalse(result.success)
        self.assertIn(
            "set_relationships",
            result.validation_result.failed_rules,
        )

    def test_incompatible_relationship_pair_is_rejected(self) -> None:
        def mutate(raw):
            raw["scenario_relationships"].append(
                {
                    "source_scenario_id": "company_event",
                    "target_scenario_id": "multi_factor",
                    "relationship_type": "mutually_exclusive",
                    "rationale": "These paths may conflict.",
                }
            )

        result = self._run_invalid(mutate)
        self.assertFalse(result.success)
        self.assertIn(
            "set_relationships",
            result.validation_result.failed_rules,
        )

    def test_future_material_event_marked_confirmed_is_rejected_before_llm(
        self,
    ) -> None:
        inputs = list(_inputs())
        inputs[5] = (
            replace(inputs[5][0], status="confirmed"),
            *inputs[5][1:],
        )
        provider = FixtureProvider([])
        result = _generator(provider).generate(*inputs)
        self.assertFalse(result.success)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "event_bundle",
            result.validation_result.failed_rules,
        )

    def test_invalid_frozen_input_returns_without_model_call(self) -> None:
        inputs = list(_inputs())
        inputs[0] = replace(
            inputs[0],
            frozen_content_hash="0" * 64,
        )
        provider = FixtureProvider([])
        result = _generator(provider).generate(*inputs)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "technical_premise",
            result.validation_result.failed_rules,
        )


class CurrentScenarioRetryAndFailureTests(unittest.TestCase):
    def test_malformed_json_provider_error_retries(self) -> None:
        inputs = _inputs()
        provider = FixtureProvider(
            [
                ProviderError(
                    "Model output could not be parsed as JSON."
                ),
                _scenario_set_response(inputs),
            ]
        )
        result = _generator(
            provider,
            adversarial=False,
        ).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(provider.strict_calls, 2)
        self.assertIn(
            "CORRECTION REQUIRED",
            provider.calls[1]["user_prompt"],
        )

    def test_missing_required_field_retries(self) -> None:
        inputs = _inputs()
        invalid = _scenario_set_response(inputs)
        invalid["scenarios"][0].pop("limitations")
        provider = FixtureProvider(
            [invalid, _scenario_set_response(inputs)]
        )
        result = _generator(
            provider,
            adversarial=False,
        ).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.attempts, 2)
        self.assertIn(
            "Use no new facts",
            provider.calls[1]["user_prompt"],
        )

    def test_target_mismatch_retries_with_validation_feedback(self) -> None:
        inputs = _inputs()
        invalid = _scenario_set_response(inputs)
        invalid["scenarios"][0]["linked_target_high"] = 900.0
        provider = FixtureProvider(
            [invalid, _scenario_set_response(inputs)]
        )
        result = _generator(
            provider,
            adversarial=False,
        ).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.attempts, 2)
        self.assertIn(
            "scenario_target_linkage",
            provider.calls[1]["user_prompt"],
        )

    def test_noncorrectable_provider_failure_does_not_retry(self) -> None:
        inputs = _inputs()
        provider = FixtureProvider(
            [ProviderError("Provider returned HTTP 401.")]
        )
        result = _generator(
            provider,
            adversarial=False,
        ).generate(*inputs)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(provider.strict_calls, 1)

    def test_structured_insufficient_evidence_does_not_retry(self) -> None:
        inputs = _inputs()
        scenario = _scenario(
            inputs,
            scenario_id="insufficient_case",
            title="Insufficient evidence case",
            scenario_type="insufficient_evidence",
            required_exposures=(),
            mechanisms=("uncertainty_premium",),
            support_ids=("ev-support",),
        )
        raw = {
            "scenarios": [scenario],
            "scenario_relationships": [],
            "overall_missing_evidence": [
                "Independent driver evidence is unavailable."
            ],
            "overall_limitations": [
                "A competing set cannot be supported."
            ],
            "insufficient_evidence_reason": (
                "The supplied evidence cannot support three distinct "
                "traceable scenarios."
            ),
        }
        provider = FixtureProvider([raw])
        result = _generator(provider).generate(*inputs)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(provider.strict_calls, 1)
        self.assertTrue(result.validation_result.is_valid)
        self.assertIsNotNone(
            result.scenario_set.insufficient_evidence_reason
        )

    def test_packet_provider_returns_structured_no_reasoning_result(
        self,
    ) -> None:
        from elliott_ai.providers import PacketProvider

        inputs = _inputs()
        result = _generator(PacketProvider()).generate(*inputs)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertIn(
            "packet_provider_no_reasoning",
            result.validation_result.failed_rules,
        )

    def test_unsupported_provider_returns_structured_failure(self) -> None:
        inputs = _inputs()
        provider = FixtureProvider([])
        provider.name = "unsupported"
        result = _generator(provider).generate(*inputs)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertIn(
            "unsupported_provider",
            result.validation_result.failed_rules,
        )

    def test_no_primary_match_stops_without_model_call(self) -> None:
        from elliott_ai.pattern_retrieval import MatchQuality

        inputs = list(_inputs(include_counter=False))
        _, _, _, pattern, case_hashes = _base_bundle()
        retrieval = _retrieve(
            inputs[1],
            _library((pattern,), case_hashes),
            minimum_match_quality=MatchQuality.VERY_STRONG,
        ).retrieval_result
        self.assertEqual(retrieval.matches, ())
        inputs[2] = retrieval
        provider = FixtureProvider([])
        result = _generator(provider).generate(*inputs)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "no_suitable_historical_matches",
            result.validation_result.failed_rules,
        )

    def test_repeated_runs_with_fixed_inputs_and_clock_are_deterministic(
        self,
    ) -> None:
        inputs = _inputs()
        raw = _scenario_set_response(inputs)
        first = _generator(
            FixtureProvider([raw]),
            adversarial=False,
        ).generate(*inputs)
        second = _generator(
            FixtureProvider([raw]),
            adversarial=False,
        ).generate(*inputs)
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)


class CurrentScenarioAdversarialTests(unittest.TestCase):
    def test_adversarial_revision_returns_full_replacement(self) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        replacement = copy.deepcopy(draft)
        replacement["scenarios"][0]["summary"] = (
            "Revised supplied conditions could plausibly interact."
        )
        review = _review_response(
            draft,
            recommendation="revise",
            replacement=replacement,
        )
        result = _generator(
            FixtureProvider([draft, review])
        ).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertIs(
            result.adversarial_recommendation,
            ScenarioAdversarialRecommendation.REVISE,
        )
        self.assertIn(
            "Revised",
            result.scenario_set.scenarios[0].summary,
        )
        self.assertEqual(
            result.adversarial_review.prompt_version,
            CURRENT_SCENARIO_ADVERSARIAL_PROMPT_VERSION,
        )

    def test_adversarial_reject_returns_unsuccessful_result(self) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        result = _generator(
            FixtureProvider(
                [
                    draft,
                    _review_response(
                        draft,
                        recommendation="reject",
                    ),
                ]
            )
        ).generate(*inputs)
        self.assertFalse(result.success)
        self.assertIs(
            result.adversarial_recommendation,
            ScenarioAdversarialRecommendation.REJECT,
        )
        self.assertIsNotNone(result.scenario_set)

    def test_adversarial_insufficient_evidence_is_not_accepted(
        self,
    ) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        result = _generator(
            FixtureProvider(
                [
                    draft,
                    _review_response(
                        draft,
                        recommendation="insufficient_evidence",
                    ),
                ]
            )
        ).generate(*inputs)
        self.assertFalse(result.success)
        self.assertIs(
            result.adversarial_recommendation,
            ScenarioAdversarialRecommendation.INSUFFICIENT_EVIDENCE,
        )

    def test_revision_without_replacement_retries(self) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        invalid = _review_response(
            draft,
            recommendation="revise",
        )
        valid = _review_response(draft)
        provider = FixtureProvider([draft, invalid, valid])
        result = _generator(provider).generate(*inputs)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.attempts, 3)
        self.assertIn(
            "review_recommendation",
            provider.calls[2]["user_prompt"],
        )

    def test_keep_with_replacement_is_rejected(self) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        invalid = _review_response(
            draft,
            recommendation="keep",
            replacement=copy.deepcopy(draft),
        )
        result = _generator(
            FixtureProvider([draft, invalid]),
            attempts=1,
        ).generate(*inputs)
        self.assertFalse(result.success)
        self.assertIn(
            "review_recommendation",
            result.validation_result.failed_rules,
        )

    def test_adversarial_review_cannot_introduce_new_pattern(self) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        invalid = _review_response(draft)
        invalid["referenced_pattern_refs"].append("invented@1")
        result = _generator(
            FixtureProvider([draft, invalid]),
            attempts=1,
        ).generate(*inputs)
        self.assertFalse(result.success)
        self.assertIn(
            "review_references",
            result.validation_result.failed_rules,
        )

    def test_replacement_target_mismatch_is_rejected(self) -> None:
        inputs = _inputs()
        draft = _scenario_set_response(inputs)
        replacement = copy.deepcopy(draft)
        replacement["scenarios"][0]["linked_target_low"] = 1.0
        invalid = _review_response(
            draft,
            recommendation="revise",
            replacement=replacement,
        )
        result = _generator(
            FixtureProvider([draft, invalid]),
            attempts=1,
        ).generate(*inputs)
        self.assertFalse(result.success)
        self.assertIn(
            "review_replacement",
            result.validation_result.failed_rules,
        )
