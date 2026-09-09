from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

import elliott_ai
from elliott_ai.agent import ElliottAgent
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.mistake_memory import RetrievedLesson
from elliott_ai.providers import OllamaProvider, PacketProvider, ProviderError
from elliott_ai.technical_agent_orchestrator import (
    FORECAST_AGENT_ORCHESTRATION_MIGRATION_SQL,
    FORECAST_AGENT_ORCHESTRATION_TABLES,
    ShadowComparisonStatus,
    ShadowResolutionStatus,
    TechnicalAgentOrchestration,
    TechnicalAgentOrchestrator,
    TechnicalAgentRequest,
    TechnicalAgentResultStatus,
    TechnicalAgentRole,
    TechnicalOrchestrationStatus,
    build_technical_agent_request_from_stored_records,
    replay_technical_agent_orchestration,
    technical_agent_orchestration_content_hash,
    validate_technical_agent_orchestration,
)


CUTOFF = "2026-01-05T21:00:00+00:00"
EXECUTED = "2026-01-05T21:05:00+00:00"
SYMBOL = "NASDAQ:TEST"


def _technical_evidence(*, end_date: str = CUTOFF) -> dict:
    return {
        "request": {"symbol": SYMBOL},
        "knowledge": [{"evidence_id": "K1", "content": "prior count"}],
        "context_data": [
            {"evidence_id": "C1", "fundamentals": "must remain excluded"}
        ],
        "market_data": [
            {
                "evidence_id": "M1",
                "source": "fixture.json",
                "timeframe": "daily",
                "start_date": "2026-01-01T21:00:00+00:00",
                "end_date": end_date,
                "price_pivot_candidates": {
                    "method": "fixture pivots",
                    "pivots": [
                        {
                            "type": "low",
                            "date": "2026-01-01T21:00:00+00:00",
                            "price": 100.0,
                        },
                        {
                            "type": "high",
                            "date": "2026-01-02T21:00:00+00:00",
                            "price": 120.0,
                        },
                        {
                            "type": "low",
                            "date": "2026-01-03T21:00:00+00:00",
                            "price": 108.0,
                        },
                        {
                            "type": "high",
                            "date": CUTOFF,
                            "price": 135.0,
                        },
                    ],
                },
                "technical_features": {
                    "active_features": ["volume", "rsi", "ewo", "macd"],
                    "volume": {"status": "available"},
                    "rsi": {"status": "available"},
                },
            }
        ],
        "zoom_windows": [],
        "data_reconciliation": {
            "evidence_id": "D1",
            "overall_status": "passed",
        },
        "wave_verification": None,
        "feature_manifest": {
            "active_candle_features": ["volume", "rsi", "ewo", "macd"]
        },
        "data_freshness": {"overall_status": "fixture"},
        "data_errors": [],
    }


def _node(
    *,
    wave_id: str,
    start_date: str,
    start_price: float,
    end_date: str,
    end_price: float,
    structure: str,
) -> dict:
    return {
        "wave_id": wave_id,
        "parent_wave_id": None,
        "degree": "Primary",
        "degree_status": "candidate",
        "degree_confidence": 0.6,
        "wave": "(1)",
        "sequence_position": "1",
        "timeframe": "daily",
        "direction": "up",
        "start": {"date": start_date, "price": start_price},
        "end": {"date": end_date, "price": end_price},
        "structure": structure,
        "completion_status": "completed",
        "child_structure_status": "unproven",
        "terminal_at_available_resolution": False,
        "child_wave_ids": [],
        "evidence": ["M1"],
        "confirmations": ["Price pivot is present in the frozen catalog."],
        "concerns": ["RSI and volume remain soft evidence."],
        "invalidation": None,
    }


def _resolution(
    *,
    run_id: int,
    end_date: str = CUTOFF,
    end_price: float = 135.0,
    structure: str = "impulse",
    wave_id: str = "primary-1",
) -> dict:
    node = _node(
        wave_id=wave_id,
        start_date="2026-01-01T21:00:00+00:00",
        start_price=100.0,
        end_date=end_date,
        end_price=end_price,
        structure=structure,
    )
    return {
        "resolution_status": "provisional",
        "symbol": SYMBOL,
        "source_run_id": run_id,
        "data_cutoff": CUTOFF,
        "scale_mode": "arithmetic",
        "scope": "Fixture decision-time count.",
        "degree_hierarchy": [node],
        "active_position": {
            "summary": "Primary (1) is the active interpretation.",
            "wave_ids": [wave_id],
            "confirmation": "Future price confirmation is not part of this fixture.",
            "invalidation": "No invented level is supplied.",
        },
        "alternate_counts": [],
        "invalidation_levels": [],
        "unresolved_items": ["Exact degree remains candidate."],
        "confidence": {"structure": 0.6, "degree": 0.5, "active_wave": 0.5},
        "citations": [{"evidence_id": "M1", "claim": "Observed price pivots."}],
        "risk_note": "Shadow research only.",
    }


