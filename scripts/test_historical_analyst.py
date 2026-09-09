from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from elliott_ai.historical_analyst import (
    HISTORICAL_ADVERSARIAL_PROMPT_VERSION,
    HISTORICAL_ANALYST_SCHEMA_VERSION,
    HISTORICAL_CAUSAL_PROMPT_VERSION,
    HistoricalAdversarialRecommendation,
    HistoricalAnalysisExecutionResult,
    HistoricalCaseAnalyst,
    historical_analysis_execution_result_content_hash,
)
from elliott_ai.historical_market import (
    HistoricalAnalystType,
    HistoricalCaseStatus,
    HistoricalEvidencePacket,
    HistoricalMoveType,
    ScenarioDirection,
    TransmissionMechanism,
    finalize_historical_evidence_packet,
)
from elliott_ai.providers import (
    PacketProvider,
    ProviderError,
    _parse_strict_model_json,
)
from scripts.test_historical_market import (
    CUTOFF,
    _draft_causal,
    _draft_packet,
    _move,
    _packet,
)


FIXED_NOW = datetime(2020, 2, 2, tzinfo=timezone.utc)


@dataclass
class FixtureProvider:
    responses: list[Any]
    name: str = "fixture"
    model: str | None = "fixture-model"
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


def _causal_response(**overrides: Any) -> dict[str, Any]:
    raw = _draft_causal().to_dict()
    for field_name in (
        "case_id",
        "analyst_type",
        "model_name",
        "prompt_version",
        "generated_at",
        "applicable_cutoff",
        "validation_result",
        "content_hash",
        "schema_version",
    ):
        raw.pop(field_name, None)
    raw.update(overrides)
    return raw


def _review_response(
    recommendation: str = "keep",
    *,
    replacement_analysis: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "recommendation": recommendation,
        "summary": "The draft was checked against the frozen packet.",
        "hindsight_bias_findings": (),
        "post_outcome_leakage_findings": (),
        "cherry_picking_findings": (),
        "unsupported_causal_links": (),
        "omitted_bullish_evidence_ids": ("ev-after",),
        "omitted_bearish_evidence_ids": (),
        "single_catalyst_assessment": (
            "The draft retains more than one contributing driver."
        ),
        "market_regime_assessment": "Market regime was considered.",
        "sector_assessment": "Sector contribution was considered.",
        "positioning_assessment": "Positioning uncertainty was retained.",
        "simpler_explanations": (
            "Broad market beta may explain part of the move.",
        ),
        "contradictory_timing": (),
        "magnitude_assessment": "Magnitude remains conditionally explained.",
        "referenced_evidence_ids": (
            "ev-before",
            "ev-during",
            "ev-after",
            "ev-retrospective",
        ),
        "replacement_analysis": replacement_analysis,
    }
    raw.update(overrides)
    return raw


def _analyst(
    provider: Any,
    *,
    adversarial: bool = True,
    attempts: int = 2,
) -> HistoricalCaseAnalyst:
    return HistoricalCaseAnalyst(
        provider,
        max_attempts=attempts,
        use_adversarial_pass=adversarial,
        clock=lambda: FIXED_NOW,
    )


