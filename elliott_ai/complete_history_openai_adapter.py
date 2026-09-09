"""Strict OpenAI adapter for the manual complete-history shadow runner.

This module intentionally does not discover data, load prior analyses, query
SQLite, or execute a runner.  It converts one already-isolated candidate slot
into one strict JSON request and rejects output before it can enter the runner
when it is not a blind, pivot-catalog-bound candidate graph.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .blind_candidate_rules import BlindCandidateRulesPack
from .complete_history_shadow_runner import (
    CHILD_GRAPH_OUTPUT_SCHEMA,
    MONTHLY_MAP_OUTPUT_SCHEMA,
    MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
    WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA,
    CompleteHistoryShadowError,
    CompleteHistoryShadowReasonCode,
)
from .lower_timeframe_candidate_generator import CandidateInvalidation
from .lower_timeframe_recursive_proof import RecursiveProofHypothesisRole
from .providers import OpenAIResponsesProvider, ProviderError


COMPLETE_HISTORY_OPENAI_PROVIDER_NAME = "openai"
COMPLETE_HISTORY_OPENAI_MODEL = "gpt-5.6-terra"

_MONTHLY_ROOT_SCHEMA_NAME = "complete_history_monthly_root"
_MONTHLY_MAP_SCHEMA_NAME = "complete_history_monthly_map"
_CHILD_GRAPH_SCHEMA_NAME = "complete_history_child_graph"
_WEEKLY_BATCH_SCHEMA_NAME = "complete_history_weekly_child_graph_batch"

_FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "verified",
        "verification_status",
        "confidence",
        "report_prose",
    }
)
_ROOT_FIELDS = frozenset(
    {
        "candidate_id",
        "degree",
        "declared_family",
        "direction",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "origin_control",
        "invalidation",
    }
)
_MONTHLY_MAP_FIELDS = frozenset({"map_id", "outer_parent", "origin_control", "as_of_monthly_candle_id", "segments"})
_MONTHLY_MAP_PARENT_FIELDS = frozenset(
    {
        "parent_id",
        "degree",
        "declared_family",
        "direction",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "invalidation",
    }
)
_MONTHLY_MAP_SEGMENT_FIELDS = frozenset(
    {
        "segment_id",
        "degree",
        "declared_family",
        "direction",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "invalidation",
    }
)
_ORIGIN_CONTROL_FIELDS = frozenset({"start_pivot_id", "end_pivot_id"})
_CHILD_GRAPH_FIELDS = frozenset({"graph_id", "declared_family", "boundary_lineage", "children"})
_WEEKLY_BATCH_FIELDS = frozenset({"batch_id", "graphs"})
_WEEKLY_BATCH_ENTRY_FIELDS = frozenset({"segment_id", "graph"})
_BOUNDARY_LINEAGE_FIELDS = frozenset(
    {"lineage_id", "parent_pivot_id", "child_pivot_id", "mapping_kind"}
)
_CHILD_FIELDS = frozenset(
    {
        "child_id",
        "sequence_position",
        "direction",
        "declared_family",
        "start_pivot_id",
        "end_pivot_id",
        "invalidation",
    }
)


class CompleteHistoryStrictJsonTransport(Protocol):
    """Minimal strict-JSON boundary used by the adapter and test fixtures."""

    name: str
    model: str

    def generate_strict_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        ...


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return value


def _strict_error(detail: str) -> CompleteHistoryShadowError:
    return CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID, detail)


def _require_packet_constraints(
    packet: Mapping[str, Any],
    *,
    role: RecursiveProofHypothesisRole,
) -> None:
    if not isinstance(packet, Mapping):
        raise _strict_error("Complete-history candidate packet must be an object.")
    if packet.get("role") != role.value:
        raise _strict_error("Candidate packet role does not match the isolated request role.")
    constraints = packet.get("constraints")
    if not isinstance(constraints, Mapping):
        raise _strict_error("Candidate packet is missing its blind constraints.")
    expected = {
        "blind": True,
        "candidate_only": True,
        "no_alternative_input": True,
        "no_news_or_fundamental_input": True,
    }
    for key, expected_value in expected.items():
        if constraints.get(key) is not expected_value:
            raise _strict_error(f"Candidate packet does not enforce {key}={expected_value!r}.")
    _require_packet_rules_pack(packet, constraints=constraints)


def _require_packet_rules_pack(
    packet: Mapping[str, Any],
    *,
    constraints: Mapping[str, Any],
) -> None:
    """Require the closed v1.0 pack before a provider sees a blind packet."""

    declared_hash = packet.get("rules_pack_content_hash")
    if not isinstance(declared_hash, str) or not declared_hash:
        raise _strict_error("Candidate packet is missing its blind rules-pack hash.")
    if constraints.get("rules_pack_content_hash") != declared_hash:
        raise _strict_error("Candidate packet rules-pack hash is not bound into its blind constraints.")
    raw_pack = packet.get("blind_candidate_rules_pack")
    if not isinstance(raw_pack, Mapping):
        raise _strict_error("Candidate packet is missing its closed blind rules pack.")
    try:
        rules_pack = BlindCandidateRulesPack.from_dict(_json_value(raw_pack))
    except (TypeError, ValueError) as exc:
        raise _strict_error("Candidate packet contains an invalid blind rules pack.") from exc
    if rules_pack.content_hash != declared_hash:
        raise _strict_error("Candidate packet rules-pack hash does not match its canonical pack.")


def _catalog_pivot_ids(packet: Mapping[str, Any]) -> frozenset[str]:
    catalog = packet.get("pivot_catalog")
    if isinstance(catalog, (str, bytes)) or not isinstance(catalog, Sequence) or not catalog:
        raise _strict_error("Candidate packet has no immutable pivot catalog.")
    pivot_ids: set[str] = set()
    for index, pivot in enumerate(catalog):
        if not isinstance(pivot, Mapping):
            raise _strict_error(f"Pivot catalog entry {index} is not an object.")
        pivot_id = pivot.get("pivot_id")
        if not isinstance(pivot_id, str) or not pivot_id.strip() or pivot_id in pivot_ids:
            raise _strict_error("Pivot catalog contains an invalid or duplicate pivot ID.")
        pivot_ids.add(pivot_id)
    return frozenset(pivot_ids)


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _FORBIDDEN_OUTPUT_FIELDS:
                raise _strict_error(f"Candidate output contains forbidden field {key!r}.")
            _reject_forbidden_fields(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _reject_forbidden_fields(nested)


def _require_exact_fields(value: Mapping[str, Any], *, allowed: frozenset[str], name: str) -> None:
    keys = set(value)
    unknown = sorted(keys - allowed)
    missing = sorted(allowed - keys)
    if unknown or missing:
        detail: list[str] = []
        if unknown:
            detail.append("unknown=" + ", ".join(unknown))
        if missing:
            detail.append("missing=" + ", ".join(missing))
        raise _strict_error(f"{name} is not an exact strict-schema object ({'; '.join(detail)}).")


def _validate_invalidation(value: Any, *, pivot_ids: frozenset[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise _strict_error(f"{name}.invalidation must be an object.")
    try:
        invalidation = CandidateInvalidation.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise _strict_error(f"{name}.invalidation is invalid.") from exc
    if invalidation.source_pivot_id not in pivot_ids:
        raise _strict_error(f"{name}.invalidation references an invented pivot.")


def _validate_pivot_reference(value: Any, *, pivot_ids: frozenset[str], name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value not in pivot_ids:
        raise _strict_error(f"{name} references an invented or unavailable pivot.")


def _validate_root_output(value: Any, *, pivot_ids: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _strict_error("Monthly root output must be an object.")
    result = _json_value(value)
    if not isinstance(result, dict):
        raise _strict_error("Monthly root output must be a JSON object.")
    _reject_forbidden_fields(result)
    _require_exact_fields(result, allowed=_ROOT_FIELDS, name="Monthly root output")
    _validate_pivot_reference(result["start_pivot_id"], pivot_ids=pivot_ids, name="start_pivot_id")
    _validate_pivot_reference(result["end_pivot_id"], pivot_ids=pivot_ids, name="end_pivot_id")
    origin_control = result["origin_control"]
    if origin_control is not None:
        if not isinstance(origin_control, Mapping):
            raise _strict_error("Monthly root origin_control must be null or an object.")
        origin = _json_value(origin_control)
        if not isinstance(origin, dict):
            raise _strict_error("Monthly root origin_control must be a JSON object.")
        _require_exact_fields(origin, allowed=_ORIGIN_CONTROL_FIELDS, name="Monthly root origin_control")
        _validate_pivot_reference(origin["start_pivot_id"], pivot_ids=pivot_ids, name="origin_control.start_pivot_id")
        _validate_pivot_reference(origin["end_pivot_id"], pivot_ids=pivot_ids, name="origin_control.end_pivot_id")
    _validate_invalidation(result["invalidation"], pivot_ids=pivot_ids, name="Monthly root output")
    return result


def _validate_monthly_map_output(
    value: Any,
    *,
    pivot_ids: frozenset[str],
    expected_as_of_candle_id: str,
    maximum_segments: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _strict_error("Monthly map output must be an object.")
    result = _json_value(value)
    if not isinstance(result, dict):
        raise _strict_error("Monthly map output must be a JSON object.")
    _reject_forbidden_fields(result)
    _require_exact_fields(result, allowed=_MONTHLY_MAP_FIELDS, name="Monthly map output")
    if result["as_of_monthly_candle_id"] != expected_as_of_candle_id:
        raise _strict_error("Monthly map output must echo the immutable as-of Monthly candle ID.")
    outer_parent = result["outer_parent"]
    if not isinstance(outer_parent, Mapping):
        raise _strict_error("Monthly map outer_parent must be an object.")
    outer = _json_value(outer_parent)
    if not isinstance(outer, dict):
        raise _strict_error("Monthly map outer_parent must be a JSON object.")
    _require_exact_fields(outer, allowed=_MONTHLY_MAP_PARENT_FIELDS, name="Monthly map outer_parent")
    if outer["completion_state"] != "active":
        raise _strict_error("Monthly map outer_parent must remain active.")
    _validate_pivot_reference(outer["start_pivot_id"], pivot_ids=pivot_ids, name="outer_parent.start_pivot_id")
    _validate_pivot_reference(outer["end_pivot_id"], pivot_ids=pivot_ids, name="outer_parent.end_pivot_id")
    _validate_invalidation(outer["invalidation"], pivot_ids=pivot_ids, name="Monthly map outer_parent")
    origin_control = result["origin_control"]
    if origin_control is not None:
        if not isinstance(origin_control, Mapping):
            raise _strict_error("Monthly map origin_control must be null or an object.")
        origin = _json_value(origin_control)
        if not isinstance(origin, dict):
            raise _strict_error("Monthly map origin_control must be a JSON object.")
        _require_exact_fields(origin, allowed=_ORIGIN_CONTROL_FIELDS, name="Monthly map origin_control")
        _validate_pivot_reference(origin["start_pivot_id"], pivot_ids=pivot_ids, name="origin_control.start_pivot_id")
        _validate_pivot_reference(origin["end_pivot_id"], pivot_ids=pivot_ids, name="origin_control.end_pivot_id")
    segments = result["segments"]
    if isinstance(segments, (str, bytes)) or not isinstance(segments, list) or not segments:
        raise _strict_error("Monthly map segments must be a non-empty array.")
    if len(segments) > maximum_segments:
        raise _strict_error("Monthly map exceeds the immutable maximum segment bound.")
    segment_ids: set[str] = set()
    active_positions: list[int] = []
    for index, raw_segment in enumerate(segments):
        if not isinstance(raw_segment, Mapping):
            raise _strict_error(f"Monthly map segment {index} must be an object.")
        segment = _json_value(raw_segment)
        if not isinstance(segment, dict):
            raise _strict_error(f"Monthly map segment {index} must be a JSON object.")
        _require_exact_fields(segment, allowed=_MONTHLY_MAP_SEGMENT_FIELDS, name=f"Monthly map segment {index}")
        segment_id = segment["segment_id"]
        if not isinstance(segment_id, str) or not segment_id.strip() or segment_id in segment_ids:
            raise _strict_error("Monthly map segment IDs must be present and unique.")
        segment_ids.add(segment_id)
        if segment["completion_state"] == "active":
            active_positions.append(index)
        elif segment["completion_state"] != "completed":
            raise _strict_error("Monthly map segments may be completed or one final active segment only.")
        _validate_pivot_reference(segment["start_pivot_id"], pivot_ids=pivot_ids, name=f"segments[{index}].start_pivot_id")
        _validate_pivot_reference(segment["end_pivot_id"], pivot_ids=pivot_ids, name=f"segments[{index}].end_pivot_id")
        _validate_invalidation(segment["invalidation"], pivot_ids=pivot_ids, name=f"Monthly map segment {index}")
    if active_positions != [len(segments) - 1]:
        raise _strict_error("Monthly map must contain exactly one terminal active segment.")
    return result


def _validate_child_output(
    value: Any,
    *,
    pivot_ids: frozenset[str],
    expected_boundary_lineage: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _strict_error("Child graph output must be an object.")
    result = _json_value(value)
    if not isinstance(result, dict):
        raise _strict_error("Child graph output must be a JSON object.")
    _reject_forbidden_fields(result)
    _require_exact_fields(result, allowed=_CHILD_GRAPH_FIELDS, name="Child graph output")
    boundary_lineage = result["boundary_lineage"]
    if isinstance(boundary_lineage, (str, bytes)) or not isinstance(boundary_lineage, list):
        raise _strict_error("Child graph boundary_lineage must be an array.")
    normalized_lineage: list[dict[str, Any]] = []
    for index, raw_lineage in enumerate(boundary_lineage):
        if not isinstance(raw_lineage, Mapping):
            raise _strict_error(f"Child graph boundary_lineage entry {index} must be an object.")
        lineage = _json_value(raw_lineage)
        if not isinstance(lineage, dict):
            raise _strict_error(f"Child graph boundary_lineage entry {index} must be a JSON object.")
        _require_exact_fields(lineage, allowed=_BOUNDARY_LINEAGE_FIELDS, name=f"Child graph boundary_lineage entry {index}")
        _validate_pivot_reference(lineage["child_pivot_id"], pivot_ids=pivot_ids, name=f"boundary_lineage[{index}].child_pivot_id")
        normalized_lineage.append(lineage)
    if _json_value(normalized_lineage) != _json_value(list(expected_boundary_lineage)):
        raise _strict_error("Child graph boundary_lineage does not exactly echo the immutable packet mapping.")
    children = result["children"]
    if isinstance(children, (str, bytes)) or not isinstance(children, list):
        raise _strict_error("Child graph output children must be an array.")
    for index, raw_child in enumerate(children):
        if not isinstance(raw_child, Mapping):
            raise _strict_error(f"Child graph entry {index} must be an object.")
        child = _json_value(raw_child)
        if not isinstance(child, dict):
            raise _strict_error(f"Child graph entry {index} must be a JSON object.")
        _require_exact_fields(child, allowed=_CHILD_FIELDS, name=f"Child graph entry {index}")
        _validate_pivot_reference(child["start_pivot_id"], pivot_ids=pivot_ids, name=f"children[{index}].start_pivot_id")
        _validate_pivot_reference(child["end_pivot_id"], pivot_ids=pivot_ids, name=f"children[{index}].end_pivot_id")
        _validate_invalidation(child["invalidation"], pivot_ids=pivot_ids, name=f"Child graph entry {index}")
    return result


def _weekly_batch_context(packet: Mapping[str, Any]) -> dict[str, tuple[frozenset[str], Sequence[Mapping[str, Any]]]]:
    if packet.get("target_timeframe") != "weekly":
        raise _strict_error("Weekly graph batch packet must target Weekly data.")
    raw_requests = packet.get("segment_requests")
    if isinstance(raw_requests, (str, bytes)) or not isinstance(raw_requests, Sequence) or not raw_requests:
        raise _strict_error("Weekly graph batch packet requires at least one eligible Monthly segment request.")
    contexts: dict[str, tuple[frozenset[str], Sequence[Mapping[str, Any]]]] = {}
    for index, raw_request in enumerate(raw_requests):
        if not isinstance(raw_request, Mapping):
            raise _strict_error(f"Weekly graph batch segment request {index} must be an object.")
        segment_id = raw_request.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id.strip() or segment_id in contexts:
            raise _strict_error("Weekly graph batch segment IDs must be present and unique.")
        if raw_request.get("target_timeframe") != "weekly":
            raise _strict_error("Weekly graph batch includes a non-Weekly segment request.")
        pivot_ids = _catalog_pivot_ids(raw_request)
        lineage = raw_request.get("boundary_lineage")
        if isinstance(lineage, (str, bytes)) or not isinstance(lineage, Sequence) or len(lineage) != 2:
            raise _strict_error("Weekly graph batch segment request lacks exact two-boundary lineage.")
        if not all(isinstance(item, Mapping) for item in lineage):
            raise _strict_error("Weekly graph batch segment request has malformed boundary lineage.")
        contexts[segment_id] = (pivot_ids, lineage)
    return contexts


def _validate_weekly_batch_output(
    value: Any,
    *,
    contexts: Mapping[str, tuple[frozenset[str], Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _strict_error("Weekly graph batch output must be an object.")
    result = _json_value(value)
    if not isinstance(result, dict):
        raise _strict_error("Weekly graph batch output must be a JSON object.")
    _reject_forbidden_fields(result)
    _require_exact_fields(result, allowed=_WEEKLY_BATCH_FIELDS, name="Weekly graph batch output")
    graphs = result["graphs"]
    if isinstance(graphs, (str, bytes)) or not isinstance(graphs, list):
        raise _strict_error("Weekly graph batch graphs must be an array.")
    if len(graphs) != len(contexts):
        raise _strict_error("Weekly graph batch must return one graph for every eligible Monthly segment.")
    returned: list[str] = []
    for index, raw_entry in enumerate(graphs):
        if not isinstance(raw_entry, Mapping):
            raise _strict_error(f"Weekly graph batch entry {index} must be an object.")
        entry = _json_value(raw_entry)
        if not isinstance(entry, dict):
            raise _strict_error(f"Weekly graph batch entry {index} must be a JSON object.")
        _require_exact_fields(entry, allowed=_WEEKLY_BATCH_ENTRY_FIELDS, name=f"Weekly graph batch entry {index}")
        segment_id = entry["segment_id"]
        if not isinstance(segment_id, str) or segment_id not in contexts or segment_id in returned:
            raise _strict_error("Weekly graph batch output references a missing, invented, or duplicate Monthly segment.")
        pivot_ids, lineage = contexts[segment_id]
        _validate_child_output(entry["graph"], pivot_ids=pivot_ids, expected_boundary_lineage=lineage)
        returned.append(segment_id)
    if tuple(returned) != tuple(contexts):
        raise _strict_error("Weekly graph batch output must preserve deterministic Monthly segment request order.")
    return result


def _prompt_payload(
    *,
    operation: str,
    role: RecursiveProofHypothesisRole,
    packet: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    normalized_packet = _json_value(packet)
    normalized_schema = _json_value(output_schema)
    if not isinstance(normalized_packet, dict) or not isinstance(normalized_schema, dict):
        raise _strict_error("Candidate packet and output schema must be JSON objects.")
    system_prompt = (
        "You are a blind Elliott Wave candidate-graph generator. Use only the supplied immutable packet. "
        "Do not use, request, or infer prior symbol-specific counts, forecasts, outcomes, mistake memory, news, "
        "fundamentals, external data, confidence, verification status, or report prose. "
        "Generate one candidate-only structured response for the supplied role. Return exactly one JSON object matching "
        "the supplied schema and use only pivot IDs from the packet's pivot_catalog. "
        "Do not include reasoning, markdown, extra fields, or any verification claim."
    )
    user_payload = {
        "operation": operation,
        "role": role.value,
        "candidate_packet": normalized_packet,
        "output_schema": normalized_schema,
    }
    user_prompt = json.dumps(user_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return system_prompt, user_prompt, normalized_schema, normalized_packet


class CompleteHistoryOpenAICandidateProvider:
    """One-call strict adapter around :class:`OpenAIResponsesProvider`.

    A production instance wraps ``OpenAIResponsesProvider``. Tests may inject
    a fake strict-JSON transport, but the adapter has no retry or repair path:
    one method invocation performs at most one transport request.
    """

    __slots__ = (
        "_transport",
        "name",
        "model",
        "attempted_provider_calls",
        "accepted_provider_calls",
        "rejected_provider_calls",
    )

    def __init__(self, transport: CompleteHistoryStrictJsonTransport) -> None:
        name = getattr(transport, "name", None)
        model = getattr(transport, "model", None)
        if name != COMPLETE_HISTORY_OPENAI_PROVIDER_NAME or model != COMPLETE_HISTORY_OPENAI_MODEL:
            raise ValueError(
                "Complete-history OpenAI candidate adapter requires provider='openai' "
                "and model='gpt-5.6-terra'."
            )
        method = getattr(transport, "generate_strict_json", None)
        if not callable(method):
            raise TypeError("transport must expose generate_strict_json().")
        self._transport = transport
        self.name = COMPLETE_HISTORY_OPENAI_PROVIDER_NAME
        self.model = COMPLETE_HISTORY_OPENAI_MODEL
        self.attempted_provider_calls = 0
        self.accepted_provider_calls = 0
        self.rejected_provider_calls = 0

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, model={self.model!r})"

    def call_accounting(self) -> dict[str, int]:
        """Return deterministic call counters without exposing model output.

        An attempt is reserved immediately before the transport invocation.
        A response is accepted only after the strict local parser validates it;
        malformed or rejected responses remain attempted-and-rejected and are
        never retained by this adapter.
        """

        return {
            "attempted_provider_calls": self.attempted_provider_calls,
            "accepted_provider_calls": self.accepted_provider_calls,
            "rejected_provider_calls": self.rejected_provider_calls,
        }

    def _invoke_transport(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
        schema_name: str,
        schema_description: str,
    ) -> dict[str, Any]:
        """Make exactly one transport request with structured output for OpenAI.

        Fake transports retain the minimal protocol used by offline tests.
        Every real ``OpenAIResponsesProvider`` request supplies the exact
        runner schema through the Responses API strict structured-output
        boundary before the adapter's local candidate validation runs.
        """

        self.attempted_provider_calls += 1
        try:
            if isinstance(self._transport, OpenAIResponsesProvider):
                return self._transport.generate_strict_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema=schema,
                    packet=packet,
                    schema_name=schema_name,
                    schema_description=schema_description,
                    enforce_schema=True,
                )
            return self._transport.generate_strict_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=schema,
                packet=packet,
            )
        except Exception:
            self.rejected_provider_calls += 1
            raise

    def _accept_root_output(self, raw: Any, *, pivot_ids: frozenset[str]) -> dict[str, Any]:
        try:
            result = _validate_root_output(raw, pivot_ids=pivot_ids)
        except Exception:
            self.rejected_provider_calls += 1
            raise
        self.accepted_provider_calls += 1
        return result

    def _accept_monthly_map_output(
        self,
        raw: Any,
        *,
        pivot_ids: frozenset[str],
        expected_as_of_candle_id: str,
        maximum_segments: int,
    ) -> dict[str, Any]:
        try:
            result = _validate_monthly_map_output(
                raw,
                pivot_ids=pivot_ids,
                expected_as_of_candle_id=expected_as_of_candle_id,
                maximum_segments=maximum_segments,
            )
        except Exception:
            self.rejected_provider_calls += 1
            raise
        self.accepted_provider_calls += 1
        return result

    def _accept_child_output(
        self,
        raw: Any,
        *,
        pivot_ids: frozenset[str],
        expected_boundary_lineage: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        try:
            result = _validate_child_output(
                raw,
                pivot_ids=pivot_ids,
                expected_boundary_lineage=expected_boundary_lineage,
            )
        except Exception:
            self.rejected_provider_calls += 1
            raise
        self.accepted_provider_calls += 1
        return result

    def _accept_weekly_batch_output(
        self,
        raw: Any,
        *,
        contexts: Mapping[str, tuple[frozenset[str], Sequence[Mapping[str, Any]]]],
    ) -> dict[str, Any]:
        try:
            result = _validate_weekly_batch_output(raw, contexts=contexts)
        except Exception:
            self.rejected_provider_calls += 1
            raise
        self.accepted_provider_calls += 1
        return result

    @classmethod
    def from_environment(
        cls,
        *,
        model: str = COMPLETE_HISTORY_OPENAI_MODEL,
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 600,
    ) -> "CompleteHistoryOpenAICandidateProvider":
        if model != COMPLETE_HISTORY_OPENAI_MODEL:
            raise ValueError("Complete-history candidate generation only permits model='gpt-5.6-terra'.")
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key.strip():
            raise ProviderError("OPENAI_API_KEY is required for the complete-history OpenAI candidate adapter.")
        return cls(OpenAIResponsesProvider(api_key=api_key, model=model, base_url=base_url, timeout=timeout))

    def generate_monthly_map(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_packet_constraints(packet, role=role)
        pivot_ids = _catalog_pivot_ids(packet)
        as_of = packet.get("as_of_monthly_candle")
        scope = packet.get("approved_monthly_map_scope")
        if not isinstance(as_of, Mapping) or not isinstance(as_of.get("candle_id"), str):
            raise _strict_error("Monthly-map packet is missing its immutable as-of Monthly candle.")
        if not isinstance(scope, Mapping) or not isinstance(scope.get("maximum_segments"), int):
            raise _strict_error("Monthly-map packet is missing its bounded map scope.")
        if _json_value(output_schema) != _json_value(MONTHLY_MAP_OUTPUT_SCHEMA):
            raise _strict_error("Monthly-map call did not receive the runner's exact strict schema.")
        system_prompt, user_prompt, schema, normalized_packet = _prompt_payload(
            operation="monthly_map",
            role=role,
            packet=packet,
            output_schema=output_schema,
        )
        raw = self._invoke_transport(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            packet=normalized_packet,
            schema_name=_MONTHLY_MAP_SCHEMA_NAME,
            schema_description="Blind full-history Monthly structural map; candidate-only, pivot-catalog bound, and terminal-active-segment required.",
        )
        return self._accept_monthly_map_output(
            raw,
            pivot_ids=pivot_ids,
            expected_as_of_candle_id=as_of["candle_id"],
            maximum_segments=scope["maximum_segments"],
        )

    def generate_weekly_child_graph_batch(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_packet_constraints(packet, role=role)
        contexts = _weekly_batch_context(packet)
        if _json_value(output_schema) != _json_value(WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA):
            raise _strict_error("Weekly graph batch call did not receive the runner's exact strict schema.")
        system_prompt, user_prompt, schema, normalized_packet = _prompt_payload(
            operation="weekly_child_graph_batch",
            role=role,
            packet=packet,
            output_schema=output_schema,
        )
        raw = self._invoke_transport(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            packet=normalized_packet,
            schema_name=_WEEKLY_BATCH_SCHEMA_NAME,
            schema_description="Blind batched Weekly child graphs for completed Monthly segments; candidate-only and exact-lineage bound.",
        )
        return self._accept_weekly_batch_output(raw, contexts=contexts)

    def generate_monthly_root_candidate(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_packet_constraints(packet, role=role)
        pivot_ids = _catalog_pivot_ids(packet)
        if _json_value(output_schema) != _json_value(MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA):
            raise _strict_error("Monthly root call did not receive the runner's exact strict schema.")
        system_prompt, user_prompt, schema, normalized_packet = _prompt_payload(
            operation="monthly_root_candidate",
            role=role,
            packet=packet,
            output_schema=output_schema,
        )
        raw = self._invoke_transport(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            packet=normalized_packet,
            schema_name=_MONTHLY_ROOT_SCHEMA_NAME,
            schema_description="Blind complete-history monthly root candidate; candidate-only and pivot-catalog bound.",
        )
        return self._accept_root_output(raw, pivot_ids=pivot_ids)

    def generate_child_graph(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_packet_constraints(packet, role=role)
        pivot_ids = _catalog_pivot_ids(packet)
        expected_boundary_lineage = packet.get("boundary_lineage")
        if isinstance(expected_boundary_lineage, (str, bytes)) or not isinstance(expected_boundary_lineage, Sequence) or len(expected_boundary_lineage) != 2:
            raise _strict_error("Child-graph packet is missing its exact two-boundary lineage mapping.")
        if not all(isinstance(item, Mapping) for item in expected_boundary_lineage):
            raise _strict_error("Child-graph packet has malformed boundary lineage entries.")
        if _json_value(output_schema) != _json_value(CHILD_GRAPH_OUTPUT_SCHEMA):
            raise _strict_error("Child-graph call did not receive the runner's exact strict schema.")
        system_prompt, user_prompt, schema, normalized_packet = _prompt_payload(
            operation="child_graph",
            role=role,
            packet=packet,
            output_schema=output_schema,
        )
        raw = self._invoke_transport(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            packet=normalized_packet,
            schema_name=_CHILD_GRAPH_SCHEMA_NAME,
            schema_description="Blind complete-history child graph; candidate-only and pivot-catalog bound.",
        )
        return self._accept_child_output(
            raw,
            pivot_ids=pivot_ids,
            expected_boundary_lineage=tuple(expected_boundary_lineage),
        )


__all__ = [
    "COMPLETE_HISTORY_OPENAI_MODEL",
    "COMPLETE_HISTORY_OPENAI_PROVIDER_NAME",
    "CompleteHistoryOpenAICandidateProvider",
    "CompleteHistoryStrictJsonTransport",
]