def _source_records(store: KnowledgeStore) -> tuple[dict, dict]:
    evidence = _technical_evidence()
    run_id = store.save_run(
        symbol=SYMBOL,
        provider="fixture",
        model="fixture-v1",
        question="Fixture analysis",
        request={"symbol": SYMBOL, "features": ["volume", "rsi", "ewo", "macd"]},
        evidence=evidence,
        response={"data_cutoff": CUTOFF},
        validation_errors=[],
    )
    resolution_id = store.save_degree_resolution(
        run_id=run_id,
        provider="fixture",
        model="fixture-v1",
        request={"source_run_id": run_id},
        response=_resolution(run_id=run_id),
        validation_errors=[],
        readiness={"final_report_ready": False},
    )
    run = store.get_run(run_id)
    resolution = store.get_degree_resolution(resolution_id)
    assert run is not None and resolution is not None
    return run, resolution


def _pivot_for(packet: dict, date: str, price: float) -> str:
    for item in packet["pivot_catalog"]:
        if item["date"] == date and item["price"] == price:
            return item["pivot_id"]
    raise AssertionError(f"Missing fixture pivot {date} at {price}.")


def _counter_output(packet: dict, *, alternative: bool = False) -> dict:
    reference = packet["request_reference"]
    pivots = sorted(packet["pivot_catalog"], key=lambda item: item["date"])
    if alternative:
        end = pivots[2]
        structure = "zigzag"
        wave_id = "alternative-a"
    else:
        end = pivots[-1]
        structure = "impulse"
        wave_id = "primary-1"
    resolution = _resolution(
        run_id=reference["source_analysis_run_id"],
        end_date=end["date"],
        end_price=end["price"],
        structure=structure,
        wave_id=wave_id,
    )
    return {
        "candidate": {
            "degree_resolution": resolution,
            "pivot_reference_ids": [pivots[0]["pivot_id"], end["pivot_id"]],
            "evidence_reference_ids": ["M1"],
            "concise_reasoning_summary": [
                "Price structure was counted before indicator evidence."
            ],
            "distinguishing_features": [
                "Independent alternative structure."
                if alternative
                else "Strongest price-first candidate."
            ],
        }
    }