class HistoricalCaseAnalystSuccessTests(unittest.TestCase):
    def test_valid_upward_impulse_analysis(self) -> None:
        provider = FixtureProvider(
            [_causal_response(), _review_response("keep")]
        )
        result = _analyst(provider).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(result.used_adversarial_pass)
        self.assertIs(
            result.adversarial_recommendation,
            HistoricalAdversarialRecommendation.KEEP,
        )
        self.assertIsNotNone(result.analysis)
        self.assertTrue(result.validation_result.is_valid)
        self.assertEqual(provider.strict_calls, 2)

    def test_valid_historical_crash_analysis(self) -> None:
        crash = _move(
            case_id="case-crash",
            end_price=50.0,
            direction=ScenarioDirection.DOWN,
            move_type=HistoricalMoveType.CRASH,
        )
        packet = _packet(move=crash)
        provider = FixtureProvider([_causal_response()])
        result = _analyst(provider, adversarial=False).analyze(packet)
        self.assertTrue(result.success)
        self.assertEqual(result.analysis.case_id, "case-crash")
        self.assertEqual(
            result.input_packet_hash,
            packet.content_hash,
        )

    def test_multi_driver_and_no_single_catalyst_are_preserved(self) -> None:
        raw = _causal_response(
            primary_drivers=(
                "Earnings revisions plausibly contributed.",
                "Multiple expansion may have amplified the move.",
            ),
            secondary_drivers=("Sector demand was supportive.",),
            no_single_catalyst=True,
        )
        result = _analyst(
            FixtureProvider([raw]),
            adversarial=False,
        ).analyze(_packet())
        self.assertTrue(result.success)
        self.assertTrue(result.analysis.no_single_catalyst)
        self.assertGreaterEqual(
            len(result.analysis.primary_drivers)
            + len(result.analysis.secondary_drivers),
            2,
        )

    def test_controlled_transmission_mechanisms_are_parsed(self) -> None:
        raw = _causal_response(
            transmission_mechanisms=(
                TransmissionMechanism.EARNINGS_REVISION.value,
                TransmissionMechanism.MULTIPLE_EXPANSION.value,
            )
        )
        result = _analyst(
            FixtureProvider([raw]),
            adversarial=False,
        ).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(
            result.analysis.transmission_mechanisms,
            (
                TransmissionMechanism.EARNINGS_REVISION,
                TransmissionMechanism.MULTIPLE_EXPANSION,
            ),
        )

    def test_prompt_contains_frozen_temporal_boundary(self) -> None:
        provider = FixtureProvider([_causal_response()])
        _analyst(provider, adversarial=False).analyze(_packet())
        call = provider.calls[0]
        self.assertIn("is frozen", call["system_prompt"])
        self.assertIn("post-outcome", call["system_prompt"])
        self.assertIn("FROZEN HISTORICAL PACKET", call["user_prompt"])


class HistoricalCaseAnalystAdversarialTests(unittest.TestCase):
    def test_adversarial_keep(self) -> None:
        provider = FixtureProvider(
            [_causal_response(), _review_response("keep")]
        )
        result = _analyst(provider).analyze(_packet())
        self.assertTrue(result.success)
        self.assertIs(
            result.adversarial_recommendation,
            HistoricalAdversarialRecommendation.KEEP,
        )
        self.assertEqual(
            result.adversarial_review.prompt_version,
            HISTORICAL_ADVERSARIAL_PROMPT_VERSION,
        )

    def test_adversarial_revise_replaces_analysis(self) -> None:
        replacement = _causal_response(
            primary_drivers=(
                "A revised multi-driver explanation is consistent with evidence.",
            ),
            secondary_drivers=(
                "Sector conditions may have amplified repricing.",
            ),
        )
        provider = FixtureProvider(
            [
                _causal_response(),
                _review_response(
                    "revise",
                    replacement_analysis=replacement,
                ),
            ]
        )
        result = _analyst(provider).analyze(_packet())
        self.assertTrue(result.success)
        self.assertIs(
            result.adversarial_recommendation,
            HistoricalAdversarialRecommendation.REVISE,
        )
        self.assertEqual(
            result.analysis.primary_drivers,
            (
                "A revised multi-driver explanation is consistent with evidence.",
            ),
        )

    def test_adversarial_reject_returns_structured_rejection(self) -> None:
        provider = FixtureProvider(
            [_causal_response(), _review_response("reject")]
        )
        result = _analyst(provider).analyze(_packet())
        self.assertFalse(result.success)
        self.assertIsNotNone(result.analysis)
        self.assertIs(
            result.adversarial_recommendation,
            HistoricalAdversarialRecommendation.REJECT,
        )

    def test_adversarial_missing_replacement_retries(self) -> None:
        provider = FixtureProvider(
            [
                _causal_response(),
                _review_response("revise", replacement_analysis=None),
                _review_response("keep"),
            ]
        )
        result = _analyst(provider).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 3)
        self.assertIn("CORRECTION REQUIRED", provider.calls[2]["user_prompt"])


