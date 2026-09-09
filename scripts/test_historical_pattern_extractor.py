from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from elliott_ai.historical_market import HistoricalMoveType
from elliott_ai.historical_pattern_extractor import (
    HISTORICAL_PATTERN_ADVERSARIAL_PROMPT_VERSION,
    HISTORICAL_PATTERN_EXTRACTION_SCHEMA_VERSION,
    HISTORICAL_PATTERN_PROMPT_VERSION,
    HistoricalPatternExtractionResult,
    HistoricalPatternExtractor,
    PatternAdversarialRecommendation,
    historical_pattern_extraction_result_content_hash,
)
from elliott_ai.historical_patterns import (
    HistoricalPatternCandidateBuilder,
    PatternStatus,
)
from elliott_ai.market_scenario import ScenarioDirection
from elliott_ai.providers import PacketProvider, ProviderError
from scripts.test_historical_patterns import (
    FIXED_NOW,
    _bundle,
    _candidate_bundle,
    _pattern,
)


_APPLICATION_PATTERN_FIELDS = (
    "pattern_id",
    "pattern_version",
    "pattern_status",
    "source_analysis_hashes",
    "support_metrics",
    "analyst_type",
    "model_name",
    "prompt_version",
    "generated_at",
    "applicable_cutoff",
    "replacement_pattern_ref",
    "validation_result",
    "content_hash",
    "schema_version",
)

_REVIEW_ASSESSMENTS = (
    "overgeneralization_assessment",
    "hindsight_bias_assessment",
    "survivorship_bias_assessment",
    "selection_bias_assessment",
    "concentration_assessment",
    "regime_dependence_assessment",
    "duplicate_case_assessment",
    "correlated_evidence_assessment",
    "contradiction_assessment",
    "falsifiability_assessment",
    "outcome_leakage_assessment",
    "tautology_assessment",
    "causal_layer_assessment",
    "correlation_causation_assessment",
    "support_assessment",
    "invalidation_assessment",
)


@dataclass
class FixtureProvider:
    responses: list[Any]
    name: str = "fixture"
    model: str | None = "fixture-pattern-model"
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

    def generate_strict_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        self.strict_calls += 1
        return self.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            packet=packet,
        )


def _pattern_response(
    packets,
    analyses,
    candidate,
    **pattern_kwargs: Any,
) -> dict[str, Any]:
    raw = _pattern(
        packets,
        analyses,
        candidate,
        **pattern_kwargs,
    ).to_dict()
    for field_name in _APPLICATION_PATTERN_FIELDS:
        raw.pop(field_name, None)
    return raw


def _review_response(
    candidate,
    recommendation: str = "keep",
    *,
    replacement_pattern: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "recommendation": recommendation,
        "summary": "The draft was tested against every frozen candidate case.",
        **{
            field_name: "Assessed against the bounded historical sources."
            for field_name in _REVIEW_ASSESSMENTS
        },
        "referenced_case_ids": candidate.case_ids,
        "replacement_pattern": replacement_pattern,
    }
    raw.update(overrides)
    return raw


def _extractor(
    provider: Any,
    *,
    adversarial: bool = True,
    attempts: int = 2,
    max_prompt_characters: int = 300_000,
) -> HistoricalPatternExtractor:
    return HistoricalPatternExtractor(
        provider,
        max_attempts=attempts,
        use_adversarial_pass=adversarial,
        max_prompt_characters=max_prompt_characters,
        clock=lambda: FIXED_NOW,
    )