class FixtureProvider:
    name = "fixture"
    model = "fixture-v1"

    def __init__(
        self,
        *,
        fail_roles: set[str] | None = None,
        malformed_roles: set[str] | None = None,
        same_alternative: bool = False,
        invented_pivot_roles: set[str] | None = None,
        invented_evidence_roles: set[str] | None = None,
        invented_anchor_price_roles: set[str] | None = None,
        post_cutoff_anchor_roles: set[str] | None = None,
        hard_invalid_roles: set[str] | None = None,
        auditor_creates_count: bool = False,
        final_unknown_candidate: bool = False,
        transient_failures: dict[str, int] | None = None,
        audit_contradictory: bool = False,
    ) -> None:
        self.fail_roles = fail_roles or set()
        self.malformed_roles = malformed_roles or set()
        self.same_alternative = same_alternative
        self.invented_pivot_roles = invented_pivot_roles or set()
        self.invented_evidence_roles = invented_evidence_roles or set()
        self.invented_anchor_price_roles = invented_anchor_price_roles or set()
        self.post_cutoff_anchor_roles = post_cutoff_anchor_roles or set()
        self.hard_invalid_roles = hard_invalid_roles or set()
        self.auditor_creates_count = auditor_creates_count
        self.final_unknown_candidate = final_unknown_candidate
        self.transient_failures = dict(transient_failures or {})
        self.audit_contradictory = audit_contradictory
        self.calls: list[dict] = []
        self.role_counts: dict[str, int] = {}

    def generate_strict_json(self, **kwargs):
        packet = copy.deepcopy(kwargs["packet"])
        role = packet["agent_role"]
        self.calls.append(
            {
                "role": role,
                "packet": packet,
                "system_prompt": kwargs["system_prompt"],
                "user_prompt": kwargs["user_prompt"],
                "schema": copy.deepcopy(kwargs["schema"]),
            }
        )
        count = self.role_counts.get(role, 0)
        self.role_counts[role] = count + 1
        if count < self.transient_failures.get(role, 0):
            raise ProviderError("transient fixture failure")
        if role in self.fail_roles:
            raise ProviderError("permanent fixture failure")
        if role in self.malformed_roles:
            return {"chain_of_thought": "This field must be rejected."}
        if role in {
            TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value,
            TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value,
        }:
            alternative = role == TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value
            output = _counter_output(
                packet,
                alternative=(alternative and not self.same_alternative),
            )
            if role in self.invented_pivot_roles:
                output["candidate"]["pivot_reference_ids"].append("pivot_invented")
            if role in self.invented_evidence_roles:
                output["candidate"]["evidence_reference_ids"].append("M999")
                output["candidate"]["degree_resolution"]["citations"].append(
                    {"evidence_id": "M999", "claim": "Invented."}
                )
            if role in self.invented_anchor_price_roles:
                output["candidate"]["degree_resolution"]["degree_hierarchy"][0][
                    "end"
                ]["price"] += 0.25
            if role in self.post_cutoff_anchor_roles:
                output["candidate"]["degree_resolution"]["degree_hierarchy"][0][
                    "end"
                ]["date"] = "2026-01-06T21:00:00+00:00"
            if role in self.hard_invalid_roles:
                node = output["candidate"]["degree_resolution"]["degree_hierarchy"][0]
                node["child_structure_status"] = "verified"
            return output
        if role == TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR.value:
            audits = []
            for candidate in packet["frozen_candidates"]:
                candidate_id = candidate["candidate_id"]
                lesson_items = packet["retrieved_lessons_by_candidate"].get(
                    candidate_id, []
                )
                audit = {
                    "candidate_id": candidate_id,
                    "supporting_evidence_ids": (
                        [] if self.audit_contradictory else ["M1"]
                    ),
                    "contradictory_evidence_ids": (
                        ["M1"] if self.audit_contradictory else []
                    ),
                    "neutral_evidence_ids": [],
                    "unavailable_evidence": [
                        "No unavailable indicator was treated as a hard rule."
                    ],
                    "incomparable_evidence": [],
                    "lesson_retrieval_ids": [
                        item["retrieval_id"] for item in lesson_items
                    ],
                    "lesson_warnings": [
                        item["warning_text"] for item in lesson_items
                    ],
                    "concise_summary": [
                        "Soft evidence was audited without changing hard validity."
                    ],
                }
                if self.auditor_creates_count:
                    audit["degree_resolution"] = candidate["degree_resolution"]
                audits.append(audit)
            return {"audits": audits}
        if role == TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR.value:
            valid = packet["selection_boundary"]["allowed_candidate_ids"]
            invalid = packet["selection_boundary"]["invalid_candidate_ids"]
            selected = "invented-third-count" if self.final_unknown_candidate else valid[0]
            return {
                "status": "selected",
                "selected_candidate_id": selected,
                "ranked_valid_candidate_ids": valid,
                "rejected_candidate_ids": invalid,
                "unresolved_reason": None,
                "concise_summary": ["Only frozen eligible candidate IDs were ranked."],
            }
        raise AssertionError(f"Unexpected fixture role: {role}")

    def generate(self, **kwargs):
        return self.generate_strict_json(**kwargs)


class RecordingLessonStore:
    def __init__(self, log: list[str] | None = None) -> None:
        self.log = log if log is not None else []

    def list_mistake_memory_lessons(self, **kwargs):
        self.log.append("lesson_store:lessons")
        return []

    def list_mistake_memory_sources(self, **kwargs):
        self.log.append("lesson_store:sources")
        return []

    def list_mistake_memory_events(self, **kwargs):
        self.log.append("lesson_store:events")
        return []


