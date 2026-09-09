"""Offline contract tests for the strict Complete-History OpenAI adapter."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Mapping
from unittest.mock import patch

from elliott_ai.blind_candidate_rules import default_blind_candidate_rules_pack
from elliott_ai.complete_history_openai_adapter import (
    COMPLETE_HISTORY_OPENAI_MODEL,
    CompleteHistoryOpenAICandidateProvider,
)
from elliott_ai.complete_history_shadow_runner import (
    CHILD_GRAPH_OUTPUT_SCHEMA,
    MONTHLY_MAP_OUTPUT_SCHEMA,
    MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
    WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA,
    CompleteHistoryShadowError,
    CompleteHistoryShadowReasonCode,
)
from elliott_ai.lower_timeframe_recursive_proof import RecursiveProofHypothesisRole
from elliott_ai.lower_timeframe_candidate_generator import (
    InvalidationDirection,
    InvalidationEvaluationBasis,
    candidate_invalidation_openai_json_schema,
)


def _invalidation(pivot_id: str) -> dict[str, Any]:
    return {
        "invalidation_id": f"invalid-{pivot_id}",
        "threshold_price": 1.0,
        "direction": "below",
        "evaluation_basis": "intrabar_touch_or_breach",
        "source_pivot_id": pivot_id,
    }


def _packet(role: RecursiveProofHypothesisRole) -> dict[str, Any]:
    rules_pack = default_blind_candidate_rules_pack()
    return {
        "packet_kind": "blind_complete_history_monthly_root_candidate",
        "role": role.value,
        "rules_pack_content_hash": rules_pack.content_hash,
        "blind_candidate_rules_pack": rules_pack.to_dict(),
        "pivot_catalog": [
            {"pivot_id": "p1", "timestamp_utc": "2020-01-01T00:00:00+00:00", "price": 10.0},
            {"pivot_id": "p2", "timestamp_utc": "2020-02-01T00:00:00+00:00", "price": 20.0},
            {"pivot_id": "p3", "timestamp_utc": "2020-03-01T00:00:00+00:00", "price": 15.0},
        ],
        "boundary_lineage": [
            {
                "lineage_id": "lineage-start",
                "parent_pivot_id": "parent-start",
                "child_pivot_id": "p1",
                "mapping_kind": "exact_price_source_candle_bracket",
            },
            {
                "lineage_id": "lineage-end",
                "parent_pivot_id": "parent-end",
                "child_pivot_id": "p2",
                "mapping_kind": "exact_price_source_candle_bracket",
            },
        ],
        "constraints": {
            "blind": True,
            "candidate_only": True,
            "no_alternative_input": True,
            "no_news_or_fundamental_input": True,
            "rules_pack_content_hash": rules_pack.content_hash,
        },
    }


def _root_output() -> dict[str, Any]:
    return {
        "candidate_id": "primary-root",
        "degree": "Cycle",
        "declared_family": "impulse",
        "direction": "up",
        "completion_state": "completed",
        "start_pivot_id": "p1",
        "end_pivot_id": "p2",
        "origin_control": None,
        "invalidation": _invalidation("p1"),
    }


def _monthly_map_packet(role: RecursiveProofHypothesisRole) -> dict[str, Any]:
    packet = _packet(role)
    packet.update(
        {
            "packet_kind": "blind_complete_history_monthly_map",
            "as_of_monthly_candle": {"candle_id": "monthly-as-of-p3"},
            "approved_monthly_map_scope": {"maximum_segments": 12},
        }
    )
    return packet


def _monthly_map_output() -> dict[str, Any]:
    return {
        "map_id": "primary-monthly-map",
        "outer_parent": {
            "parent_id": "outer-active",
            "degree": "Cycle",
            "declared_family": "impulse",
            "direction": "up",
            "completion_state": "active",
            "start_pivot_id": "p1",
            "end_pivot_id": "p3",
            "invalidation": _invalidation("p1"),
        },
        "origin_control": None,
        "as_of_monthly_candle_id": "monthly-as-of-p3",
        "segments": [
            {
                "segment_id": "completed-monthly-segment",
                "degree": "Primary",
                "declared_family": "zigzag",
                "direction": "up",
                "completion_state": "completed",
                "start_pivot_id": "p1",
                "end_pivot_id": "p2",
                "invalidation": _invalidation("p1"),
            },
            {
                "segment_id": "active-monthly-segment",
                "degree": "Primary",
                "declared_family": "impulse",
                "direction": "down",
                "completion_state": "active",
                "start_pivot_id": "p2",
                "end_pivot_id": "p3",
                "invalidation": _invalidation("p2"),
            },
        ],
    }


def _child_output() -> dict[str, Any]:
    return {
        "graph_id": "primary-weekly",
        "declared_family": "impulse",
        "boundary_lineage": _packet(RecursiveProofHypothesisRole.PRIMARY)["boundary_lineage"],
        "children": [
            {
                "child_id": "primary-weekly-1",
                "sequence_position": "1",
                "direction": "up",
                "declared_family": "impulse",
                "start_pivot_id": "p1",
                "end_pivot_id": "p2",
                "invalidation": _invalidation("p1"),
            }
        ],
    }


def _weekly_batch_packet(role: RecursiveProofHypothesisRole) -> dict[str, Any]:
    packet = _packet(role)
    packet.update(
        {
            "packet_kind": "blind_complete_history_weekly_child_graph_batch",
            "target_timeframe": "weekly",
            "segment_requests": [
                {
                    "segment_id": "completed-monthly-segment",
                    "target_timeframe": "weekly",
                    "pivot_catalog": packet["pivot_catalog"],
                    "boundary_lineage": packet["boundary_lineage"],
                }
            ],
        }
    )
    return packet


def _weekly_batch_output() -> dict[str, Any]:
    return {
        "batch_id": "primary-weekly-batch",
        "graphs": [
            {
                "segment_id": "completed-monthly-segment",
                "graph": _child_output(),
            }
        ],
    }


class _FakeStrictTransport:
    name = "openai"
    model = COMPLETE_HISTORY_OPENAI_MODEL

    def __init__(self, *responses: Mapping[str, Any] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_strict_json(
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
                "schema": schema,
                "packet": packet,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return dict(response)


class CompleteHistoryOpenAICandidateProviderTests(unittest.TestCase):
    def assert_call_accounting(
        self,
        provider: CompleteHistoryOpenAICandidateProvider,
        *,
        attempted: int,
        accepted: int,
        rejected: int,
    ) -> None:
        self.assertEqual(
            provider.call_accounting(),
            {
                "attempted_provider_calls": attempted,
                "accepted_provider_calls": accepted,
                "rejected_provider_calls": rejected,
            },
        )

    def test_invalidation_schema_is_derived_from_the_contract(self) -> None:
        schema = candidate_invalidation_openai_json_schema()

        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["required"],
            [
                "invalidation_id",
                "threshold_price",
                "direction",
                "evaluation_basis",
                "source_pivot_id",
            ],
        )
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertEqual(schema["properties"]["threshold_price"]["type"], "number")
        self.assertEqual(
            schema["properties"]["direction"]["enum"],
            [item.value for item in InvalidationDirection],
        )
        self.assertEqual(
            schema["properties"]["evaluation_basis"]["enum"],
            [item.value for item in InvalidationEvaluationBasis],
        )
        self.assertEqual(
            schema["examples"],
            [
                {
                    "invalidation_id": "invalidation_placeholder",
                    "threshold_price": 0.0,
                    "direction": "below",
                    "evaluation_basis": "intrabar_touch_or_breach",
                    "source_pivot_id": "pivot_placeholder",
                }
            ],
        )
        self.assertNotIn("GOOGL", json.dumps(schema, sort_keys=True))

    def test_root_request_is_one_call_blind_isolated_and_schema_bound(self) -> None:
        transport = _FakeStrictTransport(_root_output())
        provider = CompleteHistoryOpenAICandidateProvider(transport)
        packet = _packet(RecursiveProofHypothesisRole.PRIMARY)

        result = provider.generate_monthly_root_candidate(
            role=RecursiveProofHypothesisRole.PRIMARY,
            packet=packet,
            output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
        )

        self.assertEqual(result, _root_output())
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(set(call["schema"]), set(MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA))
        self.assertEqual(set(call["schema"]["properties"]), set(MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA["properties"]))
        self.assertEqual(call["packet"]["role"], "primary")
        self.assertTrue(call["packet"]["constraints"]["blind"])
        self.assertEqual(
            call["packet"]["rules_pack_content_hash"],
            default_blind_candidate_rules_pack().content_hash,
        )
        request = json.loads(call["user_prompt"])
        self.assertEqual(request["operation"], "monthly_root_candidate")
        self.assertEqual(request["role"], "primary")
        self.assertTrue(request["candidate_packet"]["constraints"]["no_alternative_input"])
        self.assertIn("Do not include reasoning", call["system_prompt"])
        self.assert_call_accounting(provider, attempted=1, accepted=1, rejected=0)

    def test_monthly_map_request_requires_one_terminal_active_segment(self) -> None:
        transport = _FakeStrictTransport(_monthly_map_output())
        provider = CompleteHistoryOpenAICandidateProvider(transport)
        packet = _monthly_map_packet(RecursiveProofHypothesisRole.PRIMARY)

        result = provider.generate_monthly_map(
            role=RecursiveProofHypothesisRole.PRIMARY,
            packet=packet,
            output_schema=MONTHLY_MAP_OUTPUT_SCHEMA,
        )

        self.assertEqual(result, _monthly_map_output())
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        request = json.loads(call["user_prompt"])
        self.assertEqual(request["operation"], "monthly_map")
        self.assertEqual(call["schema"], MONTHLY_MAP_OUTPUT_SCHEMA)
        self.assertEqual(call["packet"]["as_of_monthly_candle"]["candle_id"], "monthly-as-of-p3")
        self.assert_call_accounting(provider, attempted=1, accepted=1, rejected=0)

        invalid = _monthly_map_output()
        invalid["segments"][-1]["completion_state"] = "completed"
        rejected_transport = _FakeStrictTransport(invalid)
        rejected_provider = CompleteHistoryOpenAICandidateProvider(rejected_transport)
        with self.assertRaises(CompleteHistoryShadowError) as raised:
            rejected_provider.generate_monthly_map(
                role=RecursiveProofHypothesisRole.PRIMARY,
                packet=packet,
                output_schema=MONTHLY_MAP_OUTPUT_SCHEMA,
            )
        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
        self.assert_call_accounting(rejected_provider, attempted=1, accepted=0, rejected=1)

    def test_weekly_batch_requires_exact_segment_order_and_lineage(self) -> None:
        transport = _FakeStrictTransport(_weekly_batch_output())
        provider = CompleteHistoryOpenAICandidateProvider(transport)
        packet = _weekly_batch_packet(RecursiveProofHypothesisRole.ALTERNATIVE)

        result = provider.generate_weekly_child_graph_batch(
            role=RecursiveProofHypothesisRole.ALTERNATIVE,
            packet=packet,
            output_schema=WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA,
        )

        self.assertEqual(result, _weekly_batch_output())
        request = json.loads(transport.calls[0]["user_prompt"])
        self.assertEqual(request["operation"], "weekly_child_graph_batch")
        self.assertEqual(request["role"], "alternative")
        self.assert_call_accounting(provider, attempted=1, accepted=1, rejected=0)

        invented = _weekly_batch_output()
        invented["graphs"][0]["segment_id"] = "invented-segment"
        rejected_transport = _FakeStrictTransport(invented)
        rejected_provider = CompleteHistoryOpenAICandidateProvider(rejected_transport)
        with self.assertRaises(CompleteHistoryShadowError) as raised:
            rejected_provider.generate_weekly_child_graph_batch(
                role=RecursiveProofHypothesisRole.ALTERNATIVE,
                packet=packet,
                output_schema=WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA,
            )
        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
        self.assert_call_accounting(rejected_provider, attempted=1, accepted=0, rejected=1)

    def test_child_request_uses_same_strict_blind_boundary(self) -> None:
        transport = _FakeStrictTransport(_child_output())
        provider = CompleteHistoryOpenAICandidateProvider(transport)

        result = provider.generate_child_graph(
            role=RecursiveProofHypothesisRole.ALTERNATIVE,
            packet=_packet(RecursiveProofHypothesisRole.ALTERNATIVE),
            output_schema=CHILD_GRAPH_OUTPUT_SCHEMA,
        )

        self.assertEqual(result, _child_output())
        self.assertEqual(len(transport.calls), 1)
        request = json.loads(transport.calls[0]["user_prompt"])
        self.assertEqual(request["operation"], "child_graph")
        self.assertEqual(request["role"], "alternative")
        self.assert_call_accounting(provider, attempted=1, accepted=1, rejected=0)

    def test_forbidden_or_invented_output_stops_after_one_request_without_retry(self) -> None:
        forbidden = _root_output()
        forbidden["verification_status"] = "verified"
        invented = _root_output()
        invented["end_pivot_id"] = "made-up-pivot"

        for response in (forbidden, invented):
            with self.subTest(response=response):
                transport = _FakeStrictTransport(response)
                provider = CompleteHistoryOpenAICandidateProvider(transport)
                with self.assertRaises(CompleteHistoryShadowError) as raised:
                    provider.generate_monthly_root_candidate(
                        role=RecursiveProofHypothesisRole.PRIMARY,
                        packet=_packet(RecursiveProofHypothesisRole.PRIMARY),
                        output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
                    )
                self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
                self.assertEqual(len(transport.calls), 1)
                self.assert_call_accounting(provider, attempted=1, accepted=0, rejected=1)

    def test_child_forbidden_field_and_invented_invalidation_pivot_are_rejected(self) -> None:
        forbidden = _child_output()
        forbidden["children"][0]["report_prose"] = "not allowed"
        invented = _child_output()
        invented["children"][0]["invalidation"]["source_pivot_id"] = "not-in-catalog"

        for response in (forbidden, invented):
            with self.subTest(response=response):
                transport = _FakeStrictTransport(response)
                provider = CompleteHistoryOpenAICandidateProvider(transport)
                with self.assertRaises(CompleteHistoryShadowError) as raised:
                    provider.generate_child_graph(
                        role=RecursiveProofHypothesisRole.PRIMARY,
                        packet=_packet(RecursiveProofHypothesisRole.PRIMARY),
                        output_schema=CHILD_GRAPH_OUTPUT_SCHEMA,
                    )
                self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
                self.assertEqual(len(transport.calls), 1)
                self.assert_call_accounting(provider, attempted=1, accepted=0, rejected=1)

    def test_each_malformed_invalidation_field_is_rejected_after_one_attempt(self) -> None:
        mutations = {
            "invalidation_id": lambda value: value.update({"invalidation_id": ""}),
            "threshold_price": lambda value: value.update({"threshold_price": "not-a-number"}),
            "direction": lambda value: value.update({"direction": "sideways"}),
            "evaluation_basis": lambda value: value.update({"evaluation_basis": "opening_print"}),
            "source_pivot_id": lambda value: value.update({"source_pivot_id": ""}),
        }
        for field_name, mutate in mutations.items():
            with self.subTest(field_name=field_name):
                response = _root_output()
                mutate(response["invalidation"])
                transport = _FakeStrictTransport(response)
                provider = CompleteHistoryOpenAICandidateProvider(transport)

                with self.assertRaises(CompleteHistoryShadowError) as raised:
                    provider.generate_monthly_root_candidate(
                        role=RecursiveProofHypothesisRole.PRIMARY,
                        packet=_packet(RecursiveProofHypothesisRole.PRIMARY),
                        output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
                    )

                self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
                self.assertEqual(len(transport.calls), 1)
                self.assert_call_accounting(provider, attempted=1, accepted=0, rejected=1)

    def test_rejected_response_stops_immediately_without_retry_or_raw_persistence(self) -> None:
        marker = "raw-rejected-output-marker"
        rejected = _root_output()
        rejected["invalidation"]["unexpected_field"] = marker
        transport = _FakeStrictTransport(rejected, _root_output())
        provider = CompleteHistoryOpenAICandidateProvider(transport)

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            provider.generate_monthly_root_candidate(
                role=RecursiveProofHypothesisRole.PRIMARY,
                packet=_packet(RecursiveProofHypothesisRole.PRIMARY),
                output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.responses), 1)
        self.assert_call_accounting(provider, attempted=1, accepted=0, rejected=1)
        self.assertNotIn(marker, repr(provider))
        self.assertNotIn(marker, json.dumps(transport.calls, sort_keys=True))

    def test_invalid_blind_packet_or_schema_is_rejected_before_transport(self) -> None:
        transport = _FakeStrictTransport(_root_output())
        provider = CompleteHistoryOpenAICandidateProvider(transport)
        packet = _packet(RecursiveProofHypothesisRole.PRIMARY)
        packet["constraints"]["blind"] = False

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            provider.generate_monthly_root_candidate(
                role=RecursiveProofHypothesisRole.PRIMARY,
                packet=packet,
                output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
            )
        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
        self.assertEqual(transport.calls, [])
        self.assert_call_accounting(provider, attempted=0, accepted=0, rejected=0)

        transport = _FakeStrictTransport(_root_output())
        provider = CompleteHistoryOpenAICandidateProvider(transport)
        packet = _packet(RecursiveProofHypothesisRole.PRIMARY)
        packet["blind_candidate_rules_pack"]["rules"][0]["instruction"] = "Use a prior symbol count."

        with self.assertRaises(CompleteHistoryShadowError) as invalid_pack:
            provider.generate_monthly_root_candidate(
                role=RecursiveProofHypothesisRole.PRIMARY,
                packet=packet,
                output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
            )
        self.assertEqual(invalid_pack.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
        self.assertEqual(transport.calls, [])
        self.assert_call_accounting(provider, attempted=0, accepted=0, rejected=0)

        with self.assertRaises(ValueError):
            CompleteHistoryOpenAICandidateProvider(type("WrongTransport", (), {"name": "openai", "model": "other", "generate_strict_json": lambda self, **_: {}})())

    def test_environment_factory_wraps_openai_provider_without_exposing_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "adapter-test-secret"}, clear=False), patch(
            "elliott_ai.providers._post_json",
            return_value={"output_text": json.dumps(_root_output())},
        ) as post_json:
            provider = CompleteHistoryOpenAICandidateProvider.from_environment()
            result = provider.generate_monthly_root_candidate(
                role=RecursiveProofHypothesisRole.PRIMARY,
                packet=_packet(RecursiveProofHypothesisRole.PRIMARY),
                output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
            )
        self.assertEqual(provider.name, "openai")
        self.assertEqual(provider.model, COMPLETE_HISTORY_OPENAI_MODEL)
        self.assertEqual(result, _root_output())
        self.assertEqual(post_json.call_count, 1)
        request_payload = post_json.call_args.args[1]
        response_format = request_payload["text"]["format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["name"], "complete_history_monthly_root")
        self.assertTrue(response_format["strict"])
        self.assertEqual(
            response_format["schema"]["properties"]["invalidation"],
            candidate_invalidation_openai_json_schema(),
        )
        self.assert_call_accounting(provider, attempted=1, accepted=1, rejected=0)
        self.assertNotIn("adapter-test-secret", repr(provider))


if __name__ == "__main__":
    unittest.main()
