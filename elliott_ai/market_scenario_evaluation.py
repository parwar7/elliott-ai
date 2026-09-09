"""Deterministic structural evaluation for the Market Scenario Engine.

Phase 9 evaluates existing immutable orchestration artifacts. It does not
perform research, generate scenarios, alter Elliott analysis, predict
outcomes, assign probabilities, or repair an evaluated result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .company_knowledge import (
    CandidateEligibilityStatus,
    CompanyEventArchetypeLibrary,
    CompanyKnowledgeSnapshot,
    CompanyScenarioContext,
    CompanyStateValue,
    company_event_archetype_library_content_hash,
    company_event_candidate_content_hash,
    company_knowledge_snapshot_content_hash,
    company_scenario_context_content_hash,
    default_company_event_archetype_library,
    validate_company_knowledge_snapshot,
    validate_company_scenario_context,
    validate_company_scenario_usage,
)
from .current_scenarios import (
    CurrentScenarioNarrative,
    CurrentScenarioSet,
    ScenarioStepType,
    ScenarioType,
    current_scenario_narrative_content_hash,
    current_scenario_set_content_hash,
    validate_current_scenario_set,
)
from .current_state import (
    current_market_state_content_hash,
    evidence_packet_content_hash,
    exposure_report_content_hash,
    material_event_packet_content_hash,
)
from .historical_market import DurationBucket
from .historical_patterns import (
    HistoricalPatternLibrary,
    historical_pattern_library_content_hash,
)
from .market_scenario import (
    EvidenceImplication,
    EvidenceStatus,
    ScenarioDirection,
    ValidationIssue,
    ValidationResult,
    frozen_technical_premise_content_hash,
)
from .market_scenario_orchestrator import (
    MARKET_SCENARIO_ORCHESTRATOR_VERSION,
    MarketScenarioOrchestrationInput,
    MarketScenarioOrchestrationResult,
    MarketScenarioReplayPacket,
    OrchestrationStage,
    OrchestrationStatus,
    market_scenario_orchestration_input_content_hash,
    market_scenario_orchestration_result_analytical_hash,
    market_scenario_orchestration_result_content_hash,
    market_scenario_replay_packet_content_hash,
    replay_market_scenario_packet,
    validate_market_scenario_orchestration_result,
    validate_market_scenario_replay_packet,
)
from .pattern_retrieval import (
    RetrievalLane,
    pattern_retrieval_result_content_hash,
)
from .reasoning_audit import (
    REASONING_AUDIT_GRAPH_SCHEMA_VERSION,
    AuditEdgeType,
    AuditNodeType,
    ReasoningAuditGraph,
    audit_coverage_metrics_content_hash,
    reasoning_audit_edge_content_hash,
    reasoning_audit_graph_content_hash,
    reasoning_audit_node_content_hash,
    validate_reasoning_audit_graph,
)


EVALUATION_RUN_SCHEMA_VERSION = "evaluation-run-1.0.0"
EVALUATION_METRIC_SCHEMA_VERSION = "evaluation-metric-1.0.0"
EVALUATION_ISSUE_SCHEMA_VERSION = "evaluation-issue-1.0.0"
STRUCTURAL_EVALUATION_SCHEMA_VERSION = (
    "structural-evaluation-result-1.0.0"
)
SCENARIO_QUALITY_EVALUATION_SCHEMA_VERSION = (
    "scenario-quality-evaluation-1.0.0"
)
RETRIEVAL_QUALITY_EVALUATION_SCHEMA_VERSION = (
    "retrieval-quality-evaluation-1.0.0"
)
GROUNDING_EVALUATION_SCHEMA_VERSION = (
    "grounding-evaluation-1.0.0"
)
AUDIT_GRAPH_EVALUATION_SCHEMA_VERSION = (
    "audit-graph-evaluation-1.0.0"
)
REPRODUCIBILITY_EVALUATION_SCHEMA_VERSION = (
    "reproducibility-evaluation-1.0.0"
)
REGRESSION_BASELINE_SCHEMA_VERSION = "regression-baseline-1.0.0"
REGRESSION_COMPARISON_SCHEMA_VERSION = (
    "regression-comparison-1.0.0"
)
EVALUATION_SUITE_RESULT_SCHEMA_VERSION = (
    "evaluation-suite-result-1.0.0"
)
SYNTHETIC_EVALUATION_CASE_SCHEMA_VERSION = (
    "synthetic-evaluation-case-1.0.0"
)
COMPANY_KNOWLEDGE_EVALUATION_SCHEMA_VERSION = (
    "company-knowledge-evaluation-1.0.0"
)
MARKET_SCENARIO_EVALUATOR_VERSION = (
    "market-scenario-structural-evaluator-1.0.0"
)


class EvaluationCategory(StrEnum):
    STRUCTURAL = "structural"
    SCENARIO_QUALITY = "scenario_quality"
    RETRIEVAL = "retrieval"
    GROUNDING = "grounding"
    AUDIT_GRAPH = "audit_graph"
    REPRODUCIBILITY = "reproducibility"
    REGRESSION = "regression"
    COMPANY_KNOWLEDGE = "company_knowledge"


class EvaluationMetricStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class EvaluationIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EvaluationSuiteStatus(StrEnum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    FAILED = "failed"


class SyntheticCaseType(StrEnum):
    SOURCE_FIXTURE = "source_fixture"
    BULLISH_PREMISE = "bullish_premise"
    BEARISH_PREMISE = "bearish_premise"
    SHORT_DURATION = "short_duration"
    MEDIUM_DURATION = "medium_duration"
    LONG_DURATION = "long_duration"
    PRIMARY_COUNTER_COMBINATION = "primary_counter_combination"
    SPARSE_EVIDENCE = "sparse_evidence"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    MISSING_OPTIONAL_STATE = "missing_optional_state"
    INVALID_CUTOFF = "invalid_cutoff"
    DUPLICATE_SCENARIO = "duplicate_scenario"
    WEAK_CAUSAL_CHAIN = "weak_causal_chain"
    MISSING_INVALIDATION = "missing_invalidation"
    INVENTED_REFERENCE = "invented_reference"
    REPLAY_MISMATCH = "replay_mismatch"
    STAGE_FAILURE = "stage_failure"
    ADVERSARIAL_KEEP = "adversarial_keep"
    ADVERSARIAL_REVISE = "adversarial_revise"
    ADVERSARIAL_REJECT = "adversarial_reject"
    COMPATIBLE_EVIDENCE_COMBINATION = (
        "compatible_evidence_combination"
    )


HARD_FAILURE_TECHNICAL_PREMISE_MUTATION = (
    "technical_premise_mutation"
)
HARD_FAILURE_INVENTED_REFERENCE = "invented_reference"
HARD_FAILURE_FUTURE_EVIDENCE = "future_evidence_beyond_cutoff"
HARD_FAILURE_HASH_CONTINUITY = "hash_continuity_failure"
HARD_FAILURE_DANGLING_AUDIT = "dangling_audit_reference"
HARD_FAILURE_TECHNICAL_CONSTRAINT = (
    "technical_constraint_violation"
)
HARD_FAILURE_NON_REPRODUCIBLE = (
    "non_reproducible_fixture_execution"
)
HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE = (
    "downstream_after_upstream_failure"
)
HARD_FAILURE_MISSING_INVALIDATION = (
    "missing_invalidation_condition"
)
HARD_FAILURE_AUDIT_INCOMPLETE = "audit_graph_incomplete"
HARD_FAILURE_COMPANY_EVENT_CONFUSION = (
    "known_hypothetical_company_event_confusion"
)
HARD_FAILURE_INVENTED_COMPANY_FACT = "invented_company_fact"
HARD_FAILURE_INVENTED_COMPANY_EVENT = "invented_scheduled_company_event"
HARD_FAILURE_INVALID_COMPANY_CANDIDATE = (
    "invalid_company_event_candidate_reference"
)
HARD_FAILURE_COMPANY_CUTOFF = "company_knowledge_cutoff_violation"
HARD_FAILURE_COMPANY_SNAPSHOT_MUTATION = "company_snapshot_mutation"
HARD_FAILURE_UNSUPPORTED_ARCHETYPE = "unsupported_company_event_archetype"
HARD_FAILURE_HYPOTHETICAL_AS_FACT = (
    "hypothetical_company_event_stated_as_fact"
)

HARD_FAILURE_CODES = frozenset(
    {
        HARD_FAILURE_TECHNICAL_PREMISE_MUTATION,
        HARD_FAILURE_INVENTED_REFERENCE,
        HARD_FAILURE_FUTURE_EVIDENCE,
        HARD_FAILURE_HASH_CONTINUITY,
        HARD_FAILURE_DANGLING_AUDIT,
        HARD_FAILURE_TECHNICAL_CONSTRAINT,
        HARD_FAILURE_NON_REPRODUCIBLE,
        HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE,
        HARD_FAILURE_MISSING_INVALIDATION,
        HARD_FAILURE_AUDIT_INCOMPLETE,
        HARD_FAILURE_COMPANY_EVENT_CONFUSION,
        HARD_FAILURE_INVENTED_COMPANY_FACT,
        HARD_FAILURE_INVENTED_COMPANY_EVENT,
        HARD_FAILURE_INVALID_COMPANY_CANDIDATE,
        HARD_FAILURE_COMPANY_CUTOFF,
        HARD_FAILURE_COMPANY_SNAPSHOT_MUTATION,
        HARD_FAILURE_UNSUPPORTED_ARCHETYPE,
        HARD_FAILURE_HYPOTHETICAL_AS_FACT,
    }
)


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


def _temporal_point(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.fromisoformat(raw)
        else:
            parsed = datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )
    except (AttributeError, TypeError, ValueError):
        return None
    aware = (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
    return aware.astimezone(timezone.utc)


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
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON values must be finite.")
        return value
    raise TypeError(
        f"JSON contract cannot contain {type(value).__name__} values."
    )


def _string_mapping(
    value: Mapping[str, str] | None,
    *,
    field_name: str,
) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    raw = dict(value)
    if not all(
        isinstance(key, str)
        and key
        and isinstance(item, str)
        for key, item in raw.items()
    ):
        raise TypeError(
            f"{field_name} requires non-empty string keys and string values."
        )
    return MappingProxyType(dict(sorted(raw.items())))


def _json_mapping(
    value: Mapping[str, Any] | None,
    *,
    field_name: str,
) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    return _freeze_json(value)


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


def _duplicates(values: Sequence[str]) -> set[str]:
    return {
        value
        for value, count in Counter(values).items()
        if count > 1
    }


def _ratio(passed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(passed / total, 6)


def _same_number(
    first: float | None,
    second: float | None,
) -> bool:
    if first is None or second is None:
        return first is second
    return math.isclose(
        float(first),
        float(second),
        rel_tol=1e-12,
        abs_tol=1e-12,
    )


def _canonical_number(value: float) -> str:
    return format(float(value), ".15g")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class EvaluationIssue(_JsonContract):
    issue_id: str
    code: str
    severity: EvaluationIssueSeverity
    category: EvaluationCategory
    path: str
    message: str
    hard_failure: bool
    source_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    schema_version: str = EVALUATION_ISSUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "severity",
            _enum_value(
                self.severity,
                EvaluationIssueSeverity,
                field_name="severity",
            ),
        )
        object.__setattr__(
            self,
            "category",
            _enum_value(
                self.category,
                EvaluationCategory,
                field_name="category",
            ),
        )
        object.__setattr__(
            self,
            "source_refs",
            _string_tuple(
                self.source_refs,
                field_name="source_refs",
            ),
        )
        object.__setattr__(
            self,
            "details",
            _json_mapping(self.details, field_name="details"),
        )

    @classmethod
    def create(
        cls,
        *,
        code: str,
        severity: EvaluationIssueSeverity,
        category: EvaluationCategory,
        path: str,
        message: str,
        hard_failure: bool,
        source_refs: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
    ) -> "EvaluationIssue":
        issue_id = "evaluation_issue_" + _hash_payload(
            {
                "code": code,
                "category": category.value,
                "path": path,
                "message": message,
                "source_refs": tuple(source_refs),
                "details": details or {},
            }
        )[:20]
        seed = cls(
            issue_id=issue_id,
            code=code,
            severity=severity,
            category=category,
            path=path,
            message=message,
            hard_failure=hard_failure,
            source_refs=tuple(source_refs),
            details=details or {},
        )
        return replace(
            seed,
            content_hash=evaluation_issue_content_hash(seed),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "EvaluationIssue":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class EvaluationMetric(_JsonContract):
    metric_id: str
    category: EvaluationCategory
    name: str
    status: EvaluationMetricStatus
    value: Any
    expected: Any
    unit: str
    hard_gate: bool
    higher_is_better: bool | None
    source_refs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    content_hash: str = ""
    schema_version: str = EVALUATION_METRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "category",
            _enum_value(
                self.category,
                EvaluationCategory,
                field_name="category",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                EvaluationMetricStatus,
                field_name="status",
            ),
        )
        object.__setattr__(self, "value", _freeze_json(self.value))
        object.__setattr__(
            self,
            "expected",
            _freeze_json(self.expected),
        )
        object.__setattr__(
            self,
            "source_refs",
            _string_tuple(
                self.source_refs,
                field_name="source_refs",
            ),
        )
        object.__setattr__(
            self,
            "notes",
            _string_tuple(self.notes, field_name="notes"),
        )
        if self.higher_is_better is not None and not isinstance(
            self.higher_is_better,
            bool,
        ):
            raise TypeError(
                "higher_is_better must be bool or null."
            )

    @classmethod
    def create(
        cls,
        *,
        metric_id: str,
        category: EvaluationCategory,
        name: str,
        status: EvaluationMetricStatus,
        value: Any,
        expected: Any,
        unit: str = "state",
        hard_gate: bool = False,
        higher_is_better: bool | None = None,
        source_refs: Sequence[str] = (),
        notes: Sequence[str] = (),
    ) -> "EvaluationMetric":
        seed = cls(
            metric_id=metric_id,
            category=category,
            name=name,
            status=status,
            value=value,
            expected=expected,
            unit=unit,
            hard_gate=hard_gate,
            higher_is_better=higher_is_better,
            source_refs=tuple(source_refs),
            notes=tuple(notes),
        )
        return replace(
            seed,
            content_hash=evaluation_metric_content_hash(seed),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "EvaluationMetric":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeEvaluation(_JsonContract):
    snapshot_hash: str
    context_hash: str
    scenario_set_hash: str
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    validation_result: ValidationResult
    content_hash: str = ""
    schema_version: str = COMPANY_KNOWLEDGE_EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metrics",
            _model_tuple(
                self.metrics,
                EvaluationMetric,
                field_name="metrics",
            ),
        )
        object.__setattr__(
            self,
            "issues",
            _model_tuple(
                self.issues,
                EvaluationIssue,
                field_name="issues",
            ),
        )
        object.__setattr__(
            self,
            "hard_failure_codes",
            _string_tuple(
                self.hard_failure_codes,
                field_name="hard_failure_codes",
            ),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyKnowledgeEvaluation":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["metrics"] = tuple(
            EvaluationMetric.from_dict(item)
            for item in raw.get("metrics", ())
        )
        raw["issues"] = tuple(
            EvaluationIssue.from_dict(item)
            for item in raw.get("issues", ())
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class EvaluationRun(_JsonContract):
    evaluation_run_id: str
    orchestration_input: MarketScenarioOrchestrationInput
    orchestration_result: MarketScenarioOrchestrationResult
    replay_packet: MarketScenarioReplayPacket | None
    fixture_tags: tuple[str, ...]
    evaluator_version: str
    created_at: str
    content_hash: str = ""
    schema_version: str = EVALUATION_RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(
            self.orchestration_input,
            MarketScenarioOrchestrationInput,
        ):
            raise TypeError(
                "orchestration_input must be MarketScenarioOrchestrationInput."
            )
        if not isinstance(
            self.orchestration_result,
            MarketScenarioOrchestrationResult,
        ):
            raise TypeError(
                "orchestration_result must be MarketScenarioOrchestrationResult."
            )
        if self.replay_packet is not None and not isinstance(
            self.replay_packet,
            MarketScenarioReplayPacket,
        ):
            raise TypeError(
                "replay_packet must be MarketScenarioReplayPacket or null."
            )
        object.__setattr__(
            self,
            "fixture_tags",
            _string_tuple(
                self.fixture_tags,
                field_name="fixture_tags",
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(
                self.created_at,
                field_name="created_at",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        orchestration_input: MarketScenarioOrchestrationInput,
        orchestration_result: MarketScenarioOrchestrationResult,
        replay_packet: MarketScenarioReplayPacket | None = None,
        fixture_tags: Sequence[str] = (),
        evaluator_version: str = MARKET_SCENARIO_EVALUATOR_VERSION,
        created_at: str | datetime,
        evaluation_run_id: str | None = None,
    ) -> "EvaluationRun":
        selected_id = (
            evaluation_run_id
            if evaluation_run_id is not None
            else "evaluation_run_"
            + _hash_payload(
                {
                    "input_hash": orchestration_input.content_hash,
                    "result_hash": orchestration_result.analytical_hash,
                    "replay_hash": (
                        replay_packet.content_hash
                        if replay_packet is not None
                        else None
                    ),
                    "fixture_tags": tuple(sorted(fixture_tags)),
                }
            )[:20]
        )
        seed = cls(
            evaluation_run_id=selected_id,
            orchestration_input=orchestration_input,
            orchestration_result=orchestration_result,
            replay_packet=replay_packet,
            fixture_tags=tuple(sorted(set(fixture_tags))),
            evaluator_version=evaluator_version,
            created_at=created_at,
        )
        return replace(
            seed,
            content_hash=evaluation_run_content_hash(seed),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "EvaluationRun":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["orchestration_input"] = (
            MarketScenarioOrchestrationInput.from_dict(
                raw["orchestration_input"]
            )
        )
        raw["orchestration_result"] = (
            MarketScenarioOrchestrationResult.from_dict(
                raw["orchestration_result"]
            )
        )
        if raw.get("replay_packet") is not None:
            raw["replay_packet"] = MarketScenarioReplayPacket.from_dict(
                raw["replay_packet"]
            )
        return cls(**raw)


def _component_post_init(
    value: Any,
    *,
    extra_model_fields: Sequence[
        tuple[str, type]
    ] = (),
) -> None:
    object.__setattr__(
        value,
        "metrics",
        _model_tuple(
            value.metrics,
            EvaluationMetric,
            field_name="metrics",
        ),
    )
    object.__setattr__(
        value,
        "issues",
        _model_tuple(
            value.issues,
            EvaluationIssue,
            field_name="issues",
        ),
    )
    object.__setattr__(
        value,
        "hard_failure_codes",
        tuple(
            sorted(
                set(
                    _string_tuple(
                        value.hard_failure_codes,
                        field_name="hard_failure_codes",
                    )
                )
            )
        ),
    )
    for name, model_type in extra_model_fields:
        model_value = getattr(value, name)
        if model_value is not None and not isinstance(
            model_value,
            model_type,
        ):
            raise TypeError(
                f"{name} must be {model_type.__name__} or null."
            )


def _component_from_dict(
    cls: type,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = _mapping(value, model_name=cls.__name__)
    _reject_unknown(
        raw,
        {item.name for item in fields(cls)},
        model_name=cls.__name__,
    )
    raw["metrics"] = tuple(
        EvaluationMetric.from_dict(item)
        for item in raw.get("metrics", ())
    )
    raw["issues"] = tuple(
        EvaluationIssue.from_dict(item)
        for item in raw.get("issues", ())
    )
    return raw


@dataclass(frozen=True, slots=True)
class StructuralEvaluationResult(_JsonContract):
    evaluation_id: str
    evaluation_run_id: str
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    evaluated_hashes: Mapping[str, str]
    passed_hard_gates: bool
    content_hash: str = ""
    schema_version: str = STRUCTURAL_EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _component_post_init(self)
        object.__setattr__(
            self,
            "evaluated_hashes",
            _string_mapping(
                self.evaluated_hashes,
                field_name="evaluated_hashes",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "StructuralEvaluationResult":
        return cls(**_component_from_dict(cls, value))


@dataclass(frozen=True, slots=True)
class ScenarioQualityEvaluation(_JsonContract):
    evaluation_id: str
    evaluation_run_id: str
    evaluated_scenario_ids: tuple[str, ...]
    causal_chain_signatures: Mapping[str, str]
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    passed_hard_gates: bool
    content_hash: str = ""
    schema_version: str = (
        SCENARIO_QUALITY_EVALUATION_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        _component_post_init(self)
        object.__setattr__(
            self,
            "evaluated_scenario_ids",
            _string_tuple(
                self.evaluated_scenario_ids,
                field_name="evaluated_scenario_ids",
            ),
        )
        object.__setattr__(
            self,
            "causal_chain_signatures",
            _string_mapping(
                self.causal_chain_signatures,
                field_name="causal_chain_signatures",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ScenarioQualityEvaluation":
        return cls(**_component_from_dict(cls, value))


@dataclass(frozen=True, slots=True)
class RetrievalQualityEvaluation(_JsonContract):
    evaluation_id: str
    evaluation_run_id: str
    primary_pattern_refs: tuple[str, ...]
    counter_pattern_refs: tuple[str, ...]
    used_pattern_refs: tuple[str, ...]
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    passed_hard_gates: bool
    content_hash: str = ""
    schema_version: str = (
        RETRIEVAL_QUALITY_EVALUATION_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        _component_post_init(self)
        for name in (
            "primary_pattern_refs",
            "counter_pattern_refs",
            "used_pattern_refs",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "RetrievalQualityEvaluation":
        return cls(**_component_from_dict(cls, value))


@dataclass(frozen=True, slots=True)
class GroundingEvaluation(_JsonContract):
    evaluation_id: str
    evaluation_run_id: str
    referenced_evidence_ids: tuple[str, ...]
    referenced_exposure_types: tuple[str, ...]
    referenced_event_ids: tuple[str, ...]
    referenced_pattern_refs: tuple[str, ...]
    unknown_reference_ids: tuple[str, ...]
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    passed_hard_gates: bool
    content_hash: str = ""
    schema_version: str = GROUNDING_EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _component_post_init(self)
        for name in (
            "referenced_evidence_ids",
            "referenced_exposure_types",
            "referenced_event_ids",
            "referenced_pattern_refs",
            "unknown_reference_ids",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "GroundingEvaluation":
        return cls(**_component_from_dict(cls, value))


@dataclass(frozen=True, slots=True)
class AuditGraphEvaluation(_JsonContract):
    evaluation_id: str
    evaluation_run_id: str
    graph_id: str | None
    node_count: int
    edge_count: int
    orphan_node_ids: tuple[str, ...]
    dangling_edge_ids: tuple[str, ...]
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    passed_hard_gates: bool
    content_hash: str = ""
    schema_version: str = AUDIT_GRAPH_EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _component_post_init(self)
        for name in ("orphan_node_ids", "dangling_edge_ids"):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        for name in ("node_count", "edge_count"):
            item = getattr(self, name)
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or item < 0
            ):
                raise ValueError(
                    f"{name} must be a non-negative integer."
                )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "AuditGraphEvaluation":
        return cls(**_component_from_dict(cls, value))


@dataclass(frozen=True, slots=True)
class ReproducibilityEvaluation(_JsonContract):
    evaluation_id: str
    evaluation_run_id: str
    replay_attempt_count: int
    expected_final_hash: str | None
    observed_final_hashes: tuple[str, ...]
    expected_stage_hashes: Mapping[str, str]
    observed_stage_hashes: Mapping[str, str]
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    passed_hard_gates: bool
    content_hash: str = ""
    schema_version: str = (
        REPRODUCIBILITY_EVALUATION_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        _component_post_init(self)
        object.__setattr__(
            self,
            "observed_final_hashes",
            _string_tuple(
                self.observed_final_hashes,
                field_name="observed_final_hashes",
            ),
        )
        for name in (
            "expected_stage_hashes",
            "observed_stage_hashes",
        ):
            object.__setattr__(
                self,
                name,
                _string_mapping(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        if (
            isinstance(self.replay_attempt_count, bool)
            or not isinstance(self.replay_attempt_count, int)
            or self.replay_attempt_count < 0
        ):
            raise ValueError(
                "replay_attempt_count must be non-negative."
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ReproducibilityEvaluation":
        return cls(**_component_from_dict(cls, value))


@dataclass(frozen=True, slots=True)
class RegressionBaseline(_JsonContract):
    baseline_id: str
    source_evaluation_run_id: str
    orchestration_input_hash: str
    orchestration_result_hash: str
    evaluation_result_hash: str
    version_manifest: Mapping[str, str]
    metric_snapshot: Mapping[str, Any]
    hard_failure_codes: tuple[str, ...]
    scenario_ids: tuple[str, ...]
    retrieval_order: tuple[str, ...]
    pattern_usage: Mapping[str, Any]
    audit_coverage: Mapping[str, Any]
    content_hashes: Mapping[str, str]
    created_at: str
    content_hash: str = ""
    schema_version: str = REGRESSION_BASELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("version_manifest", "content_hashes"):
            object.__setattr__(
                self,
                name,
                _string_mapping(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        for name in (
            "metric_snapshot",
            "pattern_usage",
            "audit_coverage",
        ):
            object.__setattr__(
                self,
                name,
                _json_mapping(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        for name in (
            "hard_failure_codes",
            "scenario_ids",
            "retrieval_order",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(
                self.created_at,
                field_name="created_at",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "RegressionBaseline":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class RegressionComparison(_JsonContract):
    comparison_id: str
    baseline_id: str
    current_evaluation_run_id: str
    newly_introduced_hard_failures: tuple[str, ...]
    resolved_hard_failures: tuple[str, ...]
    deteriorated_metrics: Mapping[str, Any]
    improved_metrics: Mapping[str, Any]
    changed_scenario_count: Mapping[str, Any]
    changed_retrieval_order: Mapping[str, Any]
    changed_pattern_usage: Mapping[str, Any]
    changed_audit_coverage: Mapping[str, Any]
    changed_content_hashes: Mapping[str, Any]
    version_differences: Mapping[str, Any]
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    passed_hard_gates: bool
    content_hash: str = ""
    schema_version: str = REGRESSION_COMPARISON_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _component_post_init(self)
        for name in (
            "newly_introduced_hard_failures",
            "resolved_hard_failures",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        for name in (
            "deteriorated_metrics",
            "improved_metrics",
            "changed_scenario_count",
            "changed_retrieval_order",
            "changed_pattern_usage",
            "changed_audit_coverage",
            "changed_content_hashes",
            "version_differences",
        ):
            object.__setattr__(
                self,
                name,
                _json_mapping(
                    getattr(self, name),
                    field_name=name,
                ),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "RegressionComparison":
        return cls(**_component_from_dict(cls, value))


@dataclass(frozen=True, slots=True)
class EvaluationSuiteResult(_JsonContract):
    evaluation_suite_id: str
    evaluation_run_id: str
    status: EvaluationSuiteStatus
    structural_evaluation: StructuralEvaluationResult
    scenario_quality_evaluation: ScenarioQualityEvaluation
    retrieval_quality_evaluation: RetrievalQualityEvaluation
    grounding_evaluation: GroundingEvaluation
    audit_graph_evaluation: AuditGraphEvaluation
    reproducibility_evaluation: ReproducibilityEvaluation
    regression_comparison: RegressionComparison | None
    metrics: tuple[EvaluationMetric, ...]
    issues: tuple[EvaluationIssue, ...]
    hard_failure_codes: tuple[str, ...]
    generated_at: str
    evaluator_version: str
    content_hash: str = ""
    schema_version: str = EVALUATION_SUITE_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                EvaluationSuiteStatus,
                field_name="status",
            ),
        )
        for name, model_type in (
            (
                "structural_evaluation",
                StructuralEvaluationResult,
            ),
            (
                "scenario_quality_evaluation",
                ScenarioQualityEvaluation,
            ),
            (
                "retrieval_quality_evaluation",
                RetrievalQualityEvaluation,
            ),
            ("grounding_evaluation", GroundingEvaluation),
            ("audit_graph_evaluation", AuditGraphEvaluation),
            (
                "reproducibility_evaluation",
                ReproducibilityEvaluation,
            ),
        ):
            if not isinstance(getattr(self, name), model_type):
                raise TypeError(
                    f"{name} must be {model_type.__name__}."
                )
        if self.regression_comparison is not None and not isinstance(
            self.regression_comparison,
            RegressionComparison,
        ):
            raise TypeError(
                "regression_comparison must be RegressionComparison or null."
            )
        _component_post_init(self)
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
    ) -> "EvaluationSuiteResult":
        raw = _component_from_dict(cls, value)
        raw["structural_evaluation"] = (
            StructuralEvaluationResult.from_dict(
                raw["structural_evaluation"]
            )
        )
        raw["scenario_quality_evaluation"] = (
            ScenarioQualityEvaluation.from_dict(
                raw["scenario_quality_evaluation"]
            )
        )
        raw["retrieval_quality_evaluation"] = (
            RetrievalQualityEvaluation.from_dict(
                raw["retrieval_quality_evaluation"]
            )
        )
        raw["grounding_evaluation"] = GroundingEvaluation.from_dict(
            raw["grounding_evaluation"]
        )
        raw["audit_graph_evaluation"] = (
            AuditGraphEvaluation.from_dict(
                raw["audit_graph_evaluation"]
            )
        )
        raw["reproducibility_evaluation"] = (
            ReproducibilityEvaluation.from_dict(
                raw["reproducibility_evaluation"]
            )
        )
        if raw.get("regression_comparison") is not None:
            raw["regression_comparison"] = (
                RegressionComparison.from_dict(
                    raw["regression_comparison"]
                )
            )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class SyntheticEvaluationCase(_JsonContract):
    synthetic_case_id: str
    case_type: SyntheticCaseType
    source_evaluation_run_ids: tuple[str, ...]
    evaluation_run: EvaluationRun
    mutation_notes: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = SYNTHETIC_EVALUATION_CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "case_type",
            _enum_value(
                self.case_type,
                SyntheticCaseType,
                field_name="case_type",
            ),
        )
        object.__setattr__(
            self,
            "source_evaluation_run_ids",
            _string_tuple(
                self.source_evaluation_run_ids,
                field_name="source_evaluation_run_ids",
            ),
        )
        if not isinstance(self.evaluation_run, EvaluationRun):
            raise TypeError(
                "evaluation_run must be EvaluationRun."
            )
        object.__setattr__(
            self,
            "mutation_notes",
            _string_tuple(
                self.mutation_notes,
                field_name="mutation_notes",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "SyntheticEvaluationCase":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["evaluation_run"] = EvaluationRun.from_dict(
            raw["evaluation_run"]
        )
        return cls(**raw)


def evaluation_issue_content_hash(value: EvaluationIssue) -> str:
    return _contract_hash(value)


def evaluation_metric_content_hash(value: EvaluationMetric) -> str:
    return _contract_hash(value)


def company_knowledge_evaluation_content_hash(
    value: CompanyKnowledgeEvaluation,
) -> str:
    return _contract_hash(value)


def evaluation_run_content_hash(value: EvaluationRun) -> str:
    return _contract_hash(value)


def structural_evaluation_content_hash(
    value: StructuralEvaluationResult,
) -> str:
    return _contract_hash(value)


def scenario_quality_evaluation_content_hash(
    value: ScenarioQualityEvaluation,
) -> str:
    return _contract_hash(value)


def retrieval_quality_evaluation_content_hash(
    value: RetrievalQualityEvaluation,
) -> str:
    return _contract_hash(value)


def grounding_evaluation_content_hash(
    value: GroundingEvaluation,
) -> str:
    return _contract_hash(value)


def audit_graph_evaluation_content_hash(
    value: AuditGraphEvaluation,
) -> str:
    return _contract_hash(value)


def reproducibility_evaluation_content_hash(
    value: ReproducibilityEvaluation,
) -> str:
    return _contract_hash(value)


def regression_baseline_content_hash(
    value: RegressionBaseline,
) -> str:
    return _contract_hash(value)


def regression_comparison_content_hash(
    value: RegressionComparison,
) -> str:
    return _contract_hash(value)


def evaluation_suite_result_content_hash(
    value: EvaluationSuiteResult,
) -> str:
    return _contract_hash(value)


def synthetic_evaluation_case_content_hash(
    value: SyntheticEvaluationCase,
) -> str:
    return _contract_hash(value)


def _metric(
    metric_id: str,
    category: EvaluationCategory,
    *,
    value: Any,
    expected: Any,
    passed: bool | None,
    hard_gate: bool = False,
    higher_is_better: bool | None = None,
    unit: str = "state",
    source_refs: Sequence[str] = (),
    notes: Sequence[str] = (),
) -> EvaluationMetric:
    status = (
        EvaluationMetricStatus.UNAVAILABLE
        if passed is None
        else (
            EvaluationMetricStatus.PASSED
            if passed
            else (
                EvaluationMetricStatus.FAILED
                if hard_gate
                else EvaluationMetricStatus.WARNING
            )
        )
    )
    return EvaluationMetric.create(
        metric_id=metric_id,
        category=category,
        name=metric_id.replace("_", " "),
        status=status,
        value=value,
        expected=expected,
        unit=unit,
        hard_gate=hard_gate,
        higher_is_better=higher_is_better,
        source_refs=source_refs,
        notes=notes,
    )


def _issue(
    code: str,
    category: EvaluationCategory,
    path: str,
    message: str,
    *,
    hard: bool = False,
    severity: EvaluationIssueSeverity | None = None,
    refs: Sequence[str] = (),
    details: Mapping[str, Any] | None = None,
) -> EvaluationIssue:
    return EvaluationIssue.create(
        code=code,
        severity=(
            severity
            if severity is not None
            else (
                EvaluationIssueSeverity.ERROR
                if hard
                else EvaluationIssueSeverity.WARNING
            )
        ),
        category=category,
        path=path,
        message=message,
        hard_failure=hard,
        source_refs=refs,
        details=details,
    )


def _hard_codes(
    issues: Sequence[EvaluationIssue],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.code
                for item in issues
                if item.hard_failure
            }
        )
    )


def _finalize_component(value: Any, hash_function: Any) -> Any:
    return replace(value, content_hash=hash_function(value))


def _scenario_chain_signature(
    scenario: CurrentScenarioNarrative,
) -> str:
    return _hash_payload(
        [
            {
                "step_type": step.step_type.value,
                "exposures": sorted(
                    item.value
                    for item in step.current_exposure_types
                ),
                "events": sorted(step.event_ids),
                "hypothetical_events": sorted(
                    step.hypothetical_event_ids
                ),
                "transmissions": sorted(
                    item.value
                    for item in step.transmission_mechanisms
                ),
            }
            for step in scenario.timeline_sequence
        ]
    )


def _stage_has_downstream_after_failure(
    result: MarketScenarioOrchestrationResult,
) -> bool:
    analytical = {
        OrchestrationStage.INPUT_VALIDATION,
        OrchestrationStage.CURRENT_STATE_BUILD,
        OrchestrationStage.PATTERN_RETRIEVAL,
        OrchestrationStage.SCENARIO_GENERATION,
    }
    failures = {
        OrchestrationStatus.FAILED,
        OrchestrationStatus.INSUFFICIENT_EVIDENCE,
        OrchestrationStatus.CANCELLED,
    }
    failed_index = next(
        (
            index
            for index, record in enumerate(result.stage_records)
            if record.stage in analytical
            and record.status in failures
        ),
        None,
    )
    return failed_index is not None and any(
        record.stage in analytical
        for record in result.stage_records[failed_index + 1 :]
    )


def _cutoff_violations(
    orchestration_input: MarketScenarioOrchestrationInput,
    result: MarketScenarioOrchestrationResult,
) -> tuple[str, ...]:
    cutoff = _temporal_point(orchestration_input.applicable_cutoff)
    violations: list[str] = []
    if cutoff is None:
        return ("orchestration_input.applicable_cutoff",)

    sources: list[tuple[str, str | None]] = [
        (
            "frozen_technical_premise.market_data_cutoff",
            orchestration_input.frozen_technical_premise.market_data_cutoff,
        ),
        (
            "historical_pattern_library.applicable_cutoff",
            orchestration_input.historical_pattern_library.applicable_cutoff,
        ),
        ("result.applicable_cutoff", result.applicable_cutoff),
    ]
    if result.current_state is not None:
        sources.append(
            (
                "current_state.applicable_cutoff",
                result.current_state.applicable_cutoff,
            )
        )
    if result.current_scenario_set is not None:
        sources.append(
            (
                "current_scenario_set.applicable_cutoff",
                result.current_scenario_set.applicable_cutoff,
            )
        )
    for index, item in enumerate(
        orchestration_input.current_evidence
    ):
        sources.append(
            (
                f"current_evidence[{index}].applicable_cutoff",
                item.applicable_cutoff,
            )
        )
        if item.publication_date is not None:
            published = _temporal_point(item.publication_date)
            item_cutoff = _temporal_point(item.applicable_cutoff)
            if (
                published is None
                or item_cutoff is None
                or published > item_cutoff
            ):
                violations.append(
                    f"current_evidence[{index}].publication_date"
                )
    for index, item in enumerate(orchestration_input.current_events):
        sources.append(
            (
                f"current_events[{index}].applicable_cutoff",
                item.applicable_cutoff,
            )
        )
        if item.publication_date is not None:
            published = _temporal_point(item.publication_date)
            item_cutoff = _temporal_point(item.applicable_cutoff)
            if (
                published is None
                or item_cutoff is None
                or published > item_cutoff
            ):
                violations.append(
                    f"current_events[{index}].publication_date"
                )
    for path, raw in sources:
        point = _temporal_point(raw)
        if point is None or point > cutoff:
            violations.append(path)
    return tuple(sorted(set(violations)))


def _evaluate_structural(
    run: EvaluationRun,
) -> StructuralEvaluationResult:
    category = EvaluationCategory.STRUCTURAL
    orchestration_input = run.orchestration_input
    result = run.orchestration_result
    premise = orchestration_input.frozen_technical_premise
    state = result.current_state
    scenario_set = result.current_scenario_set
    metrics: list[EvaluationMetric] = []
    issues: list[EvaluationIssue] = []

    run_hash_valid = (
        run.content_hash == evaluation_run_content_hash(run)
    )
    input_hash_valid = (
        orchestration_input.content_hash
        == market_scenario_orchestration_input_content_hash(
            orchestration_input
        )
        and premise.frozen_content_hash
        == frozen_technical_premise_content_hash(premise)
        and orchestration_input.historical_pattern_library.content_hash
        == historical_pattern_library_content_hash(
            orchestration_input.historical_pattern_library
        )
    )
    try:
        result_validation = (
            validate_market_scenario_orchestration_result(result)
        )
        result_hash_valid = (
            result.content_hash
            == market_scenario_orchestration_result_content_hash(
                result
            )
            and result.analytical_hash
            == market_scenario_orchestration_result_analytical_hash(
                result
            )
            and result_validation.is_valid
        )
    except Exception:
        result_hash_valid = False
    hash_continuity = (
        run_hash_valid
        and input_hash_valid
        and result_hash_valid
        and result.orchestration_input_hash
        == orchestration_input.content_hash
    )
    metrics.append(
        _metric(
            "hash_continuity",
            category,
            value=hash_continuity,
            expected=True,
            passed=hash_continuity,
            hard_gate=True,
            source_refs=(
                orchestration_input.content_hash,
                result.content_hash,
            ),
        )
    )
    if not hash_continuity:
        issues.append(
            _issue(
                HARD_FAILURE_HASH_CONTINUITY,
                category,
                "orchestration",
                "One or more run, input, result, or nested source hashes are inconsistent.",
                hard=True,
            )
        )

    technical_links = [
        result.frozen_technical_premise_hash
        == premise.frozen_content_hash,
        result.symbol == premise.symbol,
    ]
    if state is not None:
        technical_links.extend(
            (
                state.frozen_technical_premise_hash
                == premise.frozen_content_hash,
                state.symbol == premise.symbol,
            )
        )
    if scenario_set is not None:
        technical_links.extend(
            (
                scenario_set.frozen_technical_premise_hash
                == premise.frozen_content_hash,
                scenario_set.symbol == premise.symbol,
            )
        )
    technical_immutable = all(technical_links)
    metrics.append(
        _metric(
            "technical_premise_immutability",
            category,
            value=technical_immutable,
            expected=True,
            passed=technical_immutable,
            hard_gate=True,
            source_refs=(premise.frozen_content_hash,),
        )
    )
    if not technical_immutable:
        issues.append(
            _issue(
                HARD_FAILURE_TECHNICAL_PREMISE_MUTATION,
                category,
                "frozen_technical_premise",
                "Downstream artifacts do not preserve the frozen technical premise.",
                hard=True,
                refs=(premise.resolution_id,),
            )
        )

    cutoff_violations = _cutoff_violations(
        orchestration_input,
        result,
    )
    cutoff_ok = not cutoff_violations
    metrics.append(
        _metric(
            "applicable_cutoff_compliance",
            category,
            value=len(cutoff_violations),
            expected=0,
            passed=cutoff_ok,
            hard_gate=True,
            higher_is_better=False,
            unit="violation_count",
            notes=cutoff_violations,
        )
    )
    if not cutoff_ok:
        issues.append(
            _issue(
                HARD_FAILURE_FUTURE_EVIDENCE,
                category,
                "applicable_cutoff",
                "Evidence or provenance extends beyond the permitted cutoff.",
                hard=True,
                refs=cutoff_violations,
            )
        )

    downstream_after_failure = (
        _stage_has_downstream_after_failure(result)
    )
    metrics.append(
        _metric(
            "downstream_execution_after_failure",
            category,
            value=downstream_after_failure,
            expected=False,
            passed=not downstream_after_failure,
            hard_gate=True,
        )
    )
    if downstream_after_failure:
        issues.append(
            _issue(
                HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE,
                category,
                "stage_records",
                "A downstream analytical stage executed after an unrecoverable upstream status.",
                hard=True,
            )
        )

    scenarios = (
        scenario_set.scenarios
        if scenario_set is not None
        else ()
    )
    if scenarios:
        target_valid = sum(
            _same_number(item.linked_target_low, premise.target_low)
            and _same_number(
                item.linked_target_high,
                premise.target_high,
            )
            for item in scenarios
        )
        direction_valid = sum(
            (
                item.direction is premise.direction
                if item.scenario_type
                is not ScenarioType.TECHNICAL_INVALIDATION
                else (
                    item.direction is not premise.direction
                    or premise.direction
                    in {
                        ScenarioDirection.UNKNOWN,
                        ScenarioDirection.SIDEWAYS,
                    }
                )
            )
            for item in scenarios
        )
        duration_valid = sum(
            state is not None
            and item.linked_duration_bucket
            is state.expected_duration_bucket
            for item in scenarios
        )
        invalidation_valid = sum(
            bool(item.invalidation_conditions)
            for item in scenarios
        )
        technical_invalidation = tuple(
            item
            for item in scenarios
            if item.scenario_type
            is ScenarioType.TECHNICAL_INVALIDATION
        )
        invalidation_token_ok = bool(technical_invalidation)
        if premise.invalidation_level is not None:
            token = (
                "price_invalidation_level:"
                + _canonical_number(premise.invalidation_level)
            )
            invalidation_token_ok = invalidation_token_ok and all(
                token in item.invalidation_conditions
                for item in technical_invalidation
            )
        checks = (
            (
                "target_consistency",
                target_valid,
                HARD_FAILURE_TECHNICAL_CONSTRAINT,
            ),
            (
                "direction_consistency",
                direction_valid,
                HARD_FAILURE_TECHNICAL_CONSTRAINT,
            ),
            (
                "duration_consistency",
                duration_valid,
                HARD_FAILURE_TECHNICAL_CONSTRAINT,
            ),
            (
                "invalidation_condition_coverage",
                invalidation_valid,
                HARD_FAILURE_MISSING_INVALIDATION,
            ),
        )
        for metric_id, passed_count, issue_code in checks:
            passed = passed_count == len(scenarios)
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=_ratio(passed_count, len(scenarios)),
                    expected=1.0,
                    passed=passed,
                    hard_gate=True,
                    higher_is_better=True,
                    unit="coverage_ratio",
                )
            )
            if not passed:
                issues.append(
                    _issue(
                        issue_code,
                        category,
                        f"current_scenario_set.{metric_id}",
                        f"{metric_id.replace('_', ' ')} failed for one or more scenarios.",
                        hard=True,
                    )
                )
        metrics.append(
            _metric(
                "technical_invalidation_linkage",
                category,
                value=invalidation_token_ok,
                expected=True,
                passed=invalidation_token_ok,
                hard_gate=True,
            )
        )
        if not invalidation_token_ok:
            issues.append(
                _issue(
                    HARD_FAILURE_TECHNICAL_CONSTRAINT,
                    category,
                    "current_scenario_set.technical_invalidation",
                    "Technical invalidation scenario or frozen invalidation token is missing.",
                    hard=True,
                )
            )
    else:
        for metric_id in (
            "target_consistency",
            "direction_consistency",
            "duration_consistency",
            "invalidation_condition_coverage",
            "technical_invalidation_linkage",
        ):
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=None,
                    expected=True,
                    passed=None,
                    hard_gate=False,
                    notes=(
                        "No scenario set was retained for this run.",
                    ),
                )
            )

    stage_consistency = all(
        (
            record.status
            in {
                OrchestrationStatus.COMPLETED,
                OrchestrationStatus.COMPLETED_WITH_WARNINGS,
            }
            and record.validation_result.is_valid
        )
        or (
            record.status
            in {
                OrchestrationStatus.FAILED,
                OrchestrationStatus.INSUFFICIENT_EVIDENCE,
                OrchestrationStatus.CANCELLED,
            }
            and (
                bool(record.errors)
                or bool(record.warnings)
                or not record.validation_result.is_valid
            )
        )
        for record in result.stage_records
        if record.stage is not OrchestrationStage.COMPLETED
    )
    metrics.append(
        _metric(
            "validation_status_consistency",
            category,
            value=stage_consistency,
            expected=True,
            passed=stage_consistency,
        )
    )
    if not stage_consistency:
        issues.append(
            _issue(
                "validation_status_inconsistency",
                category,
                "stage_records",
                "Stage status, validation, warnings, or errors are inconsistent.",
            )
        )

    hard_codes = _hard_codes(issues)
    seed = StructuralEvaluationResult(
        evaluation_id=(
            "structural_" + _hash_payload(run.content_hash)[:20]
        ),
        evaluation_run_id=run.evaluation_run_id,
        metrics=tuple(sorted(metrics, key=lambda item: item.metric_id)),
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
        hard_failure_codes=hard_codes,
        evaluated_hashes={
            "evaluation_run": run.content_hash,
            "orchestration_input": orchestration_input.content_hash,
            "orchestration_result": result.content_hash,
            "technical_premise": premise.frozen_content_hash,
        },
        passed_hard_gates=not hard_codes,
    )
    return _finalize_component(
        seed,
        structural_evaluation_content_hash,
    )


def _collect_grounding_references(
    scenario_set: CurrentScenarioSet | None,
) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    evidence_refs: set[str] = set()
    exposure_refs: set[str] = set()
    event_refs: set[str] = set()
    pattern_refs: set[str] = set()
    hypothetical_refs: set[str] = set()
    if scenario_set is None:
        return (
            evidence_refs,
            exposure_refs,
            event_refs,
            pattern_refs,
            hypothetical_refs,
        )
    for scenario in scenario_set.scenarios:
        evidence_refs.update(
            scenario.supporting_current_evidence_ids
        )
        evidence_refs.update(
            scenario.contradicting_current_evidence_ids
        )
        exposure_refs.update(
            item.value
            for item in (
                scenario.required_current_exposure_types
                + scenario.optional_current_exposure_types
            )
        )
        pattern_refs.update(scenario.supporting_pattern_refs)
        pattern_refs.update(scenario.counter_pattern_refs)
        pattern_refs.update(
            item.pattern_ref for item in scenario.pattern_grounding
        )
        hypothetical_ids = {
            item.hypothetical_event_id
            for item in scenario.hypothetical_future_events
        }
        hypothetical_refs.update(hypothetical_ids)
        for step in scenario.timeline_sequence:
            evidence_refs.update(step.current_evidence_ids)
            exposure_refs.update(
                item.value for item in step.current_exposure_types
            )
            event_refs.update(step.event_ids)
            hypothetical_refs.update(step.hypothetical_event_ids)
    return (
        evidence_refs,
        exposure_refs,
        event_refs,
        pattern_refs,
        hypothetical_refs,
    )


def _evaluate_grounding(
    run: EvaluationRun,
) -> GroundingEvaluation:
    category = EvaluationCategory.GROUNDING
    orchestration_input = run.orchestration_input
    result = run.orchestration_result
    scenario_set = result.current_scenario_set
    retrieval = result.pattern_retrieval_result
    evidence_ids = {
        item.evidence_id
        for item in orchestration_input.current_evidence
    }
    exposure_ids = {
        item.exposure_id
        for item in orchestration_input.current_exposures
    }
    exposure_types = {
        item.exposure_type.value
        for item in orchestration_input.current_exposures
    }
    event_ids = {
        item.event_id
        for item in orchestration_input.current_events
    }
    library_refs = {
        item.pattern_ref
        for item in orchestration_input.historical_pattern_library.patterns
    }
    (
        evidence_refs,
        exposure_refs,
        event_refs,
        pattern_refs,
        hypothetical_refs,
    ) = _collect_grounding_references(scenario_set)
    available_pattern_refs = (
        {
            item.pattern_ref
            for item in retrieval.matches
            + retrieval.counter_patterns
        }
        if retrieval is not None
        else set()
    )
    unknown: set[str] = set()
    unknown.update(
        f"evidence:{item}"
        for item in evidence_refs - evidence_ids
    )
    unknown.update(
        f"exposure_type:{item}"
        for item in exposure_refs - exposure_types
    )
    unknown.update(
        f"event:{item}" for item in event_refs - event_ids
    )
    unknown.update(
        f"pattern:{item}"
        for item in pattern_refs - available_pattern_refs
    )
    for item in orchestration_input.current_exposures:
        unknown.update(
            f"evidence:{reference}"
            for reference in (
                set(item.supporting_evidence_ids)
                | set(item.contradicting_evidence_ids)
            )
            - evidence_ids
        )
    for item in orchestration_input.current_events:
        unknown.update(
            f"evidence:{reference}"
            for reference in set(item.related_evidence_ids)
            - evidence_ids
        )
    if result.current_state is not None:
        unknown.update(
            f"state_evidence:{item}"
            for item in set(result.current_state.evidence_ids)
            - evidence_ids
        )
        unknown.update(
            f"state_exposure:{item}"
            for item in set(result.current_state.exposure_ids)
            - exposure_ids
        )
        unknown.update(
            f"state_event:{item}"
            for item in set(result.current_state.event_ids)
            - event_ids
        )
    if retrieval is not None:
        unknown.update(
            f"library_pattern:{item.pattern_ref}"
            for item in retrieval.matches + retrieval.counter_patterns
            if item.pattern_ref not in library_refs
        )
    if scenario_set is not None:
        for scenario in scenario_set.scenarios:
            local_hypothetical = {
                item.hypothetical_event_id
                for item in scenario.hypothetical_future_events
            }
            for step in scenario.timeline_sequence:
                unknown.update(
                    f"hypothetical_event:{item}"
                    for item in step.hypothetical_event_ids
                    if item not in local_hypothetical
                )
    issues: list[EvaluationIssue] = []
    metrics: list[EvaluationMetric] = []
    reference_valid = not unknown
    metrics.append(
        _metric(
            "unknown_reference_count",
            category,
            value=len(unknown),
            expected=0,
            passed=reference_valid,
            hard_gate=True,
            higher_is_better=False,
            unit="reference_count",
            notes=tuple(sorted(unknown)),
        )
    )
    if unknown:
        issues.append(
            _issue(
                HARD_FAILURE_INVENTED_REFERENCE,
                category,
                "references",
                "Unknown evidence, exposure, event, pattern, or hypothetical references were found.",
                hard=True,
                refs=tuple(sorted(unknown)),
            )
        )

    separation_failures: list[str] = []
    claims = {
        item.claim for item in orchestration_input.current_evidence
    }
    for scenario in (
        scenario_set.scenarios if scenario_set is not None else ()
    ):
        hypothetical_ids = {
            item.hypothetical_event_id
            for item in scenario.hypothetical_future_events
        }
        if hypothetical_ids & event_ids:
            separation_failures.append(
                f"{scenario.scenario_id}:hypothetical_current_overlap"
            )
        if any(
            not item.explicitly_hypothetical
            for item in scenario.hypothetical_future_events
        ):
            separation_failures.append(
                f"{scenario.scenario_id}:hypothetical_not_explicit"
            )
        if set(scenario.assumptions) & claims:
            separation_failures.append(
                f"{scenario.scenario_id}:assumption_claim_overlap"
            )
    cutoff = _temporal_point(orchestration_input.applicable_cutoff)
    for event in orchestration_input.current_events:
        scheduled = _temporal_point(event.scheduled_at)
        if (
            cutoff is not None
            and scheduled is not None
            and scheduled > cutoff
            and event.status is not EvidenceStatus.SCHEDULED
        ):
            separation_failures.append(
                f"{event.event_id}:future_event_not_scheduled"
            )
    separation_ok = not separation_failures
    metrics.append(
        _metric(
            "evidence_type_separation",
            category,
            value=len(separation_failures),
            expected=0,
            passed=separation_ok,
            higher_is_better=False,
            unit="violation_count",
            notes=tuple(sorted(separation_failures)),
        )
    )
    if not separation_ok:
        issues.append(
            _issue(
                "evidence_type_separation_failure",
                category,
                "evidence_separation",
                "Fact, interpretation, assumption, scheduled-event, or hypothetical-event boundaries overlap.",
                refs=tuple(sorted(separation_failures)),
            )
        )

    all_reference_tokens = {
        *(f"evidence:{item}" for item in evidence_refs),
        *(f"exposure_type:{item}" for item in exposure_refs),
        *(f"event:{item}" for item in event_refs),
        *(f"pattern:{item}" for item in pattern_refs),
        *(
            f"hypothetical_event:{item}"
            for item in hypothetical_refs
        ),
    }
    for item in orchestration_input.current_exposures:
        all_reference_tokens.update(
            f"evidence:{reference}"
            for reference in (
                item.supporting_evidence_ids
                + item.contradicting_evidence_ids
            )
        )
    for item in orchestration_input.current_events:
        all_reference_tokens.update(
            f"evidence:{reference}"
            for reference in item.related_evidence_ids
        )
    if result.current_state is not None:
        all_reference_tokens.update(
            f"state_evidence:{item}"
            for item in result.current_state.evidence_ids
        )
        all_reference_tokens.update(
            f"state_exposure:{item}"
            for item in result.current_state.exposure_ids
        )
        all_reference_tokens.update(
            f"state_event:{item}"
            for item in result.current_state.event_ids
        )
    if retrieval is not None:
        all_reference_tokens.update(
            f"library_pattern:{item.pattern_ref}"
            for item in retrieval.matches
            + retrieval.counter_patterns
        )
    total_refs = len(all_reference_tokens)
    valid_refs = len(all_reference_tokens - unknown)
    metrics.append(
        _metric(
            "grounding_reference_coverage",
            category,
            value=_ratio(valid_refs, total_refs),
            expected=1.0,
            passed=reference_valid,
            hard_gate=True,
            higher_is_better=True,
            unit="coverage_ratio",
        )
    )
    hard_codes = _hard_codes(issues)
    seed = GroundingEvaluation(
        evaluation_id=(
            "grounding_" + _hash_payload(run.content_hash)[:20]
        ),
        evaluation_run_id=run.evaluation_run_id,
        referenced_evidence_ids=tuple(sorted(evidence_refs)),
        referenced_exposure_types=tuple(sorted(exposure_refs)),
        referenced_event_ids=tuple(sorted(event_refs & event_ids)),
        referenced_pattern_refs=tuple(sorted(pattern_refs)),
        unknown_reference_ids=tuple(sorted(unknown)),
        metrics=tuple(sorted(metrics, key=lambda item: item.metric_id)),
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
        hard_failure_codes=hard_codes,
        passed_hard_gates=not hard_codes,
    )
    return _finalize_component(
        seed,
        grounding_evaluation_content_hash,
    )


def _evaluate_retrieval(
    run: EvaluationRun,
) -> RetrievalQualityEvaluation:
    category = EvaluationCategory.RETRIEVAL
    orchestration_input = run.orchestration_input
    result = run.orchestration_result
    retrieval = result.pattern_retrieval_result
    scenario_set = result.current_scenario_set
    metrics: list[EvaluationMetric] = []
    issues: list[EvaluationIssue] = []
    primary_refs: tuple[str, ...] = ()
    counter_refs: tuple[str, ...] = ()
    used_refs: tuple[str, ...] = ()
    if retrieval is None:
        for metric_id in (
            "retrieval_hash_validity",
            "retrieval_ordering",
            "primary_pattern_grounding",
            "counter_pattern_grounding",
        ):
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=None,
                    expected=True,
                    passed=None,
                )
            )
    else:
        primary_refs = tuple(
            item.pattern_ref for item in retrieval.matches
        )
        counter_refs = tuple(
            item.pattern_ref for item in retrieval.counter_patterns
        )
        used = {
            pattern_ref
            for scenario in (
                scenario_set.scenarios
                if scenario_set is not None
                else ()
            )
            for pattern_ref in (
                scenario.supporting_pattern_refs
                + scenario.counter_pattern_refs
            )
        }
        used_refs = tuple(sorted(used))
        library_by_ref = {
            item.pattern_ref: item
            for item in orchestration_input.historical_pattern_library.patterns
        }
        hash_valid = (
            retrieval.content_hash
            == pattern_retrieval_result_content_hash(retrieval)
            and retrieval.query.current_state_hash
            == (
                result.current_state.content_hash
                if result.current_state is not None
                else ""
            )
            and retrieval.query.library_hash
            == orchestration_input.historical_pattern_library.content_hash
            and all(
                item.pattern_ref in library_by_ref
                and item.pattern_hash
                == library_by_ref[item.pattern_ref].content_hash
                for item in (
                    retrieval.matches
                    + retrieval.counter_patterns
                )
            )
        )
        metrics.append(
            _metric(
                "retrieval_hash_validity",
                category,
                value=hash_valid,
                expected=True,
                passed=hash_valid,
                hard_gate=True,
            )
        )
        if not hash_valid:
            issues.append(
                _issue(
                    HARD_FAILURE_HASH_CONTINUITY,
                    category,
                    "pattern_retrieval_result",
                    "Retrieval result, state, library, or pattern hashes are inconsistent.",
                    hard=True,
                )
            )
        primary_order = tuple(
            item.rank for item in retrieval.matches
        )
        counter_order = tuple(
            item.rank for item in retrieval.counter_patterns
        )
        ordering_ok = (
            primary_order
            == tuple(range(1, len(primary_order) + 1))
            and counter_order
            == tuple(range(1, len(counter_order) + 1))
            and all(
                item.lane is RetrievalLane.PRIMARY
                for item in retrieval.matches
            )
            and all(
                item.lane is RetrievalLane.COUNTER
                for item in retrieval.counter_patterns
            )
        )
        metrics.append(
            _metric(
                "retrieval_ordering",
                category,
                value=ordering_ok,
                expected=True,
                passed=ordering_ok,
            )
        )
        if not ordering_ok:
            issues.append(
                _issue(
                    "retrieval_ordering_invalid",
                    category,
                    "pattern_retrieval_result",
                    "Primary or counter-pattern ranks and lanes are inconsistent.",
                )
            )
        primary_used = {
            item
            for scenario in (
                scenario_set.scenarios
                if scenario_set is not None
                else ()
            )
            for item in scenario.supporting_pattern_refs
        }
        counter_used = {
            item
            for scenario in (
                scenario_set.scenarios
                if scenario_set is not None
                else ()
            )
            for item in scenario.counter_pattern_refs
        }
        primary_grounding = (
            not primary_refs
            or bool(primary_used)
            and primary_used <= set(primary_refs)
        )
        counter_grounding = (
            not counter_refs
            or bool(counter_used)
            and counter_used <= set(counter_refs)
        )
        metrics.extend(
            (
                _metric(
                    "primary_pattern_grounding",
                    category,
                    value=_ratio(
                        len(primary_used & set(primary_refs)),
                        len(primary_refs),
                    ),
                    expected=1.0 if primary_refs else 0.0,
                    passed=primary_grounding,
                    hard_gate=bool(primary_refs),
                    higher_is_better=True,
                    unit="coverage_ratio",
                ),
                _metric(
                    "counter_pattern_grounding",
                    category,
                    value=_ratio(
                        len(counter_used & set(counter_refs)),
                        len(counter_refs),
                    ),
                    expected=1.0 if counter_refs else 0.0,
                    passed=counter_grounding,
                    hard_gate=bool(counter_refs),
                    higher_is_better=True,
                    unit="coverage_ratio",
                ),
            )
        )
        if not primary_grounding or not counter_grounding:
            issues.append(
                _issue(
                    HARD_FAILURE_INVENTED_REFERENCE,
                    category,
                    "scenario_pattern_grounding",
                    "Scenario pattern grounding uses an unknown lane or omits an available required lane.",
                    hard=True,
                )
            )
        if (
            not retrieval.matches
            and any(
                item.stage
                is OrchestrationStage.SCENARIO_GENERATION
                for item in result.stage_records
            )
        ):
            issues.append(
                _issue(
                    HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE,
                    category,
                    "stage_records",
                    "Scenario generation ran without an eligible primary pattern.",
                    hard=True,
                )
            )
    hard_codes = _hard_codes(issues)
    seed = RetrievalQualityEvaluation(
        evaluation_id=(
            "retrieval_" + _hash_payload(run.content_hash)[:20]
        ),
        evaluation_run_id=run.evaluation_run_id,
        primary_pattern_refs=primary_refs,
        counter_pattern_refs=counter_refs,
        used_pattern_refs=used_refs,
        metrics=tuple(sorted(metrics, key=lambda item: item.metric_id)),
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
        hard_failure_codes=hard_codes,
        passed_hard_gates=not hard_codes,
    )
    return _finalize_component(
        seed,
        retrieval_quality_evaluation_content_hash,
    )


def _chain_complete(
    scenario: CurrentScenarioNarrative,
) -> bool:
    steps = scenario.timeline_sequence
    if len(steps) < 3:
        return False
    if tuple(item.step_number for item in steps) != tuple(
        range(1, len(steps) + 1)
    ):
        return False
    step_types = {item.step_type for item in steps}
    has_start = (
        ScenarioStepType.PRECONDITION in step_types
        or ScenarioStepType.TRIGGER in step_types
    )
    has_effect = bool(
        step_types
        & {
            ScenarioStepType.INITIAL_REPRICING,
            ScenarioStepType.AMPLIFICATION,
            ScenarioStepType.CONTINUATION,
            ScenarioStepType.EXHAUSTION,
            ScenarioStepType.REVERSAL,
            ScenarioStepType.INVALIDATION,
        }
    )
    details_complete = all(
        bool(item.description.strip())
        and bool(item.expected_market_effect.strip())
        and bool(item.timing_relation.strip())
        and bool(item.uncertainty.strip())
        and bool(
            item.current_evidence_ids
            or item.current_exposure_types
            or item.event_ids
            or item.hypothetical_event_ids
            or item.assumptions
        )
        for item in steps
    )
    return has_start and has_effect and details_complete


def _evaluate_scenarios(
    run: EvaluationRun,
) -> ScenarioQualityEvaluation:
    category = EvaluationCategory.SCENARIO_QUALITY
    orchestration_input = run.orchestration_input
    result = run.orchestration_result
    scenario_set = result.current_scenario_set
    retrieval = result.pattern_retrieval_result
    state = result.current_state
    metrics: list[EvaluationMetric] = []
    issues: list[EvaluationIssue] = []
    scenario_ids: tuple[str, ...] = ()
    signatures: dict[str, str] = {}

    if scenario_set is None:
        for metric_id in (
            "scenario_contract_validity",
            "causal_chain_completeness",
            "scenario_chain_distinctness",
            "scenario_type_diversity",
            "contradiction_coverage",
            "missing_evidence_disclosure",
            "invalidation_coverage",
        ):
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=None,
                    expected=True,
                    passed=None,
                )
            )
    else:
        scenarios = scenario_set.scenarios
        scenario_ids = tuple(
            item.scenario_id for item in scenarios
        )
        signatures = {
            item.scenario_id: _scenario_chain_signature(item)
            for item in scenarios
        }
        contract_valid = False
        if state is not None and retrieval is not None:
            try:
                validation = validate_current_scenario_set(
                    scenario_set,
                    premise=(
                        orchestration_input.frozen_technical_premise
                    ),
                    state=state,
                    retrieval=retrieval,
                    evidence=orchestration_input.current_evidence,
                    exposures=orchestration_input.current_exposures,
                    events=orchestration_input.current_events,
                )
                contract_valid = (
                    validation.is_valid
                    and scenario_set.validation_result == validation
                    and scenario_set.content_hash
                    == current_scenario_set_content_hash(
                        scenario_set
                    )
                    and all(
                        item.content_hash
                        == current_scenario_narrative_content_hash(
                            item
                        )
                        for item in scenarios
                    )
                )
            except Exception:
                contract_valid = False
        metrics.append(
            _metric(
                "scenario_contract_validity",
                category,
                value=contract_valid,
                expected=True,
                passed=contract_valid,
                hard_gate=True,
            )
        )
        if not contract_valid:
            issues.append(
                _issue(
                    HARD_FAILURE_HASH_CONTINUITY,
                    category,
                    "current_scenario_set",
                    "Scenario-set validation or content hashes are inconsistent.",
                    hard=True,
                )
            )

        complete_count = sum(
            _chain_complete(item) for item in scenarios
        )
        chain_complete = complete_count == len(scenarios)
        metrics.append(
            _metric(
                "causal_chain_completeness",
                category,
                value=_ratio(complete_count, len(scenarios)),
                expected=1.0,
                passed=chain_complete,
                higher_is_better=True,
                unit="coverage_ratio",
            )
        )
        if not chain_complete:
            issues.append(
                _issue(
                    "weak_causal_chain",
                    category,
                    "current_scenario_set.scenarios",
                    "One or more scenarios lack a complete referenced causal chain.",
                    refs=tuple(
                        item.scenario_id
                        for item in scenarios
                        if not _chain_complete(item)
                    ),
                )
            )

        duplicate_signatures = _duplicates(
            tuple(signatures.values())
        )
        duplicate_scenarios = tuple(
            sorted(
                scenario_id
                for scenario_id, signature in signatures.items()
                if signature in duplicate_signatures
            )
        )
        distinct = not duplicate_signatures
        metrics.append(
            _metric(
                "scenario_chain_distinctness",
                category,
                value=len(duplicate_scenarios),
                expected=0,
                passed=distinct,
                higher_is_better=False,
                unit="duplicate_scenario_count",
                notes=duplicate_scenarios,
            )
        )
        if not distinct:
            issues.append(
                _issue(
                    "duplicate_causal_chain",
                    category,
                    "current_scenario_set.scenarios",
                    "Two or more scenarios use the same normalized causal chain.",
                    refs=duplicate_scenarios,
                )
            )

        unique_types = {
            item.scenario_type for item in scenarios
        }
        diversity_ok = (
            len(unique_types) >= min(3, len(scenarios))
            and ScenarioType.MULTI_FACTOR in unique_types
            and ScenarioType.TECHNICAL_INVALIDATION in unique_types
        )
        metrics.append(
            _metric(
                "scenario_type_diversity",
                category,
                value=len(unique_types),
                expected=min(3, len(scenarios)),
                passed=diversity_ok,
                higher_is_better=True,
                unit="unique_type_count",
            )
        )
        if not diversity_ok:
            issues.append(
                _issue(
                    "scenario_diversity_insufficient",
                    category,
                    "current_scenario_set.scenarios",
                    "Scenario set lacks required structural diversity.",
                )
            )

        coverage_checks = (
            (
                "contradiction_coverage",
                lambda item: bool(
                    item.contradicting_current_evidence_ids
                ),
                False,
                "Scenario does not disclose contradicting current evidence.",
            ),
            (
                "missing_evidence_disclosure",
                lambda item: bool(item.missing_evidence),
                False,
                "Scenario does not disclose missing evidence.",
            ),
            (
                "invalidation_coverage",
                lambda item: bool(item.invalidation_conditions),
                True,
                "Scenario does not provide an invalidation condition.",
            ),
        )
        for metric_id, predicate, hard, message in coverage_checks:
            count = sum(predicate(item) for item in scenarios)
            passed = count == len(scenarios)
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=_ratio(count, len(scenarios)),
                    expected=1.0,
                    passed=passed,
                    hard_gate=hard,
                    higher_is_better=True,
                    unit="coverage_ratio",
                )
            )
            if not passed:
                issues.append(
                    _issue(
                        (
                            HARD_FAILURE_MISSING_INVALIDATION
                            if hard
                            else metric_id + "_incomplete"
                        ),
                        category,
                        f"current_scenario_set.{metric_id}",
                        message,
                        hard=hard,
                        refs=tuple(
                            item.scenario_id
                            for item in scenarios
                            if not predicate(item)
                        ),
                    )
                )

    hard_codes = _hard_codes(issues)
    seed = ScenarioQualityEvaluation(
        evaluation_id=(
            "scenario_quality_"
            + _hash_payload(run.content_hash)[:20]
        ),
        evaluation_run_id=run.evaluation_run_id,
        evaluated_scenario_ids=scenario_ids,
        causal_chain_signatures=signatures,
        metrics=tuple(sorted(metrics, key=lambda item: item.metric_id)),
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
        hard_failure_codes=hard_codes,
        passed_hard_gates=not hard_codes,
    )
    return _finalize_component(
        seed,
        scenario_quality_evaluation_content_hash,
    )


def _audit_dangling_edges(
    graph: ReasoningAuditGraph,
) -> tuple[str, ...]:
    node_ids = {item.node_id for item in graph.nodes}
    return tuple(
        sorted(
            item.edge_id
            for item in graph.edges
            if item.source_node_id not in node_ids
            or item.target_node_id not in node_ids
        )
    )


def _evaluate_audit_graph(
    run: EvaluationRun,
) -> AuditGraphEvaluation:
    category = EvaluationCategory.AUDIT_GRAPH
    result = run.orchestration_result
    graph = result.reasoning_audit_graph
    scenario_set = result.current_scenario_set
    metrics: list[EvaluationMetric] = []
    issues: list[EvaluationIssue] = []
    graph_id: str | None = None
    node_count = 0
    edge_count = 0
    orphans: tuple[str, ...] = ()
    dangling: tuple[str, ...] = ()

    if graph is None:
        required = scenario_set is not None
        metrics.append(
            _metric(
                "audit_graph_availability",
                category,
                value=False,
                expected=required,
                passed=(not required),
                hard_gate=required,
            )
        )
        if required:
            issues.append(
                _issue(
                    HARD_FAILURE_AUDIT_INCOMPLETE,
                    category,
                    "reasoning_audit_graph",
                    "A retained scenario set has no reasoning audit graph.",
                    hard=True,
                )
            )
        for metric_id in (
            "audit_graph_validity",
            "audit_node_completeness",
            "audit_edge_completeness",
            "audit_orphan_count",
            "audit_dangling_count",
        ):
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=None,
                    expected=True,
                    passed=None,
                )
            )
    else:
        graph_id = graph.graph_id
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)
        orphans = graph.orphan_node_ids
        dangling = _audit_dangling_edges(graph)
        try:
            validation = validate_reasoning_audit_graph(
                graph,
                scenario_set=scenario_set,
            )
            graph_valid = (
                validation.is_valid
                and graph.validation_result == validation
                and graph.content_hash
                == reasoning_audit_graph_content_hash(graph)
                and all(
                    item.content_hash
                    == reasoning_audit_node_content_hash(item)
                    for item in graph.nodes
                )
                and all(
                    item.content_hash
                    == reasoning_audit_edge_content_hash(item)
                    for item in graph.edges
                )
            )
        except Exception:
            graph_valid = False
        metrics.append(
            _metric(
                "audit_graph_validity",
                category,
                value=graph_valid,
                expected=True,
                passed=graph_valid,
                hard_gate=True,
            )
        )
        if not graph_valid:
            issues.append(
                _issue(
                    HARD_FAILURE_HASH_CONTINUITY,
                    category,
                    "reasoning_audit_graph",
                    "Reasoning audit graph schema, validation, or hashes are inconsistent.",
                    hard=True,
                )
            )

        node_types = {item.node_type for item in graph.nodes}
        required_node_types = {
            AuditNodeType.FROZEN_TECHNICAL_PREMISE,
            AuditNodeType.CURRENT_MARKET_STATE,
        }
        if scenario_set is not None:
            required_node_types.update(
                {
                    AuditNodeType.SCENARIO,
                    AuditNodeType.SCENARIO_STEP,
                    AuditNodeType.INVALIDATION_CONDITION,
                }
            )
        node_complete = required_node_types <= node_types
        required_edge_types = {
            AuditEdgeType.DERIVED_FROM,
        }
        if scenario_set is not None:
            required_edge_types.update(
                {
                    AuditEdgeType.REQUIRES,
                    AuditEdgeType.INVALIDATES,
                    AuditEdgeType.FOLLOWS,
                }
            )
        edge_types = {item.edge_type for item in graph.edges}
        edge_complete = required_edge_types <= edge_types
        metrics.extend(
            (
                _metric(
                    "audit_node_completeness",
                    category,
                    value=_ratio(
                        len(required_node_types & node_types),
                        len(required_node_types),
                    ),
                    expected=1.0,
                    passed=node_complete,
                    hard_gate=True,
                    higher_is_better=True,
                    unit="coverage_ratio",
                ),
                _metric(
                    "audit_edge_completeness",
                    category,
                    value=_ratio(
                        len(required_edge_types & edge_types),
                        len(required_edge_types),
                    ),
                    expected=1.0,
                    passed=edge_complete,
                    hard_gate=True,
                    higher_is_better=True,
                    unit="coverage_ratio",
                ),
                _metric(
                    "audit_orphan_count",
                    category,
                    value=len(orphans),
                    expected=0,
                    passed=not orphans,
                    higher_is_better=False,
                    unit="node_count",
                    notes=orphans,
                ),
                _metric(
                    "audit_dangling_count",
                    category,
                    value=len(dangling),
                    expected=0,
                    passed=not dangling,
                    hard_gate=True,
                    higher_is_better=False,
                    unit="edge_count",
                    notes=dangling,
                ),
            )
        )
        if not node_complete or not edge_complete:
            issues.append(
                _issue(
                    HARD_FAILURE_AUDIT_INCOMPLETE,
                    category,
                    "reasoning_audit_graph",
                    "Required audit node or edge types are absent.",
                    hard=True,
                )
            )
        if orphans:
            issues.append(
                _issue(
                    "orphan_audit_node",
                    category,
                    "reasoning_audit_graph.orphan_node_ids",
                    "Reasoning audit graph contains orphan nodes.",
                    refs=orphans,
                )
            )
        if dangling:
            issues.append(
                _issue(
                    HARD_FAILURE_DANGLING_AUDIT,
                    category,
                    "reasoning_audit_graph.edges",
                    "Reasoning audit graph contains dangling references.",
                    hard=True,
                    refs=dangling,
                )
            )
        if result.audit_coverage_metrics is not None:
            metrics_hash_valid = (
                result.audit_coverage_metrics.content_hash
                == audit_coverage_metrics_content_hash(
                    result.audit_coverage_metrics
                )
                and result.audit_coverage_metrics.orphan_node_count
                == len(orphans)
                and result.audit_coverage_metrics.dangling_reference_count
                == len(dangling)
            )
            metrics.append(
                _metric(
                    "audit_coverage_hash_validity",
                    category,
                    value=metrics_hash_valid,
                    expected=True,
                    passed=metrics_hash_valid,
                    hard_gate=True,
                )
            )
            if not metrics_hash_valid:
                issues.append(
                    _issue(
                        HARD_FAILURE_HASH_CONTINUITY,
                        category,
                        "audit_coverage_metrics",
                        "Stored audit coverage does not match the graph.",
                        hard=True,
                    )
                )

    hard_codes = _hard_codes(issues)
    seed = AuditGraphEvaluation(
        evaluation_id=(
            "audit_graph_" + _hash_payload(run.content_hash)[:20]
        ),
        evaluation_run_id=run.evaluation_run_id,
        graph_id=graph_id,
        node_count=node_count,
        edge_count=edge_count,
        orphan_node_ids=orphans,
        dangling_edge_ids=dangling,
        metrics=tuple(sorted(metrics, key=lambda item: item.metric_id)),
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
        hard_failure_codes=hard_codes,
        passed_hard_gates=not hard_codes,
    )
    return _finalize_component(
        seed,
        audit_graph_evaluation_content_hash,
    )


def _evaluate_reproducibility(
    run: EvaluationRun,
    *,
    replay_attempts: int = 2,
) -> ReproducibilityEvaluation:
    category = EvaluationCategory.REPRODUCIBILITY
    packet = run.replay_packet
    metrics: list[EvaluationMetric] = []
    issues: list[EvaluationIssue] = []
    observed_final_hashes: list[str] = []
    observed_stage_hashes: dict[str, str] = {}
    expected_stage_hashes: Mapping[str, str] = {}
    expected_final_hash: str | None = None

    if packet is None:
        for metric_id in (
            "replay_packet_validity",
            "provider_fixture_reproducibility",
            "repeated_run_stability",
            "evaluated_result_replay_match",
        ):
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=None,
                    expected=True,
                    passed=None,
                    notes=(
                        "No replay packet was supplied; reproducibility was not evaluated.",
                    ),
                )
            )
        attempt_count = 0
    else:
        if replay_attempts < 2:
            raise ValueError(
                "replay_attempts must be at least 2 when a replay packet is supplied."
            )
        expected_stage_hashes = packet.expected_stage_hashes
        expected_final_hash = packet.expected_final_hash
        packet_validation = validate_market_scenario_replay_packet(
            packet
        )
        packet_valid = (
            packet_validation.is_valid
            and packet.content_hash
            == market_scenario_replay_packet_content_hash(packet)
            and packet.orchestration_input.content_hash
            == run.orchestration_input.content_hash
        )
        metrics.append(
            _metric(
                "replay_packet_validity",
                category,
                value=packet_valid,
                expected=True,
                passed=packet_valid,
                hard_gate=True,
            )
        )
        if not packet_valid:
            issues.append(
                _issue(
                    HARD_FAILURE_NON_REPRODUCIBLE,
                    category,
                    "replay_packet",
                    "Replay packet validation, hash, or evaluated-input linkage failed.",
                    hard=True,
                )
            )

        replay_results = []
        if packet_valid:
            replay_clock = _temporal_point(run.created_at)
            if replay_clock is None:
                packet_valid = False
            else:
                for _ in range(replay_attempts):
                    replay_results.append(
                        replay_market_scenario_packet(
                            packet,
                            clock=lambda point=replay_clock: point,
                        )
                    )

        for attempt, replay in enumerate(replay_results, start=1):
            if replay.actual_final_hash is not None:
                observed_final_hashes.append(
                    replay.actual_final_hash
                )
            for stage, value in replay.actual_stage_hashes.items():
                observed_stage_hashes[
                    f"attempt_{attempt}:{stage}"
                ] = value

        provider_reproducible = bool(replay_results) and all(
            item.success
            and item.validation_result.is_valid
            and not item.mismatch_codes
            and item.remaining_fixture_responses == 0
            for item in replay_results
        )
        final_stable = (
            len(observed_final_hashes) == replay_attempts
            and len(set(observed_final_hashes)) == 1
        )
        stage_snapshots = tuple(
            tuple(sorted(item.actual_stage_hashes.items()))
            for item in replay_results
        )
        stages_stable = (
            len(stage_snapshots) == replay_attempts
            and len(set(stage_snapshots)) == 1
        )
        repeated_stable = final_stable and stages_stable
        evaluated_stage_hashes = {
            item.stage.value: item.analytical_hash
            for item in run.orchestration_result.stage_records
        }
        evaluated_result_match = (
            bool(replay_results)
            and all(
                item.actual_final_hash
                == run.orchestration_result.analytical_hash
                and item.actual_stage_hashes
                == evaluated_stage_hashes
                for item in replay_results
            )
        )

        for metric_id, passed in (
            (
                "provider_fixture_reproducibility",
                provider_reproducible,
            ),
            ("repeated_run_stability", repeated_stable),
            (
                "evaluated_result_replay_match",
                evaluated_result_match,
            ),
        ):
            metrics.append(
                _metric(
                    metric_id,
                    category,
                    value=passed,
                    expected=True,
                    passed=passed,
                    hard_gate=True,
                )
            )
            if not passed:
                issues.append(
                    _issue(
                        HARD_FAILURE_NON_REPRODUCIBLE,
                        category,
                        metric_id,
                        (
                            "Offline fixture replay did not reproduce "
                            "the expected and evaluated analytical hashes."
                        ),
                        hard=True,
                    )
                )
        attempt_count = len(replay_results)

    hard_codes = _hard_codes(issues)
    seed = ReproducibilityEvaluation(
        evaluation_id=(
            "reproducibility_"
            + _hash_payload(run.content_hash)[:20]
        ),
        evaluation_run_id=run.evaluation_run_id,
        replay_attempt_count=attempt_count,
        expected_final_hash=expected_final_hash,
        observed_final_hashes=tuple(observed_final_hashes),
        expected_stage_hashes=expected_stage_hashes,
        observed_stage_hashes=observed_stage_hashes,
        metrics=tuple(sorted(metrics, key=lambda item: item.metric_id)),
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
        hard_failure_codes=hard_codes,
        passed_hard_gates=not hard_codes,
    )
    return _finalize_component(
        seed,
        reproducibility_evaluation_content_hash,
    )


def _component_sequence(
    suite: EvaluationSuiteResult,
) -> tuple[Any, ...]:
    components: tuple[Any, ...] = (
        suite.structural_evaluation,
        suite.scenario_quality_evaluation,
        suite.retrieval_quality_evaluation,
        suite.grounding_evaluation,
        suite.audit_graph_evaluation,
        suite.reproducibility_evaluation,
    )
    if suite.regression_comparison is not None:
        return components + (suite.regression_comparison,)
    return components


def _version_manifest(
    run: EvaluationRun,
) -> Mapping[str, str]:
    orchestration_input = run.orchestration_input
    result = run.orchestration_result
    manifest = {
        "evaluator": run.evaluator_version,
        "evaluation_run_schema": run.schema_version,
        "orchestration_input_schema": (
            orchestration_input.schema_version
        ),
        "orchestration_result_schema": result.schema_version,
        "orchestrator": MARKET_SCENARIO_ORCHESTRATOR_VERSION,
        "retrieval_scoring_profile": (
            orchestration_input.retrieval_configuration.scoring_profile_version
        ),
        "scenario_prompt": (
            orchestration_input.scenario_generation_configuration.prompt_version
        ),
        "model": result.model_name,
        "provider": result.provider,
        "reasoning_audit_schema": (
            result.reasoning_audit_graph.schema_version
            if result.reasoning_audit_graph is not None
            else REASONING_AUDIT_GRAPH_SCHEMA_VERSION
        ),
    }
    for key, value in result.prompt_versions.items():
        manifest[f"result_prompt:{key}"] = value
    if result.current_state is not None:
        manifest["current_state_schema"] = (
            result.current_state.schema_version
        )
    if result.pattern_retrieval_result is not None:
        manifest["retrieval_result_schema"] = (
            result.pattern_retrieval_result.schema_version
        )
    if result.current_scenario_set is not None:
        manifest["scenario_set_schema"] = (
            result.current_scenario_set.schema_version
        )
    return MappingProxyType(dict(sorted(manifest.items())))


def _metric_snapshot(
    metrics: Sequence[EvaluationMetric],
) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            f"{item.category.value}.{item.metric_id}": {
                "value": _json_value(item.value),
                "status": item.status.value,
                "hard_gate": item.hard_gate,
                "higher_is_better": item.higher_is_better,
                "unit": item.unit,
            }
            for item in sorted(
                metrics,
                key=lambda value: (
                    value.category.value,
                    value.metric_id,
                ),
            )
        }
    )


def _retrieval_order(
    run: EvaluationRun,
) -> tuple[str, ...]:
    retrieval = run.orchestration_result.pattern_retrieval_result
    if retrieval is None:
        return ()
    return tuple(
        [f"primary:{item.pattern_ref}" for item in retrieval.matches]
        + [
            f"counter:{item.pattern_ref}"
            for item in retrieval.counter_patterns
        ]
    )


def _pattern_usage(
    run: EvaluationRun,
) -> Mapping[str, Any]:
    scenario_set = run.orchestration_result.current_scenario_set
    if scenario_set is None:
        return MappingProxyType({})
    return MappingProxyType(
        {
            scenario.scenario_id: {
                "supporting": tuple(
                    scenario.supporting_pattern_refs
                ),
                "counter": tuple(scenario.counter_pattern_refs),
            }
            for scenario in sorted(
                scenario_set.scenarios,
                key=lambda item: item.scenario_id,
            )
        }
    )


def _audit_coverage_snapshot(
    suite: EvaluationSuiteResult,
) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            item.metric_id: _json_value(item.value)
            for item in suite.audit_graph_evaluation.metrics
        }
    )


def _content_hash_snapshot(
    run: EvaluationRun,
    suite: EvaluationSuiteResult,
) -> Mapping[str, str]:
    result = run.orchestration_result
    hashes: dict[str, str] = {
        "evaluation_run": run.content_hash,
        "orchestration_input": run.orchestration_input.content_hash,
        "orchestration_result": result.content_hash,
        "orchestration_analytical": result.analytical_hash,
        "technical_premise": (
            run.orchestration_input.frozen_technical_premise.frozen_content_hash
        ),
        "evaluation_suite": suite.content_hash,
    }
    optional_values = (
        ("current_state", result.current_state),
        ("pattern_retrieval", result.pattern_retrieval_result),
        ("current_scenario_set", result.current_scenario_set),
        ("reasoning_audit_graph", result.reasoning_audit_graph),
    )
    for key, value in optional_values:
        if value is not None:
            hashes[key] = value.content_hash
    return MappingProxyType(dict(sorted(hashes.items())))


def _scenario_ids(
    run: EvaluationRun,
) -> tuple[str, ...]:
    scenario_set = run.orchestration_result.current_scenario_set
    return (
        tuple(item.scenario_id for item in scenario_set.scenarios)
        if scenario_set is not None
        else ()
    )


def _finalize_suite(
    run: EvaluationRun,
    *,
    structural: StructuralEvaluationResult,
    scenario_quality: ScenarioQualityEvaluation,
    retrieval: RetrievalQualityEvaluation,
    grounding: GroundingEvaluation,
    audit_graph: AuditGraphEvaluation,
    reproducibility: ReproducibilityEvaluation,
    regression: RegressionComparison | None,
) -> EvaluationSuiteResult:
    components: tuple[Any, ...] = (
        structural,
        scenario_quality,
        retrieval,
        grounding,
        audit_graph,
        reproducibility,
    ) + ((regression,) if regression is not None else ())
    metrics = tuple(
        sorted(
            (
                metric
                for component in components
                for metric in component.metrics
            ),
            key=lambda item: (
                item.category.value,
                item.metric_id,
            ),
        )
    )
    issues = tuple(
        sorted(
            (
                issue
                for component in components
                for issue in component.issues
            ),
            key=lambda item: item.issue_id,
        )
    )
    hard_codes = _hard_codes(issues)
    status = (
        EvaluationSuiteStatus.FAILED
        if hard_codes
        else (
            EvaluationSuiteStatus.PASSED_WITH_WARNINGS
            if issues
            or any(
                item.status
                in {
                    EvaluationMetricStatus.WARNING,
                    EvaluationMetricStatus.UNAVAILABLE,
                }
                for item in metrics
            )
            else EvaluationSuiteStatus.PASSED
        )
    )
    seed = EvaluationSuiteResult(
        evaluation_suite_id=(
            "evaluation_suite_"
            + _hash_payload(
                {
                    "run_hash": run.content_hash,
                    "regression_baseline": (
                        regression.baseline_id
                        if regression is not None
                        else None
                    ),
                }
            )[:20]
        ),
        evaluation_run_id=run.evaluation_run_id,
        status=status,
        structural_evaluation=structural,
        scenario_quality_evaluation=scenario_quality,
        retrieval_quality_evaluation=retrieval,
        grounding_evaluation=grounding,
        audit_graph_evaluation=audit_graph,
        reproducibility_evaluation=reproducibility,
        regression_comparison=regression,
        metrics=metrics,
        issues=issues,
        hard_failure_codes=hard_codes,
        generated_at=run.created_at,
        evaluator_version=run.evaluator_version,
    )
    return replace(
        seed,
        content_hash=evaluation_suite_result_content_hash(seed),
    )


def create_regression_baseline(
    suite: EvaluationSuiteResult,
    run: EvaluationRun,
    *,
    baseline_id: str | None = None,
    created_at: str | datetime | None = None,
) -> RegressionBaseline:
    if not isinstance(suite, EvaluationSuiteResult):
        raise TypeError("suite must be EvaluationSuiteResult.")
    if not isinstance(run, EvaluationRun):
        raise TypeError("run must be EvaluationRun.")
    if suite.evaluation_run_id != run.evaluation_run_id:
        raise ValueError(
            "suite and run must reference the same evaluation run."
        )
    selected_id = baseline_id or (
        "regression_baseline_"
        + _hash_payload(
            {
                "suite_hash": suite.content_hash,
                "run_hash": run.content_hash,
            }
        )[:20]
    )
    seed = RegressionBaseline(
        baseline_id=selected_id,
        source_evaluation_run_id=run.evaluation_run_id,
        orchestration_input_hash=run.orchestration_input.content_hash,
        orchestration_result_hash=(
            run.orchestration_result.analytical_hash
        ),
        evaluation_result_hash=suite.content_hash,
        version_manifest=_version_manifest(run),
        metric_snapshot=_metric_snapshot(suite.metrics),
        hard_failure_codes=suite.hard_failure_codes,
        scenario_ids=_scenario_ids(run),
        retrieval_order=_retrieval_order(run),
        pattern_usage=_pattern_usage(run),
        audit_coverage=_audit_coverage_snapshot(suite),
        content_hashes=_content_hash_snapshot(run, suite),
        created_at=created_at or suite.generated_at,
    )
    return replace(
        seed,
        content_hash=regression_baseline_content_hash(seed),
    )


_STATUS_RANK = MappingProxyType(
    {
        EvaluationMetricStatus.FAILED.value: 0,
        EvaluationMetricStatus.UNAVAILABLE.value: 1,
        EvaluationMetricStatus.WARNING.value: 2,
        EvaluationMetricStatus.PASSED.value: 3,
    }
)


def _metric_change_direction(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> int:
    baseline_status = str(baseline.get("status", "unavailable"))
    current_status = str(current.get("status", "unavailable"))
    status_change = (
        _STATUS_RANK.get(current_status, 1)
        - _STATUS_RANK.get(baseline_status, 1)
    )
    if status_change:
        return 1 if status_change > 0 else -1
    old = baseline.get("value")
    new = current.get("value")
    direction = current.get("higher_is_better")
    if (
        isinstance(old, (int, float))
        and not isinstance(old, bool)
        and isinstance(new, (int, float))
        and not isinstance(new, bool)
        and math.isfinite(float(old))
        and math.isfinite(float(new))
        and not math.isclose(
            float(old),
            float(new),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        and isinstance(direction, bool)
    ):
        improved = float(new) > float(old)
        if not direction:
            improved = not improved
        return 1 if improved else -1
    return 0


def _changed_value(
    baseline: Any,
    current: Any,
) -> Mapping[str, Any]:
    if _json_value(baseline) == _json_value(current):
        return MappingProxyType({})
    return MappingProxyType(
        {
            "baseline": _freeze_json(_json_value(baseline)),
            "current": _freeze_json(_json_value(current)),
        }
    )


def compare_regression_baseline(
    baseline: RegressionBaseline,
    current_suite: EvaluationSuiteResult,
    current_run: EvaluationRun,
) -> RegressionComparison:
    if not isinstance(baseline, RegressionBaseline):
        raise TypeError("baseline must be RegressionBaseline.")
    if not isinstance(current_suite, EvaluationSuiteResult):
        raise TypeError(
            "current_suite must be EvaluationSuiteResult."
        )
    if not isinstance(current_run, EvaluationRun):
        raise TypeError("current_run must be EvaluationRun.")
    if (
        baseline.content_hash
        != regression_baseline_content_hash(baseline)
    ):
        raise ValueError("baseline content hash is invalid.")

    current_metrics = _metric_snapshot(current_suite.metrics)
    deteriorated: dict[str, Any] = {}
    improved: dict[str, Any] = {}
    for metric_id in sorted(
        set(baseline.metric_snapshot) | set(current_metrics)
    ):
        old = baseline.metric_snapshot.get(metric_id)
        new = current_metrics.get(metric_id)
        if old is None or new is None:
            continue
        direction = _metric_change_direction(old, new)
        change = {
            "baseline": _json_value(old),
            "current": _json_value(new),
        }
        if direction < 0:
            deteriorated[metric_id] = change
        elif direction > 0:
            improved[metric_id] = change

    baseline_hard = set(baseline.hard_failure_codes)
    current_hard = set(current_suite.hard_failure_codes)
    newly_hard = tuple(sorted(current_hard - baseline_hard))
    resolved = tuple(sorted(baseline_hard - current_hard))
    scenario_count_change = _changed_value(
        len(baseline.scenario_ids),
        len(_scenario_ids(current_run)),
    )
    retrieval_change = _changed_value(
        baseline.retrieval_order,
        _retrieval_order(current_run),
    )
    pattern_change = _changed_value(
        baseline.pattern_usage,
        _pattern_usage(current_run),
    )
    audit_change = _changed_value(
        baseline.audit_coverage,
        _audit_coverage_snapshot(current_suite),
    )
    hash_change = _changed_value(
        baseline.content_hashes,
        _content_hash_snapshot(current_run, current_suite),
    )
    version_change = _changed_value(
        baseline.version_manifest,
        _version_manifest(current_run),
    )

    metrics = (
        _metric(
            "new_hard_failure_count",
            EvaluationCategory.REGRESSION,
            value=len(newly_hard),
            expected=0,
            passed=not newly_hard,
            hard_gate=True,
            higher_is_better=False,
            unit="failure_count",
            notes=newly_hard,
        ),
        _metric(
            "resolved_failure_count",
            EvaluationCategory.REGRESSION,
            value=len(resolved),
            expected=0,
            passed=True,
            higher_is_better=True,
            unit="failure_count",
            notes=resolved,
        ),
        _metric(
            "deteriorated_metric_count",
            EvaluationCategory.REGRESSION,
            value=len(deteriorated),
            expected=0,
            passed=not deteriorated,
            higher_is_better=False,
            unit="metric_count",
        ),
        _metric(
            "improved_metric_count",
            EvaluationCategory.REGRESSION,
            value=len(improved),
            expected=0,
            passed=True,
            higher_is_better=True,
            unit="metric_count",
        ),
    )
    issues: list[EvaluationIssue] = [
        _issue(
            code,
            EvaluationCategory.REGRESSION,
            "hard_failure_codes",
            "A hard-gate failure is newly present relative to the baseline.",
            hard=True,
            refs=(baseline.baseline_id,),
        )
        for code in newly_hard
    ]
    if deteriorated:
        issues.append(
            _issue(
                "metric_deterioration",
                EvaluationCategory.REGRESSION,
                "metric_snapshot",
                "One or more directional evaluation metrics deteriorated.",
                refs=tuple(sorted(deteriorated)),
            )
        )
    if resolved:
        issues.append(
            _issue(
                "resolved_regression_failure",
                EvaluationCategory.REGRESSION,
                "hard_failure_codes",
                "One or more baseline hard failures are resolved.",
                severity=EvaluationIssueSeverity.INFO,
                refs=resolved,
            )
        )
    seed = RegressionComparison(
        comparison_id=(
            "regression_comparison_"
            + _hash_payload(
                {
                    "baseline": baseline.content_hash,
                    "current": current_suite.content_hash,
                }
            )[:20]
        ),
        baseline_id=baseline.baseline_id,
        current_evaluation_run_id=current_run.evaluation_run_id,
        newly_introduced_hard_failures=newly_hard,
        resolved_hard_failures=resolved,
        deteriorated_metrics=deteriorated,
        improved_metrics=improved,
        changed_scenario_count=scenario_count_change,
        changed_retrieval_order=retrieval_change,
        changed_pattern_usage=pattern_change,
        changed_audit_coverage=audit_change,
        changed_content_hashes=hash_change,
        version_differences=version_change,
        metrics=tuple(
            sorted(metrics, key=lambda item: item.metric_id)
        ),
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
        hard_failure_codes=newly_hard,
        passed_hard_gates=not newly_hard,
    )
    return replace(
        seed,
        content_hash=regression_comparison_content_hash(seed),
    )


def _contract_validation_result(
    issues: Sequence[ValidationIssue],
    *,
    validation_version: str,
) -> ValidationResult:
    errors = tuple(
        item for item in issues if item.severity == "error"
    )
    warnings = tuple(
        item for item in issues if item.severity != "error"
    )
    failed = tuple(dict.fromkeys(item.code for item in errors))
    return ValidationResult(
        is_valid=not errors,
        score=100.0 if not errors else 0.0,
        errors=errors,
        warnings=warnings,
        failed_rules=failed,
        passed_rules=() if errors else ("evaluation_contract_valid",),
        validation_version=validation_version,
    )


def validate_evaluation_run(
    run: EvaluationRun,
) -> ValidationResult:
    if not isinstance(run, EvaluationRun):
        raise TypeError("run must be EvaluationRun.")
    issues: list[ValidationIssue] = []

    def require(
        code: str,
        condition: bool,
        path: str,
        message: str,
    ) -> None:
        if not condition:
            issues.append(
                ValidationIssue(
                    code=code,
                    severity="error",
                    path=path,
                    message=message,
                )
            )

    require(
        "evaluation_run_schema",
        run.schema_version == EVALUATION_RUN_SCHEMA_VERSION,
        "schema_version",
        "Evaluation-run schema version is unsupported.",
    )
    require(
        "evaluation_run_hash",
        bool(run.content_hash)
        and run.content_hash == evaluation_run_content_hash(run),
        "content_hash",
        "Evaluation-run content hash is invalid.",
    )
    require(
        "evaluation_run_identity",
        bool(run.evaluation_run_id.strip())
        and bool(run.evaluator_version.strip()),
        "evaluation_run_id",
        "Evaluation-run identity or evaluator version is missing.",
    )
    require(
        "evaluation_run_linkage",
        run.orchestration_result.orchestration_input_hash
        == run.orchestration_input.content_hash,
        "orchestration_result.orchestration_input_hash",
        "Evaluation run does not link its result to its input.",
    )
    if run.replay_packet is not None:
        require(
            "evaluation_replay_linkage",
            run.replay_packet.expected_input_hash
            == run.orchestration_input.content_hash,
            "replay_packet.expected_input_hash",
            "Replay packet does not link to the evaluated input.",
        )
    return _contract_validation_result(
        issues,
        validation_version="evaluation-run-validation-1.0.0",
    )


def validate_evaluation_suite_result(
    suite: EvaluationSuiteResult,
    *,
    run: EvaluationRun | None = None,
) -> ValidationResult:
    if not isinstance(suite, EvaluationSuiteResult):
        raise TypeError(
            "suite must be EvaluationSuiteResult."
        )
    issues: list[ValidationIssue] = []

    def require(
        code: str,
        condition: bool,
        path: str,
        message: str,
    ) -> None:
        if not condition:
            issues.append(
                ValidationIssue(
                    code=code,
                    severity="error",
                    path=path,
                    message=message,
                )
            )

    require(
        "evaluation_suite_schema",
        suite.schema_version
        == EVALUATION_SUITE_RESULT_SCHEMA_VERSION,
        "schema_version",
        "Evaluation-suite schema version is unsupported.",
    )
    require(
        "evaluation_suite_hash",
        bool(suite.content_hash)
        and suite.content_hash
        == evaluation_suite_result_content_hash(suite),
        "content_hash",
        "Evaluation-suite content hash is invalid.",
    )
    component_hash_functions = (
        (
            suite.structural_evaluation,
            structural_evaluation_content_hash,
        ),
        (
            suite.scenario_quality_evaluation,
            scenario_quality_evaluation_content_hash,
        ),
        (
            suite.retrieval_quality_evaluation,
            retrieval_quality_evaluation_content_hash,
        ),
        (
            suite.grounding_evaluation,
            grounding_evaluation_content_hash,
        ),
        (
            suite.audit_graph_evaluation,
            audit_graph_evaluation_content_hash,
        ),
        (
            suite.reproducibility_evaluation,
            reproducibility_evaluation_content_hash,
        ),
    )
    if suite.regression_comparison is not None:
        component_hash_functions += (
            (
                suite.regression_comparison,
                regression_comparison_content_hash,
            ),
        )
    require(
        "evaluation_component_hashes",
        all(
            component.content_hash == hash_function(component)
            for component, hash_function in component_hash_functions
        ),
        "components",
        "One or more evaluation-component hashes are invalid.",
    )
    require(
        "evaluation_leaf_hashes",
        all(
            item.content_hash == evaluation_metric_content_hash(item)
            for item in suite.metrics
        )
        and all(
            item.content_hash == evaluation_issue_content_hash(item)
            for item in suite.issues
        ),
        "metrics",
        "One or more metric or issue hashes are invalid.",
    )
    expected_metrics = tuple(
        sorted(
            (
                metric
                for component in _component_sequence(suite)
                for metric in component.metrics
            ),
            key=lambda item: (
                item.category.value,
                item.metric_id,
            ),
        )
    )
    expected_issues = tuple(
        sorted(
            (
                issue
                for component in _component_sequence(suite)
                for issue in component.issues
            ),
            key=lambda item: item.issue_id,
        )
    )
    require(
        "evaluation_component_aggregation",
        suite.metrics == expected_metrics
        and suite.issues == expected_issues
        and suite.hard_failure_codes == _hard_codes(expected_issues),
        "metrics",
        "Suite metric, issue, or hard-gate aggregation is inconsistent.",
    )
    expected_status = (
        EvaluationSuiteStatus.FAILED
        if suite.hard_failure_codes
        else (
            EvaluationSuiteStatus.PASSED_WITH_WARNINGS
            if suite.issues
            or any(
                item.status
                in {
                    EvaluationMetricStatus.WARNING,
                    EvaluationMetricStatus.UNAVAILABLE,
                }
                for item in suite.metrics
            )
            else EvaluationSuiteStatus.PASSED
        )
    )
    require(
        "evaluation_suite_status",
        suite.status is expected_status,
        "status",
        "Evaluation-suite status is inconsistent with its metrics and issues.",
    )
    require(
        "evaluation_component_linkage",
        all(
            getattr(
                component,
                "evaluation_run_id",
                getattr(
                    component,
                    "current_evaluation_run_id",
                    None,
                ),
            )
            == suite.evaluation_run_id
            for component in _component_sequence(suite)
        ),
        "evaluation_run_id",
        "Evaluation components do not reference the suite run.",
    )
    if run is not None:
        require(
            "evaluation_suite_run_linkage",
            suite.evaluation_run_id == run.evaluation_run_id,
            "evaluation_run_id",
            "Evaluation suite does not reference the supplied run.",
        )
    return _contract_validation_result(
        issues,
        validation_version="evaluation-suite-validation-1.0.0",
    )


class MarketScenarioEvaluationEngine:
    """Evaluate frozen Phase 8 outputs without repairing them."""

    def __init__(self, *, replay_attempts: int = 2) -> None:
        if (
            isinstance(replay_attempts, bool)
            or not isinstance(replay_attempts, int)
            or replay_attempts < 2
        ):
            raise ValueError("replay_attempts must be an integer >= 2.")
        self.replay_attempts = replay_attempts

    def evaluate(
        self,
        run: EvaluationRun,
        *,
        baseline: RegressionBaseline | None = None,
    ) -> EvaluationSuiteResult:
        if not isinstance(run, EvaluationRun):
            raise TypeError("run must be EvaluationRun.")
        structural = _evaluate_structural(run)
        scenario_quality = _evaluate_scenarios(run)
        retrieval = _evaluate_retrieval(run)
        grounding = _evaluate_grounding(run)
        audit_graph = _evaluate_audit_graph(run)
        reproducibility = _evaluate_reproducibility(
            run,
            replay_attempts=self.replay_attempts,
        )
        initial = _finalize_suite(
            run,
            structural=structural,
            scenario_quality=scenario_quality,
            retrieval=retrieval,
            grounding=grounding,
            audit_graph=audit_graph,
            reproducibility=reproducibility,
            regression=None,
        )
        if baseline is None:
            return initial
        comparison = compare_regression_baseline(
            baseline,
            initial,
            run,
        )
        return _finalize_suite(
            run,
            structural=structural,
            scenario_quality=scenario_quality,
            retrieval=retrieval,
            grounding=grounding,
            audit_graph=audit_graph,
            reproducibility=reproducibility,
            regression=comparison,
        )

    def evaluate_many(
        self,
        runs: Sequence[EvaluationRun],
    ) -> tuple[EvaluationSuiteResult, ...]:
        if isinstance(runs, (str, bytes)):
            raise TypeError("runs must be a sequence.")
        return tuple(self.evaluate(run) for run in runs)


def _synthetic_run(
    source: EvaluationRun,
    case_type: SyntheticCaseType,
    *,
    orchestration_input: MarketScenarioOrchestrationInput | None = None,
    orchestration_result: MarketScenarioOrchestrationResult | None = None,
    replay_packet: MarketScenarioReplayPacket | None | object = ...,
) -> EvaluationRun:
    selected_input = orchestration_input or source.orchestration_input
    selected_result = orchestration_result or source.orchestration_result
    selected_packet = (
        source.replay_packet
        if replay_packet is ...
        else replay_packet
    )
    identity = _hash_payload(
        {
            "source": source.evaluation_run_id,
            "case_type": case_type.value,
            "input": selected_input.content_hash,
            "result": selected_result.to_dict(),
            "replay": (
                selected_packet.to_dict()
                if isinstance(
                    selected_packet,
                    MarketScenarioReplayPacket,
                )
                else None
            ),
        }
    )
    return EvaluationRun.create(
        orchestration_input=selected_input,
        orchestration_result=selected_result,
        replay_packet=(
            selected_packet
            if isinstance(
                selected_packet,
                MarketScenarioReplayPacket,
            )
            else None
        ),
        fixture_tags=source.fixture_tags
        + (f"synthetic:{case_type.value}",),
        evaluator_version=source.evaluator_version,
        created_at=source.created_at,
        evaluation_run_id=(
            f"synthetic_run_{case_type.value}_{identity[:16]}"
        ),
    )


def _synthetic_case(
    case_type: SyntheticCaseType,
    run: EvaluationRun,
    *,
    source_ids: Sequence[str],
    notes: Sequence[str],
) -> SyntheticEvaluationCase:
    seed = SyntheticEvaluationCase(
        synthetic_case_id=(
            "synthetic_case_"
            + _hash_payload(
                {
                    "case_type": case_type.value,
                    "run_hash": run.content_hash,
                    "source_ids": tuple(sorted(source_ids)),
                }
            )[:20]
        ),
        case_type=case_type,
        source_evaluation_run_ids=tuple(sorted(source_ids)),
        evaluation_run=run,
        mutation_notes=tuple(notes),
    )
    return replace(
        seed,
        content_hash=synthetic_evaluation_case_content_hash(seed),
    )


def _replace_scenario(
    result: MarketScenarioOrchestrationResult,
    scenario: CurrentScenarioNarrative,
) -> MarketScenarioOrchestrationResult:
    scenario_set = result.current_scenario_set
    if scenario_set is None or not scenario_set.scenarios:
        return result
    changed = replace(
        scenario_set,
        scenarios=(scenario,) + scenario_set.scenarios[1:],
    )
    return replace(result, current_scenario_set=changed)


class SyntheticEvaluationCaseGenerator:
    """Create deterministic structural test cases from valid fixtures."""

    def generate(
        self,
        source_runs: Sequence[EvaluationRun],
    ) -> tuple[SyntheticEvaluationCase, ...]:
        if isinstance(source_runs, (str, bytes)) or not isinstance(
            source_runs,
            Sequence,
        ):
            raise TypeError(
                "source_runs must be a sequence of EvaluationRun values."
            )
        runs = tuple(source_runs)
        if not runs:
            return ()
        if not all(isinstance(item, EvaluationRun) for item in runs):
            raise TypeError(
                "source_runs must contain only EvaluationRun values."
            )
        invalid_sources = tuple(
            item.evaluation_run_id
            for item in runs
            if (
                not validate_evaluation_run(item).is_valid
                or item.orchestration_input.content_hash
                != market_scenario_orchestration_input_content_hash(
                    item.orchestration_input
                )
                or item.orchestration_result.content_hash
                != market_scenario_orchestration_result_content_hash(
                    item.orchestration_result
                )
                or item.orchestration_result.analytical_hash
                != market_scenario_orchestration_result_analytical_hash(
                    item.orchestration_result
                )
                or not validate_market_scenario_orchestration_result(
                    item.orchestration_result
                ).is_valid
            )
        )
        if invalid_sources:
            raise ValueError(
                "Synthetic generation requires valid source fixtures: "
                + ", ".join(sorted(invalid_sources))
                + "."
            )
        ordered = tuple(
            sorted(runs, key=lambda item: item.evaluation_run_id)
        )
        base = next(
            (
                item
                for item in ordered
                if item.orchestration_result.current_scenario_set
                is not None
            ),
            ordered[0],
        )
        cases: list[SyntheticEvaluationCase] = []

        def add(
            case_type: SyntheticCaseType,
            run: EvaluationRun,
            *,
            sources: Sequence[EvaluationRun] = (base,),
            notes: Sequence[str],
        ) -> None:
            cases.append(
                _synthetic_case(
                    case_type,
                    run,
                    source_ids=tuple(
                        item.evaluation_run_id for item in sources
                    ),
                    notes=notes,
                )
            )

        for source in ordered:
            add(
                SyntheticCaseType.SOURCE_FIXTURE,
                _synthetic_run(
                    source,
                    SyntheticCaseType.SOURCE_FIXTURE,
                ),
                sources=(source,),
                notes=(
                    "Unmodified source fixture wrapped as an evaluation case.",
                ),
            )

        for direction, case_type in (
            (ScenarioDirection.UP, SyntheticCaseType.BULLISH_PREMISE),
            (
                ScenarioDirection.DOWN,
                SyntheticCaseType.BEARISH_PREMISE,
            ),
        ):
            source = next(
                (
                    item
                    for item in ordered
                    if item.orchestration_input.frozen_technical_premise.direction
                    is direction
                ),
                None,
            )
            if source is not None:
                add(
                    case_type,
                    _synthetic_run(source, case_type),
                    sources=(source,),
                    notes=(
                        f"Existing {direction.value} frozen-premise fixture selected without relabelling.",
                    ),
                )

        if (
            base.orchestration_result.current_scenario_set is not None
            and base.orchestration_result.current_scenario_set.scenarios
        ):
            first_scenario = (
                base.orchestration_result.current_scenario_set.scenarios[
                    0
                ]
            )
            for duration, case_type in (
                (DurationBucket.DAYS, SyntheticCaseType.SHORT_DURATION),
                (
                    DurationBucket.WEEKS,
                    SyntheticCaseType.MEDIUM_DURATION,
                ),
                (DurationBucket.YEARS, SyntheticCaseType.LONG_DURATION),
            ):
                changed_scenario = replace(
                    first_scenario,
                    linked_duration_bucket=duration,
                )
                changed_result = _replace_scenario(
                    base.orchestration_result,
                    changed_scenario,
                )
                add(
                    case_type,
                    _synthetic_run(
                        base,
                        case_type,
                        orchestration_result=changed_result,
                        replay_packet=None,
                    ),
                    notes=(
                        "Controlled linked-duration perturbation; stale nested hashes are retained so continuity checks remain testable.",
                    ),
                )

            duplicate = replace(
                first_scenario,
                scenario_id=(
                    first_scenario.scenario_id
                    + "__synthetic_duplicate"
                ),
            )
            scenario_set = (
                base.orchestration_result.current_scenario_set
            )
            duplicate_result = replace(
                base.orchestration_result,
                current_scenario_set=replace(
                    scenario_set,
                    scenarios=scenario_set.scenarios + (duplicate,),
                ),
            )
            add(
                SyntheticCaseType.DUPLICATE_SCENARIO,
                _synthetic_run(
                    base,
                    SyntheticCaseType.DUPLICATE_SCENARIO,
                    orchestration_result=duplicate_result,
                    replay_packet=None,
                ),
                notes=(
                    "A copied causal chain receives a distinct synthetic scenario ID.",
                ),
            )

            weak_scenario = replace(
                first_scenario,
                timeline_sequence=first_scenario.timeline_sequence[:1],
            )
            add(
                SyntheticCaseType.WEAK_CAUSAL_CHAIN,
                _synthetic_run(
                    base,
                    SyntheticCaseType.WEAK_CAUSAL_CHAIN,
                    orchestration_result=_replace_scenario(
                        base.orchestration_result,
                        weak_scenario,
                    ),
                    replay_packet=None,
                ),
                notes=(
                    "The first scenario is reduced to one causal step.",
                ),
            )

            no_invalidation = replace(
                first_scenario,
                invalidation_conditions=(),
            )
            add(
                SyntheticCaseType.MISSING_INVALIDATION,
                _synthetic_run(
                    base,
                    SyntheticCaseType.MISSING_INVALIDATION,
                    orchestration_result=_replace_scenario(
                        base.orchestration_result,
                        no_invalidation,
                    ),
                    replay_packet=None,
                ),
                notes=(
                    "The first scenario's invalidation conditions are removed.",
                ),
            )

            invented = replace(
                first_scenario,
                supporting_current_evidence_ids=(
                    first_scenario.supporting_current_evidence_ids
                    + ("synthetic_unknown_evidence",)
                ),
            )
            add(
                SyntheticCaseType.INVENTED_REFERENCE,
                _synthetic_run(
                    base,
                    SyntheticCaseType.INVENTED_REFERENCE,
                    orchestration_result=_replace_scenario(
                        base.orchestration_result,
                        invented,
                    ),
                    replay_packet=None,
                ),
                notes=(
                    "An unknown evidence reference is injected into one scenario.",
                ),
            )

        retrieval = base.orchestration_result.pattern_retrieval_result
        if (
            retrieval is not None
            and retrieval.matches
            and retrieval.counter_patterns
        ):
            add(
                SyntheticCaseType.PRIMARY_COUNTER_COMBINATION,
                _synthetic_run(
                    base,
                    SyntheticCaseType.PRIMARY_COUNTER_COMBINATION,
                ),
                notes=(
                    "Existing primary and counter-pattern lanes are retained together.",
                ),
            )

        if base.orchestration_input.current_evidence:
            sparse_input = replace(
                base.orchestration_input,
                current_evidence=(
                    base.orchestration_input.current_evidence[0],
                ),
                content_hash="",
            )
            sparse_input = replace(
                sparse_input,
                content_hash=market_scenario_orchestration_input_content_hash(
                    sparse_input
                ),
            )
            add(
                SyntheticCaseType.SPARSE_EVIDENCE,
                _synthetic_run(
                    base,
                    SyntheticCaseType.SPARSE_EVIDENCE,
                    orchestration_input=sparse_input,
                    replay_packet=None,
                ),
                notes=(
                    "Only the first existing evidence item is retained; no evidence is fabricated.",
                ),
            )
            first_evidence = base.orchestration_input.current_evidence[0]
            new_implication = (
                EvidenceImplication.CONTRADICTORY
                if first_evidence.implication
                is not EvidenceImplication.CONTRADICTORY
                else EvidenceImplication.SUPPORTIVE
            )
            contradictory_input = replace(
                base.orchestration_input,
                current_evidence=(
                    replace(
                        first_evidence,
                        implication=new_implication,
                    ),
                )
                + base.orchestration_input.current_evidence[1:],
                content_hash="",
            )
            contradictory_input = replace(
                contradictory_input,
                content_hash=market_scenario_orchestration_input_content_hash(
                    contradictory_input
                ),
            )
            add(
                SyntheticCaseType.CONTRADICTORY_EVIDENCE,
                _synthetic_run(
                    base,
                    SyntheticCaseType.CONTRADICTORY_EVIDENCE,
                    orchestration_input=contradictory_input,
                    replay_packet=None,
                ),
                notes=(
                    "One existing evidence implication is deterministically inverted.",
                ),
            )
            future_evidence = replace(
                first_evidence,
                applicable_cutoff="2999-01-01T00:00:00+00:00",
            )
            cutoff_input = replace(
                base.orchestration_input,
                current_evidence=(future_evidence,)
                + base.orchestration_input.current_evidence[1:],
                content_hash="",
            )
            cutoff_input = replace(
                cutoff_input,
                content_hash=market_scenario_orchestration_input_content_hash(
                    cutoff_input
                ),
            )
            add(
                SyntheticCaseType.INVALID_CUTOFF,
                _synthetic_run(
                    base,
                    SyntheticCaseType.INVALID_CUTOFF,
                    orchestration_input=cutoff_input,
                    replay_packet=None,
                ),
                notes=(
                    "One evidence cutoff is moved beyond the orchestration cutoff.",
                ),
            )

        optional_input = replace(
            base.orchestration_input,
            current_regime_inputs=None,
            valuation_inputs=None,
            liquidity_inputs=None,
            positioning_inputs=None,
            benchmark_relative_inputs=None,
            sector_relative_inputs=None,
            content_hash="",
        )
        optional_input = replace(
            optional_input,
            content_hash=market_scenario_orchestration_input_content_hash(
                optional_input
            ),
        )
        add(
            SyntheticCaseType.MISSING_OPTIONAL_STATE,
            _synthetic_run(
                base,
                SyntheticCaseType.MISSING_OPTIONAL_STATE,
                orchestration_input=optional_input,
                replay_packet=None,
            ),
            notes=(
                "All optional current-state input fields are explicitly unavailable.",
            ),
        )

        if base.replay_packet is not None:
            mismatch_seed = replace(
                base.replay_packet,
                expected_final_hash=("f" * 64),
                content_hash="",
            )
            mismatch_packet = replace(
                mismatch_seed,
                content_hash=market_scenario_replay_packet_content_hash(
                    mismatch_seed
                ),
            )
            add(
                SyntheticCaseType.REPLAY_MISMATCH,
                _synthetic_run(
                    base,
                    SyntheticCaseType.REPLAY_MISMATCH,
                    replay_packet=mismatch_packet,
                ),
                notes=(
                    "The replay packet contains a canonical but incorrect expected analytical hash.",
                ),
            )

        analytical_records = tuple(
            (
                index,
                record,
            )
            for index, record in enumerate(
                base.orchestration_result.stage_records
            )
            if record.stage
            in {
                OrchestrationStage.INPUT_VALIDATION,
                OrchestrationStage.CURRENT_STATE_BUILD,
                OrchestrationStage.PATTERN_RETRIEVAL,
                OrchestrationStage.SCENARIO_GENERATION,
            }
        )
        if len(analytical_records) >= 2:
            index, record = analytical_records[0]
            failed_record = replace(
                record,
                status=OrchestrationStatus.FAILED,
                errors=record.errors
                + ("synthetic_upstream_failure",),
            )
            records = list(
                base.orchestration_result.stage_records
            )
            records[index] = failed_record
            failed_result = replace(
                base.orchestration_result,
                stage_records=tuple(records),
            )
            add(
                SyntheticCaseType.STAGE_FAILURE,
                _synthetic_run(
                    base,
                    SyntheticCaseType.STAGE_FAILURE,
                    orchestration_result=failed_result,
                    replay_packet=None,
                ),
                notes=(
                    "An early analytical stage is marked failed while later analytical stages remain.",
                ),
            )

        recommendation_to_type = {
            "keep": SyntheticCaseType.ADVERSARIAL_KEEP,
            "revise": SyntheticCaseType.ADVERSARIAL_REVISE,
            "reject": SyntheticCaseType.ADVERSARIAL_REJECT,
        }
        for recommendation, case_type in recommendation_to_type.items():
            source = next(
                (
                    item
                    for item in ordered
                    if item.orchestration_result.scenario_generation_result
                    is not None
                    and item.orchestration_result.scenario_generation_result.adversarial_recommendation
                    is not None
                    and item.orchestration_result.scenario_generation_result.adversarial_recommendation.value
                    == recommendation
                ),
                None,
            )
            if source is not None:
                add(
                    case_type,
                    _synthetic_run(source, case_type),
                    sources=(source,),
                    notes=(
                        f"Existing adversarial {recommendation} fixture selected without changing its review.",
                    ),
                )

        compatible_pair = next(
            (
                (left, right)
                for index, left in enumerate(ordered)
                for right in ordered[index + 1 :]
                if (
                    left.orchestration_input.symbol
                    == right.orchestration_input.symbol
                    and left.orchestration_input.applicable_cutoff
                    == right.orchestration_input.applicable_cutoff
                    and left.orchestration_input.frozen_technical_premise.frozen_content_hash
                    == right.orchestration_input.frozen_technical_premise.frozen_content_hash
                    and left.orchestration_input.historical_pattern_library.content_hash
                    == right.orchestration_input.historical_pattern_library.content_hash
                )
            ),
            None,
        )
        if compatible_pair is not None:
            left, right = compatible_pair
            combined_by_id = {
                item.evidence_id: item
                for item in (
                    left.orchestration_input.current_evidence
                    + right.orchestration_input.current_evidence
                )
            }
            combined_input = replace(
                left.orchestration_input,
                current_evidence=tuple(
                    combined_by_id[key]
                    for key in sorted(combined_by_id)
                ),
                content_hash="",
            )
            combined_input = replace(
                combined_input,
                content_hash=market_scenario_orchestration_input_content_hash(
                    combined_input
                ),
            )
            add(
                SyntheticCaseType.COMPATIBLE_EVIDENCE_COMBINATION,
                _synthetic_run(
                    left,
                    SyntheticCaseType.COMPATIBLE_EVIDENCE_COMBINATION,
                    orchestration_input=combined_input,
                    replay_packet=None,
                ),
                sources=(left, right),
                notes=(
                    "Evidence is unioned by stable evidence ID only after symbol, cutoff, premise, and library compatibility.",
                ),
            )

        return tuple(
            sorted(
                cases,
                key=lambda item: (
                    item.case_type.value,
                    item.synthetic_case_id,
                ),
            )
        )


def _company_metric(
    metric_id: str,
    name: str,
    value: Any,
    *,
    status: EvaluationMetricStatus,
    expected: Any,
    unit: str = "ratio",
    hard_gate: bool = False,
    higher_is_better: bool | None = True,
    source_refs: Sequence[str] = (),
    notes: Sequence[str] = (),
) -> EvaluationMetric:
    return EvaluationMetric.create(
        metric_id=metric_id,
        category=EvaluationCategory.COMPANY_KNOWLEDGE,
        name=name,
        status=status,
        value=value,
        expected=expected,
        unit=unit,
        hard_gate=hard_gate,
        higher_is_better=higher_is_better,
        source_refs=source_refs,
        notes=notes,
    )


def _company_issue(
    code: str,
    path: str,
    message: str,
    *,
    hard_failure: bool,
    source_refs: Sequence[str] = (),
) -> EvaluationIssue:
    return EvaluationIssue.create(
        code=code,
        severity=(
            EvaluationIssueSeverity.ERROR
            if hard_failure
            else EvaluationIssueSeverity.WARNING
        ),
        category=EvaluationCategory.COMPANY_KNOWLEDGE,
        path=path,
        message=message,
        hard_failure=hard_failure,
        source_refs=source_refs,
    )


def _company_text(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(
            text
            for item in value.values()
            for text in _company_text(item)
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(
            text for item in value for text in _company_text(item)
        )
    return ()


def evaluate_company_knowledge(
    snapshot: CompanyKnowledgeSnapshot,
    *,
    context: CompanyScenarioContext | None = None,
    scenario_set: CurrentScenarioSet | None = None,
    archetype_library: CompanyEventArchetypeLibrary | None = None,
) -> CompanyKnowledgeEvaluation:
    """Evaluate Phase 10 artifacts without changing Phase 9 base suites."""

    if not isinstance(snapshot, CompanyKnowledgeSnapshot):
        raise TypeError("snapshot must be CompanyKnowledgeSnapshot.")
    if context is not None and not isinstance(
        context,
        CompanyScenarioContext,
    ):
        raise TypeError(
            "context must be CompanyScenarioContext or null."
        )
    if scenario_set is not None and not isinstance(
        scenario_set,
        CurrentScenarioSet,
    ):
        raise TypeError(
            "scenario_set must be CurrentScenarioSet or null."
        )
    library = (
        archetype_library
        if archetype_library is not None
        else default_company_event_archetype_library()
    )
    issues: list[EvaluationIssue] = []
    metrics: list[EvaluationMetric] = []
    snapshot_validation = validate_company_knowledge_snapshot(snapshot)
    snapshot_hash_ok = (
        snapshot.content_hash
        == company_knowledge_snapshot_content_hash(snapshot)
    )
    if not snapshot_hash_ok:
        issues.append(
            _company_issue(
                HARD_FAILURE_COMPANY_SNAPSHOT_MUTATION,
                "snapshot.content_hash",
                "Company snapshot content changed after hashing.",
                hard_failure=True,
                source_refs=(snapshot.snapshot_id,),
            )
        )
    if any(
        item.code == "scheduled_event_cutoff"
        for item in snapshot_validation.errors
    ):
        issues.append(
            _company_issue(
                HARD_FAILURE_COMPANY_CUTOFF,
                "snapshot.scheduled_events",
                "A company event was unavailable at the snapshot cutoff.",
                hard_failure=True,
            )
        )

    profile_values = (
        snapshot.company_profile.company_stage.value,
        snapshot.company_profile.operating_history_class.value,
        snapshot.company_profile.cyclicality_class.value,
        snapshot.company_profile.capital_intensity_class.value,
        snapshot.company_profile.regulatory_intensity_class.value,
    )
    profile_available = sum(
        item != "unknown" for item in profile_values
    )
    financial_values = tuple(
        getattr(snapshot.financial_state, name)
        for name in (
            "revenue_state",
            "revenue_growth_state",
            "profitability_state",
            "gross_margin_state",
            "operating_margin_state",
            "free_cash_flow_state",
            "cash_balance_state",
            "cash_runway_state",
            "debt_state",
            "debt_maturity_state",
            "interest_burden_state",
            "liquidity_state",
            "working_capital_state",
            "capital_expenditure_state",
            "financing_dependency_state",
            "dilution_dependency_state",
            "guidance_state",
            "estimate_dispersion_state",
            "valuation_state",
        )
    )
    financial_available = sum(
        item
        not in {
            CompanyStateValue.UNKNOWN,
            CompanyStateValue.UNAVAILABLE,
        }
        for item in financial_values
    )
    operational_names = (
        "production_state",
        "service_delivery_state",
        "execution_state",
        "product_development_state",
        "launch_state",
        "manufacturing_state",
        "supply_chain_state",
        "backlog_state",
        "customer_demand_state",
        "customer_concentration_state",
        "contract_dependency_state",
        "regulatory_approval_state",
        "litigation_state",
        "management_execution_state",
        "hiring_state",
        "geographic_dependency_state",
        "infrastructure_dependency_state",
    )
    operational_available = sum(
        getattr(snapshot.operational_state, name)
        not in {
            CompanyStateValue.UNKNOWN,
            CompanyStateValue.UNAVAILABLE,
        }
        for name in operational_names
    )
    capital_names = (
        "share_count_state",
        "recent_dilution_state",
        "warrant_overhang",
        "employee_compensation_dilution",
        "capital_access_state",
    )
    capital_available = sum(
        getattr(snapshot.capital_structure_state, name)
        not in {
            CompanyStateValue.UNKNOWN,
            CompanyStateValue.UNAVAILABLE,
        }
        for name in capital_names
    )
    ratios = (
        (
            "company_state_completeness",
            "Company-state completeness",
            profile_available,
            len(profile_values),
        ),
        (
            "financial_state_coverage",
            "Financial-state coverage",
            financial_available,
            len(financial_values),
        ),
        (
            "operational_state_coverage",
            "Operational-state coverage",
            operational_available,
            len(operational_names),
        ),
        (
            "capital_structure_coverage",
            "Capital-structure coverage",
            capital_available,
            len(capital_names),
        ),
    )
    for metric_id, name, available, total in ratios:
        value = _ratio(available, total)
        metrics.append(
            _company_metric(
                metric_id,
                name,
                value,
                status=(
                    EvaluationMetricStatus.PASSED
                    if value == 1.0
                    else EvaluationMetricStatus.WARNING
                ),
                expected=1.0,
                notes=(
                    f"{available} of {total} controlled fields are explicitly available.",
                ),
            )
        )
    scheduled_valid = sum(
        _temporal_point(item.announced_at)
        <= _temporal_point(snapshot.applicable_cutoff)
        and item.known_at_cutoff
        for item in snapshot.scheduled_events
    )
    scheduled_ratio = (
        _ratio(scheduled_valid, len(snapshot.scheduled_events))
        if snapshot.scheduled_events
        else None
    )
    metrics.append(
        _company_metric(
            "scheduled_event_coverage",
            "Scheduled-event coverage",
            scheduled_ratio,
            status=(
                EvaluationMetricStatus.UNAVAILABLE
                if scheduled_ratio is None
                else (
                    EvaluationMetricStatus.PASSED
                    if scheduled_ratio == 1.0
                    else EvaluationMetricStatus.FAILED
                )
            ),
            expected=1.0,
            notes=(
                "No scheduled event was supplied."
                if scheduled_ratio is None
                else "Only cutoff-valid known events count as covered."
            ,),
        )
    )
    dependency_valid = sum(
        bool(item.provenance)
        and bool(item.content_hash)
        for item in snapshot.dependencies
    )
    dependency_ratio = (
        _ratio(dependency_valid, len(snapshot.dependencies))
        if snapshot.dependencies
        else None
    )
    metrics.append(
        _company_metric(
            "dependency_coverage",
            "Dependency coverage",
            dependency_ratio,
            status=(
                EvaluationMetricStatus.UNAVAILABLE
                if dependency_ratio is None
                else (
                    EvaluationMetricStatus.PASSED
                    if dependency_ratio == 1.0
                    else EvaluationMetricStatus.WARNING
                )
            ),
            expected=1.0,
            notes=(
                "No dependency was supplied."
                if dependency_ratio is None
                else "Dependencies require provenance and stable hashes."
            ,),
        )
    )

    candidate_grounding = None
    label_integrity = None
    duplicate_count = None
    unsupported_archetypes = 0
    invalid_company_refs = 0
    missing_contradiction_count = 0
    missing_invalidation_count = 0
    if context is not None:
        context_validation = validate_company_scenario_context(
            context,
            snapshot=snapshot,
        )
        if not context_validation.is_valid:
            invalid_company_refs += len(context_validation.errors)
        candidates = (
            context.eligible_event_candidates
            + context.excluded_event_candidates
        )
        library_ids = {
            item.archetype_id for item in library.archetypes
        }
        unsupported = tuple(
            item
            for item in candidates
            if item.archetype_id not in library_ids
        )
        unsupported_archetypes = len(unsupported)
        for item in unsupported:
            issues.append(
                _company_issue(
                    HARD_FAILURE_UNSUPPORTED_ARCHETYPE,
                    f"context.candidates[{item.candidate_id}]",
                    "Candidate references an unsupported event archetype.",
                    hard_failure=True,
                    source_refs=(item.candidate_id, item.archetype_id),
                )
            )
        eligible = context.eligible_event_candidates
        grounded = sum(
            bool(
                item.supporting_company_facts
                or item.supporting_dependencies
                or item.supporting_structural_risks
                or item.supporting_structural_opportunities
                or item.related_scheduled_events
                or item.supporting_financial_states
                or item.supporting_operational_states
            )
            and item.content_hash
            == company_event_candidate_content_hash(item)
            for item in eligible
        )
        candidate_grounding = (
            _ratio(grounded, len(eligible)) if eligible else None
        )
        signatures = [
            item.causal_signature for item in eligible
        ]
        duplicate_count = sum(
            count - 1
            for count in Counter(signatures).values()
            if count > 1
        )
        label_integrity = 1.0

        if scenario_set is not None:
            usage = validate_company_scenario_usage(
                scenario_set,
                context,
            )
            invalid_company_refs += sum(
                item.code
                in {
                    "company_candidate_reference",
                    "company_candidate_grounding",
                    "company_candidate_known_ids",
                }
                for item in usage.errors
            )
            missing_contradiction_count = sum(
                item.code == "company_candidate_contradictions"
                for item in usage.errors
            )
            missing_invalidation_count = sum(
                item.code == "company_candidate_invalidation"
                for item in usage.errors
            )
            for item in usage.errors:
                hard_code = {
                    "company_candidate_reference": (
                        HARD_FAILURE_INVALID_COMPANY_CANDIDATE
                    ),
                    "company_candidate_known_ids": (
                        HARD_FAILURE_INVALID_COMPANY_CANDIDATE
                    ),
                    "company_candidate_grounding": (
                        HARD_FAILURE_INVENTED_COMPANY_FACT
                    ),
                    "company_candidate_hypothetical_label": (
                        HARD_FAILURE_COMPANY_EVENT_CONFUSION
                    ),
                    "company_candidate_classification": (
                        HARD_FAILURE_COMPANY_EVENT_CONFUSION
                    ),
                    "company_candidate_no_certainty": (
                        HARD_FAILURE_HYPOTHETICAL_AS_FACT
                    ),
                }.get(item.code)
                if hard_code is not None:
                    issues.append(
                        _company_issue(
                            hard_code,
                            item.path,
                            item.message,
                            hard_failure=True,
                        )
                    )
            scheduled_ids = {
                item.event_id for item in context.scheduled_events
            }
            hypothetical_ids = {
                item.hypothetical_event_id
                for scenario in scenario_set.scenarios
                for item in scenario.hypothetical_future_events
            }
            if scheduled_ids & hypothetical_ids:
                label_integrity = 0.0
                issues.append(
                    _company_issue(
                        HARD_FAILURE_COMPANY_EVENT_CONFUSION,
                        "scenario_set.hypothetical_future_events",
                        "A known scheduled event was represented as hypothetical.",
                        hard_failure=True,
                        source_refs=tuple(
                            sorted(scheduled_ids & hypothetical_ids)
                        ),
                    )
                )
            known_event_refs = {
                event_id
                for scenario in scenario_set.scenarios
                for step in scenario.timeline_sequence
                for event_id in step.event_ids
                if event_id.startswith("company_event_")
            }
            invented_events = known_event_refs - scheduled_ids
            if invented_events:
                issues.append(
                    _company_issue(
                        HARD_FAILURE_INVENTED_COMPANY_EVENT,
                        "scenario_set.timeline_sequence.event_ids",
                        "Scenario references an unknown scheduled company event.",
                        hard_failure=True,
                        source_refs=tuple(sorted(invented_events)),
                    )
                )

    def add_optional_metric(
        metric_id: str,
        name: str,
        value: Any,
        *,
        expected: Any,
        hard_gate: bool = False,
        higher_is_better: bool | None = True,
    ) -> None:
        status = (
            EvaluationMetricStatus.UNAVAILABLE
            if value is None
            else (
                EvaluationMetricStatus.PASSED
                if value == expected
                else (
                    EvaluationMetricStatus.FAILED
                    if hard_gate
                    else EvaluationMetricStatus.WARNING
                )
            )
        )
        metrics.append(
            _company_metric(
                metric_id,
                name,
                value,
                status=status,
                expected=expected,
                unit="count" if isinstance(expected, int) else "ratio",
                hard_gate=hard_gate,
                higher_is_better=higher_is_better,
            )
        )

    add_optional_metric(
        "event_candidate_grounding",
        "Event-candidate grounding",
        candidate_grounding,
        expected=1.0,
    )
    add_optional_metric(
        "hypothetical_label_integrity",
        "Hypothetical-label integrity",
        label_integrity,
        expected=1.0,
        hard_gate=True,
    )
    add_optional_metric(
        "candidate_duplication",
        "Candidate duplication",
        duplicate_count,
        expected=0,
        higher_is_better=False,
    )
    add_optional_metric(
        "invalid_archetype_usage",
        "Invalid archetype usage",
        unsupported_archetypes if context is not None else None,
        expected=0,
        hard_gate=True,
        higher_is_better=False,
    )
    add_optional_metric(
        "unsupported_company_event_reference",
        "Unsupported company-event reference",
        invalid_company_refs if context is not None else None,
        expected=0,
        hard_gate=True,
        higher_is_better=False,
    )
    add_optional_metric(
        "missing_contradiction_coverage",
        "Missing contradiction coverage",
        missing_contradiction_count if scenario_set is not None else None,
        expected=0,
        higher_is_better=False,
    )
    add_optional_metric(
        "missing_invalidation_coverage",
        "Missing invalidation coverage",
        missing_invalidation_count if scenario_set is not None else None,
        expected=0,
        hard_gate=True,
        higher_is_better=False,
    )

    hard_codes = tuple(
        sorted(
            {
                item.code
                for item in issues
                if item.hard_failure
            }
        )
    )
    validation_errors = tuple(
        ValidationIssue(
            code=item.code,
            severity="error",
            path=item.path,
            message=item.message,
        )
        for item in issues
        if item.hard_failure
    )
    validation = ValidationResult(
        is_valid=not validation_errors,
        score=(
            round(
                100.0
                * sum(
                    item.status is EvaluationMetricStatus.PASSED
                    for item in metrics
                )
                / len(metrics),
                2,
            )
            if metrics
            else 100.0
        ),
        errors=validation_errors,
        warnings=tuple(
            ValidationIssue(
                code=item.code,
                severity="warning",
                path=item.path,
                message=item.message,
            )
            for item in issues
            if not item.hard_failure
        ),
        failed_rules=hard_codes,
        passed_rules=tuple(
            item.metric_id
            for item in metrics
            if item.status is EvaluationMetricStatus.PASSED
        ),
        frozen_hash_before=snapshot.content_hash,
        frozen_hash_after=snapshot.content_hash,
        validation_version=COMPANY_KNOWLEDGE_EVALUATION_SCHEMA_VERSION,
    )
    seed = CompanyKnowledgeEvaluation(
        snapshot_hash=snapshot.content_hash,
        context_hash=context.content_hash if context is not None else "",
        scenario_set_hash=(
            scenario_set.content_hash if scenario_set is not None else ""
        ),
        metrics=tuple(metrics),
        issues=tuple(issues),
        hard_failure_codes=hard_codes,
        validation_result=validation,
    )
    return replace(
        seed,
        content_hash=company_knowledge_evaluation_content_hash(seed),
    )


__all__ = [
    "AUDIT_GRAPH_EVALUATION_SCHEMA_VERSION",
    "AuditGraphEvaluation",
    "COMPANY_KNOWLEDGE_EVALUATION_SCHEMA_VERSION",
    "CompanyKnowledgeEvaluation",
    "EVALUATION_ISSUE_SCHEMA_VERSION",
    "EVALUATION_METRIC_SCHEMA_VERSION",
    "EVALUATION_RUN_SCHEMA_VERSION",
    "EVALUATION_SUITE_RESULT_SCHEMA_VERSION",
    "EvaluationCategory",
    "EvaluationIssue",
    "EvaluationIssueSeverity",
    "EvaluationMetric",
    "EvaluationMetricStatus",
    "EvaluationRun",
    "EvaluationSuiteResult",
    "EvaluationSuiteStatus",
    "GROUNDING_EVALUATION_SCHEMA_VERSION",
    "GroundingEvaluation",
    "HARD_FAILURE_CODES",
    "HARD_FAILURE_AUDIT_INCOMPLETE",
    "HARD_FAILURE_COMPANY_CUTOFF",
    "HARD_FAILURE_COMPANY_EVENT_CONFUSION",
    "HARD_FAILURE_COMPANY_SNAPSHOT_MUTATION",
    "HARD_FAILURE_DANGLING_AUDIT",
    "HARD_FAILURE_DOWNSTREAM_AFTER_FAILURE",
    "HARD_FAILURE_FUTURE_EVIDENCE",
    "HARD_FAILURE_HASH_CONTINUITY",
    "HARD_FAILURE_INVENTED_REFERENCE",
    "HARD_FAILURE_INVENTED_COMPANY_EVENT",
    "HARD_FAILURE_INVENTED_COMPANY_FACT",
    "HARD_FAILURE_HYPOTHETICAL_AS_FACT",
    "HARD_FAILURE_INVALID_COMPANY_CANDIDATE",
    "HARD_FAILURE_MISSING_INVALIDATION",
    "HARD_FAILURE_NON_REPRODUCIBLE",
    "HARD_FAILURE_TECHNICAL_CONSTRAINT",
    "HARD_FAILURE_TECHNICAL_PREMISE_MUTATION",
    "HARD_FAILURE_UNSUPPORTED_ARCHETYPE",
    "MARKET_SCENARIO_EVALUATOR_VERSION",
    "MarketScenarioEvaluationEngine",
    "REGRESSION_BASELINE_SCHEMA_VERSION",
    "REGRESSION_COMPARISON_SCHEMA_VERSION",
    "REPRODUCIBILITY_EVALUATION_SCHEMA_VERSION",
    "RETRIEVAL_QUALITY_EVALUATION_SCHEMA_VERSION",
    "RegressionBaseline",
    "RegressionComparison",
    "ReproducibilityEvaluation",
    "SCENARIO_QUALITY_EVALUATION_SCHEMA_VERSION",
    "STRUCTURAL_EVALUATION_SCHEMA_VERSION",
    "SYNTHETIC_EVALUATION_CASE_SCHEMA_VERSION",
    "ScenarioQualityEvaluation",
    "StructuralEvaluationResult",
    "SyntheticCaseType",
    "SyntheticEvaluationCase",
    "SyntheticEvaluationCaseGenerator",
    "audit_graph_evaluation_content_hash",
    "compare_regression_baseline",
    "company_knowledge_evaluation_content_hash",
    "create_regression_baseline",
    "evaluation_issue_content_hash",
    "evaluation_metric_content_hash",
    "evaluation_run_content_hash",
    "evaluation_suite_result_content_hash",
    "evaluate_company_knowledge",
    "grounding_evaluation_content_hash",
    "regression_baseline_content_hash",
    "regression_comparison_content_hash",
    "reproducibility_evaluation_content_hash",
    "retrieval_quality_evaluation_content_hash",
    "scenario_quality_evaluation_content_hash",
    "structural_evaluation_content_hash",
    "synthetic_evaluation_case_content_hash",
    "validate_evaluation_run",
    "validate_evaluation_suite_result",
]