class Phase11D1Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "brain.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.run, self.resolution = _source_records(self.store)

    def request(
        self,
        *,
        provider: str = "fixture",
        model: str | None = "fixture-v1",
        blind: bool = False,
        shadow: bool = True,
    ) -> TechnicalAgentRequest:
        return build_technical_agent_request_from_stored_records(
            self.run,
            self.resolution,
            provider=provider,
            model=model,
            created_at_utc=EXECUTED,
            blind_mode=blind,
            shadow_mode=shadow,
        )

    def execute_orchestration(
        self,
        provider: FixtureProvider | None = None,
        *,
        request: TechnicalAgentRequest | None = None,
        lesson_store=None,
        max_retries: int = 0,
        stage_callback=None,
        orchestration_version: int = 1,
        supersedes: str | None = None,
        executed_at: str = EXECUTED,
    ):
        selected_provider = provider or FixtureProvider()
        result = TechnicalAgentOrchestrator(
            provider=selected_provider,
            lesson_store=lesson_store,
            max_retries=max_retries,
            stage_callback=stage_callback,
        ).orchestrate(
            request or self.request(),
            existing_degree_resolution=self.resolution,
            shadow_mode=True,
            orchestration_version=orchestration_version,
            supersedes_orchestration_id=supersedes,
            executed_at_utc=executed_at,
        )
        return result, selected_provider


class ContractAndIsolationTests(Phase11D1Fixture):
    def test_immutable_request_round_trip_hash_and_tamper_detection(self) -> None:
        request = self.request()
        with self.assertRaises(FrozenInstanceError):
            request.symbol = "NASDAQ:OTHER"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            request.decision_time_market_packet["news"] = {}  # type: ignore[index]
        self.assertEqual(TechnicalAgentRequest.from_dict(request.to_dict()), request)
        payload = request.to_dict()
        payload["symbol"] = "NASDAQ:TAMPERED"
        with self.assertRaisesRegex(ValueError, "content_hash|input_hash"):
            TechnicalAgentRequest.from_dict(payload)

    def test_builder_excludes_prior_counts_knowledge_context_and_fundamentals(self) -> None:
        request = self.request()
        packet = request.to_dict()["decision_time_market_packet"]
        self.assertNotIn("knowledge", packet)
        self.assertNotIn("context_data", packet)
        text = json.dumps(packet).casefold()
        self.assertNotIn("prior count", text)
        self.assertNotIn("fundamentals", text)
        self.assertNotIn("degree_resolution", text)

    def test_request_rejects_post_cutoff_market_data(self) -> None:
        payload = self.request().to_dict()
        payload.pop("content_hash")
        payload["decision_time_input_hash"] = ""
        payload["decision_time_market_packet"]["market_data"][0]["end_date"] = (
            "2026-01-06T21:00:00+00:00"
        )
        with self.assertRaisesRegex(ValueError, "after the analysis cutoff"):
            TechnicalAgentRequest.create(**payload)

    def test_request_rejects_forbidden_company_or_news_fields(self) -> None:
        payload = self.request().to_dict()
        payload.pop("content_hash")
        payload["decision_time_input_hash"] = ""
        payload["decision_time_market_packet"]["company_knowledge"] = {
            "fact": "forbidden"
        }
        with self.assertRaisesRegex(ValueError, "unsupported top-level|forbidden"):
            TechnicalAgentRequest.create(**payload)

    def test_shadow_mode_is_default_off_and_provider_is_not_called(self) -> None:
        provider = FixtureProvider()
        request = self.request(shadow=False)
        orchestrator = TechnicalAgentOrchestrator(provider=provider)
        with self.assertRaisesRegex(RuntimeError, "disabled by default"):
            orchestrator.orchestrate(
                request,
                existing_degree_resolution=self.resolution,
            )
        self.assertEqual(provider.calls, [])

    def test_primary_and_alternative_are_blind_and_freeze_before_lessons(self) -> None:
        log: list[str] = []
        provider = FixtureProvider()

        def stage(name: str) -> None:
            log.append(f"stage:{name}")

        result, provider = self.execute_orchestration(
            provider,
            lesson_store=RecordingLessonStore(log),
            stage_callback=stage,
        )
        counter_calls = provider.calls[:2]
        self.assertEqual(
            [item["role"] for item in counter_calls],
            [
                TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value,
                TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value,
            ],
        )
        for call in counter_calls:
            serialized = json.dumps(call["packet"]).casefold()
            self.assertGreaterEqual(
                len(call["packet"]["hard_rule_contract"]["rules"]), 7
            )
            self.assertNotIn("frozen_candidates", serialized)
            self.assertNotIn("retrieved_lessons", serialized)
            self.assertNotIn("mistake_memory", serialized)
            self.assertNotIn("fundamentals", serialized)
            self.assertNotIn("company_knowledge", serialized)
            self.assertNotIn("post_cutoff", serialized)
        primary_packet = copy.deepcopy(counter_calls[0]["packet"])
        alternative_packet = copy.deepcopy(counter_calls[1]["packet"])
        primary_packet.pop("agent_role")
        alternative_packet.pop("agent_role")
        self.assertEqual(primary_packet, alternative_packet)
        self.assertLess(log.index("stage:alternative_frozen"), log.index("lesson_store:lessons"))
        self.assertEqual(result.stage_trace[:3], (
            "request_validated",
            "primary_frozen",
            "alternative_frozen",
        ))

    def test_blind_mode_never_reads_lesson_store(self) -> None:
        store = RecordingLessonStore()
        result, _ = self.execute_orchestration(request=self.request(blind=True), lesson_store=store)
        self.assertEqual(store.log, [])
        self.assertEqual(result.retrieved_lessons, ())