class HistoricalPatternExtractorSuccessTests(unittest.TestCase):
    def test_valid_bullish_impulse_pattern(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        provider = FixtureProvider(
            [
                _pattern_response(packets, analyses, candidate),
                _review_response(candidate),
            ]
        )
        result = _extractor(provider).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.pattern.pattern_status, PatternStatus.REVIEWED)
        self.assertEqual(result.pattern.pattern_version, 1)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(provider.strict_calls, 2)
        self.assertEqual(
            result.adversarial_recommendation,
            PatternAdversarialRecommendation.KEEP,
        )

    def test_valid_bearish_crash_pattern(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle(
            direction=ScenarioDirection.DOWN,
            move_type=HistoricalMoveType.CRASH,
        )
        provider = FixtureProvider(
            [_pattern_response(packets, analyses, candidate)]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.pattern.pattern_direction, "bearish")
        self.assertEqual(
            result.pattern.expected_outcome_profile.expected_move_types,
            (HistoricalMoveType.CRASH,),
        )

    def test_valid_no_single_trigger_pattern(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        response = _pattern_response(
            packets,
            analyses,
            candidate,
            trigger_event_types=(),
            no_single_trigger=True,
        )
        result = _extractor(
            FixtureProvider([response]),
            adversarial=False,
        ).extract(candidate, packets, analyses)
        self.assertTrue(result.success)
        self.assertTrue(result.pattern.no_single_trigger)
        self.assertEqual(result.pattern.trigger_event_types, ())

    def test_regime_dependent_pattern_preserves_regime_lane(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        result = _extractor(
            FixtureProvider(
                [_pattern_response(packets, analyses, candidate)]
            ),
            adversarial=False,
        ).extract(candidate, packets, analyses)
        self.assertTrue(result.success)
        self.assertTrue(result.pattern.required_market_regimes)
        self.assertIn(
            result.pattern.required_market_regimes[0],
            candidate.shared_market_regime_features,
        )

    def test_contradicting_and_exception_cases_are_accounted_for(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle(5)
        support = candidate.case_ids[:3]
        response = _pattern_response(
            packets,
            analyses,
            candidate,
            supporting_case_ids=support,
            contradicting_case_ids=(candidate.case_ids[3],),
            exception_case_ids=(candidate.case_ids[4],),
        )
        result = _extractor(
            FixtureProvider([response]),
            adversarial=False,
        ).extract(candidate, packets, analyses)
        self.assertTrue(result.success)
        self.assertEqual(result.pattern.supporting_case_ids, support)
        self.assertEqual(len(result.pattern.contradicting_case_ids), 1)
        self.assertEqual(len(result.pattern.exception_case_ids), 1)

    def test_application_provenance_fields_are_ignored(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        response = _pattern_response(packets, analyses, candidate)
        response.update(
            {
                "pattern_id": "provider_owned_id",
                "pattern_version": 99,
                "pattern_status": "validated",
                "model_name": "provider-claimed-model",
                "content_hash": "provider-claimed-hash",
            }
        )
        provider = FixtureProvider([response])
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertNotEqual(result.pattern.pattern_id, "provider_owned_id")
        self.assertEqual(result.pattern.pattern_version, 1)
        self.assertEqual(result.pattern.model_name, provider.model)
        self.assertNotEqual(
            result.pattern.content_hash,
            "provider-claimed-hash",
        )


class HistoricalPatternAdversarialTests(unittest.TestCase):
    def test_adversarial_revision_creates_next_version(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        original = _pattern_response(packets, analyses, candidate)
        replacement = copy.deepcopy(original)
        replacement["description"] = (
            "A narrower earnings-revision hypothesis may recur only within "
            "the supplied regime and bounded source set."
        )
        provider = FixtureProvider(
            [
                original,
                _review_response(
                    candidate,
                    "revise",
                    replacement_pattern=replacement,
                ),
            ]
        )
        result = _extractor(provider).extract(
            candidate,
            packets,
            analyses,
            pattern_id="revision_pattern",
            pattern_version=4,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.pattern.pattern_id, "revision_pattern")
        self.assertEqual(result.pattern.pattern_version, 5)
        self.assertEqual(result.pattern.pattern_status, PatternStatus.REVIEWED)
        self.assertNotEqual(
            result.adversarial_review.reviewed_pattern_hash,
            result.pattern.content_hash,
        )
        self.assertEqual(
            len(result.adversarial_review.reviewed_pattern_hash),
            64,
        )

    def test_adversarial_reject_is_structured_failure(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        result = _extractor(
            FixtureProvider(
                [
                    _pattern_response(packets, analyses, candidate),
                    _review_response(candidate, "reject"),
                ]
            )
        ).extract(candidate, packets, analyses)
        self.assertFalse(result.success)
        self.assertEqual(result.pattern.pattern_status, PatternStatus.REJECTED)
        self.assertTrue(result.validation_result.is_valid)
        self.assertEqual(
            result.adversarial_recommendation,
            PatternAdversarialRecommendation.REJECT,
        )

    def test_adversarial_insufficient_support_is_structured_failure(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        result = _extractor(
            FixtureProvider(
                [
                    _pattern_response(packets, analyses, candidate),
                    _review_response(candidate, "insufficient_support"),
                ]
            )
        ).extract(candidate, packets, analyses)
        self.assertFalse(result.success)
        self.assertEqual(
            result.pattern.pattern_status,
            PatternStatus.INSUFFICIENT_SUPPORT,
        )
        self.assertEqual(
            result.adversarial_recommendation,
            PatternAdversarialRecommendation.INSUFFICIENT_SUPPORT,
        )

    def test_review_requires_all_cases_and_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        incomplete = _review_response(
            candidate,
            referenced_case_ids=candidate.case_ids[:-1],
        )
        provider = FixtureProvider(
            [
                _pattern_response(packets, analyses, candidate),
                incomplete,
                _review_response(candidate),
            ]
        )
        result = _extractor(provider).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 3)
        self.assertIn("CORRECTION REQUIRED", provider.calls[2]["user_prompt"])

    def test_revise_without_replacement_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        provider = FixtureProvider(
            [
                _pattern_response(packets, analyses, candidate),
                _review_response(candidate, "revise"),
                _review_response(candidate),
            ]
        )
        result = _extractor(provider).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 3)


class HistoricalPatternRetryAndBoundaryTests(unittest.TestCase):
    def test_malformed_json_provider_error_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        provider = FixtureProvider(
            [
                ProviderError("Model output could not be parsed as JSON."),
                _pattern_response(packets, analyses, candidate),
            ]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertIn("CORRECTION REQUIRED", provider.calls[1]["user_prompt"])

    def test_dangling_case_reference_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        invalid = _pattern_response(packets, analyses, candidate)
        invalid["supporting_case_ids"] = (
            *invalid["supporting_case_ids"],
            "invented-case",
        )
        provider = FixtureProvider(
            [
                invalid,
                _pattern_response(packets, analyses, candidate),
            ]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_invented_exposure_type_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        invalid = _pattern_response(packets, analyses, candidate)
        invalid["initiating_exposure_types"] = ("invented_exposure",)
        provider = FixtureProvider(
            [
                invalid,
                _pattern_response(packets, analyses, candidate),
            ]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_invented_transmission_mechanism_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        invalid = _pattern_response(packets, analyses, candidate)
        invalid["transmission_mechanisms"] = ("invented_mechanism",)
        provider = FixtureProvider(
            [
                invalid,
                _pattern_response(packets, analyses, candidate),
            ]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_certainty_language_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        invalid = _pattern_response(packets, analyses, candidate)
        invalid["description"] = "This pattern is guaranteed."
        provider = FixtureProvider(
            [
                invalid,
                _pattern_response(packets, analyses, candidate),
            ]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_missing_limitations_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        invalid = _pattern_response(packets, analyses, candidate)
        invalid["limitations"] = ()
        provider = FixtureProvider(
            [
                invalid,
                _pattern_response(packets, analyses, candidate),
            ]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_circular_pattern_retries(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        invalid = _pattern_response(packets, analyses, candidate)
        invalid["description"] = "Momentum causes momentum."
        provider = FixtureProvider(
            [
                invalid,
                _pattern_response(packets, analyses, candidate),
            ]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_concentration_failure_does_not_retry(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle(
            symbol="NASDAQ:SAME",
        )
        provider = FixtureProvider(
            [_pattern_response(packets, analyses, candidate)]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(provider.strict_calls, 1)
        self.assertIn(
            "pattern_concentration",
            result.validation_result.failed_rules,
        )

    def test_genuine_insufficient_evidence_does_not_retry(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        response = _pattern_response(packets, analyses, candidate)
        response["confidence"] = "insufficient_evidence"
        provider = FixtureProvider([response])
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(provider.strict_calls, 1)
        self.assertEqual(
            result.pattern.pattern_status,
            PatternStatus.INSUFFICIENT_SUPPORT,
        )

    def test_infrastructure_provider_error_does_not_retry(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        provider = FixtureProvider(
            [ProviderError("OPENAI_API_KEY is required.")]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(provider.strict_calls, 1)

    def test_invalid_candidate_never_calls_provider(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        invalid = replace(candidate, content_hash="bad")
        provider = FixtureProvider([])
        result = _extractor(provider).extract(
            invalid,
            packets,
            analyses,
        )
        self.assertFalse(result.success)
        self.assertEqual(provider.strict_calls, 0)

    def test_packet_provider_never_reasons(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        result = _extractor(PacketProvider()).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertIn(
            "packet_provider_no_reasoning",
            result.validation_result.failed_rules,
        )

    def test_unsupported_provider_never_reasons(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        provider = FixtureProvider([], name="unsupported")
        result = _extractor(provider).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertFalse(result.success)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "unsupported_provider",
            result.validation_result.failed_rules,
        )

    def test_prompt_size_is_bounded_before_provider_call(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        provider = FixtureProvider([])
        result = _extractor(
            provider,
            max_prompt_characters=1_000,
        ).extract(candidate, packets, analyses)
        self.assertFalse(result.success)
        self.assertEqual(provider.strict_calls, 0)
        self.assertIn(
            "prompt_size_limit",
            result.validation_result.failed_rules,
        )


class HistoricalPatternResultContractTests(unittest.TestCase):
    def test_result_round_trip_and_hash(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        result = _extractor(
            FixtureProvider(
                [_pattern_response(packets, analyses, candidate)]
            ),
            adversarial=False,
        ).extract(candidate, packets, analyses)
        restored = HistoricalPatternExtractionResult.from_dict(
            result.to_dict()
        )
        self.assertEqual(restored, result)
        self.assertEqual(
            result.content_hash,
            historical_pattern_extraction_result_content_hash(result),
        )
        self.assertEqual(
            result.schema_version,
            HISTORICAL_PATTERN_EXTRACTION_SCHEMA_VERSION,
        )

    def test_repeated_run_is_deterministic(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        response = _pattern_response(packets, analyses, candidate)
        first = _extractor(
            FixtureProvider([response]),
            adversarial=False,
        ).extract(candidate, packets, analyses)
        second = _extractor(
            FixtureProvider([response]),
            adversarial=False,
        ).extract(candidate, packets, analyses)
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_frozen_inputs_and_result_hash_map_are_immutable(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        before = (
            candidate.to_dict(),
            tuple(item.to_dict() for item in packets),
            tuple(item.to_dict() for item in analyses),
        )
        result = _extractor(
            FixtureProvider(
                [_pattern_response(packets, analyses, candidate)]
            ),
            adversarial=False,
        ).extract(candidate, packets, analyses)
        after = (
            candidate.to_dict(),
            tuple(item.to_dict() for item in packets),
            tuple(item.to_dict() for item in analyses),
        )
        self.assertEqual(after, before)
        with self.assertRaises(TypeError):
            result.input_hashes["candidate"] = "mutated"

    def test_prompts_preserve_question_and_non_prediction_boundary(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        provider = FixtureProvider(
            [_pattern_response(packets, analyses, candidate)]
        )
        result = _extractor(provider, adversarial=False).extract(
            candidate,
            packets,
            analyses,
        )
        self.assertTrue(result.success)
        prompt = provider.calls[0]["system_prompt"]
        self.assertIn(
            "What recurring combination of initiating conditions",
            prompt,
        )
        self.assertIn("not a fact, prediction, universal law", prompt)
        self.assertEqual(
            result.pattern.prompt_version,
            HISTORICAL_PATTERN_PROMPT_VERSION,
        )

    def test_adversarial_prompt_version_is_recorded(self) -> None:
        packets, analyses, _, candidate, _ = _candidate_bundle()
        result = _extractor(
            FixtureProvider(
                [
                    _pattern_response(packets, analyses, candidate),
                    _review_response(candidate),
                ]
            )
        ).extract(candidate, packets, analyses)
        self.assertEqual(
            result.adversarial_review.prompt_version,
            HISTORICAL_PATTERN_ADVERSARIAL_PROMPT_VERSION,
        )

    def test_candidate_below_extractor_minimum_never_calls_provider(self) -> None:
        packets, analyses, features = _bundle(2)
        build = HistoricalPatternCandidateBuilder(
            minimum_supporting_cases=2,
            clock=lambda: datetime(2020, 3, 1, tzinfo=timezone.utc),
        ).build(packets, analyses, features)
        self.assertTrue(build.candidates)
        provider = FixtureProvider([])
        result = _extractor(provider).extract(
            build.candidates[0],
            packets,
            analyses,
        )
        self.assertFalse(result.success)
        self.assertEqual(provider.strict_calls, 0)


if __name__ == "__main__":
    unittest.main()