class HistoricalCaseAnalystRetryTests(unittest.TestCase):
    def test_malformed_output_then_successful_retry(self) -> None:
        provider = FixtureProvider(["not-json-object", _causal_response()])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertIn("CORRECTION REQUIRED", provider.calls[1]["user_prompt"])

    def test_dangling_evidence_reference_is_retried(self) -> None:
        invalid = _causal_response(
            supporting_evidence_ids=("ev-before", "ev-missing")
        )
        provider = FixtureProvider([invalid, _causal_response()])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(
            any("dangling" in error.casefold() for error in result.errors)
        )

    def test_temporal_leakage_is_retried(self) -> None:
        invalid = _causal_response(
            initiating_condition_evidence_ids=("ev-after",),
            supporting_evidence_ids=("ev-after", "ev-before"),
        )
        provider = FixtureProvider([invalid, _causal_response()])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(
            any("future_leakage" in error for error in result.errors)
        )

    def test_unsupported_certainty_is_retried(self) -> None:
        invalid = _causal_response(
            primary_drivers=("Earnings definitely caused the move.",)
        )
        provider = FixtureProvider([invalid, _causal_response()])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertTrue(result.success)
        self.assertTrue(
            any(
                "unsupported_causal_certainty" in error
                for error in result.errors
            )
        )

    def test_all_named_certainty_phrases_are_retried(self) -> None:
        for phrase in (
            "This proves that earnings caused the move.",
            "This certainly happened because of the event.",
            "The chart predicted the announcement.",
            "The event must have caused the move.",
        ):
            with self.subTest(phrase=phrase):
                provider = FixtureProvider(
                    [
                        _causal_response(primary_drivers=(phrase,)),
                        _causal_response(),
                    ]
                )
                result = _analyst(
                    provider,
                    adversarial=False,
                ).analyze(_packet())
                self.assertTrue(result.success)
                self.assertEqual(result.attempts, 2)

    def test_missing_contradicting_evidence_is_retried(self) -> None:
        invalid = _causal_response(contradicting_evidence_ids=())
        provider = FixtureProvider([invalid, _causal_response()])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_invalid_mechanism_then_successful_retry(self) -> None:
        invalid = _causal_response(
            transmission_mechanisms=("invented_mechanism",)
        )
        provider = FixtureProvider([invalid, _causal_response()])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_retry_exhaustion_returns_structured_failure(self) -> None:
        invalid = _causal_response(contradicting_evidence_ids=())
        provider = FixtureProvider([invalid, invalid])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertIsNotNone(result.analysis)
        self.assertFalse(result.validation_result.is_valid)

    def test_genuine_insufficient_evidence_is_not_retried(self) -> None:
        insufficient = _causal_response(
            initiating_conditions=(),
            primary_drivers=(),
            secondary_drivers=(),
            transmission_mechanisms=(),
            contradicting_evidence_ids=(),
            supporting_evidence_ids=(),
            initiating_condition_evidence_ids=(),
            retrospective_explanations=(),
            retrospective_evidence_ids=(),
            confidence="insufficient_evidence",
            missing_evidence=(
                "The supplied packet cannot support a causal conclusion.",
            ),
        )
        provider = FixtureProvider([insufficient])
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(provider.calls), 1)