class ValidationAndAgentBoundaryTests(Phase11D1Fixture):
    def test_successful_shadow_selection_and_comparison_do_not_mutate_stored_resolution(self) -> None:
        before = copy.deepcopy(self.resolution)
        result, _ = self.execute_orchestration(lesson_store=RecordingLessonStore())
        self.assertEqual(result.shadow_resolution.status, ShadowResolutionStatus.SELECTED)
        self.assertEqual(result.shadow_resolution.selected_candidate_id, result.candidates[0].candidate_id)
        self.assertIn(
            result.shadow_comparison.comparison_status,
            {
                ShadowComparisonStatus.EXACT_MATCH,
                ShadowComparisonStatus.STRUCTURALLY_EQUIVALENT,
            },
        )
        self.assertEqual(self.store.get_degree_resolution(self.resolution["id"]), before)
        self.assertEqual(self.store.list_forecast_records(), [])

    def test_structured_output_rejects_hidden_reasoning_or_unknown_fields(self) -> None:
        provider = FixtureProvider(
            malformed_roles={TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value}
        )
        result, _ = self.execute_orchestration(provider, lesson_store=RecordingLessonStore())
        self.assertEqual(result.primary_result.status, TechnicalAgentResultStatus.FAILED)
        serialized = json.dumps(result.to_dict())
        self.assertNotIn("This field must be rejected.", serialized)
        self.assertIn("unknown field 'chain_of_thought'", serialized)
        self.assertEqual(len(result.candidates), 1)

    def test_invented_pivot_reference_is_rejected(self) -> None:
        provider = FixtureProvider(
            invented_pivot_roles={TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value}
        )
        result, _ = self.execute_orchestration(provider, lesson_store=RecordingLessonStore())
        primary = next(
            item
            for item in result.validation_results
            if item.candidate_id == result.candidates[0].candidate_id
        )
        self.assertFalse(primary.structurally_valid)
        self.assertTrue(any("invented pivot" in item for item in primary.reference_errors))
        self.assertNotEqual(result.shadow_resolution.selected_candidate_id, primary.candidate_id)

    def test_invented_evidence_reference_is_rejected(self) -> None:
        provider = FixtureProvider(
            invented_evidence_roles={TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value}
        )
        result, _ = self.execute_orchestration(provider, lesson_store=RecordingLessonStore())
        alternate_validation = result.validation_results[1]
        self.assertFalse(alternate_validation.structurally_valid)
        self.assertTrue(any("invented evidence" in item for item in alternate_validation.reference_errors))

    def test_invented_anchor_price_and_post_cutoff_timestamp_are_rejected(self) -> None:
        price_result, _ = self.execute_orchestration(
            FixtureProvider(
                invented_anchor_price_roles={
                    TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value
                }
            ),
            lesson_store=RecordingLessonStore(),
        )
        price_validation = price_result.validation_results[0]
        self.assertFalse(price_validation.structurally_valid)
        self.assertTrue(
            any("not present" in item for item in price_validation.reference_errors)
        )

        cutoff_result, _ = self.execute_orchestration(
            FixtureProvider(
                post_cutoff_anchor_roles={
                    TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value
                }
            ),
            lesson_store=RecordingLessonStore(),
        )
        cutoff_validation = cutoff_result.validation_results[0]
        self.assertFalse(cutoff_validation.structurally_valid)
        self.assertTrue(
            any(
                "after the analysis cutoff" in item
                for item in cutoff_validation.cutoff_errors
            )
        )

    def test_existing_hard_validator_controls_selection(self) -> None:
        provider = FixtureProvider(
            hard_invalid_roles={TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value}
        )
        result, _ = self.execute_orchestration(provider, lesson_store=RecordingLessonStore())
        primary = result.validation_results[0]
        self.assertFalse(primary.structurally_valid)
        self.assertTrue(primary.shape_errors or primary.hard_rule_errors)
        self.assertEqual(result.shadow_resolution.selected_candidate_id, result.candidates[1].candidate_id)

    def test_soft_indicator_contradiction_never_invalidates_candidate(self) -> None:
        provider = FixtureProvider(audit_contradictory=True)
        result, _ = self.execute_orchestration(provider, lesson_store=RecordingLessonStore())
        self.assertTrue(all(item.structurally_valid for item in result.validation_results))
        self.assertTrue(
            all(item.contradictory_evidence_ids == ("M1",) for item in result.evidence_audits)
        )
        self.assertEqual(result.shadow_resolution.status, ShadowResolutionStatus.SELECTED)

    def test_non_material_alternative_remains_visible_but_is_not_eligible(self) -> None:
        result, _ = self.execute_orchestration(
            FixtureProvider(same_alternative=True),
            lesson_store=RecordingLessonStore(),
        )
        alternate = result.validation_results[1]
        self.assertTrue(alternate.structurally_valid)
        self.assertFalse(alternate.selection_eligible)
        self.assertTrue(alternate.role_errors)
        self.assertIn(alternate.candidate_id, result.shadow_resolution.rejected_candidate_ids)

    def test_auditor_cannot_create_or_repair_a_count(self) -> None:
        result, _ = self.execute_orchestration(
            FixtureProvider(auditor_creates_count=True),
            lesson_store=RecordingLessonStore(),
        )
        self.assertEqual(result.auditor_result.status, TechnicalAgentResultStatus.FAILED)
        self.assertEqual(result.final_result.status, TechnicalAgentResultStatus.SKIPPED)
        self.assertEqual(result.shadow_resolution.status, ShadowResolutionStatus.UNRESOLVED)

    def test_final_orchestrator_cannot_invent_third_count(self) -> None:
        result, _ = self.execute_orchestration(
            FixtureProvider(final_unknown_candidate=True),
            lesson_store=RecordingLessonStore(),
        )
        self.assertEqual(result.final_result.status, TechnicalAgentResultStatus.FAILED)
        self.assertEqual(result.shadow_resolution.status, ShadowResolutionStatus.UNRESOLVED)
        self.assertIsNone(result.shadow_resolution.selected_candidate_id)

    def test_no_forced_count_when_every_candidate_fails(self) -> None:
        roles = {
            TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value,
            TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER.value,
        }
        result, provider = self.execute_orchestration(
            FixtureProvider(hard_invalid_roles=roles),
            lesson_store=RecordingLessonStore(),
        )
        self.assertEqual(result.shadow_resolution.status, ShadowResolutionStatus.REJECTED_INVALID)
        self.assertIsNone(result.shadow_resolution.selected_candidate_id)
        self.assertNotIn(
            TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR.value,
            [item["role"] for item in provider.calls],
        )


