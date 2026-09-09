"""Strict candidate-only provider adapter for adaptive Elliott recounts.

This module performs no retrieval, market-data access, persistence, retry, or
verification.  It converts an already frozen blind packet into one strict JSON
request.  The deterministic coordinator and subdivision verifier remain the
only owners of structural status.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol

from .adaptive_recount import (
    ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA,
    ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA,
    adaptive_root_group_classification_schema_for_packet,
    adaptive_root_classification_schema_for_packet,
    adaptive_root_output_schema_for_packet,
)
from .lower_timeframe_candidate_generator import (
    CandidateGraphRole,
    graph_generation_output_schema_for_packet,
)
from .providers import OpenAIResponsesProvider


ADAPTIVE_RECOUNT_PROVIDER_POLICY_VERSION = "adaptive-recount-provider-policy-3.3.0"
_ROOT_SCHEMA_NAME = "adaptive_elliott_root_candidate_v1"
_ROOT_SEGMENTATION_SCHEMA_NAME = "adaptive_elliott_root_segmentation_v4"
_ROOT_CLASSIFICATION_SCHEMA_NAME = "adaptive_elliott_root_classification_v1"
_ROOT_GROUPING_SCHEMA_NAME = "adaptive_elliott_root_grouping_v1"
_ROOT_GROUP_CLASSIFICATION_SCHEMA_NAME = "adaptive_elliott_root_group_classification_v1"
_CHILD_SCHEMA_NAME = "adaptive_elliott_child_graph_v2"


class AdaptiveStrictJsonTransport(Protocol):
    name: str
    model: str | None

    def generate_strict_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
        **options: Any,
    ) -> dict[str, Any]:
        ...


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return value


def _prompt(
    *,
    operation: str,
    role: CandidateGraphRole,
    packet: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    normalized_packet = _json_value(packet)
    normalized_schema = _json_value(schema)
    if operation == "root_segmentation":
        system_prompt = (
            "Segment the supplied immutable price history before applying Elliott labels. "
            "Return ordered steps covering the complete analysis scope. The scope start is "
            "implicit; each step selects exactly one supplied end_boundary_id and a segment_kind, "
            "and the final step must end at the exact as-of boundary. Select at most one member "
            "from each chronology group unless a finer native dataset has deterministically "
            "resolved the order. chronology_group_ordinal, not boundary_ordinal, controls path "
            "chronology. Do not reorder, repeat, skip, or invent boundaries. Do not return "
            "segment IDs, completion states, degrees, wave labels, families, invalidations, "
            "indicators, confidence, report prose, or verification status. The scope start and "
            "as-of observation are container boundaries, not Elliott pivots. Primary and "
            "Alternative calls are blind and isolated. A closed atomic_swing must connect a "
            "typed high to a typed low or a typed low to a typed high. Same-extremum intervals "
            "must be continuation_interval or unresolved_interval. When a typed repair context "
            "is present, correct only its deterministic contract facts and do not infer peer "
            "output, prior prose, external memory, outcomes, or hidden reasoning."
        )
    elif operation == "root_grouping":
        system_prompt = (
            "Group the frozen price-only Stage-A segments into zero or more ordered, contiguous "
            "structural proposals. A group may span one or several segments. Different proposed "
            "groups may overlap as alternatives, but each group itself must be gap-free. Account "
            "for every segment outside all groups explicitly as unresolved. Do not "
            "assign Elliott degrees, families, wave labels, verification status, indicators, confidence, "
            "or report prose. Do not force a group when price structure is inadequate. Primary "
            "and Alternative calls are blind and isolated."
        )
    elif operation == "root_group_classification":
        system_prompt = (
            "Classify the frozen contiguous structural groups as candidate Elliott families. "
            "Each completed wave node must span the exact ordered source segments and exact "
            "catalog-backed group boundaries. Indicators are soft ranking evidence only and "
            "cannot create pivots or verify waves. Choose a provisional degree only from the "
            "packet's allowed_root_degrees; the analysis scope itself has no Elliott degree. "
            "Return candidate or unresolved structure, "
            "never verification status, confidence, probability, report prose, hidden reasoning, "
            "or trade advice. Primary and Alternative calls remain blind and isolated."
        )
    elif operation == "root_classification":
        system_prompt = (
            "Classify the already frozen price segments using only the supplied immutable "
            "catalog pivots, symbol-independent Elliott rules, and candidate-scoped soft "
            "technical evidence. Unresolved or context segments may contain no wave node. "
            "Indicators may rank candidates but cannot create pivots, determine verification, "
            "or override price rules. Choose a provisional degree only from the packet's "
            "allowed_root_degrees. Never return verified status, confidence, probability, "
            "report prose, hidden reasoning, or trade advice. Primary and Alternative calls "
            "remain blind and isolated."
        )
    else:
        system_prompt = (
            "Generate one blind Elliott Wave candidate graph using only the supplied immutable "
            "price packet and its symbol-independent rules pack. Use only supplied pivot IDs. "
            "Select at most one pivot from each source-candle chronology group unless the packet "
            "marks its order as resolved by finer native data. Never infer high/low order from "
            "OHLC candle direction, distance, volume, or indicators. "
            "Price boundaries and Elliott hard rules come first. Do not retrieve or infer prior "
            "symbol counts, accepted cases, forecasts, outcomes, or lessons. Indicators are not "
            "supplied and must not be claimed. Return candidate structure only. Never return "
            "verified status, confidence, probabilities, report prose, hidden reasoning, or trade "
            "advice. The deterministic verifier owns every verification status. Primary and "
            "Alternative calls are independent; do not infer or refer to the peer output."
        )
    user_prompt = json.dumps(
        {
            "operation": operation,
            "candidate_role": role.value,
            "policy_version": ADAPTIVE_RECOUNT_PROVIDER_POLICY_VERSION,
            "candidate_packet": normalized_packet,
            "output_schema": normalized_schema,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return system_prompt, user_prompt, normalized_schema, normalized_packet


class AdaptiveRecountStructuredProvider:
    """One-request adapter shared by root and child candidate generation."""

    __slots__ = (
        "_transport",
        "name",
        "model",
        "attempted_provider_calls",
        "returned_provider_calls",
        "failed_provider_calls",
    )

    def __init__(self, transport: AdaptiveStrictJsonTransport) -> None:
        method = getattr(transport, "generate_strict_json", None)
        name = getattr(transport, "name", None)
        model = getattr(transport, "model", None)
        if not callable(method):
            raise TypeError("Adaptive recount transport must expose generate_strict_json().")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Adaptive recount transport name must be non-empty.")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("Adaptive recount transport model must be non-empty or null.")
        self._transport = transport
        self.name = name.strip()
        self.model = model.strip() if isinstance(model, str) else None
        self.attempted_provider_calls = 0
        self.returned_provider_calls = 0
        self.failed_provider_calls = 0

    def call_accounting(self) -> dict[str, int]:
        return {
            "attempted_provider_calls": self.attempted_provider_calls,
            "returned_provider_calls": self.returned_provider_calls,
            "failed_provider_calls": self.failed_provider_calls,
        }

    def _invoke(
        self,
        *,
        operation: str,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        expected_schema: Mapping[str, Any],
        schema_name: str,
    ) -> Mapping[str, Any]:
        role = CandidateGraphRole(role)
        normalized_output = _json_value(output_schema)
        if normalized_output != _json_value(expected_schema):
            raise ValueError("Adaptive provider did not receive the exact versioned output schema.")
        blind = packet.get("blind_isolation") if isinstance(packet, Mapping) else None
        if operation in {
            "root_candidate",
            "root_segmentation",
            "root_grouping",
            "root_classification",
            "root_group_classification",
        }:
            if not isinstance(blind, Mapping) or blind.get("strict") is not True:
                raise ValueError("Root candidate packet must assert strict blind isolation.")
            forbidden_flags = (
                "peer_candidate_available",
                "prior_symbol_counts_available",
                "accepted_cases_available",
                "forecasts_outcomes_lessons_available",
            )
            if any(blind.get(field) is not False for field in forbidden_flags):
                raise ValueError("Root candidate packet exposes forbidden prior or peer context.")
        system_prompt, user_prompt, schema, normalized_packet = _prompt(
            operation=operation,
            role=role,
            packet=packet,
            schema=output_schema,
        )
        self.attempted_provider_calls += 1
        try:
            if isinstance(self._transport, OpenAIResponsesProvider):
                result = self._transport.generate_strict_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema=schema,
                    packet=normalized_packet,
                    schema_name=schema_name,
                    schema_description=(
                        "Blind candidate-only Elliott graph bound to immutable catalog pivots; "
                        "verification is excluded."
                    ),
                    enforce_schema=True,
                )
            else:
                result = self._transport.generate_strict_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema=schema,
                    packet=normalized_packet,
                )
        except Exception:
            self.failed_provider_calls += 1
            raise
        if not isinstance(result, Mapping):
            self.failed_provider_calls += 1
            raise ValueError("Adaptive provider output must be one structured JSON object.")
        self.returned_provider_calls += 1
        return dict(result)

    def generate_root_candidate(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected_schema = adaptive_root_output_schema_for_packet(packet)
        return self._invoke(
            operation="root_candidate",
            role=role,
            packet=packet,
            output_schema=output_schema,
            expected_schema=expected_schema,
            schema_name=_ROOT_SCHEMA_NAME,
        )

    def generate_root_segmentation(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke(
            operation="root_segmentation",
            role=role,
            packet=packet,
            output_schema=output_schema,
            expected_schema=ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA,
            schema_name=_ROOT_SEGMENTATION_SCHEMA_NAME,
        )

    def classify_root_segments(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected_schema = adaptive_root_classification_schema_for_packet(packet)
        return self._invoke(
            operation="root_classification",
            role=role,
            packet=packet,
            output_schema=output_schema,
            expected_schema=expected_schema,
            schema_name=_ROOT_CLASSIFICATION_SCHEMA_NAME,
        )

    def generate_root_grouping(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._invoke(
            operation="root_grouping",
            role=role,
            packet=packet,
            output_schema=output_schema,
            expected_schema=ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA,
            schema_name=_ROOT_GROUPING_SCHEMA_NAME,
        )

    def classify_root_groups(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected_schema = adaptive_root_group_classification_schema_for_packet(packet)
        return self._invoke(
            operation="root_group_classification",
            role=role,
            packet=packet,
            output_schema=output_schema,
            expected_schema=expected_schema,
            schema_name=_ROOT_GROUP_CLASSIFICATION_SCHEMA_NAME,
        )

    def generate_child_graph(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected_schema = graph_generation_output_schema_for_packet(packet)
        return self._invoke(
            operation="child_graph",
            role=role,
            packet=packet,
            output_schema=output_schema,
            expected_schema=expected_schema,
            schema_name=_CHILD_SCHEMA_NAME,
        )


__all__ = [
    "ADAPTIVE_RECOUNT_PROVIDER_POLICY_VERSION",
    "AdaptiveRecountStructuredProvider",
    "AdaptiveStrictJsonTransport",
]
