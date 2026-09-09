"""Phase 11D1 decision-time technical agents in disabled-by-default shadow mode.

The module is deliberately disconnected from ``ElliottAgent``. It gives two
blinded counters the same immutable decision-time technical packet, freezes
their outputs, applies the existing deterministic Elliott validators, then and
only then retrieves cutoff-valid mistake-memory lessons for a bounded audit.
The final role may select frozen candidate IDs only. No result is promoted to
the live degree resolution or to a ``ForecastRecord``.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .correction_semantics import structure_family
from .degrees import validate_degree_hierarchy_rules
from .forecast_records import canonical_sha256, stored_record_content_hash
from .mistake_memory import (
    LessonEvent,
    LessonRetrievalQuery,
    LessonSource,
    MistakeMemoryLesson,
    RetrievedLesson,
    retrieve_applicable_lessons,
    retrieved_lesson_content_hash,
)
from .providers import AnalysisProvider, ProviderError
from .schema import (
    DEGREE_RESOLUTION_SCHEMA,
    DEGREE_TIMEFRAMES,
    STANDARD_DEGREES,
    validate_degree_resolution_shape,
)


TECHNICAL_AGENT_POLICY_VERSION = "phase11d1-shadow-policy-1.0.0"
TECHNICAL_AGENT_CALCULATION_VERSION = "phase11d1-deterministic-1.0.0"
TECHNICAL_AGENT_ROLE_SCHEMA_VERSION = "technical-agent-role-1.0.0"
TECHNICAL_AGENT_REQUEST_SCHEMA_VERSION = "technical-agent-request-1.0.0"
TECHNICAL_AGENT_RESULT_SCHEMA_VERSION = "technical-agent-result-1.0.0"
CANDIDATE_WAVE_COUNT_SCHEMA_VERSION = "candidate-wave-count-1.0.0"
CANDIDATE_VALIDATION_SCHEMA_VERSION = "candidate-validation-1.0.0"
EVIDENCE_AUDIT_SCHEMA_VERSION = "evidence-audit-result-1.0.0"
SHADOW_RESOLUTION_SCHEMA_VERSION = "shadow-technical-resolution-1.0.0"
SHADOW_COMPARISON_SCHEMA_VERSION = "shadow-resolution-comparison-1.0.0"
TECHNICAL_AGENT_ORCHESTRATION_SCHEMA_VERSION = (
    "technical-agent-orchestration-1.0.0"
)

PRIMARY_COUNTER_PROMPT_VERSION = "phase11d1-primary-counter-1.0.0"
ALTERNATIVE_COUNTER_PROMPT_VERSION = "phase11d1-alternative-counter-1.0.0"
RULES_AUDITOR_PROMPT_VERSION = "phase11d1-rules-auditor-1.0.0"
FINAL_ORCHESTRATOR_PROMPT_VERSION = "phase11d1-final-orchestrator-1.0.0"

TECHNICAL_AGENT_PROMPT_VERSIONS = MappingProxyType(
    {
        "primary_wave_counter": PRIMARY_COUNTER_PROMPT_VERSION,
        "alternative_wave_counter": ALTERNATIVE_COUNTER_PROMPT_VERSION,
        "rules_and_evidence_auditor": RULES_AUDITOR_PROMPT_VERSION,
        "final_technical_orchestrator": FINAL_ORCHESTRATOR_PROMPT_VERSION,
    }
)

TECHNICAL_AGENT_STAGE_VERSIONS = MappingProxyType(
    {
        "role_contract": TECHNICAL_AGENT_ROLE_SCHEMA_VERSION,
        "request_validation": "phase11d1-request-validation-1.0.0",
        "candidate_validation": "phase11d1-candidate-validation-1.0.0",
        "lesson_retrieval": "mistake-memory-policy-1.0.0",
        "shadow_comparison": "phase11d1-shadow-comparison-1.0.0",
    }
)


class TechnicalAgentRole(StrEnum):
    PRIMARY_WAVE_COUNTER = "primary_wave_counter"
    ALTERNATIVE_WAVE_COUNTER = "alternative_wave_counter"
    RULES_AND_EVIDENCE_AUDITOR = "rules_and_evidence_auditor"
    FINAL_TECHNICAL_ORCHESTRATOR = "final_technical_orchestrator"


class TechnicalAgentResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ShadowResolutionStatus(StrEnum):
    SELECTED = "selected"
    UNRESOLVED = "unresolved"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REJECTED_INVALID = "rejected_invalid"


class ShadowComparisonStatus(StrEnum):
    EXACT_MATCH = "exact_match"
    STRUCTURALLY_EQUIVALENT = "structurally_equivalent"
    MATERIALLY_DIFFERENT = "materially_different"
    NO_SELECTION = "no_selection"
    UNAVAILABLE = "unavailable"


class TechnicalOrchestrationStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"
    REJECTED_INVALID = "rejected_invalid"
    FAILED = "failed"


_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_TECHNICAL_KEYS = frozenset(
    {
        "analysis",
        "existing_analysis",
        "source_hypothesis",
        "previous_degree_resolution",
        "degree_resolution",
        "human_review",
        "knowledge",
        "context_data",
        "company_knowledge",
        "company_fundamentals",
        "fundamentals",
        "fundamental_analysis",
        "news",
        "market_news",
        "macro_news",
        "market_scenario",
        "scenario_narratives",
        "outcomes",
        "forecast_outcomes",
        "mistake_memory",
        "lessons",
        "post_cutoff_candles",
    }
)
_PERMITTED_PACKET_KEYS = frozenset(
    {
        "market_data",
        "zoom_windows",
        "data_reconciliation",
        "wave_verification",
        "feature_manifest",
        "data_freshness",
        "data_errors",
    }
)
_SOFT_EVIDENCE_TOKENS = frozenset(
    {"rsi", "volume", "ewo", "macd", "fibonacci", "duration", "channel", "scale"}
)
_HARD_RULE_SUMMARY = (
    "Every child wave must remain inside its declared parent time boundary.",
    "A verified impulse or diagonal has positions 1-2-3-4-5; Wave 2 cannot pass the Wave 1 origin and Wave 3 cannot be the shortest actionary wave.",
    "Wave 3 must exceed the Wave 1 extreme; Wave 4 cannot overlap Wave 1 territory in an impulse, while an allowed diagonal is exempt from that overlap rule.",
    "A verified zigzag is A-B-C with motive or allowed-diagonal A and C and corrective B.",
    "A verified flat is A-B-C with corrective A and B and motive or allowed-diagonal C.",
    "A verified triangle is A-B-C-D-E with five corrective legs.",
    "A verified double three is W-X-Y and a verified triple three is W-X-Y-X2-Z; every component must be a valid corrective family.",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Canonical JSON cannot contain non-finite numbers.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}.")


def canonical_technical_agent_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_hash(value: Any) -> str:
    payload = value.to_dict() if isinstance(value, _JsonContract) else _json_value(value)
    if isinstance(payload, dict):
        payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Immutable JSON cannot contain non-finite numbers.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported immutable JSON value: {type(value).__name__}.")


def _normalize_utc(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name=field_name)


def _require_hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


def _string_tuple(
    value: Sequence[str], *, field_name: str, sort_unique: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    normalized = tuple(_require_text(item, field_name=field_name) for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} cannot contain duplicates.")
    return tuple(sorted(normalized)) if sort_unique else normalized


def _string_mapping(value: Mapping[str, str], *, field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    return MappingProxyType(
        {
            _require_text(key, field_name=f"{field_name}.key"): _require_text(
                item, field_name=f"{field_name}[{key!r}]"
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    )


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is invalid.") from exc


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], *, model_name: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"{model_name} contains unknown fields: {', '.join(sorted(unknown))}."
        )


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


def _find_forbidden_keys(value: Any, path: str = "decision_time_market_packet") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            child_path = f"{path}.{key}"
            if normalized in _FORBIDDEN_TECHNICAL_KEYS:
                violations.append(child_path)
            violations.extend(_find_forbidden_keys(child, child_path))
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            violations.extend(_find_forbidden_keys(child, f"{path}[{index}]"))
    return violations


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _packet_market_timestamps(value: Any, key: str | None = None) -> tuple[datetime, ...]:
    timestamps: list[datetime] = []
    timestamp_keys = {
        "date",
        "datetime",
        "time",
        "timestamp",
        "start_date",
        "end_date",
        "bar_start",
        "bar_end",
        "open_time",
        "close_time",
    }
    if isinstance(value, Mapping):
        columns = value.get("columns")
        bars = value.get("bars")
        if isinstance(columns, (tuple, list)) and isinstance(bars, (tuple, list)):
            time_indexes = [
                index
                for index, column in enumerate(columns)
                if str(column).strip().lower() in timestamp_keys | {"start", "end"}
            ]
            for bar in bars:
                if not isinstance(bar, (tuple, list)):
                    continue
                for index in time_indexes:
                    if index < len(bar):
                        parsed = _parse_timestamp(bar[index])
                        if parsed is not None:
                            timestamps.append(parsed)
        for child_key, child in value.items():
            normalized = str(child_key).strip().lower()
            if normalized in timestamp_keys:
                parsed = _parse_timestamp(child)
                if parsed is not None:
                    timestamps.append(parsed)
            timestamps.extend(_packet_market_timestamps(child, normalized))
    elif isinstance(value, (tuple, list)):
        for child in value:
            timestamps.extend(_packet_market_timestamps(child, key))
    return tuple(timestamps)


def _technical_input_hash(
    *,
    symbol: str,
    analysis_cutoff_utc: str,
    decision_time_market_packet: Mapping[str, Any],
    allowed_evidence_ids: Sequence[str],
    pivot_catalog: Sequence[Mapping[str, Any]],
) -> str:
    return canonical_sha256(
        {
            "symbol": symbol,
            "analysis_cutoff_utc": analysis_cutoff_utc,
            "decision_time_market_packet": decision_time_market_packet,
            "allowed_evidence_ids": sorted(allowed_evidence_ids),
            "pivot_catalog": pivot_catalog,
        }
    )


@dataclass(frozen=True, slots=True)
class TechnicalAgentRequest(_JsonContract):
    request_id: str
    source_analysis_run_id: int
    source_analysis_run_content_hash: str
    source_degree_resolution_id: int
    source_degree_resolution_content_hash: str
    symbol: str
    analysis_cutoff_utc: str
    decision_time_market_packet: Mapping[str, Any]
    decision_time_input_hash: str
    allowed_evidence_ids: tuple[str, ...]
    pivot_catalog: tuple[Mapping[str, Any], ...]
    blind_mode: bool
    shadow_mode: bool
    provider: str
    model: str | None
    prompt_versions: Mapping[str, str]
    created_at_utc: str
    schema_version: str = TECHNICAL_AGENT_REQUEST_SCHEMA_VERSION
    policy_version: str = TECHNICAL_AGENT_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        for name in ("source_analysis_run_id", "source_degree_resolution_id"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        for name in (
            "source_analysis_run_content_hash",
            "source_degree_resolution_content_hash",
        ):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "symbol", _require_text(self.symbol, field_name="symbol"))
        object.__setattr__(
            self,
            "analysis_cutoff_utc",
            _normalize_utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"),
        )
        if not isinstance(self.decision_time_market_packet, Mapping):
            raise TypeError("decision_time_market_packet must be a mapping.")
        packet = _freeze_json(self.decision_time_market_packet)
        unknown_top_level = set(packet) - _PERMITTED_PACKET_KEYS
        if unknown_top_level:
            raise ValueError(
                "Decision-time packet contains unsupported top-level fields: "
                + ", ".join(sorted(unknown_top_level))
                + "."
            )
        forbidden = _find_forbidden_keys(packet)
        if forbidden:
            raise ValueError(
                "Decision-time packet contains forbidden non-technical or prior-result fields: "
                + ", ".join(forbidden)
                + "."
            )
        cutoff = _utc(self.analysis_cutoff_utc)
        if any(item > cutoff for item in _packet_market_timestamps(packet)):
            raise ValueError("Decision-time packet contains market data after the analysis cutoff.")
        object.__setattr__(self, "decision_time_market_packet", packet)
        evidence_ids = _string_tuple(
            self.allowed_evidence_ids,
            field_name="allowed_evidence_ids",
            sort_unique=True,
        )
        object.__setattr__(self, "allowed_evidence_ids", evidence_ids)
        if isinstance(self.pivot_catalog, (str, bytes)) or not isinstance(
            self.pivot_catalog, Sequence
        ):
            raise TypeError("pivot_catalog must be a sequence of mappings.")
        pivots: list[Mapping[str, Any]] = []
        pivot_ids: set[str] = set()
        for index, raw in enumerate(self.pivot_catalog):
            if not isinstance(raw, Mapping):
                raise TypeError("pivot_catalog items must be mappings.")
            item = dict(raw)
            _reject_unknown(
                item,
                {"pivot_id", "date", "price", "evidence_id", "pivot_type"},
                model_name=f"pivot_catalog[{index}]",
            )
            pivot_id = _require_text(item.get("pivot_id"), field_name="pivot_id")
            if pivot_id in pivot_ids:
                raise ValueError("pivot_catalog contains duplicate pivot IDs.")
            pivot_ids.add(pivot_id)
            date = _normalize_utc(item.get("date"), field_name="pivot.date")
            if _utc(date) > cutoff:
                raise ValueError("pivot_catalog contains a pivot after the analysis cutoff.")
            price = item.get("price")
            if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(float(price)):
                raise ValueError("pivot.price must be finite and numeric.")
            evidence_id = _require_text(item.get("evidence_id"), field_name="pivot.evidence_id")
            if evidence_id not in evidence_ids:
                raise ValueError("A pivot references an unavailable evidence ID.")
            pivots.append(
                _freeze_json(
                    {
                        "pivot_id": pivot_id,
                        "date": date,
                        "price": float(price),
                        "evidence_id": evidence_id,
                        "pivot_type": _optional_text(item.get("pivot_type"), field_name="pivot_type"),
                    }
                )
            )
        if not pivots:
            raise ValueError("A decision-time request requires at least one verifiable pivot.")
        object.__setattr__(self, "pivot_catalog", tuple(pivots))
        if not isinstance(self.blind_mode, bool) or not isinstance(self.shadow_mode, bool):
            raise TypeError("blind_mode and shadow_mode must be boolean.")
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        prompts = _string_mapping(self.prompt_versions, field_name="prompt_versions")
        missing_prompts = set(TECHNICAL_AGENT_PROMPT_VERSIONS) - set(prompts)
        if missing_prompts:
            raise ValueError(
                "prompt_versions is missing roles: " + ", ".join(sorted(missing_prompts)) + "."
            )
        object.__setattr__(self, "prompt_versions", prompts)
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        expected_input_hash = _technical_input_hash(
            symbol=self.symbol,
            analysis_cutoff_utc=self.analysis_cutoff_utc,
            decision_time_market_packet=self.decision_time_market_packet,
            allowed_evidence_ids=self.allowed_evidence_ids,
            pivot_catalog=self.pivot_catalog,
        )
        if self.decision_time_input_hash:
            object.__setattr__(
                self,
                "decision_time_input_hash",
                _require_hash(self.decision_time_input_hash, field_name="decision_time_input_hash"),
            )
            if self.decision_time_input_hash != expected_input_hash:
                raise ValueError("decision_time_input_hash does not match the frozen technical input.")
        else:
            object.__setattr__(self, "decision_time_input_hash", expected_input_hash)
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "TechnicalAgentRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        values.setdefault("decision_time_input_hash", "")
        values.setdefault("prompt_versions", dict(TECHNICAL_AGENT_PROMPT_VERSIONS))
        if not values.get("request_id"):
            seed = {
                "source_analysis_run_id": values.get("source_analysis_run_id"),
                "source_degree_resolution_id": values.get("source_degree_resolution_id"),
                "symbol": values.get("symbol"),
                "analysis_cutoff_utc": values.get("analysis_cutoff_utc"),
                "created_at_utc": values.get("created_at_utc"),
            }
            values["request_id"] = f"technical_request_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "TechnicalAgentRequest":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("TechnicalAgentRequest content_hash does not match its payload.")
        return result


@dataclass(frozen=True, slots=True)
class CandidateWaveCount(_JsonContract):
    candidate_id: str
    role: TechnicalAgentRole
    request_id: str
    request_content_hash: str
    decision_time_input_hash: str
    symbol: str
    analysis_cutoff_utc: str
    degree_resolution: Mapping[str, Any]
    degree_resolution_content_hash: str
    pivot_reference_ids: tuple[str, ...]
    evidence_reference_ids: tuple[str, ...]
    concise_reasoning_summary: tuple[str, ...]
    distinguishing_features: tuple[str, ...]
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    generated_at_utc: str
    schema_version: str = CANDIDATE_WAVE_COUNT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _require_text(self.candidate_id, field_name="candidate_id"))
        role = _enum_value(self.role, TechnicalAgentRole, field_name="role")
        if role not in {
            TechnicalAgentRole.PRIMARY_WAVE_COUNTER,
            TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER,
        }:
            raise ValueError("Only counter roles may create CandidateWaveCount records.")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        for name in ("request_content_hash", "decision_time_input_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "symbol", _require_text(self.symbol, field_name="symbol"))
        object.__setattr__(self, "analysis_cutoff_utc", _normalize_utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        if not isinstance(self.degree_resolution, Mapping):
            raise TypeError("degree_resolution must be a mapping.")
        resolution = _freeze_json(self.degree_resolution)
        object.__setattr__(self, "degree_resolution", resolution)
        expected_resolution_hash = canonical_sha256(resolution)
        if self.degree_resolution_content_hash:
            object.__setattr__(self, "degree_resolution_content_hash", _require_hash(self.degree_resolution_content_hash, field_name="degree_resolution_content_hash"))
            if self.degree_resolution_content_hash != expected_resolution_hash:
                raise ValueError("degree_resolution_content_hash does not match.")
        else:
            object.__setattr__(self, "degree_resolution_content_hash", expected_resolution_hash)
        for name in (
            "pivot_reference_ids",
            "evidence_reference_ids",
            "concise_reasoning_summary",
            "distinguishing_features",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=name in {"pivot_reference_ids", "evidence_reference_ids"}))
        if not self.concise_reasoning_summary:
            raise ValueError("A candidate requires a concise reasoning summary.")
        if len(self.concise_reasoning_summary) > 12 or any(len(item) > 600 for item in self.concise_reasoning_summary):
            raise ValueError("Candidate reasoning must remain concise; hidden chain-of-thought is not accepted.")
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, field_name="prompt_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "generated_at_utc", _normalize_utc(self.generated_at_utc, field_name="generated_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "CandidateWaveCount":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        values.setdefault("degree_resolution_content_hash", "")
        if not values.get("candidate_id"):
            seed = {
                "role": _json_value(values.get("role")),
                "request_content_hash": values.get("request_content_hash"),
                "degree_resolution": values.get("degree_resolution"),
                "pivot_reference_ids": values.get("pivot_reference_ids", ()),
            }
            values["candidate_id"] = f"wave_candidate_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "CandidateWaveCount":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("CandidateWaveCount content_hash does not match its payload.")
        return result


@dataclass(frozen=True, slots=True)
class TechnicalAgentResult(_JsonContract):
    result_id: str
    role: TechnicalAgentRole
    status: TechnicalAgentResultStatus
    request_id: str
    request_content_hash: str
    decision_time_input_hash: str
    input_packet_hash: str
    prompt_hash: str
    structured_output_hash: str | None
    output_reference_ids: tuple[str, ...]
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    retry_count: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    started_at_utc: str
    completed_at_utc: str
    schema_version: str = TECHNICAL_AGENT_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _require_text(self.result_id, field_name="result_id"))
        object.__setattr__(self, "role", _enum_value(self.role, TechnicalAgentRole, field_name="role"))
        object.__setattr__(self, "status", _enum_value(self.status, TechnicalAgentResultStatus, field_name="status"))
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        for name in ("request_content_hash", "decision_time_input_hash", "input_packet_hash", "prompt_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "structured_output_hash", None if self.structured_output_hash is None else _require_hash(self.structured_output_hash, field_name="structured_output_hash"))
        object.__setattr__(self, "output_reference_ids", _string_tuple(self.output_reference_ids, field_name="output_reference_ids", sort_unique=True))
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, field_name="prompt_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        if not isinstance(self.retry_count, int) or isinstance(self.retry_count, bool) or self.retry_count < 0:
            raise ValueError("retry_count must be a non-negative integer.")
        object.__setattr__(self, "warnings", _string_tuple(self.warnings, field_name="warnings"))
        object.__setattr__(self, "errors", _string_tuple(self.errors, field_name="errors"))
        object.__setattr__(self, "started_at_utc", _normalize_utc(self.started_at_utc, field_name="started_at_utc"))
        object.__setattr__(self, "completed_at_utc", _normalize_utc(self.completed_at_utc, field_name="completed_at_utc"))
        if _utc(self.completed_at_utc) < _utc(self.started_at_utc):
            raise ValueError("An agent result cannot complete before it starts.")
        if self.status is TechnicalAgentResultStatus.COMPLETED and self.errors:
            raise ValueError("A completed agent result cannot contain errors.")
        if self.status is TechnicalAgentResultStatus.FAILED and not self.errors:
            raise ValueError("A failed agent result must explain its failure.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "TechnicalAgentResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("result_id"):
            seed = {
                "role": _json_value(values.get("role")),
                "request_content_hash": values.get("request_content_hash"),
                "input_packet_hash": values.get("input_packet_hash"),
                "prompt_hash": values.get("prompt_hash"),
                "structured_output_hash": values.get("structured_output_hash"),
                "completed_at_utc": values.get("completed_at_utc"),
            }
            values["result_id"] = f"technical_result_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "TechnicalAgentResult":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("TechnicalAgentResult content_hash does not match its payload.")
        return result


@dataclass(frozen=True, slots=True)
class CandidateValidationResult(_JsonContract):
    validation_id: str
    candidate_id: str
    candidate_content_hash: str
    request_id: str
    request_content_hash: str
    shape_errors: tuple[str, ...]
    hard_rule_errors: tuple[str, ...]
    reference_errors: tuple[str, ...]
    cutoff_errors: tuple[str, ...]
    role_errors: tuple[str, ...]
    soft_evidence_warnings: tuple[str, ...]
    structurally_valid: bool
    selection_eligible: bool
    validator_versions: Mapping[str, str]
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    validated_at_utc: str
    schema_version: str = CANDIDATE_VALIDATION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("validation_id", "candidate_id", "request_id"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        for name in ("candidate_content_hash", "request_content_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        error_fields = (
            "shape_errors",
            "hard_rule_errors",
            "reference_errors",
            "cutoff_errors",
            "role_errors",
            "soft_evidence_warnings",
        )
        for name in error_fields:
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name))
        if not isinstance(self.structurally_valid, bool) or not isinstance(self.selection_eligible, bool):
            raise TypeError("Validation flags must be boolean.")
        expected_structural = not (
            self.shape_errors
            or self.hard_rule_errors
            or self.reference_errors
            or self.cutoff_errors
        )
        expected_eligible = expected_structural and not self.role_errors
        if self.structurally_valid != expected_structural:
            raise ValueError("structurally_valid does not match deterministic errors.")
        if self.selection_eligible != expected_eligible:
            raise ValueError("selection_eligible does not match deterministic errors.")
        object.__setattr__(self, "validator_versions", _string_mapping(self.validator_versions, field_name="validator_versions"))
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, field_name="prompt_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "validated_at_utc", _normalize_utc(self.validated_at_utc, field_name="validated_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "CandidateValidationResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("validation_id"):
            seed = {
                "candidate_id": values.get("candidate_id"),
                "candidate_content_hash": values.get("candidate_content_hash"),
                "validated_at_utc": values.get("validated_at_utc"),
            }
            values["validation_id"] = f"candidate_validation_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "CandidateValidationResult":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("CandidateValidationResult content_hash does not match its payload.")
        return result


@dataclass(frozen=True, slots=True)
class EvidenceAuditResult(_JsonContract):
    audit_id: str
    candidate_id: str
    candidate_content_hash: str
    validation_id: str
    validation_content_hash: str
    supporting_evidence_ids: tuple[str, ...]
    contradictory_evidence_ids: tuple[str, ...]
    neutral_evidence_ids: tuple[str, ...]
    unavailable_evidence: tuple[str, ...]
    incomparable_evidence: tuple[str, ...]
    lesson_retrieval_ids: tuple[str, ...]
    lesson_content_hashes: tuple[str, ...]
    lesson_warnings: tuple[str, ...]
    concise_summary: tuple[str, ...]
    structural_validity_unchanged: bool
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    audited_at_utc: str
    schema_version: str = EVIDENCE_AUDIT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("audit_id", "candidate_id", "validation_id"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        for name in ("candidate_content_hash", "validation_content_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        for name in (
            "supporting_evidence_ids",
            "contradictory_evidence_ids",
            "neutral_evidence_ids",
            "lesson_retrieval_ids",
            "lesson_content_hashes",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=True))
        evidence_sets = (
            set(self.supporting_evidence_ids),
            set(self.contradictory_evidence_ids),
            set(self.neutral_evidence_ids),
        )
        if any(evidence_sets[left] & evidence_sets[right] for left, right in ((0, 1), (0, 2), (1, 2))):
            raise ValueError("An evidence ID cannot have multiple audit statuses.")
        for item in self.lesson_content_hashes:
            _require_hash(item, field_name="lesson_content_hashes")
        for name in (
            "unavailable_evidence",
            "incomparable_evidence",
            "lesson_warnings",
            "concise_summary",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name))
        if not self.concise_summary:
            raise ValueError("An evidence audit requires a concise summary.")
        if len(self.concise_summary) > 12 or any(len(item) > 600 for item in self.concise_summary):
            raise ValueError("Evidence-audit summaries must remain concise.")
        if not isinstance(self.structural_validity_unchanged, bool) or not self.structural_validity_unchanged:
            raise ValueError("The auditor cannot change deterministic structural validity.")
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, field_name="prompt_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "audited_at_utc", _normalize_utc(self.audited_at_utc, field_name="audited_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "EvidenceAuditResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("audit_id"):
            seed = {
                "candidate_id": values.get("candidate_id"),
                "validation_id": values.get("validation_id"),
                "audited_at_utc": values.get("audited_at_utc"),
            }
            values["audit_id"] = f"evidence_audit_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "EvidenceAuditResult":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("EvidenceAuditResult content_hash does not match its payload.")
        return result


@dataclass(frozen=True, slots=True)
class ShadowTechnicalResolution(_JsonContract):
    shadow_resolution_id: str
    request_id: str
    request_content_hash: str
    status: ShadowResolutionStatus
    selected_candidate_id: str | None
    ranked_valid_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]
    validation_result_ids: tuple[str, ...]
    evidence_audit_ids: tuple[str, ...]
    unresolved_reason: str | None
    concise_summary: tuple[str, ...]
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    resolved_at_utc: str
    schema_version: str = SHADOW_RESOLUTION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "shadow_resolution_id", _require_text(self.shadow_resolution_id, field_name="shadow_resolution_id"))
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        object.__setattr__(self, "request_content_hash", _require_hash(self.request_content_hash, field_name="request_content_hash"))
        object.__setattr__(self, "status", _enum_value(self.status, ShadowResolutionStatus, field_name="status"))
        object.__setattr__(self, "selected_candidate_id", _optional_text(self.selected_candidate_id, field_name="selected_candidate_id"))
        for name in (
            "ranked_valid_candidate_ids",
            "rejected_candidate_ids",
            "validation_result_ids",
            "evidence_audit_ids",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=name in {"rejected_candidate_ids", "validation_result_ids", "evidence_audit_ids"}))
        if set(self.ranked_valid_candidate_ids) & set(self.rejected_candidate_ids):
            raise ValueError("A candidate cannot be both valid-ranked and rejected.")
        if self.status is ShadowResolutionStatus.SELECTED:
            if self.selected_candidate_id is None:
                raise ValueError("A selected shadow resolution requires a candidate ID.")
            if not self.ranked_valid_candidate_ids or self.ranked_valid_candidate_ids[0] != self.selected_candidate_id:
                raise ValueError("The selected candidate must be first in the valid ranking.")
        elif self.selected_candidate_id is not None:
            raise ValueError("Only selected shadow resolutions may name a selected candidate.")
        object.__setattr__(self, "unresolved_reason", _optional_text(self.unresolved_reason, field_name="unresolved_reason"))
        if self.status is not ShadowResolutionStatus.SELECTED and not self.unresolved_reason:
            raise ValueError("A non-selected shadow result must explain why it is unresolved.")
        object.__setattr__(self, "concise_summary", _string_tuple(self.concise_summary, field_name="concise_summary"))
        if not self.concise_summary:
            raise ValueError("A shadow resolution requires a concise summary.")
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, field_name="prompt_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "resolved_at_utc", _normalize_utc(self.resolved_at_utc, field_name="resolved_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "ShadowTechnicalResolution":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("shadow_resolution_id"):
            seed = {
                "request_content_hash": values.get("request_content_hash"),
                "status": _json_value(values.get("status")),
                "selected_candidate_id": values.get("selected_candidate_id"),
                "ranked_valid_candidate_ids": values.get("ranked_valid_candidate_ids", ()),
                "resolved_at_utc": values.get("resolved_at_utc"),
            }
            values["shadow_resolution_id"] = f"shadow_resolution_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ShadowTechnicalResolution":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("ShadowTechnicalResolution content_hash does not match its payload.")
        return result


@dataclass(frozen=True, slots=True)
class ShadowResolutionComparison(_JsonContract):
    comparison_id: str
    shadow_resolution_id: str
    shadow_resolution_content_hash: str
    existing_degree_resolution_id: int
    existing_degree_resolution_content_hash: str
    selected_candidate_id: str | None
    selected_candidate_content_hash: str | None
    comparison_status: ShadowComparisonStatus
    existing_structure_hash: str
    selected_structure_hash: str | None
    differences: tuple[str, ...]
    concise_summary: tuple[str, ...]
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    compared_at_utc: str
    schema_version: str = SHADOW_COMPARISON_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("comparison_id", "shadow_resolution_id"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "shadow_resolution_content_hash", _require_hash(self.shadow_resolution_content_hash, field_name="shadow_resolution_content_hash"))
        if not isinstance(self.existing_degree_resolution_id, int) or isinstance(self.existing_degree_resolution_id, bool) or self.existing_degree_resolution_id < 1:
            raise ValueError("existing_degree_resolution_id must be a positive integer.")
        object.__setattr__(self, "existing_degree_resolution_content_hash", _require_hash(self.existing_degree_resolution_content_hash, field_name="existing_degree_resolution_content_hash"))
        object.__setattr__(self, "selected_candidate_id", _optional_text(self.selected_candidate_id, field_name="selected_candidate_id"))
        object.__setattr__(self, "selected_candidate_content_hash", None if self.selected_candidate_content_hash is None else _require_hash(self.selected_candidate_content_hash, field_name="selected_candidate_content_hash"))
        if (self.selected_candidate_id is None) != (self.selected_candidate_content_hash is None):
            raise ValueError("Selected candidate ID and hash must be supplied together.")
        object.__setattr__(self, "comparison_status", _enum_value(self.comparison_status, ShadowComparisonStatus, field_name="comparison_status"))
        object.__setattr__(self, "existing_structure_hash", _require_hash(self.existing_structure_hash, field_name="existing_structure_hash"))
        object.__setattr__(self, "selected_structure_hash", None if self.selected_structure_hash is None else _require_hash(self.selected_structure_hash, field_name="selected_structure_hash"))
        if self.comparison_status in {ShadowComparisonStatus.NO_SELECTION, ShadowComparisonStatus.UNAVAILABLE}:
            if self.selected_candidate_id is not None:
                raise ValueError("A no-selection comparison cannot name a selected candidate.")
        elif self.selected_candidate_id is None or self.selected_structure_hash is None:
            raise ValueError("A completed comparison requires a selected candidate and structure hash.")
        object.__setattr__(self, "differences", _string_tuple(self.differences, field_name="differences"))
        object.__setattr__(self, "concise_summary", _string_tuple(self.concise_summary, field_name="concise_summary"))
        if not self.concise_summary:
            raise ValueError("A shadow comparison requires a concise summary.")
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, field_name="prompt_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "compared_at_utc", _normalize_utc(self.compared_at_utc, field_name="compared_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "ShadowResolutionComparison":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("comparison_id"):
            seed = {
                "shadow_resolution_content_hash": values.get("shadow_resolution_content_hash"),
                "existing_degree_resolution_content_hash": values.get("existing_degree_resolution_content_hash"),
                "selected_candidate_content_hash": values.get("selected_candidate_content_hash"),
                "compared_at_utc": values.get("compared_at_utc"),
            }
            values["comparison_id"] = f"shadow_comparison_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ShadowResolutionComparison":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("ShadowResolutionComparison content_hash does not match its payload.")
        return result


def _model_tuple(
    value: Sequence[Any], model_type: type[Any], *, field_name: str
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    result: list[Any] = []
    for item in value:
        if isinstance(item, model_type):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(model_type.from_dict(item))
        else:
            raise TypeError(f"{field_name} contains an invalid item.")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TechnicalAgentOrchestration(_JsonContract):
    orchestration_id: str
    orchestration_version: int
    supersedes_orchestration_id: str | None
    request: TechnicalAgentRequest
    primary_result: TechnicalAgentResult
    alternative_result: TechnicalAgentResult
    candidates: tuple[CandidateWaveCount, ...]
    validation_results: tuple[CandidateValidationResult, ...]
    lesson_query_hashes: Mapping[str, str]
    retrieved_lessons: tuple[RetrievedLesson, ...]
    auditor_result: TechnicalAgentResult
    evidence_audits: tuple[EvidenceAuditResult, ...]
    final_result: TechnicalAgentResult
    shadow_resolution: ShadowTechnicalResolution
    shadow_comparison: ShadowResolutionComparison
    status: TechnicalOrchestrationStatus
    stage_trace: tuple[str, ...]
    stage_versions: Mapping[str, str]
    source_hashes: Mapping[str, str]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    provider: str
    model: str | None
    prompt_versions: Mapping[str, str]
    policy_version: str
    created_at_utc: str
    schema_version: str = TECHNICAL_AGENT_ORCHESTRATION_SCHEMA_VERSION
    calculation_version: str = TECHNICAL_AGENT_CALCULATION_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "orchestration_id", _require_text(self.orchestration_id, field_name="orchestration_id"))
        if not isinstance(self.orchestration_version, int) or isinstance(self.orchestration_version, bool) or self.orchestration_version < 1:
            raise ValueError("orchestration_version must be a positive integer.")
        object.__setattr__(self, "supersedes_orchestration_id", _optional_text(self.supersedes_orchestration_id, field_name="supersedes_orchestration_id"))
        if self.supersedes_orchestration_id == self.orchestration_id:
            raise ValueError("An orchestration cannot supersede itself.")
        if isinstance(self.request, Mapping):
            object.__setattr__(self, "request", TechnicalAgentRequest.from_dict(self.request))
        elif not isinstance(self.request, TechnicalAgentRequest):
            raise TypeError("request must be a TechnicalAgentRequest.")
        for name in ("primary_result", "alternative_result", "auditor_result", "final_result"):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                object.__setattr__(self, name, TechnicalAgentResult.from_dict(value))
            elif not isinstance(value, TechnicalAgentResult):
                raise TypeError(f"{name} must be a TechnicalAgentResult.")
        object.__setattr__(self, "candidates", _model_tuple(self.candidates, CandidateWaveCount, field_name="candidates"))
        object.__setattr__(self, "validation_results", _model_tuple(self.validation_results, CandidateValidationResult, field_name="validation_results"))
        object.__setattr__(self, "lesson_query_hashes", _string_mapping(self.lesson_query_hashes, field_name="lesson_query_hashes"))
        object.__setattr__(self, "retrieved_lessons", _model_tuple(self.retrieved_lessons, RetrievedLesson, field_name="retrieved_lessons"))
        object.__setattr__(self, "evidence_audits", _model_tuple(self.evidence_audits, EvidenceAuditResult, field_name="evidence_audits"))
        if isinstance(self.shadow_resolution, Mapping):
            object.__setattr__(self, "shadow_resolution", ShadowTechnicalResolution.from_dict(self.shadow_resolution))
        elif not isinstance(self.shadow_resolution, ShadowTechnicalResolution):
            raise TypeError("shadow_resolution must be a ShadowTechnicalResolution.")
        if isinstance(self.shadow_comparison, Mapping):
            object.__setattr__(self, "shadow_comparison", ShadowResolutionComparison.from_dict(self.shadow_comparison))
        elif not isinstance(self.shadow_comparison, ShadowResolutionComparison):
            raise TypeError("shadow_comparison must be a ShadowResolutionComparison.")
        object.__setattr__(self, "status", _enum_value(self.status, TechnicalOrchestrationStatus, field_name="status"))
        object.__setattr__(self, "stage_trace", _string_tuple(self.stage_trace, field_name="stage_trace"))
        object.__setattr__(self, "stage_versions", _string_mapping(self.stage_versions, field_name="stage_versions"))
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        for key, value in source_hashes.items():
            _require_hash(value, field_name=f"source_hashes[{key!r}]")
        object.__setattr__(self, "source_hashes", source_hashes)
        object.__setattr__(self, "warnings", _string_tuple(self.warnings, field_name="warnings"))
        object.__setattr__(self, "errors", _string_tuple(self.errors, field_name="errors"))
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_versions", _string_mapping(self.prompt_versions, field_name="prompt_versions"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        object.__setattr__(self, "calculation_version", _require_text(self.calculation_version, field_name="calculation_version"))

        expected_roles = {
            "primary_result": TechnicalAgentRole.PRIMARY_WAVE_COUNTER,
            "alternative_result": TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER,
            "auditor_result": TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR,
            "final_result": TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR,
        }
        for name, role in expected_roles.items():
            result = getattr(self, name)
            if result.role is not role:
                raise ValueError(f"{name} has the wrong technical-agent role.")
            if result.request_id != self.request.request_id or result.request_content_hash != self.request.content_hash:
                raise ValueError(f"{name} references a different request.")
        candidate_by_id = {item.candidate_id: item for item in self.candidates}
        if len(candidate_by_id) != len(self.candidates):
            raise ValueError("Candidate IDs must be unique.")
        validation_by_candidate = {item.candidate_id: item for item in self.validation_results}
        if set(validation_by_candidate) != set(candidate_by_id):
            raise ValueError("Every frozen candidate requires exactly one validation result.")
        for item in self.validation_results:
            candidate = candidate_by_id[item.candidate_id]
            if item.candidate_content_hash != candidate.content_hash:
                raise ValueError("A validation result references a different candidate hash.")
        audit_by_candidate = {item.candidate_id: item for item in self.evidence_audits}
        if len(audit_by_candidate) != len(self.evidence_audits):
            raise ValueError("Evidence audits must have unique candidate IDs.")
        if set(audit_by_candidate) - set(candidate_by_id):
            raise ValueError("An evidence audit references an unknown candidate.")
        for item in self.evidence_audits:
            candidate = candidate_by_id[item.candidate_id]
            validation = validation_by_candidate[item.candidate_id]
            if item.candidate_content_hash != candidate.content_hash or item.validation_content_hash != validation.content_hash:
                raise ValueError("An evidence audit references mismatched immutable inputs.")
        retrieved_by_id = {item.retrieval_id: item for item in self.retrieved_lessons}
        if len(retrieved_by_id) != len(self.retrieved_lessons):
            raise ValueError("Retrieved lesson references must be unique.")
        for item in self.retrieved_lessons:
            if item.content_hash != retrieved_lesson_content_hash(item):
                raise ValueError("A retrieved lesson hash does not match its payload.")
        for audit in self.evidence_audits:
            if set(audit.lesson_retrieval_ids) - set(retrieved_by_id):
                raise ValueError("An evidence audit references an unavailable retrieved lesson.")
        selected = self.shadow_resolution.selected_candidate_id
        if selected is not None:
            validation = validation_by_candidate.get(selected)
            if validation is None or not validation.selection_eligible:
                raise ValueError("The shadow resolution selected an ineligible candidate.")
        if self.shadow_comparison.shadow_resolution_content_hash != self.shadow_resolution.content_hash:
            raise ValueError("Shadow comparison references a different shadow result.")
        required_source_hashes = {
            "analysis_run": self.request.source_analysis_run_content_hash,
            "degree_resolution": self.request.source_degree_resolution_content_hash,
            "technical_request": self.request.content_hash,
            "primary_result": self.primary_result.content_hash,
            "alternative_result": self.alternative_result.content_hash,
            "auditor_result": self.auditor_result.content_hash,
            "final_result": self.final_result.content_hash,
            "validation_manifest": canonical_sha256([item.content_hash for item in self.validation_results]),
            "shadow_resolution": self.shadow_resolution.content_hash,
            "shadow_comparison": self.shadow_comparison.content_hash,
        }
        for key, expected in required_source_hashes.items():
            if self.source_hashes.get(key) != expected:
                raise ValueError(f"source_hashes[{key!r}] does not match.")
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "TechnicalAgentOrchestration":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("orchestration_id"):
            seed = {
                "request": _json_value(values.get("request")),
                "orchestration_version": values.get("orchestration_version", 1),
                "supersedes_orchestration_id": values.get("supersedes_orchestration_id"),
                "created_at_utc": values.get("created_at_utc"),
            }
            values["orchestration_id"] = f"technical_orchestration_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "TechnicalAgentOrchestration":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("TechnicalAgentOrchestration content_hash does not match its payload.")
        return result


def technical_agent_request_content_hash(
    value: TechnicalAgentRequest | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def candidate_wave_count_content_hash(
    value: CandidateWaveCount | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def candidate_validation_result_content_hash(
    value: CandidateValidationResult | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def evidence_audit_result_content_hash(
    value: EvidenceAuditResult | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def shadow_technical_resolution_content_hash(
    value: ShadowTechnicalResolution | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def shadow_resolution_comparison_content_hash(
    value: ShadowResolutionComparison | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def technical_agent_orchestration_content_hash(
    value: TechnicalAgentOrchestration | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def technical_agent_result_content_hash(
    value: TechnicalAgentResult | Mapping[str, Any]
) -> str:
    return _content_hash(value)


def _normalize_market_timestamp_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _collect_evidence_ids(value: Any) -> tuple[str, ...]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) == "evidence_id" and isinstance(child, str) and child.strip():
                result.add(child.strip())
            result.update(_collect_evidence_ids(child))
    elif isinstance(value, (tuple, list)):
        for child in value:
            result.update(_collect_evidence_ids(child))
    return tuple(sorted(result))


def _pivot_rows(value: Any, evidence_id: str | None = None) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        local_evidence = (
            str(value.get("evidence_id")).strip()
            if isinstance(value.get("evidence_id"), str) and value.get("evidence_id").strip()
            else evidence_id
        )
        pivots = value.get("pivots")
        if isinstance(pivots, (tuple, list)):
            for raw in pivots:
                if not isinstance(raw, Mapping):
                    continue
                date = raw.get("date") or raw.get("time") or raw.get("timestamp")
                price = raw.get("price")
                if date is None or not isinstance(price, (int, float)) or isinstance(price, bool):
                    continue
                if not math.isfinite(float(price)) or not local_evidence:
                    continue
                try:
                    normalized_date = _normalize_market_timestamp_text(date, field_name="pivot.date")
                except ValueError:
                    continue
                pivot_type = str(raw.get("type") or raw.get("pivot_type") or "observed")
                seed = {
                    "evidence_id": local_evidence,
                    "date": normalized_date,
                    "price": float(price),
                    "pivot_type": pivot_type,
                }
                result.append(
                    {
                        "pivot_id": f"pivot_{canonical_sha256(seed)[:24]}",
                        "date": normalized_date,
                        "price": float(price),
                        "evidence_id": local_evidence,
                        "pivot_type": pivot_type,
                    }
                )
        for child in value.values():
            result.extend(_pivot_rows(child, local_evidence))
    elif isinstance(value, (tuple, list)):
        for child in value:
            result.extend(_pivot_rows(child, evidence_id))
    return tuple(result)


def derive_pivot_catalog(
    decision_time_market_packet: Mapping[str, Any],
    *,
    analysis_cutoff_utc: str,
) -> tuple[Mapping[str, Any], ...]:
    """Build stable pivot references from explicit decision-time pivot lists."""

    cutoff = _utc(_normalize_utc(analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
    by_id: dict[str, Mapping[str, Any]] = {}
    for pivot in _pivot_rows(decision_time_market_packet):
        if _utc(pivot["date"]) <= cutoff:
            by_id[pivot["pivot_id"]] = _freeze_json(pivot)
    return tuple(by_id[key] for key in sorted(by_id))


def _latest_market_cutoff(packet: Mapping[str, Any]) -> str | None:
    dates: list[datetime] = []
    for item in packet.get("market_data", ()):
        if not isinstance(item, Mapping):
            continue
        parsed = _parse_timestamp(item.get("end_date"))
        if parsed is not None:
            dates.append(parsed)
    return max(dates).isoformat(timespec="seconds") if dates else None


def build_technical_agent_request_from_stored_records(
    analysis_run: Mapping[str, Any],
    degree_resolution: Mapping[str, Any],
    *,
    provider: str,
    model: str | None,
    created_at_utc: str,
    analysis_cutoff_utc: str | None = None,
    blind_mode: bool = False,
    shadow_mode: bool = False,
) -> TechnicalAgentRequest:
    """Create a pure shadow request without exposing either stored count.

    The source records are referenced and hashed for comparison/audit. Their
    ``response`` payloads are intentionally absent from the counter packet.
    Company context, knowledge retrieval, reviews, and prior resolutions are
    not copied.
    """

    run = copy.deepcopy(dict(analysis_run))
    resolution = copy.deepcopy(dict(degree_resolution))
    run_id = run.get("id")
    resolution_id = resolution.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise ValueError("Stored analysis run requires a positive id.")
    if not isinstance(resolution_id, int) or isinstance(resolution_id, bool) or resolution_id < 1:
        raise ValueError("Stored degree resolution requires a positive id.")
    if resolution.get("run_id") != run_id:
        raise ValueError("Degree resolution does not belong to the supplied analysis run.")
    symbol = _require_text(run.get("symbol"), field_name="analysis_run.symbol")
    response_symbol = resolution.get("response", {}).get("symbol") if isinstance(resolution.get("response"), Mapping) else None
    if response_symbol and str(response_symbol).casefold() != symbol.casefold():
        raise ValueError("Stored degree resolution symbol differs from the source run.")
    evidence = run.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("Stored analysis run has no decision-time evidence packet.")
    packet = {
        key: copy.deepcopy(evidence[key])
        for key in _PERMITTED_PACKET_KEYS
        if key in evidence
    }
    cutoff_candidate = analysis_cutoff_utc
    if cutoff_candidate is None and isinstance(resolution.get("response"), Mapping):
        cutoff_candidate = resolution["response"].get("data_cutoff")
    if cutoff_candidate is None and isinstance(run.get("response"), Mapping):
        cutoff_candidate = run["response"].get("data_cutoff")
    if cutoff_candidate is None:
        cutoff_candidate = _latest_market_cutoff(packet)
    cutoff = _normalize_market_timestamp_text(cutoff_candidate, field_name="analysis_cutoff_utc")
    evidence_ids = _collect_evidence_ids(packet)
    if not evidence_ids:
        raise ValueError("Stored technical packet has no verifiable evidence IDs.")
    pivots = derive_pivot_catalog(packet, analysis_cutoff_utc=cutoff)
    return TechnicalAgentRequest.create(
        source_analysis_run_id=run_id,
        source_analysis_run_content_hash=stored_record_content_hash(run),
        source_degree_resolution_id=resolution_id,
        source_degree_resolution_content_hash=stored_record_content_hash(resolution),
        symbol=symbol,
        analysis_cutoff_utc=cutoff,
        decision_time_market_packet=packet,
        allowed_evidence_ids=evidence_ids,
        pivot_catalog=pivots,
        blind_mode=blind_mode,
        shadow_mode=shadow_mode,
        provider=provider,
        model=model,
        prompt_versions=dict(TECHNICAL_AGENT_PROMPT_VERSIONS),
        created_at_utc=created_at_utc,
    )


def validate_technical_agent_request(request: TechnicalAgentRequest) -> tuple[str, ...]:
    errors: list[str] = []
    if request.content_hash != technical_agent_request_content_hash(request):
        errors.append("Technical-agent request content_hash does not match.")
    expected_input = _technical_input_hash(
        symbol=request.symbol,
        analysis_cutoff_utc=request.analysis_cutoff_utc,
        decision_time_market_packet=request.decision_time_market_packet,
        allowed_evidence_ids=request.allowed_evidence_ids,
        pivot_catalog=request.pivot_catalog,
    )
    if request.decision_time_input_hash != expected_input:
        errors.append("Decision-time input hash does not match.")
    if _find_forbidden_keys(request.decision_time_market_packet):
        errors.append("Decision-time packet contains forbidden non-technical inputs.")
    cutoff = _utc(request.analysis_cutoff_utc)
    if any(item > cutoff for item in _packet_market_timestamps(request.decision_time_market_packet)):
        errors.append("Decision-time packet contains a market timestamp after cutoff.")
    if any(_utc(str(item["date"])) > cutoff for item in request.pivot_catalog):
        errors.append("Pivot catalog contains a post-cutoff pivot.")
    return tuple(errors)


def _strict_schema_errors(value: Any, schema: Mapping[str, Any], path: str = "output") -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    types = tuple(expected_type) if isinstance(expected_type, list) else (expected_type,)

    def matches(name: Any) -> bool:
        if name == "object":
            return isinstance(value, Mapping)
        if name == "array":
            return isinstance(value, list)
        if name == "string":
            return isinstance(value, str)
        if name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
        if name == "boolean":
            return isinstance(value, bool)
        if name == "null":
            return value is None
        return name is None

    if types != (None,) and not any(matches(name) for name in types):
        errors.append(f"{path} has the wrong JSON type.")
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} is outside its controlled enum.")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for required in schema.get("required", ()):
            if required not in value:
                errors.append(f"{path} is missing {required}.")
        if schema.get("additionalProperties") is False:
            for key in set(value) - set(properties):
                errors.append(f"{path} contains unknown field {key!r}.")
        for key, child in value.items():
            if key in properties:
                errors.extend(_strict_schema_errors(child, properties[key], f"{path}.{key}"))
    elif isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            errors.extend(_strict_schema_errors(child, schema["items"], f"{path}[{index}]"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} is below its minimum.")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} exceeds its maximum.")
    return errors


_STRING_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
}

COUNTER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidate"],
    "properties": {
        "candidate": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "degree_resolution",
                "pivot_reference_ids",
                "evidence_reference_ids",
                "concise_reasoning_summary",
                "distinguishing_features",
            ],
            "properties": {
                "degree_resolution": copy.deepcopy(DEGREE_RESOLUTION_SCHEMA),
                "pivot_reference_ids": _STRING_ARRAY_SCHEMA,
                "evidence_reference_ids": _STRING_ARRAY_SCHEMA,
                "concise_reasoning_summary": _STRING_ARRAY_SCHEMA,
                "distinguishing_features": _STRING_ARRAY_SCHEMA,
            },
        }
    },
}

AUDITOR_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["audits"],
    "properties": {
        "audits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "candidate_id",
                    "supporting_evidence_ids",
                    "contradictory_evidence_ids",
                    "neutral_evidence_ids",
                    "unavailable_evidence",
                    "incomparable_evidence",
                    "lesson_retrieval_ids",
                    "lesson_warnings",
                    "concise_summary",
                ],
                "properties": {
                    "candidate_id": {"type": "string"},
                    "supporting_evidence_ids": _STRING_ARRAY_SCHEMA,
                    "contradictory_evidence_ids": _STRING_ARRAY_SCHEMA,
                    "neutral_evidence_ids": _STRING_ARRAY_SCHEMA,
                    "unavailable_evidence": _STRING_ARRAY_SCHEMA,
                    "incomparable_evidence": _STRING_ARRAY_SCHEMA,
                    "lesson_retrieval_ids": _STRING_ARRAY_SCHEMA,
                    "lesson_warnings": _STRING_ARRAY_SCHEMA,
                    "concise_summary": _STRING_ARRAY_SCHEMA,
                },
            },
        }
    },
}

FINAL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "selected_candidate_id",
        "ranked_valid_candidate_ids",
        "rejected_candidate_ids",
        "unresolved_reason",
        "concise_summary",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "selected",
                "unresolved",
                "insufficient_evidence",
                "rejected_invalid",
            ],
        },
        "selected_candidate_id": {"type": ["string", "null"]},
        "ranked_valid_candidate_ids": _STRING_ARRAY_SCHEMA,
        "rejected_candidate_ids": _STRING_ARRAY_SCHEMA,
        "unresolved_reason": {"type": ["string", "null"]},
        "concise_summary": _STRING_ARRAY_SCHEMA,
    },
}


_PRIMARY_SYSTEM_PROMPT = """You are the Phase 11D1 Primary Wave Counter in shadow mode.
Use only the supplied frozen decision-time technical packet. Lock price and pivots first,
apply hard Elliott rules before all indicators, and return one strongest price-first count.
RSI, volume, EWO, MACD, Fibonacci, duration, scale, and channels are soft evidence only.
Do not use prior analyses, degree resolutions, alternatives, memory, fundamentals, news,
company knowledge, or post-cutoff data. Cite only supplied evidence and pivot IDs. Return
exactly one JSON object matching the schema. Give concise conclusions, never hidden
chain-of-thought. Do not make trades or modify the live analysis."""

_ALTERNATIVE_SYSTEM_PROMPT = """You are the Phase 11D1 Alternative Wave Counter in shadow mode.
You are blind to the Primary Counter. Use only the same supplied frozen decision-time
technical packet and independently search for one materially different price-valid count.
Apply hard Elliott rules first. Indicators and Fibonacci are soft evidence only. Do not use
prior counts, memory, fundamentals, news, company knowledge, or post-cutoff data. Cite only
supplied evidence and pivot IDs. Return exactly one JSON object matching the schema with
concise conclusions, never hidden chain-of-thought. Do not modify live analysis."""

_AUDITOR_SYSTEM_PROMPT = """You are the Phase 11D1 Rules and Evidence Auditor.
The candidate outputs are already frozen and their hard-rule validation is immutable. Audit
supportive, contradictory, neutral, unavailable, and incomparable decision-time evidence.
Approved lessons are audit warnings only and retain their provenance. Never create or repair
a count, never promote a hard-rule-invalid candidate, and never invalidate a structurally
valid count with RSI, volume, EWO, MACD, Fibonacci, duration, scale, or channels. Reference
only supplied candidate, evidence, and retrieved-lesson IDs. Return concise structured JSON,
not hidden chain-of-thought."""

_FINAL_SYSTEM_PROMPT = """You are the Phase 11D1 Final Technical Orchestrator in shadow mode.
You may select and rank only the frozen candidate IDs explicitly marked selection-eligible.
You may not create a count, change a pivot, alter a candidate, override deterministic hard
validation, or use an invalid candidate. If the supplied evidence cannot distinguish valid
counts, return unresolved. If no valid candidate exists, return rejected_invalid. Return
only concise structured JSON and never hidden chain-of-thought. The result cannot affect the
live Elliott analysis."""


def _counter_input_packet(
    request: TechnicalAgentRequest, role: TechnicalAgentRole
) -> dict[str, Any]:
    return {
        "agent_role": role.value,
        "request_reference": {
            "request_id": request.request_id,
            "request_content_hash": request.content_hash,
            "source_analysis_run_id": request.source_analysis_run_id,
            "symbol": request.symbol,
            "analysis_cutoff_utc": request.analysis_cutoff_utc,
            "decision_time_input_hash": request.decision_time_input_hash,
            "blind_mode": request.blind_mode,
        },
        "hard_rule_contract": {
            "standard_degrees_high_to_low": list(STANDARD_DEGREES),
            "mandatory_timeframe_matrix": {
                degree: list(timeframes) for degree, timeframes in DEGREE_TIMEFRAMES.items()
            },
            "validator": "validate_degree_hierarchy_rules",
            "validator_version": TECHNICAL_AGENT_STAGE_VERSIONS[
                "candidate_validation"
            ],
            "rules": list(_HARD_RULE_SUMMARY),
            "indicator_policy": "soft evidence only",
        },
        "allowed_evidence_ids": list(request.allowed_evidence_ids),
        "pivot_catalog": _json_value(request.pivot_catalog),
        "decision_time_market_packet": _json_value(request.decision_time_market_packet),
    }


def _user_prompt(packet: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    return (
        "Return one JSON object matching this schema. Use only the frozen input packet.\n\n"
        "OUTPUT SCHEMA:\n"
        + json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n\nFROZEN INPUT:\n"
        + canonical_technical_agent_json(packet)
    )


def _candidate_resolution_references(resolution: Mapping[str, Any]) -> tuple[set[str], tuple[tuple[str, Any, Any], ...]]:
    evidence: set[str] = set()
    anchors: list[tuple[str, Any, Any]] = []
    for citation in resolution.get("citations", ()):
        if isinstance(citation, Mapping) and isinstance(citation.get("evidence_id"), str):
            evidence.add(str(citation["evidence_id"]))
    for node in resolution.get("degree_hierarchy", ()):
        if not isinstance(node, Mapping):
            continue
        for evidence_id in node.get("evidence", ()):
            if isinstance(evidence_id, str):
                evidence.add(evidence_id)
        for side in ("start", "end"):
            anchor = node.get(side)
            if not isinstance(anchor, Mapping):
                continue
            date = anchor.get("date")
            price = anchor.get("price")
            if date is not None and price is not None:
                anchors.append((f"{node.get('wave_id')}.{side}", date, price))
    return evidence, tuple(anchors)


def _anchor_match(
    date: Any,
    price: Any,
    pivots: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    try:
        normalized_date = _normalize_market_timestamp_text(date, field_name="anchor.date")
    except ValueError:
        return ()
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(float(price)):
        return ()
    matches = []
    for pivot in pivots:
        if str(pivot.get("date")) != normalized_date:
            continue
        reference_price = float(pivot["price"])
        tolerance = max(1e-8, abs(reference_price) * 1e-9)
        if math.isclose(float(price), reference_price, rel_tol=0.0, abs_tol=tolerance):
            matches.append(str(pivot["pivot_id"]))
    return tuple(sorted(matches))


def _resolution_structure_signature(resolution: Mapping[str, Any]) -> Mapping[str, Any]:
    nodes = [item for item in resolution.get("degree_hierarchy", ()) if isinstance(item, Mapping)]
    by_id = {str(item.get("wave_id")): item for item in nodes}
    signatures: list[dict[str, Any]] = []
    for node in nodes:
        parent = by_id.get(str(node.get("parent_wave_id")))
        signatures.append(
            {
                "parent": (
                    {
                        "degree": parent.get("degree"),
                        "sequence_position": parent.get("sequence_position"),
                        "start": parent.get("start"),
                        "end": parent.get("end"),
                    }
                    if parent is not None
                    else None
                ),
                "degree": node.get("degree"),
                "sequence_position": node.get("sequence_position"),
                "timeframe": node.get("timeframe"),
                "direction": node.get("direction"),
                "start": node.get("start"),
                "end": node.get("end"),
                "structure_family": structure_family(node.get("structure")),
                "completion_status": node.get("completion_status"),
                "child_structure_status": node.get("child_structure_status"),
            }
        )
    signatures.sort(
        key=lambda item: canonical_technical_agent_json(
            {
                "start": item.get("start"),
                "end": item.get("end"),
                "degree": item.get("degree"),
                "sequence_position": item.get("sequence_position"),
            }
        )
    )
    return _freeze_json(
        {
            "symbol": resolution.get("symbol"),
            "scale_mode": resolution.get("scale_mode"),
            "nodes": signatures,
            "active_position": resolution.get("active_position"),
        }
    )


def validate_candidate_wave_count(
    candidate: CandidateWaveCount,
    request: TechnicalAgentRequest,
    *,
    primary_candidate: CandidateWaveCount | None = None,
    validated_at_utc: str,
) -> CandidateValidationResult:
    resolution = candidate.degree_resolution
    shape_errors = validate_degree_resolution_shape(
        _json_value(resolution), source_run_id=request.source_analysis_run_id
    )
    hard_rule_errors = validate_degree_hierarchy_rules(_json_value(resolution))
    reference_errors: list[str] = []
    cutoff_errors: list[str] = []
    role_errors: list[str] = []
    soft_warnings: list[str] = []

    if candidate.request_id != request.request_id or candidate.request_content_hash != request.content_hash:
        reference_errors.append("Candidate references a different technical request.")
    if candidate.decision_time_input_hash != request.decision_time_input_hash:
        reference_errors.append("Candidate references a different decision-time input hash.")
    if candidate.symbol.casefold() != request.symbol.casefold():
        reference_errors.append("Candidate symbol differs from the frozen request.")
    if resolution.get("symbol") != request.symbol:
        reference_errors.append("Degree-resolution symbol differs from the frozen request.")
    resolution_cutoff = _parse_timestamp(resolution.get("data_cutoff"))
    if resolution_cutoff is None:
        cutoff_errors.append("Candidate has no verifiable data cutoff.")
    elif resolution_cutoff != _utc(request.analysis_cutoff_utc):
        cutoff_errors.append("Candidate data cutoff differs from the frozen decision-time cutoff.")

    referenced_evidence, anchors = _candidate_resolution_references(resolution)
    unknown_evidence = (set(candidate.evidence_reference_ids) | referenced_evidence) - set(request.allowed_evidence_ids)
    if unknown_evidence:
        reference_errors.append(
            "Candidate contains invented evidence references: "
            + ", ".join(sorted(unknown_evidence))
            + "."
        )
    missing_declared = referenced_evidence - set(candidate.evidence_reference_ids)
    if missing_declared:
        reference_errors.append(
            "Candidate omitted cited evidence from evidence_reference_ids: "
            + ", ".join(sorted(missing_declared))
            + "."
        )
    unknown_pivots = set(candidate.pivot_reference_ids) - {
        str(item["pivot_id"]) for item in request.pivot_catalog
    }
    if unknown_pivots:
        reference_errors.append(
            "Candidate contains invented pivot references: "
            + ", ".join(sorted(unknown_pivots))
            + "."
        )
    declared_pivots = set(candidate.pivot_reference_ids)
    for label, date, price in anchors:
        matches = _anchor_match(date, price, request.pivot_catalog)
        if not matches:
            reference_errors.append(f"Candidate anchor {label} is not present in the frozen pivot catalog.")
        elif not (declared_pivots & set(matches)):
            reference_errors.append(f"Candidate anchor {label} lacks its matching pivot reference ID.")
        parsed = _parse_timestamp(date)
        if parsed is None:
            cutoff_errors.append(f"Candidate anchor {label} has an invalid timestamp.")
        elif parsed > _utc(request.analysis_cutoff_utc):
            cutoff_errors.append(f"Candidate anchor {label} is after the analysis cutoff.")
    if candidate.role is TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER and primary_candidate is not None:
        if canonical_sha256(_resolution_structure_signature(candidate.degree_resolution)) == canonical_sha256(
            _resolution_structure_signature(primary_candidate.degree_resolution)
        ):
            role_errors.append("Alternative candidate is not materially different from the frozen primary candidate.")
    for node in resolution.get("degree_hierarchy", ()):
        if not isinstance(node, Mapping):
            continue
        for concern in node.get("concerns", ()):
            text = str(concern)
            if any(token in text.casefold() for token in _SOFT_EVIDENCE_TOKENS):
                soft_warnings.append(text)

    structurally_valid = not (shape_errors or hard_rule_errors or reference_errors or cutoff_errors)
    selection_eligible = structurally_valid and not role_errors
    return CandidateValidationResult.create(
        candidate_id=candidate.candidate_id,
        candidate_content_hash=candidate.content_hash,
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        shape_errors=tuple(shape_errors),
        hard_rule_errors=tuple(hard_rule_errors),
        reference_errors=tuple(reference_errors),
        cutoff_errors=tuple(cutoff_errors),
        role_errors=tuple(role_errors),
        soft_evidence_warnings=tuple(dict.fromkeys(soft_warnings)),
        structurally_valid=structurally_valid,
        selection_eligible=selection_eligible,
        validator_versions={
            "degree_shape": "validate_degree_resolution_shape:current",
            "elliott_hard_rules": "validate_degree_hierarchy_rules:current",
            "reference_validation": TECHNICAL_AGENT_STAGE_VERSIONS["candidate_validation"],
        },
        provider="deterministic",
        model=None,
        prompt_version="not_applicable",
        policy_version=TECHNICAL_AGENT_POLICY_VERSION,
        validated_at_utc=validated_at_utc,
    )


class MistakeMemoryReadStore(Protocol):
    def list_mistake_memory_lessons(self, **kwargs: Any) -> list[MistakeMemoryLesson]: ...

    def list_mistake_memory_sources(self, **kwargs: Any) -> list[LessonSource]: ...

    def list_mistake_memory_events(self, **kwargs: Any) -> list[LessonEvent]: ...


@dataclass(frozen=True, slots=True)
class _ProviderCall:
    output: Mapping[str, Any] | None
    output_hash: str | None
    retry_count: int
    errors: tuple[str, ...]


def _provider_method(provider: AnalysisProvider) -> Callable[..., dict[str, Any]]:
    strict = getattr(provider, "generate_strict_json", None)
    return strict if callable(strict) else provider.generate


def _call_provider(
    provider: AnalysisProvider,
    *,
    system_prompt: str,
    user_prompt: str,
    schema: Mapping[str, Any],
    packet: Mapping[str, Any],
    max_retries: int,
) -> _ProviderCall:
    method = _provider_method(provider)
    errors: list[str] = []
    last_output_hash: str | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = method(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=copy.deepcopy(dict(schema)),
                packet=copy.deepcopy(dict(packet)),
            )
            if not isinstance(raw, Mapping):
                raise ProviderError("Provider output must be a JSON object.")
            output = copy.deepcopy(dict(raw))
            last_output_hash = canonical_sha256(output)
            schema_errors = _strict_schema_errors(output, schema)
            if schema_errors:
                raise ProviderError("Strict structured-output validation failed: " + " ".join(schema_errors))
            return _ProviderCall(
                output=_freeze_json(output),
                output_hash=last_output_hash,
                retry_count=attempt,
                errors=(),
            )
        except (ProviderError, TypeError, ValueError, KeyError) as exc:
            errors.append(f"attempt {attempt + 1}: {exc}")
    return _ProviderCall(
        output=None,
        output_hash=last_output_hash,
        retry_count=max_retries,
        errors=tuple(errors),
    )


def _result(
    *,
    request: TechnicalAgentRequest,
    role: TechnicalAgentRole,
    status: TechnicalAgentResultStatus,
    packet: Mapping[str, Any],
    system_prompt: str,
    schema: Mapping[str, Any],
    prompt_version: str,
    timestamp: str,
    structured_output_hash: str | None = None,
    output_reference_ids: Sequence[str] = (),
    retry_count: int = 0,
    warnings: Sequence[str] = (),
    errors: Sequence[str] = (),
) -> TechnicalAgentResult:
    user_prompt = _user_prompt(packet, schema)
    return TechnicalAgentResult.create(
        role=role,
        status=status,
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        decision_time_input_hash=request.decision_time_input_hash,
        input_packet_hash=canonical_sha256(packet),
        prompt_hash=canonical_sha256(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema": schema,
                "prompt_version": prompt_version,
            }
        ),
        structured_output_hash=structured_output_hash,
        output_reference_ids=tuple(output_reference_ids),
        provider=request.provider,
        model=request.model,
        prompt_version=prompt_version,
        policy_version=TECHNICAL_AGENT_POLICY_VERSION,
        retry_count=retry_count,
        warnings=tuple(warnings),
        errors=tuple(errors),
        started_at_utc=timestamp,
        completed_at_utc=timestamp,
    )


def _counter_candidate(
    request: TechnicalAgentRequest,
    role: TechnicalAgentRole,
    output: Mapping[str, Any],
    *,
    timestamp: str,
) -> CandidateWaveCount:
    payload = output["candidate"]
    prompt_version = request.prompt_versions[role.value]
    return CandidateWaveCount.create(
        role=role,
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        decision_time_input_hash=request.decision_time_input_hash,
        symbol=request.symbol,
        analysis_cutoff_utc=request.analysis_cutoff_utc,
        degree_resolution=payload["degree_resolution"],
        pivot_reference_ids=tuple(payload["pivot_reference_ids"]),
        evidence_reference_ids=tuple(payload["evidence_reference_ids"]),
        concise_reasoning_summary=tuple(payload["concise_reasoning_summary"]),
        distinguishing_features=tuple(payload["distinguishing_features"]),
        provider=request.provider,
        model=request.model,
        prompt_version=prompt_version,
        policy_version=TECHNICAL_AGENT_POLICY_VERSION,
        generated_at_utc=timestamp,
    )


def _candidate_query(
    request: TechnicalAgentRequest,
    candidate: CandidateWaveCount,
    *,
    current_forecast_id: str | None = None,
) -> LessonRetrievalQuery:
    nodes = [
        item
        for item in candidate.degree_resolution.get("degree_hierarchy", ())
        if isinstance(item, Mapping)
    ]
    active_ids = set(
        candidate.degree_resolution.get("active_position", {}).get("wave_ids", ())
        if isinstance(candidate.degree_resolution.get("active_position"), Mapping)
        else ()
    )
    selected = next((item for item in nodes if item.get("wave_id") in active_ids), None)
    if selected is None and nodes:
        selected = sorted(
            nodes,
            key=lambda item: (
                str(item.get("start", {}).get("date") if isinstance(item.get("start"), Mapping) else ""),
                str(item.get("wave_id", "")),
            ),
        )[-1]
    selected = selected or {}
    return LessonRetrievalQuery.create(
        analysis_cutoff_utc=request.analysis_cutoff_utc,
        blind_mode=request.blind_mode,
        current_forecast_id=current_forecast_id,
        symbol=request.symbol,
        asset_class=None,
        timeframe=(str(selected.get("timeframe")) if selected.get("timeframe") else None),
        degree=(str(selected.get("degree")) if selected.get("degree") else None),
        wave_role=(str(selected.get("sequence_position")) if selected.get("sequence_position") else None),
        pattern_family=(structure_family(selected.get("structure")) or None),
        direction=(str(selected.get("direction")) if selected.get("direction") else None),
    )


def _retrieve_candidate_lessons(
    request: TechnicalAgentRequest,
    candidates: Sequence[CandidateWaveCount],
    store: MistakeMemoryReadStore | None,
    *,
    current_forecast_id: str | None = None,
) -> tuple[
    tuple[RetrievedLesson, ...],
    Mapping[str, str],
    Mapping[str, tuple[str, ...]],
    tuple[str, ...],
]:
    if request.blind_mode:
        return (), MappingProxyType({}), MappingProxyType({}), ()
    if not candidates:
        return (), MappingProxyType({}), MappingProxyType({}), ()
    if store is None:
        return (
            (),
            MappingProxyType({}),
            MappingProxyType({}),
            ("Mistake-memory store was unavailable; no lesson evidence was evaluated.",),
        )
    lessons = tuple(store.list_mistake_memory_lessons(limit=10_000))
    sources = tuple(store.list_mistake_memory_sources(limit=10_000))
    events = tuple(store.list_mistake_memory_events(limit=10_000))
    retrieved: list[RetrievedLesson] = []
    query_hashes: dict[str, str] = {}
    by_candidate: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        query = _candidate_query(
            request,
            candidate,
            current_forecast_id=current_forecast_id,
        )
        query_hashes[candidate.candidate_id] = query.content_hash
        matches = retrieve_applicable_lessons(
            query,
            lessons=lessons,
            sources=sources,
            events=events,
        )
        retrieved.extend(matches)
        by_candidate[candidate.candidate_id] = tuple(item.retrieval_id for item in matches)
    return (
        tuple(retrieved),
        MappingProxyType(dict(sorted(query_hashes.items()))),
        MappingProxyType(dict(sorted(by_candidate.items()))),
        (),
    )


def technical_counter_input_hash(
    request: TechnicalAgentRequest,
    role: TechnicalAgentRole | str,
) -> str:
    """Return the hash of a blind counter packet without exposing lessons."""

    normalized_role = _enum_value(role, TechnicalAgentRole, field_name="role")
    if normalized_role not in {
        TechnicalAgentRole.PRIMARY_WAVE_COUNTER,
        TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER,
    }:
        raise ValueError("Only Primary and Alternative counter inputs are available.")
    return canonical_sha256(_counter_input_packet(request, normalized_role))


def _auditor_packet(
    request: TechnicalAgentRequest,
    candidates: Sequence[CandidateWaveCount],
    validations: Sequence[CandidateValidationResult],
    retrieved_lessons: Sequence[RetrievedLesson],
    lesson_ids_by_candidate: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    validation_by_candidate = {item.candidate_id: item for item in validations}
    lesson_by_id = {item.retrieval_id: item for item in retrieved_lessons}
    return {
        "agent_role": TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR.value,
        "request_reference": {
            "request_id": request.request_id,
            "request_content_hash": request.content_hash,
            "analysis_cutoff_utc": request.analysis_cutoff_utc,
            "decision_time_input_hash": request.decision_time_input_hash,
        },
        "frozen_candidates": [item.to_dict() for item in candidates],
        "deterministic_validations": [item.to_dict() for item in validations],
        "decision_time_technical_evidence": _json_value(request.decision_time_market_packet),
        "allowed_evidence_ids": list(request.allowed_evidence_ids),
        "retrieved_lessons_by_candidate": {
            candidate_id: [lesson_by_id[item].to_dict() for item in retrieval_ids]
            for candidate_id, retrieval_ids in sorted(lesson_ids_by_candidate.items())
        },
        "audit_boundary": {
            "may_create_counts": False,
            "may_change_hard_validation": False,
            "may_use_soft_evidence_as_invalidation": False,
            "retrieval_is_post_count": True,
        },
    }


def _parse_audits(
    request: TechnicalAgentRequest,
    output: Mapping[str, Any],
    candidates: Sequence[CandidateWaveCount],
    validations: Sequence[CandidateValidationResult],
    retrieved_lessons: Sequence[RetrievedLesson],
    lesson_ids_by_candidate: Mapping[str, tuple[str, ...]],
    *,
    timestamp: str,
) -> tuple[EvidenceAuditResult, ...]:
    candidate_by_id = {item.candidate_id: item for item in candidates}
    validation_by_id = {item.candidate_id: item for item in validations}
    lesson_by_id = {item.retrieval_id: item for item in retrieved_lessons}
    raw_audits = output.get("audits", ())
    raw_ids = [item.get("candidate_id") for item in raw_audits if isinstance(item, Mapping)]
    if len(raw_ids) != len(set(raw_ids)) or set(raw_ids) != set(candidate_by_id):
        raise ValueError("Auditor must return exactly one audit for every frozen candidate ID.")
    result: list[EvidenceAuditResult] = []
    for raw in raw_audits:
        candidate_id = str(raw["candidate_id"])
        evidence_ids = (
            set(raw["supporting_evidence_ids"])
            | set(raw["contradictory_evidence_ids"])
            | set(raw["neutral_evidence_ids"])
        )
        unknown_evidence = evidence_ids - set(request.allowed_evidence_ids)
        if unknown_evidence:
            raise ValueError(
                "Auditor contains invented evidence references: "
                + ", ".join(sorted(unknown_evidence))
                + "."
            )
        retrieval_ids = tuple(raw["lesson_retrieval_ids"])
        permitted_lessons = set(lesson_ids_by_candidate.get(candidate_id, ()))
        if set(retrieval_ids) - permitted_lessons:
            raise ValueError("Auditor references a lesson not retrieved for this candidate.")
        candidate = candidate_by_id[candidate_id]
        validation = validation_by_id[candidate_id]
        result.append(
            EvidenceAuditResult.create(
                candidate_id=candidate_id,
                candidate_content_hash=candidate.content_hash,
                validation_id=validation.validation_id,
                validation_content_hash=validation.content_hash,
                supporting_evidence_ids=tuple(raw["supporting_evidence_ids"]),
                contradictory_evidence_ids=tuple(raw["contradictory_evidence_ids"]),
                neutral_evidence_ids=tuple(raw["neutral_evidence_ids"]),
                unavailable_evidence=tuple(raw["unavailable_evidence"]),
                incomparable_evidence=tuple(raw["incomparable_evidence"]),
                lesson_retrieval_ids=retrieval_ids,
                lesson_content_hashes=tuple(lesson_by_id[item].lesson_content_hash for item in retrieval_ids),
                lesson_warnings=tuple(raw["lesson_warnings"]),
                concise_summary=tuple(raw["concise_summary"]),
                structural_validity_unchanged=True,
                provider=request.provider,
                model=request.model,
                prompt_version=request.prompt_versions[TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR.value],
                policy_version=TECHNICAL_AGENT_POLICY_VERSION,
                audited_at_utc=timestamp,
            )
        )
    return tuple(result)


def _final_packet(
    request: TechnicalAgentRequest,
    candidates: Sequence[CandidateWaveCount],
    validations: Sequence[CandidateValidationResult],
    audits: Sequence[EvidenceAuditResult],
) -> dict[str, Any]:
    return {
        "agent_role": TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR.value,
        "request_reference": {
            "request_id": request.request_id,
            "request_content_hash": request.content_hash,
            "decision_time_input_hash": request.decision_time_input_hash,
        },
        "frozen_candidate_ids": [
            {
                "candidate_id": item.candidate_id,
                "candidate_content_hash": item.content_hash,
                "role": item.role.value,
            }
            for item in candidates
        ],
        "deterministic_validation": [
            {
                "candidate_id": item.candidate_id,
                "validation_id": item.validation_id,
                "validation_content_hash": item.content_hash,
                "structurally_valid": item.structurally_valid,
                "selection_eligible": item.selection_eligible,
                "hard_error_count": len(
                    item.shape_errors
                    + item.hard_rule_errors
                    + item.reference_errors
                    + item.cutoff_errors
                ),
                "role_error_count": len(item.role_errors),
            }
            for item in validations
        ],
        "evidence_audits": [
            {
                "candidate_id": item.candidate_id,
                "audit_id": item.audit_id,
                "audit_content_hash": item.content_hash,
                "supportive_count": len(item.supporting_evidence_ids),
                "contradictory_count": len(item.contradictory_evidence_ids),
                "neutral_count": len(item.neutral_evidence_ids),
                "unavailable_count": len(item.unavailable_evidence),
                "incomparable_count": len(item.incomparable_evidence),
                "lesson_warning_count": len(item.lesson_warnings),
                "concise_summary": list(item.concise_summary),
            }
            for item in audits
        ],
        "selection_boundary": {
            "allowed_candidate_ids": [
                item.candidate_id for item in validations if item.selection_eligible
            ],
            "invalid_candidate_ids": [
                item.candidate_id for item in validations if not item.selection_eligible
            ],
            "may_create_or_rewrite_counts": False,
        },
    }


def _parse_shadow_resolution(
    request: TechnicalAgentRequest,
    output: Mapping[str, Any],
    validations: Sequence[CandidateValidationResult],
    audits: Sequence[EvidenceAuditResult],
    *,
    timestamp: str,
) -> ShadowTechnicalResolution:
    valid_ids = tuple(item.candidate_id for item in validations if item.selection_eligible)
    invalid_ids = tuple(item.candidate_id for item in validations if not item.selection_eligible)
    selected = output.get("selected_candidate_id")
    ranked = tuple(output.get("ranked_valid_candidate_ids", ()))
    rejected = tuple(output.get("rejected_candidate_ids", ()))
    if len(ranked) != len(set(ranked)) or len(rejected) != len(set(rejected)):
        raise ValueError("Final orchestrator returned duplicate candidate IDs.")
    all_ids = set(valid_ids) | set(invalid_ids)
    unknown = ({selected} if selected else set()) | set(ranked) | set(rejected)
    unknown -= all_ids
    if unknown:
        raise ValueError("Final orchestrator invented candidate IDs: " + ", ".join(sorted(unknown)) + ".")
    status = _enum_value(output.get("status"), ShadowResolutionStatus, field_name="status")
    if selected is not None and selected not in valid_ids:
        raise ValueError("Final orchestrator selected a hard-invalid or role-invalid candidate.")
    if set(ranked) - set(valid_ids):
        raise ValueError("Final orchestrator ranked an ineligible candidate as valid.")
    if set(rejected) != set(invalid_ids):
        raise ValueError("Final orchestrator must preserve every invalid candidate ID as rejected.")
    if status is ShadowResolutionStatus.SELECTED:
        if set(ranked) != set(valid_ids):
            raise ValueError("A selected result must rank every selection-eligible candidate.")
        if not selected or not ranked or ranked[0] != selected:
            raise ValueError("Selected candidate must be first in the returned ranking.")
    else:
        if selected is not None:
            raise ValueError("An unresolved final result cannot select a candidate.")
        if not output.get("unresolved_reason"):
            raise ValueError("An unresolved final result requires an explanation.")
    return ShadowTechnicalResolution.create(
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        status=status,
        selected_candidate_id=selected,
        ranked_valid_candidate_ids=ranked,
        rejected_candidate_ids=rejected,
        validation_result_ids=tuple(item.validation_id for item in validations),
        evidence_audit_ids=tuple(item.audit_id for item in audits),
        unresolved_reason=output.get("unresolved_reason"),
        concise_summary=tuple(output.get("concise_summary", ())),
        provider=request.provider,
        model=request.model,
        prompt_version=request.prompt_versions[TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR.value],
        policy_version=TECHNICAL_AGENT_POLICY_VERSION,
        resolved_at_utc=timestamp,
    )


def _deterministic_shadow_resolution(
    request: TechnicalAgentRequest,
    validations: Sequence[CandidateValidationResult],
    audits: Sequence[EvidenceAuditResult],
    *,
    status: ShadowResolutionStatus,
    reason: str,
    timestamp: str,
) -> ShadowTechnicalResolution:
    return ShadowTechnicalResolution.create(
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        status=status,
        selected_candidate_id=None,
        ranked_valid_candidate_ids=tuple(
            item.candidate_id for item in validations if item.selection_eligible
        ),
        rejected_candidate_ids=tuple(
            item.candidate_id for item in validations if not item.selection_eligible
        ),
        validation_result_ids=tuple(item.validation_id for item in validations),
        evidence_audit_ids=tuple(item.audit_id for item in audits),
        unresolved_reason=reason,
        concise_summary=(reason,),
        provider="deterministic",
        model=None,
        prompt_version="not_applicable",
        policy_version=TECHNICAL_AGENT_POLICY_VERSION,
        resolved_at_utc=timestamp,
    )


def compare_shadow_resolution(
    shadow: ShadowTechnicalResolution,
    candidates: Sequence[CandidateWaveCount],
    existing_degree_resolution: Mapping[str, Any],
    *,
    timestamp: str,
) -> ShadowResolutionComparison:
    existing_hash = stored_record_content_hash(existing_degree_resolution)
    existing_response = existing_degree_resolution.get("response")
    if not isinstance(existing_response, Mapping):
        raise ValueError("Existing degree resolution has no response payload.")
    existing_structure_hash = canonical_sha256(
        _resolution_structure_signature(existing_response)
    )
    by_id = {item.candidate_id: item for item in candidates}
    selected = by_id.get(shadow.selected_candidate_id) if shadow.selected_candidate_id else None
    if selected is None:
        status = ShadowComparisonStatus.NO_SELECTION
        selected_structure_hash = None
        differences = ("No shadow candidate was selected, so structures were not compared.",)
        summary = ("The stored degree resolution remains unchanged; shadow mode produced no selected comparison candidate.",)
    else:
        selected_structure_hash = canonical_sha256(
            _resolution_structure_signature(selected.degree_resolution)
        )
        if canonical_sha256(selected.degree_resolution) == canonical_sha256(existing_response):
            status = ShadowComparisonStatus.EXACT_MATCH
            differences = ()
            summary = ("The selected shadow candidate exactly matches the stored degree-resolution payload.",)
        elif selected_structure_hash == existing_structure_hash:
            status = ShadowComparisonStatus.STRUCTURALLY_EQUIVALENT
            differences = ("Non-structural metadata or concise evidence wording differs.",)
            summary = ("The selected shadow candidate is structurally equivalent to the stored resolution.",)
        else:
            status = ShadowComparisonStatus.MATERIALLY_DIFFERENT
            differences = ("The normalized degree, pivot, family, direction, or active-position structure differs.",)
            summary = ("The selected shadow candidate materially differs from the stored resolution; neither record was modified.",)
    return ShadowResolutionComparison.create(
        shadow_resolution_id=shadow.shadow_resolution_id,
        shadow_resolution_content_hash=shadow.content_hash,
        existing_degree_resolution_id=int(existing_degree_resolution["id"]),
        existing_degree_resolution_content_hash=existing_hash,
        selected_candidate_id=(selected.candidate_id if selected else None),
        selected_candidate_content_hash=(selected.content_hash if selected else None),
        comparison_status=status,
        existing_structure_hash=existing_structure_hash,
        selected_structure_hash=selected_structure_hash,
        differences=differences,
        concise_summary=summary,
        provider="deterministic",
        model=None,
        prompt_version="not_applicable",
        policy_version=TECHNICAL_AGENT_POLICY_VERSION,
        compared_at_utc=timestamp,
    )


def validate_technical_agent_orchestration(
    orchestration: TechnicalAgentOrchestration,
) -> tuple[str, ...]:
    errors: list[str] = []
    if orchestration.content_hash != technical_agent_orchestration_content_hash(orchestration):
        errors.append("Technical-agent orchestration content_hash does not match.")
    errors.extend(validate_technical_agent_request(orchestration.request))
    for candidate in orchestration.candidates:
        if candidate.content_hash != candidate_wave_count_content_hash(candidate):
            errors.append(f"Candidate {candidate.candidate_id} hash does not match.")
    for result in (
        orchestration.primary_result,
        orchestration.alternative_result,
        orchestration.auditor_result,
        orchestration.final_result,
    ):
        if result.content_hash != technical_agent_result_content_hash(result):
            errors.append(f"Agent result {result.result_id} hash does not match.")
    for validation in orchestration.validation_results:
        if validation.content_hash != candidate_validation_result_content_hash(validation):
            errors.append(f"Validation {validation.validation_id} hash does not match.")
    for audit in orchestration.evidence_audits:
        if audit.content_hash != evidence_audit_result_content_hash(audit):
            errors.append(f"Audit {audit.audit_id} hash does not match.")
    if orchestration.shadow_resolution.content_hash != shadow_technical_resolution_content_hash(orchestration.shadow_resolution):
        errors.append("Shadow resolution hash does not match.")
    if orchestration.shadow_comparison.content_hash != shadow_resolution_comparison_content_hash(orchestration.shadow_comparison):
        errors.append("Shadow comparison hash does not match.")
    return tuple(errors)


def replay_technical_agent_orchestration(
    value: TechnicalAgentOrchestration | Mapping[str, Any],
    *,
    expected_content_hash: str | None = None,
) -> TechnicalAgentOrchestration:
    """Replay immutable validation and hash continuity without another model call."""

    result = value if isinstance(value, TechnicalAgentOrchestration) else TechnicalAgentOrchestration.from_dict(value)
    errors = validate_technical_agent_orchestration(result)
    if errors:
        raise ValueError("Technical-agent replay failed: " + " ".join(errors))
    if expected_content_hash is not None and result.content_hash != expected_content_hash:
        raise ValueError("Technical-agent replay content hash differs from the expected hash.")
    return result


class TechnicalAgentOrchestrator:
    """Execute the four approved Phase 11D1 roles without live integration."""

    def __init__(
        self,
        *,
        provider: AnalysisProvider,
        lesson_store: MistakeMemoryReadStore | None = None,
        max_retries: int = 0,
        stage_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not hasattr(provider, "generate"):
            raise TypeError("provider must implement AnalysisProvider.generate().")
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3.")
        self.provider = provider
        self.lesson_store = lesson_store
        self.max_retries = max_retries
        self.stage_callback = stage_callback

    def _stage(self, trace: list[str], name: str) -> None:
        trace.append(name)
        if self.stage_callback is not None:
            self.stage_callback(name)

    def _run_counter(
        self,
        request: TechnicalAgentRequest,
        role: TechnicalAgentRole,
        *,
        timestamp: str,
    ) -> tuple[TechnicalAgentResult, CandidateWaveCount | None]:
        packet = _counter_input_packet(request, role)
        system_prompt = (
            _PRIMARY_SYSTEM_PROMPT
            if role is TechnicalAgentRole.PRIMARY_WAVE_COUNTER
            else _ALTERNATIVE_SYSTEM_PROMPT
        )
        prompt_version = request.prompt_versions[role.value]
        prompt = _user_prompt(packet, COUNTER_OUTPUT_SCHEMA)
        call = _call_provider(
            self.provider,
            system_prompt=system_prompt,
            user_prompt=prompt,
            schema=COUNTER_OUTPUT_SCHEMA,
            packet=packet,
            max_retries=self.max_retries,
        )
        if call.output is None:
            return (
                _result(
                    request=request,
                    role=role,
                    status=TechnicalAgentResultStatus.FAILED,
                    packet=packet,
                    system_prompt=system_prompt,
                    schema=COUNTER_OUTPUT_SCHEMA,
                    prompt_version=prompt_version,
                    timestamp=timestamp,
                    structured_output_hash=call.output_hash,
                    retry_count=call.retry_count,
                    errors=call.errors,
                ),
                None,
            )
        try:
            candidate = _counter_candidate(request, role, call.output, timestamp=timestamp)
        except (TypeError, ValueError, KeyError) as exc:
            return (
                _result(
                    request=request,
                    role=role,
                    status=TechnicalAgentResultStatus.FAILED,
                    packet=packet,
                    system_prompt=system_prompt,
                    schema=COUNTER_OUTPUT_SCHEMA,
                    prompt_version=prompt_version,
                    timestamp=timestamp,
                    structured_output_hash=call.output_hash,
                    retry_count=call.retry_count,
                    errors=(f"Structured candidate could not be frozen: {exc}",),
                ),
                None,
            )
        return (
            _result(
                request=request,
                role=role,
                status=TechnicalAgentResultStatus.COMPLETED,
                packet=packet,
                system_prompt=system_prompt,
                schema=COUNTER_OUTPUT_SCHEMA,
                prompt_version=prompt_version,
                timestamp=timestamp,
                structured_output_hash=call.output_hash,
                output_reference_ids=(candidate.candidate_id,),
                retry_count=call.retry_count,
            ),
            candidate,
        )

    def orchestrate(
        self,
        request: TechnicalAgentRequest,
        *,
        existing_degree_resolution: Mapping[str, Any],
        shadow_mode: bool = False,
        lesson_context_forecast_id: str | None = None,
        orchestration_version: int = 1,
        supersedes_orchestration_id: str | None = None,
        executed_at_utc: str | None = None,
    ) -> TechnicalAgentOrchestration:
        if not shadow_mode or not request.shadow_mode:
            raise RuntimeError(
                "Phase 11D1 is disabled by default; both the immutable request and invocation must explicitly enable shadow_mode=True."
            )
        timestamp = _normalize_utc(
            executed_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            field_name="executed_at_utc",
        )
        request_errors = validate_technical_agent_request(request)
        if request_errors:
            raise ValueError("Invalid TechnicalAgentRequest: " + " ".join(request_errors))
        if request.provider != str(getattr(self.provider, "name", "")):
            raise ValueError("Request provider does not match the configured provider.")
        if request.model != getattr(self.provider, "model", None):
            raise ValueError("Request model does not match the configured provider model.")
        existing = copy.deepcopy(dict(existing_degree_resolution))
        if existing.get("id") != request.source_degree_resolution_id:
            raise ValueError("Existing resolution ID differs from the request reference.")
        if existing.get("run_id") != request.source_analysis_run_id:
            raise ValueError("Existing resolution belongs to a different analysis run.")
        if stored_record_content_hash(existing) != request.source_degree_resolution_content_hash:
            raise ValueError("Existing resolution hash differs from the immutable request reference.")

        trace: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        self._stage(trace, "request_validated")

        primary_result, primary = self._run_counter(
            request,
            TechnicalAgentRole.PRIMARY_WAVE_COUNTER,
            timestamp=timestamp,
        )
        self._stage(trace, "primary_frozen")
        alternative_result, alternative = self._run_counter(
            request,
            TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER,
            timestamp=timestamp,
        )
        self._stage(trace, "alternative_frozen")
        candidates = tuple(item for item in (primary, alternative) if item is not None)
        if primary_result.status is TechnicalAgentResultStatus.FAILED:
            warnings.append("Primary counter failed closed; no replacement count was fabricated.")
        if alternative_result.status is TechnicalAgentResultStatus.FAILED:
            warnings.append("Alternative counter failed closed; no replacement count was fabricated.")

        validations: list[CandidateValidationResult] = []
        for candidate in candidates:
            validations.append(
                validate_candidate_wave_count(
                    candidate,
                    request,
                    primary_candidate=(
                        primary
                        if candidate.role is TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER
                        else None
                    ),
                    validated_at_utc=timestamp,
                )
            )
        self._stage(trace, "candidates_validated")

        retrieved_lessons, query_hashes, lesson_ids_by_candidate, retrieval_warnings = _retrieve_candidate_lessons(
            request,
            candidates,
            self.lesson_store,
            current_forecast_id=lesson_context_forecast_id,
        )
        warnings.extend(retrieval_warnings)
        self._stage(trace, "lessons_retrieved")

        audit_packet = _auditor_packet(
            request,
            candidates,
            validations,
            retrieved_lessons,
            lesson_ids_by_candidate,
        )
        audit_prompt_version = request.prompt_versions[
            TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR.value
        ]
        audits: tuple[EvidenceAuditResult, ...] = ()
        if candidates:
            audit_call = _call_provider(
                self.provider,
                system_prompt=_AUDITOR_SYSTEM_PROMPT,
                user_prompt=_user_prompt(audit_packet, AUDITOR_OUTPUT_SCHEMA),
                schema=AUDITOR_OUTPUT_SCHEMA,
                packet=audit_packet,
                max_retries=self.max_retries,
            )
            if audit_call.output is not None:
                try:
                    audits = _parse_audits(
                        request,
                        audit_call.output,
                        candidates,
                        validations,
                        retrieved_lessons,
                        lesson_ids_by_candidate,
                        timestamp=timestamp,
                    )
                except (TypeError, ValueError, KeyError) as exc:
                    audit_call = _ProviderCall(
                        output=None,
                        output_hash=audit_call.output_hash,
                        retry_count=audit_call.retry_count,
                        errors=(f"Structured audit could not be frozen: {exc}",),
                    )
            auditor_result = _result(
                request=request,
                role=TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR,
                status=(TechnicalAgentResultStatus.COMPLETED if audit_call.output is not None else TechnicalAgentResultStatus.FAILED),
                packet=audit_packet,
                system_prompt=_AUDITOR_SYSTEM_PROMPT,
                schema=AUDITOR_OUTPUT_SCHEMA,
                prompt_version=audit_prompt_version,
                timestamp=timestamp,
                structured_output_hash=audit_call.output_hash,
                output_reference_ids=tuple(item.audit_id for item in audits),
                retry_count=audit_call.retry_count,
                errors=audit_call.errors,
            )
        else:
            auditor_result = _result(
                request=request,
                role=TechnicalAgentRole.RULES_AND_EVIDENCE_AUDITOR,
                status=TechnicalAgentResultStatus.SKIPPED,
                packet=audit_packet,
                system_prompt=_AUDITOR_SYSTEM_PROMPT,
                schema=AUDITOR_OUTPUT_SCHEMA,
                prompt_version=audit_prompt_version,
                timestamp=timestamp,
                warnings=("No frozen candidate was available to audit.",),
            )
        self._stage(trace, "auditor_frozen")

        final_packet = _final_packet(request, candidates, validations, audits)
        final_prompt_version = request.prompt_versions[
            TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR.value
        ]
        eligible = tuple(item for item in validations if item.selection_eligible)
        if not candidates:
            final_result = _result(
                request=request,
                role=TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR,
                status=TechnicalAgentResultStatus.SKIPPED,
                packet=final_packet,
                system_prompt=_FINAL_SYSTEM_PROMPT,
                schema=FINAL_OUTPUT_SCHEMA,
                prompt_version=final_prompt_version,
                timestamp=timestamp,
                warnings=("No candidate was available for selection.",),
            )
            shadow = _deterministic_shadow_resolution(
                request,
                validations,
                audits,
                status=ShadowResolutionStatus.INSUFFICIENT_EVIDENCE,
                reason="Both counter stages failed or produced no verifiable structured candidate.",
                timestamp=timestamp,
            )
        elif not eligible:
            final_result = _result(
                request=request,
                role=TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR,
                status=TechnicalAgentResultStatus.SKIPPED,
                packet=final_packet,
                system_prompt=_FINAL_SYSTEM_PROMPT,
                schema=FINAL_OUTPUT_SCHEMA,
                prompt_version=final_prompt_version,
                timestamp=timestamp,
                warnings=("Every candidate failed deterministic or role validation.",),
            )
            shadow = _deterministic_shadow_resolution(
                request,
                validations,
                audits,
                status=ShadowResolutionStatus.REJECTED_INVALID,
                reason="No candidate survived deterministic hard-rule and reference validation.",
                timestamp=timestamp,
            )
        elif auditor_result.status is not TechnicalAgentResultStatus.COMPLETED:
            final_result = _result(
                request=request,
                role=TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR,
                status=TechnicalAgentResultStatus.SKIPPED,
                packet=final_packet,
                system_prompt=_FINAL_SYSTEM_PROMPT,
                schema=FINAL_OUTPUT_SCHEMA,
                prompt_version=final_prompt_version,
                timestamp=timestamp,
                warnings=("The evidence auditor failed; final selection was not attempted.",),
            )
            shadow = _deterministic_shadow_resolution(
                request,
                validations,
                audits,
                status=ShadowResolutionStatus.UNRESOLVED,
                reason="The evidence-audit stage failed closed.",
                timestamp=timestamp,
            )
        else:
            final_call = _call_provider(
                self.provider,
                system_prompt=_FINAL_SYSTEM_PROMPT,
                user_prompt=_user_prompt(final_packet, FINAL_OUTPUT_SCHEMA),
                schema=FINAL_OUTPUT_SCHEMA,
                packet=final_packet,
                max_retries=self.max_retries,
            )
            if final_call.output is not None:
                try:
                    shadow = _parse_shadow_resolution(
                        request,
                        final_call.output,
                        validations,
                        audits,
                        timestamp=timestamp,
                    )
                except (TypeError, ValueError, KeyError) as exc:
                    final_call = _ProviderCall(
                        output=None,
                        output_hash=final_call.output_hash,
                        retry_count=final_call.retry_count,
                        errors=(f"Final selection failed closed: {exc}",),
                    )
            if final_call.output is None:
                shadow = _deterministic_shadow_resolution(
                    request,
                    validations,
                    audits,
                    status=ShadowResolutionStatus.UNRESOLVED,
                    reason="The final selector failed structured validation; no candidate was forced.",
                    timestamp=timestamp,
                )
            final_result = _result(
                request=request,
                role=TechnicalAgentRole.FINAL_TECHNICAL_ORCHESTRATOR,
                status=(TechnicalAgentResultStatus.COMPLETED if final_call.output is not None else TechnicalAgentResultStatus.FAILED),
                packet=final_packet,
                system_prompt=_FINAL_SYSTEM_PROMPT,
                schema=FINAL_OUTPUT_SCHEMA,
                prompt_version=final_prompt_version,
                timestamp=timestamp,
                structured_output_hash=final_call.output_hash,
                output_reference_ids=tuple(
                    dict.fromkeys(
                        item
                        for item in (
                            shadow.selected_candidate_id,
                            *shadow.ranked_valid_candidate_ids,
                            *shadow.rejected_candidate_ids,
                        )
                        if item is not None
                    )
                ),
                retry_count=final_call.retry_count,
                errors=final_call.errors,
            )
        self._stage(trace, "final_frozen")

        comparison = compare_shadow_resolution(
            shadow, candidates, existing, timestamp=timestamp
        )
        self._stage(trace, "shadow_compared")
        failed_results = [
            item
            for item in (primary_result, alternative_result, auditor_result, final_result)
            if item.status is TechnicalAgentResultStatus.FAILED
        ]
        if shadow.status is ShadowResolutionStatus.REJECTED_INVALID:
            status = TechnicalOrchestrationStatus.REJECTED_INVALID
        elif shadow.status is ShadowResolutionStatus.UNRESOLVED:
            status = TechnicalOrchestrationStatus.PARTIAL if failed_results else TechnicalOrchestrationStatus.UNRESOLVED
        elif shadow.status is ShadowResolutionStatus.INSUFFICIENT_EVIDENCE:
            status = TechnicalOrchestrationStatus.FAILED
        elif failed_results or warnings:
            status = TechnicalOrchestrationStatus.COMPLETED_WITH_WARNINGS
        else:
            status = TechnicalOrchestrationStatus.COMPLETED
        for result in failed_results:
            errors.extend(f"{result.role.value}: {item}" for item in result.errors)
        source_hashes = {
            "analysis_run": request.source_analysis_run_content_hash,
            "degree_resolution": request.source_degree_resolution_content_hash,
            "technical_request": request.content_hash,
            "primary_result": primary_result.content_hash,
            "alternative_result": alternative_result.content_hash,
            "auditor_result": auditor_result.content_hash,
            "final_result": final_result.content_hash,
            "validation_manifest": canonical_sha256([item.content_hash for item in validations]),
            "shadow_resolution": shadow.content_hash,
            "shadow_comparison": comparison.content_hash,
        }
        result = TechnicalAgentOrchestration.create(
            orchestration_version=orchestration_version,
            supersedes_orchestration_id=supersedes_orchestration_id,
            request=request,
            primary_result=primary_result,
            alternative_result=alternative_result,
            candidates=candidates,
            validation_results=tuple(validations),
            lesson_query_hashes=query_hashes,
            retrieved_lessons=retrieved_lessons,
            auditor_result=auditor_result,
            evidence_audits=audits,
            final_result=final_result,
            shadow_resolution=shadow,
            shadow_comparison=comparison,
            status=status,
            stage_trace=tuple(trace),
            stage_versions=dict(TECHNICAL_AGENT_STAGE_VERSIONS),
            source_hashes=source_hashes,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(errors),
            provider=request.provider,
            model=request.model,
            prompt_versions=request.prompt_versions,
            policy_version=TECHNICAL_AGENT_POLICY_VERSION,
            created_at_utc=timestamp,
        )
        validation_errors = validate_technical_agent_orchestration(result)
        if validation_errors:
            raise ValueError("Invalid TechnicalAgentOrchestration: " + " ".join(validation_errors))
        return result


FORECAST_AGENT_ORCHESTRATION_TABLES = ("forecast_agent_orchestrations",)
FORECAST_AGENT_ORCHESTRATION_INDEXES = (
    "forecast_agent_orchestrations_source_idx",
    "forecast_agent_orchestrations_status_idx",
    "forecast_agent_orchestrations_supersedes_idx",
)
FORECAST_AGENT_ORCHESTRATION_TRIGGERS = (
    "forecast_agent_orchestrations_no_update",
    "forecast_agent_orchestrations_no_delete",
)

FORECAST_AGENT_ORCHESTRATION_MIGRATION_SQL = (
    """
    CREATE TABLE forecast_agent_orchestrations (
        orchestration_id TEXT PRIMARY KEY,
        orchestration_version INTEGER NOT NULL
            CHECK (orchestration_version >= 1),
        supersedes_orchestration_id TEXT NULL
            REFERENCES forecast_agent_orchestrations(orchestration_id)
            ON DELETE RESTRICT,
        source_analysis_run_id INTEGER NOT NULL
            REFERENCES analysis_runs(id) ON DELETE RESTRICT,
        source_degree_resolution_id INTEGER NOT NULL
            REFERENCES degree_resolutions(id) ON DELETE RESTRICT,
        request_id TEXT NOT NULL,
        analysis_cutoff_utc TEXT NOT NULL,
        decision_time_input_hash TEXT NOT NULL
            CHECK (
                length(decision_time_input_hash) = 64
                AND decision_time_input_hash NOT GLOB '*[^0-9a-f]*'
            ),
        primary_result_hash TEXT NOT NULL
            CHECK (length(primary_result_hash) = 64 AND primary_result_hash NOT GLOB '*[^0-9a-f]*'),
        alternative_result_hash TEXT NOT NULL
            CHECK (length(alternative_result_hash) = 64 AND alternative_result_hash NOT GLOB '*[^0-9a-f]*'),
        auditor_result_hash TEXT NOT NULL
            CHECK (length(auditor_result_hash) = 64 AND auditor_result_hash NOT GLOB '*[^0-9a-f]*'),
        final_result_hash TEXT NOT NULL
            CHECK (length(final_result_hash) = 64 AND final_result_hash NOT GLOB '*[^0-9a-f]*'),
        validation_manifest_hash TEXT NOT NULL
            CHECK (length(validation_manifest_hash) = 64 AND validation_manifest_hash NOT GLOB '*[^0-9a-f]*'),
        shadow_resolution_hash TEXT NOT NULL
            CHECK (length(shadow_resolution_hash) = 64 AND shadow_resolution_hash NOT GLOB '*[^0-9a-f]*'),
        shadow_comparison_hash TEXT NOT NULL
            CHECK (length(shadow_comparison_hash) = 64 AND shadow_comparison_hash NOT GLOB '*[^0-9a-f]*'),
        retrieved_lesson_ids_json TEXT NOT NULL,
        selected_candidate_ids_json TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN (
                'completed', 'completed_with_warnings', 'partial',
                'unresolved', 'rejected_invalid', 'failed'
            )),
        provider TEXT NOT NULL,
        model TEXT NULL,
        prompt_versions_json TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        content_hash TEXT NOT NULL UNIQUE
            CHECK (length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
        record_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        CHECK (supersedes_orchestration_id IS NULL OR supersedes_orchestration_id <> orchestration_id)
    )
    """,
    """
    CREATE INDEX forecast_agent_orchestrations_source_idx
    ON forecast_agent_orchestrations(
        source_analysis_run_id, source_degree_resolution_id,
        orchestration_version, orchestration_id
    )
    """,
    """
    CREATE INDEX forecast_agent_orchestrations_status_idx
    ON forecast_agent_orchestrations(status, analysis_cutoff_utc, orchestration_id)
    """,
    """
    CREATE UNIQUE INDEX forecast_agent_orchestrations_supersedes_idx
    ON forecast_agent_orchestrations(supersedes_orchestration_id)
    WHERE supersedes_orchestration_id IS NOT NULL
    """,
    """
    CREATE TRIGGER forecast_agent_orchestrations_no_update
    BEFORE UPDATE ON forecast_agent_orchestrations
    BEGIN
        SELECT RAISE(ABORT, 'forecast_agent_orchestrations is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_agent_orchestrations_no_delete
    BEFORE DELETE ON forecast_agent_orchestrations
    BEGIN
        SELECT RAISE(ABORT, 'forecast_agent_orchestrations is append-only');
    END
    """,
)


__all__ = [
    "ALTERNATIVE_COUNTER_PROMPT_VERSION",
    "AUDITOR_OUTPUT_SCHEMA",
    "CANDIDATE_VALIDATION_SCHEMA_VERSION",
    "CANDIDATE_WAVE_COUNT_SCHEMA_VERSION",
    "COUNTER_OUTPUT_SCHEMA",
    "CandidateValidationResult",
    "CandidateWaveCount",
    "EVIDENCE_AUDIT_SCHEMA_VERSION",
    "EvidenceAuditResult",
    "FINAL_ORCHESTRATOR_PROMPT_VERSION",
    "FINAL_OUTPUT_SCHEMA",
    "FORECAST_AGENT_ORCHESTRATION_INDEXES",
    "FORECAST_AGENT_ORCHESTRATION_MIGRATION_SQL",
    "FORECAST_AGENT_ORCHESTRATION_TABLES",
    "FORECAST_AGENT_ORCHESTRATION_TRIGGERS",
    "PRIMARY_COUNTER_PROMPT_VERSION",
    "RULES_AUDITOR_PROMPT_VERSION",
    "SHADOW_COMPARISON_SCHEMA_VERSION",
    "SHADOW_RESOLUTION_SCHEMA_VERSION",
    "ShadowComparisonStatus",
    "ShadowResolutionComparison",
    "ShadowResolutionStatus",
    "ShadowTechnicalResolution",
    "TECHNICAL_AGENT_CALCULATION_VERSION",
    "TECHNICAL_AGENT_ORCHESTRATION_SCHEMA_VERSION",
    "TECHNICAL_AGENT_POLICY_VERSION",
    "TECHNICAL_AGENT_PROMPT_VERSIONS",
    "TECHNICAL_AGENT_ROLE_SCHEMA_VERSION",
    "TECHNICAL_AGENT_REQUEST_SCHEMA_VERSION",
    "TECHNICAL_AGENT_RESULT_SCHEMA_VERSION",
    "TechnicalAgentOrchestration",
    "TechnicalAgentOrchestrator",
    "TechnicalAgentRequest",
    "TechnicalAgentResult",
    "TechnicalAgentResultStatus",
    "TechnicalAgentRole",
    "TechnicalOrchestrationStatus",
    "build_technical_agent_request_from_stored_records",
    "candidate_validation_result_content_hash",
    "candidate_wave_count_content_hash",
    "canonical_technical_agent_json",
    "compare_shadow_resolution",
    "derive_pivot_catalog",
    "evidence_audit_result_content_hash",
    "replay_technical_agent_orchestration",
    "shadow_resolution_comparison_content_hash",
    "shadow_technical_resolution_content_hash",
    "technical_agent_orchestration_content_hash",
    "technical_counter_input_hash",
    "technical_agent_request_content_hash",
    "technical_agent_result_content_hash",
    "validate_candidate_wave_count",
    "validate_technical_agent_orchestration",
    "validate_technical_agent_request",
]
