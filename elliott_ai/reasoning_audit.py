"""Deterministic reasoning-audit graph for Phase 8 orchestration.

The graph is derived only from validated Market Scenario Engine contracts. It
does not perform research, infer causal relationships, rank scenarios, modify
technical analysis, or call a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .current_scenarios import (
    CurrentScenarioSet,
    ScenarioStepType,
    validate_current_scenario_set,
)
from .current_state import CurrentMarketState
from .market_scenario import (
    EvidenceItem,
    ExposureItem,
    FrozenTechnicalPremise,
    MaterialEvent,
    ValidationIssue,
    ValidationResult,
)
from .pattern_retrieval import PatternRetrievalResult


REASONING_AUDIT_NODE_SCHEMA_VERSION = "reasoning-audit-node-1.0.0"
REASONING_AUDIT_EDGE_SCHEMA_VERSION = "reasoning-audit-edge-1.0.0"
REASONING_AUDIT_GRAPH_SCHEMA_VERSION = "reasoning-audit-graph-1.0.0"
REASONING_AUDIT_COVERAGE_SCHEMA_VERSION = (
    "reasoning-audit-coverage-1.0.0"
)
REASONING_AUDIT_VALIDATION_VERSION = (
    "reasoning-audit-validation-1.0.0"
)


class AuditNodeType(StrEnum):
    FROZEN_TECHNICAL_PREMISE = "frozen_technical_premise"
    CURRENT_EVIDENCE = "current_evidence"
    CURRENT_EXPOSURE = "current_exposure"
    CURRENT_EVENT = "current_event"
    CURRENT_MARKET_STATE = "current_market_state"
    RETRIEVED_PRIMARY_PATTERN = "retrieved_primary_pattern"
    RETRIEVED_COUNTER_PATTERN = "retrieved_counter_pattern"
    SCENARIO = "scenario"
    SCENARIO_STEP = "scenario_step"
    CONTRADICTION = "contradiction"
    INVALIDATION_CONDITION = "invalidation_condition"
    ASSUMPTION = "assumption"
    HYPOTHETICAL_EVENT = "hypothetical_event"


class AuditEdgeType(StrEnum):
    DERIVED_FROM = "derived_from"
    SUPPORTED_BY = "supported_by"
    CONTRADICTED_BY = "contradicted_by"
    MATCHED_TO = "matched_to"
    CHALLENGED_BY = "challenged_by"
    USES_PATTERN = "uses_pattern"
    USES_COUNTER_PATTERN = "uses_counter_pattern"
    REQUIRES = "requires"
    AMPLIFIES = "amplifies"
    INVALIDATES = "invalidates"
    FOLLOWS = "follows"


def _normalize_timestamp(
    value: str | datetime,
    *,
    field_name: str,
) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be an ISO-8601 timestamp."
            ) from exc
    else:
        raise TypeError(
            f"{field_name} must be a datetime or timestamp string."
        )
    aware = (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalized_token(value: str) -> str:
    return re.sub(
        r"_+",
        "_",
        re.sub(r"[^a-z0-9]+", "_", value.casefold()),
    ).strip("_")


def _enum_value(
    value: Any,
    enum_type: type[StrEnum],
    *,
    field_name: str,
) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be a valid {enum_type.__name__}."
        ) from exc


def _string_tuple(
    value: Sequence[str] | None,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(value)
    if not all(isinstance(item, str) for item in result):
        raise TypeError(f"{field_name} must contain only strings.")
    return result


def _model_tuple(
    value: Sequence[Any] | None,
    model_type: type,
    *,
    field_name: str,
) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    result = tuple(value)
    if not all(isinstance(item, model_type) for item in result):
        raise TypeError(
            f"{field_name} must contain only {model_type.__name__} values."
        )
    return result


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json(item)
                for key, item in sorted(
                    value.items(),
                    key=lambda pair: str(pair[0]),
                )
            }
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"JSON contract cannot contain {type(value).__name__} values."
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _contract_hash(value: Any) -> str:
    payload = dict(value.to_dict())
    payload.pop("content_hash", None)
    return _hash_payload(payload)


def _mapping(
    value: Mapping[str, Any],
    *,
    model_name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{model_name}.from_dict requires a mapping.")
    return dict(value)


def _reject_unknown(
    value: Mapping[str, Any],
    allowed: set[str],
    *,
    model_name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{model_name} contains unknown fields: "
            + ", ".join(unknown)
            + "."
        )


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class ReasoningAuditNode(_JsonContract):
    node_id: str
    node_type: AuditNodeType
    source_ref: str
    label: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    schema_version: str = REASONING_AUDIT_NODE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "node_type",
            _enum_value(
                self.node_type,
                AuditNodeType,
                field_name="node_type",
            ),
        )
        object.__setattr__(
            self,
            "attributes",
            _freeze_json(self.attributes),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ReasoningAuditNode":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ReasoningAuditEdge(_JsonContract):
    edge_id: str
    edge_type: AuditEdgeType
    source_node_id: str
    target_node_id: str
    context: str = ""
    content_hash: str = ""
    schema_version: str = REASONING_AUDIT_EDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "edge_type",
            _enum_value(
                self.edge_type,
                AuditEdgeType,
                field_name="edge_type",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ReasoningAuditEdge":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class AuditCoverageMetrics(_JsonContract):
    total_scenario_count: int
    scenarios_with_evidence_support: int
    scenarios_with_pattern_support: int
    scenarios_with_counter_pattern_challenge: int
    scenarios_with_contradicting_evidence: int
    scenarios_with_invalidation_conditions: int
    evidence_reference_coverage: float
    exposure_reference_coverage: float
    pattern_reference_coverage: float
    orphan_node_count: int
    dangling_reference_count: int
    content_hash: str = ""
    schema_version: str = REASONING_AUDIT_COVERAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "total_scenario_count",
            "scenarios_with_evidence_support",
            "scenarios_with_pattern_support",
            "scenarios_with_counter_pattern_challenge",
            "scenarios_with_contradicting_evidence",
            "scenarios_with_invalidation_conditions",
            "orphan_node_count",
            "dangling_reference_count",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(
                    f"{name} must be a non-negative integer."
                )
        for name in (
            "evidence_reference_coverage",
            "exposure_reference_coverage",
            "pattern_reference_coverage",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(
                    f"{name} must be between 0 and 1."
                )
            object.__setattr__(self, name, round(float(value), 6))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "AuditCoverageMetrics":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ReasoningAuditGraph(_JsonContract):
    graph_id: str
    orchestration_id: str
    nodes: tuple[ReasoningAuditNode, ...]
    edges: tuple[ReasoningAuditEdge, ...]
    root_node_ids: tuple[str, ...]
    orphan_node_ids: tuple[str, ...]
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    generated_at: str = ""
    content_hash: str = ""
    schema_version: str = REASONING_AUDIT_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nodes",
            _model_tuple(
                self.nodes,
                ReasoningAuditNode,
                field_name="nodes",
            ),
        )
        object.__setattr__(
            self,
            "edges",
            _model_tuple(
                self.edges,
                ReasoningAuditEdge,
                field_name="edges",
            ),
        )
        for name in ("root_node_ids", "orphan_node_ids"):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError(
                "validation_result must be ValidationResult."
            )
        object.__setattr__(
            self,
            "generated_at",
            _normalize_timestamp(
                self.generated_at,
                field_name="generated_at",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ReasoningAuditGraph":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["nodes"] = tuple(
            ReasoningAuditNode.from_dict(item)
            for item in raw.get("nodes", ())
        )
        raw["edges"] = tuple(
            ReasoningAuditEdge.from_dict(item)
            for item in raw.get("edges", ())
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


def reasoning_audit_node_content_hash(
    node: ReasoningAuditNode,
) -> str:
    if not isinstance(node, ReasoningAuditNode):
        raise TypeError("node must be ReasoningAuditNode.")
    return _contract_hash(node)


def reasoning_audit_edge_content_hash(
    edge: ReasoningAuditEdge,
) -> str:
    if not isinstance(edge, ReasoningAuditEdge):
        raise TypeError("edge must be ReasoningAuditEdge.")
    return _contract_hash(edge)


def audit_coverage_metrics_content_hash(
    metrics: AuditCoverageMetrics,
) -> str:
    if not isinstance(metrics, AuditCoverageMetrics):
        raise TypeError("metrics must be AuditCoverageMetrics.")
    return _contract_hash(metrics)


def reasoning_audit_graph_content_hash(
    graph: ReasoningAuditGraph,
) -> str:
    if not isinstance(graph, ReasoningAuditGraph):
        raise TypeError("graph must be ReasoningAuditGraph.")
    return _contract_hash(graph)


def _node_id(node_type: AuditNodeType, source_ref: str) -> str:
    token = _normalized_token(source_ref)[:48] or "unnamed"
    suffix = _hash_payload(
        {"node_type": node_type.value, "source_ref": source_ref}
    )[:12]
    return f"audit:{node_type.value}:{token}:{suffix}"


def _edge_id(
    edge_type: AuditEdgeType,
    source_node_id: str,
    target_node_id: str,
    context: str,
) -> str:
    return "audit_edge:" + _hash_payload(
        {
            "edge_type": edge_type.value,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "context": context,
        }
    )[:24]


def _make_node(
    node_type: AuditNodeType,
    source_ref: str,
    label: str,
    attributes: Mapping[str, Any] | None = None,
) -> ReasoningAuditNode:
    seed = ReasoningAuditNode(
        node_id=_node_id(node_type, source_ref),
        node_type=node_type,
        source_ref=source_ref,
        label=label,
        attributes=attributes or {},
    )
    return replace(
        seed,
        content_hash=reasoning_audit_node_content_hash(seed),
    )


def _make_edge(
    edge_type: AuditEdgeType,
    source_node_id: str,
    target_node_id: str,
    context: str = "",
) -> ReasoningAuditEdge:
    seed = ReasoningAuditEdge(
        edge_id=_edge_id(
            edge_type,
            source_node_id,
            target_node_id,
            context,
        ),
        edge_type=edge_type,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        context=context,
    )
    return replace(
        seed,
        content_hash=reasoning_audit_edge_content_hash(seed),
    )


def _duplicates(values: Sequence[str]) -> set[str]:
    return {
        value
        for value, count in Counter(values).items()
        if count > 1
    }


def _orphan_node_ids(
    nodes: Sequence[ReasoningAuditNode],
    edges: Sequence[ReasoningAuditEdge],
    root_node_ids: Sequence[str],
) -> tuple[str, ...]:
    connected = {
        node_id
        for edge in edges
        for node_id in (
            edge.source_node_id,
            edge.target_node_id,
        )
    }
    roots = set(root_node_ids)
    return tuple(
        sorted(
            node.node_id
            for node in nodes
            if node.node_id not in connected
            and node.node_id not in roots
        )
    )


def _dangling_edge_ids(
    nodes: Sequence[ReasoningAuditNode],
    edges: Sequence[ReasoningAuditEdge],
) -> tuple[str, ...]:
    node_ids = {node.node_id for node in nodes}
    return tuple(
        sorted(
            edge.edge_id
            for edge in edges
            if edge.source_node_id not in node_ids
            or edge.target_node_id not in node_ids
        )
    )


class _GraphCollector:
    def __init__(self) -> None:
        self.nodes: dict[str, ReasoningAuditNode] = {}
        self.edges: dict[str, ReasoningAuditEdge] = {}

    def add_node(self, node: ReasoningAuditNode) -> str:
        existing = self.nodes.get(node.node_id)
        if existing is not None and existing != node:
            raise ValueError(
                f"Conflicting audit node identity: {node.node_id}."
            )
        self.nodes[node.node_id] = node
        return node.node_id

    def add_edge(self, edge: ReasoningAuditEdge) -> str:
        existing = self.edges.get(edge.edge_id)
        if existing is not None and existing != edge:
            raise ValueError(
                f"Conflicting audit edge identity: {edge.edge_id}."
            )
        self.edges[edge.edge_id] = edge
        return edge.edge_id


def _validation_result(
    issues: Sequence[ValidationIssue],
) -> ValidationResult:
    errors = tuple(
        item for item in issues if item.severity == "error"
    )
    warnings = tuple(
        item for item in issues if item.severity != "error"
    )
    rule_order = (
        "audit_schema",
        "audit_hash",
        "audit_node_integrity",
        "audit_edge_integrity",
        "audit_ordering",
        "audit_roots",
        "audit_orphans",
        "audit_no_dangling_edges",
        "audit_scenario_traceability",
        "audit_counter_pattern_coverage",
        "audit_invalidation_coverage",
    )
    failed = tuple(
        code
        for code in rule_order
        if any(
            item.code == code and item.severity == "error"
            for item in issues
        )
    )
    passed = tuple(code for code in rule_order if code not in failed)
    return ValidationResult(
        is_valid=not errors,
        score=round(100.0 * len(passed) / len(rule_order), 2),
        errors=errors,
        warnings=warnings,
        failed_rules=failed,
        passed_rules=passed,
        validation_version=REASONING_AUDIT_VALIDATION_VERSION,
    )


def validate_reasoning_audit_graph(
    graph: ReasoningAuditGraph,
    *,
    scenario_set: CurrentScenarioSet | None = None,
) -> ValidationResult:
    if not isinstance(graph, ReasoningAuditGraph):
        raise TypeError("graph must be ReasoningAuditGraph.")
    if scenario_set is not None and not isinstance(
        scenario_set,
        CurrentScenarioSet,
    ):
        raise TypeError(
            "scenario_set must be CurrentScenarioSet or null."
        )

    issues: list[ValidationIssue] = []

    def require(
        code: str,
        condition: bool,
        path: str,
        message: str,
        *,
        severity: str = "error",
    ) -> None:
        if not condition:
            issues.append(
                ValidationIssue(
                    code=code,
                    severity=severity,
                    path=path,
                    message=message,
                )
            )

    node_ids = tuple(node.node_id for node in graph.nodes)
    edge_ids = tuple(edge.edge_id for edge in graph.edges)
    node_id_set = set(node_ids)
    recomputed_orphans = _orphan_node_ids(
        graph.nodes,
        graph.edges,
        graph.root_node_ids,
    )
    dangling = _dangling_edge_ids(graph.nodes, graph.edges)

    require(
        "audit_schema",
        graph.schema_version == REASONING_AUDIT_GRAPH_SCHEMA_VERSION,
        "schema_version",
        "Reasoning-audit graph schema version is unsupported.",
    )
    require(
        "audit_hash",
        bool(graph.content_hash)
        and graph.content_hash
        == reasoning_audit_graph_content_hash(graph),
        "content_hash",
        "Reasoning-audit graph content hash is missing or mismatched.",
    )
    require(
        "audit_node_integrity",
        (
            not _duplicates(node_ids)
            and all(
                node.schema_version
                == REASONING_AUDIT_NODE_SCHEMA_VERSION
                and bool(node.node_id)
                and bool(node.source_ref)
                and node.content_hash
                == reasoning_audit_node_content_hash(node)
                for node in graph.nodes
            )
        ),
        "nodes",
        "Audit nodes must be unique, versioned, and content-hashed.",
    )
    require(
        "audit_edge_integrity",
        (
            not _duplicates(edge_ids)
            and all(
                edge.schema_version
                == REASONING_AUDIT_EDGE_SCHEMA_VERSION
                and edge.edge_id
                == _edge_id(
                    edge.edge_type,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.context,
                )
                and edge.content_hash
                == reasoning_audit_edge_content_hash(edge)
                for edge in graph.edges
            )
        ),
        "edges",
        "Audit edges must be unique, deterministic, and content-hashed.",
    )
    require(
        "audit_ordering",
        (
            node_ids == tuple(sorted(node_ids))
            and edge_ids == tuple(sorted(edge_ids))
            and graph.root_node_ids
            == tuple(sorted(set(graph.root_node_ids)))
            and graph.orphan_node_ids
            == tuple(sorted(set(graph.orphan_node_ids)))
        ),
        "nodes",
        "Audit nodes, edges, roots, and orphans must be deterministically ordered.",
    )
    require(
        "audit_roots",
        (
            bool(graph.root_node_ids)
            and set(graph.root_node_ids) <= node_id_set
            and all(
                next(
                    node
                    for node in graph.nodes
                    if node.node_id == root_id
                ).node_type
                is AuditNodeType.FROZEN_TECHNICAL_PREMISE
                for root_id in graph.root_node_ids
            )
        ),
        "root_node_ids",
        "Every audit root must reference a frozen technical premise node.",
    )
    require(
        "audit_orphans",
        graph.orphan_node_ids == recomputed_orphans,
        "orphan_node_ids",
        "Recorded audit orphans do not match the graph topology.",
    )
    if recomputed_orphans:
        issues.append(
            ValidationIssue(
                code="audit_orphans",
                severity="warning",
                path="orphan_node_ids",
                message=(
                    "Audit graph contains declared orphan nodes: "
                    + ", ".join(recomputed_orphans)
                    + "."
                ),
            )
        )
    require(
        "audit_no_dangling_edges",
        not dangling,
        "edges",
        "Audit graph contains dangling node references.",
    )

    if scenario_set is not None:
        scenario_nodes = {
            node.source_ref: node.node_id
            for node in graph.nodes
            if node.node_type is AuditNodeType.SCENARIO
        }
        edges_by_source: dict[str, tuple[ReasoningAuditEdge, ...]] = {
            node_id: tuple(
                edge
                for edge in graph.edges
                if edge.source_node_id == node_id
            )
            for node_id in node_id_set
        }
        traceable_types = {
            AuditEdgeType.SUPPORTED_BY,
            AuditEdgeType.USES_PATTERN,
            AuditEdgeType.USES_COUNTER_PATTERN,
            AuditEdgeType.REQUIRES,
        }
        traceable = all(
            scenario.scenario_id in scenario_nodes
            and any(
                edge.edge_type in traceable_types
                for edge in edges_by_source.get(
                    scenario_nodes[scenario.scenario_id],
                    (),
                )
            )
            for scenario in scenario_set.scenarios
        )
        require(
            "audit_scenario_traceability",
            traceable,
            "nodes",
            "Every scenario must be traceable to evidence, an exposure, a pattern, or an explicit assumption.",
        )

        expected_counter = {
            (scenario.scenario_id, pattern_ref)
            for scenario in scenario_set.scenarios
            for pattern_ref in scenario.counter_pattern_refs
        }
        actual_counter = {
            (
                next(
                    node.source_ref
                    for node in graph.nodes
                    if node.node_id == edge.source_node_id
                ),
                next(
                    node.source_ref
                    for node in graph.nodes
                    if node.node_id == edge.target_node_id
                ),
            )
            for edge in graph.edges
            if edge.edge_type
            is AuditEdgeType.USES_COUNTER_PATTERN
            and edge.source_node_id in node_id_set
            and edge.target_node_id in node_id_set
        }
        require(
            "audit_counter_pattern_coverage",
            actual_counter == expected_counter,
            "edges",
            "Every scenario counter-pattern reference must have one audit edge.",
        )

        expected_invalidations = {
            (scenario.scenario_id, condition)
            for scenario in scenario_set.scenarios
            for condition in scenario.invalidation_conditions
        }
        actual_invalidations = {
            (
                next(
                    node.source_ref.split("::", 1)[0]
                    for node in graph.nodes
                    if node.node_id == edge.source_node_id
                ),
                next(
                    node.attributes.get("condition", "")
                    for node in graph.nodes
                    if node.node_id == edge.source_node_id
                ),
            )
            for edge in graph.edges
            if edge.edge_type is AuditEdgeType.INVALIDATES
            and edge.source_node_id in node_id_set
            and next(
                (
                    node.node_type
                    for node in graph.nodes
                    if node.node_id == edge.source_node_id
                ),
                None,
            )
            is AuditNodeType.INVALIDATION_CONDITION
        }
        require(
            "audit_invalidation_coverage",
            actual_invalidations == expected_invalidations,
            "edges",
            "Every scenario invalidation condition must be represented.",
        )

    return _validation_result(issues)


def _coverage_ratio(
    referenced: set[str],
    available: set[str],
) -> float:
    if not available:
        return 0.0
    return round(len(referenced & available) / len(available), 6)


def build_audit_coverage_metrics(
    scenario_set: CurrentScenarioSet,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    retrieval: PatternRetrievalResult,
    graph: ReasoningAuditGraph,
) -> AuditCoverageMetrics:
    if not isinstance(scenario_set, CurrentScenarioSet):
        raise TypeError("scenario_set must be CurrentScenarioSet.")
    if not isinstance(retrieval, PatternRetrievalResult):
        raise TypeError("retrieval must be PatternRetrievalResult.")
    if not isinstance(graph, ReasoningAuditGraph):
        raise TypeError("graph must be ReasoningAuditGraph.")
    evidence_ids = {item.evidence_id for item in evidence}
    exposure_types = {
        item.exposure_type.value for item in exposures
    }
    pattern_refs = {
        item.pattern_ref
        for item in retrieval.matches + retrieval.counter_patterns
    }
    referenced_evidence = {
        evidence_id
        for scenario in scenario_set.scenarios
        for evidence_id in (
            scenario.supporting_current_evidence_ids
            + scenario.contradicting_current_evidence_ids
            + tuple(
                item
                for step in scenario.timeline_sequence
                for item in step.current_evidence_ids
            )
        )
    }
    referenced_exposures = {
        item.value
        for scenario in scenario_set.scenarios
        for item in (
            scenario.required_current_exposure_types
            + scenario.optional_current_exposure_types
            + tuple(
                exposure
                for step in scenario.timeline_sequence
                for exposure in step.current_exposure_types
            )
        )
    }
    referenced_patterns = {
        pattern_ref
        for scenario in scenario_set.scenarios
        for pattern_ref in (
            scenario.supporting_pattern_refs
            + scenario.counter_pattern_refs
        )
    }
    dangling = _dangling_edge_ids(graph.nodes, graph.edges)
    seed = AuditCoverageMetrics(
        total_scenario_count=len(scenario_set.scenarios),
        scenarios_with_evidence_support=sum(
            bool(item.supporting_current_evidence_ids)
            for item in scenario_set.scenarios
        ),
        scenarios_with_pattern_support=sum(
            bool(item.supporting_pattern_refs)
            for item in scenario_set.scenarios
        ),
        scenarios_with_counter_pattern_challenge=sum(
            bool(item.counter_pattern_refs)
            for item in scenario_set.scenarios
        ),
        scenarios_with_contradicting_evidence=sum(
            bool(item.contradicting_current_evidence_ids)
            for item in scenario_set.scenarios
        ),
        scenarios_with_invalidation_conditions=sum(
            bool(item.invalidation_conditions)
            for item in scenario_set.scenarios
        ),
        evidence_reference_coverage=_coverage_ratio(
            referenced_evidence,
            evidence_ids,
        ),
        exposure_reference_coverage=_coverage_ratio(
            referenced_exposures,
            exposure_types,
        ),
        pattern_reference_coverage=_coverage_ratio(
            referenced_patterns,
            pattern_refs,
        ),
        orphan_node_count=len(graph.orphan_node_ids),
        dangling_reference_count=len(dangling),
    )
    return replace(
        seed,
        content_hash=audit_coverage_metrics_content_hash(seed),
    )


def build_reasoning_audit_graph(
    *,
    orchestration_id: str,
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    retrieval: PatternRetrievalResult,
    scenario_set: CurrentScenarioSet,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    events: Sequence[MaterialEvent] = (),
    generated_at: str | datetime,
) -> tuple[ReasoningAuditGraph, AuditCoverageMetrics]:
    """Build a graph from validated references without model involvement."""

    if not orchestration_id.strip():
        raise ValueError("orchestration_id must be non-empty.")
    evidence_items = tuple(evidence)
    exposure_items = tuple(exposures)
    event_items = tuple(events)
    validation = validate_current_scenario_set(
        scenario_set,
        premise=premise,
        state=state,
        retrieval=retrieval,
        evidence=evidence_items,
        exposures=exposure_items,
        events=event_items,
    )
    if (
        not validation.is_valid
        or scenario_set.validation_result != validation
    ):
        raise ValueError(
            "Reasoning audit requires a canonically validated scenario set."
        )

    collected = _GraphCollector()

    premise_node = _make_node(
        AuditNodeType.FROZEN_TECHNICAL_PREMISE,
        premise.resolution_id,
        f"Frozen technical premise for {premise.symbol}",
        {
            "content_hash": premise.frozen_content_hash,
            "applicable_cutoff": premise.market_data_cutoff,
        },
    )
    premise_node_id = collected.add_node(premise_node)
    state_node_id = collected.add_node(
        _make_node(
            AuditNodeType.CURRENT_MARKET_STATE,
            state.state_id,
            f"Current market state for {state.symbol}",
            {
                "content_hash": state.content_hash,
                "applicable_cutoff": state.applicable_cutoff,
            },
        )
    )
    collected.add_edge(
        _make_edge(
            AuditEdgeType.DERIVED_FROM,
            state_node_id,
            premise_node_id,
            "frozen technical input",
        )
    )

    evidence_node_ids: dict[str, str] = {}
    for item in sorted(
        evidence_items,
        key=lambda value: value.evidence_id,
    ):
        node_id = collected.add_node(
            _make_node(
                AuditNodeType.CURRENT_EVIDENCE,
                item.evidence_id,
                item.claim,
                {
                    "status": item.status.value,
                    "implication": item.implication.value,
                    "applicable_cutoff": item.applicable_cutoff,
                },
            )
        )
        evidence_node_ids[item.evidence_id] = node_id
        collected.add_edge(
            _make_edge(
                AuditEdgeType.DERIVED_FROM,
                state_node_id,
                node_id,
                "current evidence input",
            )
        )

    exposure_node_ids: dict[str, str] = {}
    exposure_type_node_ids: dict[str, list[str]] = {}
    for item in sorted(
        exposure_items,
        key=lambda value: value.exposure_id,
    ):
        node_id = collected.add_node(
            _make_node(
                AuditNodeType.CURRENT_EXPOSURE,
                item.exposure_id,
                item.summary,
                {
                    "category": item.category.value,
                    "exposure_type": item.exposure_type.value,
                    "status": item.status.value,
                },
            )
        )
        exposure_node_ids[item.exposure_id] = node_id
        exposure_type_node_ids.setdefault(
            item.exposure_type.value,
            [],
        ).append(node_id)
        collected.add_edge(
            _make_edge(
                AuditEdgeType.DERIVED_FROM,
                state_node_id,
                node_id,
                "current exposure input",
            )
        )
        for evidence_id in sorted(
            set(
                item.supporting_evidence_ids
                + item.contradicting_evidence_ids
            )
        ):
            if evidence_id in evidence_node_ids:
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.DERIVED_FROM,
                        node_id,
                        evidence_node_ids[evidence_id],
                        "exposure evidence reference",
                    )
                )

    event_node_ids: dict[str, str] = {}
    for item in sorted(
        event_items,
        key=lambda value: value.event_id,
    ):
        node_id = collected.add_node(
            _make_node(
                AuditNodeType.CURRENT_EVENT,
                item.event_id,
                item.title,
                {
                    "event_type": item.event_type,
                    "scheduled_at": item.scheduled_at,
                    "status": item.status.value,
                },
            )
        )
        event_node_ids[item.event_id] = node_id
        collected.add_edge(
            _make_edge(
                AuditEdgeType.DERIVED_FROM,
                state_node_id,
                node_id,
                "current event input",
            )
        )

    pattern_node_ids: dict[str, str] = {}
    for lane, matches in (
        (
            AuditNodeType.RETRIEVED_PRIMARY_PATTERN,
            retrieval.matches,
        ),
        (
            AuditNodeType.RETRIEVED_COUNTER_PATTERN,
            retrieval.counter_patterns,
        ),
    ):
        for match in matches:
            node_id = collected.add_node(
                _make_node(
                    lane,
                    match.pattern_ref,
                    f"{lane.value}: {match.pattern_ref}",
                    {
                        "pattern_hash": match.pattern_hash,
                        "rank": match.rank,
                        "match_quality": match.match_quality.value,
                        "recommendation": match.recommendation.value,
                    },
                )
            )
            pattern_node_ids[match.pattern_ref] = node_id
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.MATCHED_TO,
                    node_id,
                    state_node_id,
                    match.lane.value,
                )
            )

    for scenario in scenario_set.scenarios:
        scenario_node_id = collected.add_node(
            _make_node(
                AuditNodeType.SCENARIO,
                scenario.scenario_id,
                scenario.title,
                {
                    "scenario_type": scenario.scenario_type.value,
                    "direction": scenario.direction.value,
                    "content_hash": scenario.content_hash,
                },
            )
        )
        for evidence_id in scenario.supporting_current_evidence_ids:
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.SUPPORTED_BY,
                    scenario_node_id,
                    evidence_node_ids[evidence_id],
                    "scenario supporting evidence",
                )
            )
        for evidence_id in scenario.contradicting_current_evidence_ids:
            contradiction_ref = (
                f"{scenario.scenario_id}::{evidence_id}"
            )
            contradiction_node_id = collected.add_node(
                _make_node(
                    AuditNodeType.CONTRADICTION,
                    contradiction_ref,
                    f"Contradiction for {scenario.scenario_id}",
                    {"evidence_id": evidence_id},
                )
            )
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.CONTRADICTED_BY,
                    scenario_node_id,
                    contradiction_node_id,
                    "scenario contradicting evidence",
                )
            )
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.DERIVED_FROM,
                    contradiction_node_id,
                    evidence_node_ids[evidence_id],
                    "contradiction evidence reference",
                )
            )
        for pattern_ref in scenario.supporting_pattern_refs:
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.USES_PATTERN,
                    scenario_node_id,
                    pattern_node_ids[pattern_ref],
                    "primary historical pattern",
                )
            )
        for pattern_ref in scenario.counter_pattern_refs:
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.USES_COUNTER_PATTERN,
                    scenario_node_id,
                    pattern_node_ids[pattern_ref],
                    "counter historical pattern",
                )
            )
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.CHALLENGED_BY,
                    scenario_node_id,
                    pattern_node_ids[pattern_ref],
                    "counter-pattern challenge",
                )
            )
        for exposure_type in sorted(
            set(
                scenario.required_current_exposure_types
                + scenario.optional_current_exposure_types
            ),
            key=lambda item: item.value,
        ):
            for node_id in exposure_type_node_ids.get(
                exposure_type.value,
                (),
            ):
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.REQUIRES,
                        scenario_node_id,
                        node_id,
                        f"exposure:{exposure_type.value}",
                    )
                )
        for assumption_index, assumption in enumerate(
            scenario.assumptions,
            start=1,
        ):
            assumption_ref = (
                f"{scenario.scenario_id}::assumption::{assumption_index}"
            )
            assumption_node_id = collected.add_node(
                _make_node(
                    AuditNodeType.ASSUMPTION,
                    assumption_ref,
                    assumption,
                    {"assumption": assumption},
                )
            )
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.REQUIRES,
                    scenario_node_id,
                    assumption_node_id,
                    "explicit scenario assumption",
                )
            )
        hypothetical_node_ids: dict[str, str] = {}
        for hypothetical in scenario.hypothetical_future_events:
            node_id = collected.add_node(
                _make_node(
                    AuditNodeType.HYPOTHETICAL_EVENT,
                    (
                        f"{scenario.scenario_id}::"
                        f"{hypothetical.hypothetical_event_id}"
                    ),
                    hypothetical.description,
                    {
                        "hypothetical_event_id": (
                            hypothetical.hypothetical_event_id
                        ),
                        "explicitly_hypothetical": (
                            hypothetical.explicitly_hypothetical
                        ),
                    },
                )
            )
            hypothetical_node_ids[
                hypothetical.hypothetical_event_id
            ] = node_id
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.REQUIRES,
                    scenario_node_id,
                    node_id,
                    "explicit hypothetical future event",
                )
            )

        previous_step_node_id: str | None = None
        for step in scenario.timeline_sequence:
            step_ref = (
                f"{scenario.scenario_id}::step::{step.step_number}"
            )
            step_node_id = collected.add_node(
                _make_node(
                    AuditNodeType.SCENARIO_STEP,
                    step_ref,
                    step.description,
                    {
                        "step_number": step.step_number,
                        "step_type": step.step_type.value,
                        "content_hash": step.content_hash,
                    },
                )
            )
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.DERIVED_FROM,
                    step_node_id,
                    scenario_node_id,
                    "scenario timeline",
                )
            )
            if previous_step_node_id is not None:
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.FOLLOWS,
                        step_node_id,
                        previous_step_node_id,
                        "timeline order",
                    )
                )
            previous_step_node_id = step_node_id
            for evidence_id in step.current_evidence_ids:
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.SUPPORTED_BY,
                        step_node_id,
                        evidence_node_ids[evidence_id],
                        "step evidence reference",
                    )
                )
            for exposure_type in step.current_exposure_types:
                for node_id in exposure_type_node_ids.get(
                    exposure_type.value,
                    (),
                ):
                    collected.add_edge(
                        _make_edge(
                            AuditEdgeType.REQUIRES,
                            step_node_id,
                            node_id,
                            f"step exposure:{exposure_type.value}",
                        )
                    )
            for event_id in step.event_ids:
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.REQUIRES,
                        step_node_id,
                        event_node_ids[event_id],
                        "scheduled current event",
                    )
                )
            for hypothetical_id in step.hypothetical_event_ids:
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.REQUIRES,
                        step_node_id,
                        hypothetical_node_ids[hypothetical_id],
                        "hypothetical event",
                    )
                )
            if step.step_type is ScenarioStepType.AMPLIFICATION:
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.AMPLIFIES,
                        step_node_id,
                        scenario_node_id,
                        "scenario amplification step",
                    )
                )
            if step.step_type is ScenarioStepType.INVALIDATION:
                collected.add_edge(
                    _make_edge(
                        AuditEdgeType.INVALIDATES,
                        step_node_id,
                        scenario_node_id,
                        "scenario invalidation step",
                    )
                )

        for index, condition in enumerate(
            scenario.invalidation_conditions,
            start=1,
        ):
            invalidation_ref = (
                f"{scenario.scenario_id}::invalidation::{index}"
            )
            invalidation_node_id = collected.add_node(
                _make_node(
                    AuditNodeType.INVALIDATION_CONDITION,
                    invalidation_ref,
                    condition,
                    {
                        "scenario_id": scenario.scenario_id,
                        "condition": condition,
                    },
                )
            )
            collected.add_edge(
                _make_edge(
                    AuditEdgeType.INVALIDATES,
                    invalidation_node_id,
                    scenario_node_id,
                    "explicit invalidation condition",
                )
            )

    nodes = tuple(
        collected.nodes[node_id]
        for node_id in sorted(collected.nodes)
    )
    edges = tuple(
        collected.edges[edge_id]
        for edge_id in sorted(collected.edges)
    )
    roots = (premise_node_id,)
    orphans = _orphan_node_ids(nodes, edges, roots)
    timestamp = _normalize_timestamp(
        generated_at,
        field_name="generated_at",
    )
    graph_id = "audit_graph_" + _hash_payload(
        {
            "orchestration_id": orchestration_id,
            "premise_hash": premise.frozen_content_hash,
            "state_hash": state.content_hash,
            "retrieval_hash": retrieval.content_hash,
            "scenario_set_hash": scenario_set.content_hash,
        }
    )[:20]
    seed = ReasoningAuditGraph(
        graph_id=graph_id,
        orchestration_id=orchestration_id,
        nodes=nodes,
        edges=edges,
        root_node_ids=roots,
        orphan_node_ids=orphans,
        generated_at=timestamp,
    )
    seed = replace(
        seed,
        content_hash=reasoning_audit_graph_content_hash(seed),
    )
    graph_validation = validate_reasoning_audit_graph(
        seed,
        scenario_set=scenario_set,
    )
    finalized = replace(
        seed,
        validation_result=graph_validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=reasoning_audit_graph_content_hash(finalized),
    )
    repeated = validate_reasoning_audit_graph(
        finalized,
        scenario_set=scenario_set,
    )
    if repeated != graph_validation:
        raise RuntimeError(
            "Reasoning-audit graph validation did not stabilize."
        )
    metrics = build_audit_coverage_metrics(
        scenario_set,
        evidence_items,
        exposure_items,
        retrieval,
        finalized,
    )
    return finalized, metrics


__all__ = [
    "AuditCoverageMetrics",
    "AuditEdgeType",
    "AuditNodeType",
    "REASONING_AUDIT_COVERAGE_SCHEMA_VERSION",
    "REASONING_AUDIT_EDGE_SCHEMA_VERSION",
    "REASONING_AUDIT_GRAPH_SCHEMA_VERSION",
    "REASONING_AUDIT_NODE_SCHEMA_VERSION",
    "REASONING_AUDIT_VALIDATION_VERSION",
    "ReasoningAuditEdge",
    "ReasoningAuditGraph",
    "ReasoningAuditNode",
    "audit_coverage_metrics_content_hash",
    "build_audit_coverage_metrics",
    "build_reasoning_audit_graph",
    "reasoning_audit_edge_content_hash",
    "reasoning_audit_graph_content_hash",
    "reasoning_audit_node_content_hash",
    "validate_reasoning_audit_graph",
]