class FailureReplayAndLessonTests(Phase11D1Fixture):
    def test_partial_counter_failure_is_recorded_without_replacement(self) -> None:
        result, _ = self.execute_orchestration(
            FixtureProvider(
                fail_roles={TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value}
            ),
            lesson_store=RecordingLessonStore(),
        )
        self.assertEqual(result.primary_result.status, TechnicalAgentResultStatus.FAILED)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.candidates[0].role,
            TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER,
        )
        self.assertIn(
            result.status,
            {TechnicalOrchestrationStatus.COMPLETED_WITH_WARNINGS, TechnicalOrchestrationStatus.PARTIAL},
        )

    def test_retry_reuses_identical_frozen_input_and_prompt(self) -> None:
        role = TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value
        provider = FixtureProvider(transient_failures={role: 1})
        result, provider = self.execute_orchestration(
            provider,
            lesson_store=RecordingLessonStore(),
            max_retries=1,
        )
        primary_calls = [item for item in provider.calls if item["role"] == role]
        self.assertEqual(len(primary_calls), 2)
        self.assertEqual(primary_calls[0]["packet"], primary_calls[1]["packet"])
        self.assertEqual(primary_calls[0]["user_prompt"], primary_calls[1]["user_prompt"])
        self.assertEqual(result.primary_result.retry_count, 1)

    def test_repeated_fixture_execution_and_replay_are_deterministic(self) -> None:
        first, _ = self.execute_orchestration(FixtureProvider(), lesson_store=RecordingLessonStore())
        second, _ = self.execute_orchestration(FixtureProvider(), lesson_store=RecordingLessonStore())
        self.assertEqual(first.content_hash, second.content_hash)
        restored = replay_technical_agent_orchestration(
            first.to_dict(), expected_content_hash=first.content_hash
        )
        self.assertEqual(restored, first)
        self.assertEqual(validate_technical_agent_orchestration(restored), ())

    def test_orchestration_hash_tampering_is_detected(self) -> None:
        result, _ = self.execute_orchestration(lesson_store=RecordingLessonStore())
        payload = result.to_dict()
        payload["warnings"].append("tampered")
        with self.assertRaisesRegex(ValueError, "content_hash"):
            TechnicalAgentOrchestration.from_dict(payload)
        self.assertEqual(result.content_hash, technical_agent_orchestration_content_hash(result))

    def test_packet_provider_fails_closed_without_crashing(self) -> None:
        request = self.request(provider="packet", model=None)
        result = TechnicalAgentOrchestrator(provider=PacketProvider()).orchestrate(
            request,
            existing_degree_resolution=self.resolution,
            shadow_mode=True,
            executed_at_utc=EXECUTED,
        )
        self.assertEqual(result.status, TechnicalOrchestrationStatus.FAILED)
        self.assertEqual(result.shadow_resolution.status, ShadowResolutionStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.candidates, ())

    def test_ollama_strict_provider_path_is_offline_fixture_compatible(self) -> None:
        fixture = FixtureProvider()

        def fake_post_json(url, payload, **kwargs):
            del url, kwargs
            marker = "\n\nFROZEN INPUT:\n"
            packet = json.loads(payload["prompt"].rsplit(marker, 1)[1])
            output = fixture.generate_strict_json(
                system_prompt="fixture",
                user_prompt="fixture",
                schema={},
                packet=packet,
            )
            return {"response": json.dumps(output)}

        provider = OllamaProvider(model="fixture-v1")
        with patch("elliott_ai.providers._post_json", side_effect=fake_post_json):
            result = TechnicalAgentOrchestrator(
                provider=provider,
                lesson_store=RecordingLessonStore(),
            ).orchestrate(
                self.request(provider="ollama", model="fixture-v1"),
                existing_degree_resolution=self.resolution,
                shadow_mode=True,
                executed_at_utc=EXECUTED,
            )
        self.assertEqual(result.status, TechnicalOrchestrationStatus.COMPLETED)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.model, "fixture-v1")

    def test_approved_lesson_provenance_is_preserved_after_freezing(self) -> None:
        store = RecordingLessonStore()

        def retrieval(query, **kwargs):
            return (
                RetrievedLesson.create(
                    query_id=query.query_id,
                    query_content_hash=query.content_hash,
                    rank=1,
                    lesson_id="lesson-approved",
                    lesson_content_hash="1" * 64,
                    retrieval_scope="scoped",
                    effective_event_id="event-approved",
                    effective_event_content_hash="2" * 64,
                    approved_at_utc="2026-01-04T21:00:00+00:00",
                    matched_dimensions=("symbol",),
                    matching_reasons=("symbol matched",),
                    source_ids=("source-reviewed",),
                    source_provenance=(
                        {
                            "review_id": "review-human",
                            "forecast_id": "forecast-historical",
                        },
                    ),
                    warning_text="Recheck premature completion.",
                    recommended_audit_check="Preserve the valid continuation alternate.",
                    applicability_conditions=("Same structural role.",),
                    non_applicability_conditions=("Hard invalidation already occurred.",),
                ),
            )

        with patch(
            "elliott_ai.technical_agent_orchestrator.retrieve_applicable_lessons",
            side_effect=retrieval,
        ):
            result, _ = self.execute_orchestration(FixtureProvider(), lesson_store=store)
        self.assertTrue(result.retrieved_lessons)
        self.assertEqual(
            result.retrieved_lessons[0].source_provenance[0]["review_id"],
            "review-human",
        )
        self.assertTrue(any(item.lesson_retrieval_ids for item in result.evidence_audits))