class HistoricalCaseAnalystBoundaryTests(unittest.TestCase):
    def test_strict_provider_parser_rejects_extra_prose(self) -> None:
        self.assertEqual(_parse_strict_model_json('{"value":1}'), {"value": 1})
        for output in (
            '```json\n{"value":1}\n```',
            'Here is the result: {"value":1}',
            '{"value":1} trailing text',
        ):
            with self.subTest(output=output):
                with self.assertRaises(ProviderError):
                    _parse_strict_model_json(output)

    def test_invalid_input_packet_is_rejected_without_provider_call(self) -> None:
        provider = FixtureProvider([_causal_response()])
        invalid_packet = _draft_packet()
        result = _analyst(provider).analyze(invalid_packet)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertEqual(provider.calls, [])

    def test_provider_failure_is_structured_and_not_retried(self) -> None:
        provider = FixtureProvider(
            [ProviderError("Provider returned HTTP 503: unavailable")]
        )
        result = _analyst(provider, adversarial=False).analyze(_packet())
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(
            any("HTTP 503" in error for error in result.errors)
        )

    def test_packet_provider_returns_offline_unavailable_state(self) -> None:
        result = _analyst(PacketProvider()).analyze(_packet())
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertIn(
            "packet_provider_no_reasoning",
            result.validation_result.failed_rules,
        )

    def test_malformed_adversarial_scalar_is_retried(self) -> None:
        provider = FixtureProvider(
            [
                _causal_response(),
                _review_response("keep", summary=42),
                _review_response("keep"),
            ]
        )
        result = _analyst(provider).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 3)

    def test_application_overrides_model_provenance(self) -> None:
        raw = _causal_response()
        raw.update(
            {
                "case_id": "spoofed-case",
                "analyst_type": "human",
                "model_name": "spoofed-model",
                "prompt_version": "spoofed-prompt",
                "generated_at": "2099-01-01T00:00:00+00:00",
                "applicable_cutoff": "2099-01-01T00:00:00+00:00",
                "content_hash": "spoofed",
                "schema_version": "spoofed",
            }
        )
        result = _analyst(
            FixtureProvider([raw]),
            adversarial=False,
        ).analyze(_packet())
        self.assertTrue(result.success)
        self.assertEqual(result.analysis.case_id, "case-up")
        self.assertIs(result.analysis.analyst_type, HistoricalAnalystType.LLM)
        self.assertEqual(result.analysis.model_name, "fixture-model")
        self.assertEqual(
            result.analysis.prompt_version,
            HISTORICAL_CAUSAL_PROMPT_VERSION,
        )
        self.assertEqual(result.analysis.generated_at, FIXED_NOW.isoformat())
        self.assertEqual(result.analysis.applicable_cutoff, CUTOFF)

    def test_frozen_input_remains_hash_identical(self) -> None:
        packet = _packet()
        before = packet.to_dict()
        provider = FixtureProvider(
            [_causal_response(), _review_response("keep")]
        )
        _analyst(provider).analyze(packet)
        self.assertEqual(packet.to_dict(), before)
        self.assertEqual(packet.content_hash, before["content_hash"])

    def test_prompt_size_limit_prevents_provider_call(self) -> None:
        packet = _packet()
        provider = FixtureProvider([_causal_response()])
        analyst = HistoricalCaseAnalyst(
            provider,
            max_prompt_characters=1_000,
            use_adversarial_pass=False,
            clock=lambda: FIXED_NOW,
        )
        result = analyst.analyze(packet)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertEqual(provider.calls, [])
        self.assertIn(
            "prompt_size_limit",
            result.validation_result.failed_rules,
        )


class HistoricalExecutionContractTests(unittest.TestCase):
    def test_execution_result_hash_and_serialization(self) -> None:
        provider = FixtureProvider(
            [_causal_response(), _review_response("keep")]
        )
        original = _analyst(provider).analyze(_packet())
        restored = HistoricalAnalysisExecutionResult.from_dict(
            original.to_dict()
        )
        self.assertEqual(restored, original)
        self.assertEqual(
            original.content_hash,
            historical_analysis_execution_result_content_hash(original),
        )
        self.assertEqual(
            original.schema_version,
            HISTORICAL_ANALYST_SCHEMA_VERSION,
        )

    def test_repeated_fixture_runs_are_deterministic(self) -> None:
        first = _analyst(
            FixtureProvider([_causal_response(), _review_response("keep")])
        ).analyze(_packet())
        second = _analyst(
            FixtureProvider([_causal_response(), _review_response("keep")])
        ).analyze(_packet())
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)


if __name__ == "__main__":
    unittest.main()