class PersistenceMigrationAndCompatibilityTests(Phase11D1Fixture):
    def test_create_get_list_idempotency_append_only_and_foreign_keys(self) -> None:
        result, _ = self.execute_orchestration(lesson_store=RecordingLessonStore())
        inserted = self.store.create_forecast_agent_orchestration(result)
        self.assertTrue(inserted["inserted"])
        self.assertFalse(self.store.create_forecast_agent_orchestration(result)["inserted"])
        self.assertEqual(
            self.store.get_forecast_agent_orchestration(result.orchestration_id),
            result,
        )
        self.assertEqual(self.store.list_forecast_agent_orchestrations(), [result])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE forecast_agent_orchestrations SET status='failed' "
                    "WHERE orchestration_id=?",
                    (result.orchestration_id,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "DELETE FROM forecast_agent_orchestrations WHERE orchestration_id=?",
                    (result.orchestration_id,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "DELETE FROM degree_resolutions WHERE id=?",
                    (self.resolution["id"],),
                )
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_linear_supersession_and_latest_listing(self) -> None:
        first, _ = self.execute_orchestration(lesson_store=RecordingLessonStore())
        self.store.create_forecast_agent_orchestration(first)
        second, _ = self.execute_orchestration(
            FixtureProvider(audit_contradictory=True),
            lesson_store=RecordingLessonStore(),
            orchestration_version=2,
            supersedes=first.orchestration_id,
            executed_at="2026-01-05T21:06:00+00:00",
        )
        self.store.create_forecast_agent_orchestration(second)
        self.assertEqual(
            self.store.list_forecast_agent_orchestrations(include_superseded=False),
            [second],
        )
        branch, _ = self.execute_orchestration(
            FixtureProvider(),
            lesson_store=RecordingLessonStore(),
            orchestration_version=2,
            supersedes=first.orchestration_id,
            executed_at="2026-01-05T21:07:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "replacement"):
            self.store.create_forecast_agent_orchestration(branch)

    def test_database_record_tampering_is_detected(self) -> None:
        result, _ = self.execute_orchestration(lesson_store=RecordingLessonStore())
        self.store.create_forecast_agent_orchestration(result)
        with self.store.connect() as connection:
            connection.execute("DROP TRIGGER forecast_agent_orchestrations_no_update")
            payload = result.to_dict()
            payload["warnings"].append("tampered")
            connection.execute(
                "UPDATE forecast_agent_orchestrations SET record_json=? "
                "WHERE orchestration_id=?",
                (json.dumps(payload), result.orchestration_id),
            )
        with self.assertRaisesRegex(RuntimeError, "immutable validation"):
            self.store.get_forecast_agent_orchestration(result.orchestration_id)

    def test_one_table_migration_rollback_is_atomic(self) -> None:
        database = Path(self.temporary.name) / "rollback.sqlite3"
        initial = KnowledgeStore(database)
        with initial.connect() as connection:
            connection.execute("DROP TABLE forecast_agent_orchestrations")
        broken_sql = (
            FORECAST_AGENT_ORCHESTRATION_MIGRATION_SQL[0],
            "CREATE INDEX broken(",
        )
        with patch(
            "elliott_ai.knowledge.FORECAST_AGENT_ORCHESTRATION_MIGRATION_SQL",
            broken_sql,
        ):
            with self.assertRaises(sqlite3.OperationalError):
                KnowledgeStore(database)
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(set(FORECAST_AGENT_ORCHESTRATION_TABLES).isdisjoint(tables))

    def test_preexisting_database_gets_backup_and_audit(self) -> None:
        database = Path(self.temporary.name) / "preexisting.sqlite3"
        initial = KnowledgeStore(database)
        with initial.connect() as connection:
            connection.execute("DROP TABLE forecast_agent_orchestrations")
        migrated = KnowledgeStore(database)
        self.assertTrue(migrated.forecast_agent_migration_applied)
        self.assertTrue(migrated.forecast_agent_backup_path.exists())
        self.assertTrue(migrated.forecast_agent_migration_audit_path.exists())

    def test_only_one_table_no_mutation_methods_and_public_interfaces_unchanged(self) -> None:
        with self.store.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if row[0] in FORECAST_AGENT_ORCHESTRATION_TABLES
            }
        self.assertEqual(tables, set(FORECAST_AGENT_ORCHESTRATION_TABLES))
        self.assertFalse(hasattr(self.store, "update_forecast_agent_orchestration"))
        self.assertFalse(hasattr(self.store, "delete_forecast_agent_orchestration"))
        self.assertEqual(
            elliott_ai.__all__, ["AnalysisRequest", "ElliottAgent", "KnowledgeStore"]
        )
        self.assertEqual(
            list(inspect.signature(ElliottAgent.analyze).parameters),
            ["self", "request"],
        )
        self.assertEqual(
            list(inspect.signature(ElliottAgent.resolve_degrees).parameters),
            ["self", "source_run_id", "question"],
        )


if __name__ == "__main__":
    unittest.main()
