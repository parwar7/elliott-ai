"""Deterministic historical pattern contracts, builders, and diagnostics.

This module is standalone. It does not perform research, call a model, persist
records, generate current-market scenarios, or integrate with ElliottAgent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from itertools import combinations
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .historical_market import (
    DurationBucket,
    HistoricalAnalystType,
    HistoricalCausalAnalysis,
    HistoricalEvidencePacket,
    HistoricalMoveType,
    HistoricalSimilarityFeatures,
    MagnitudeBucket,
    MarketRegime,
    MarketRegimeState,
    RelativePerformanceBucket,
    TransmissionMechanism,
    historical_causal_analysis_content_hash,
    historical_evidence_packet_content_hash,
    historical_similarity_features_content_hash,
    validate_historical_causal_analysis,
    validate_historical_evidence_packet,
    validate_historical_similarity_features,
)
from .market_scenario import (
    ExposureType,
    ScenarioDirection,
    ValidationIssue,
    ValidationResult,
)


HISTORICAL_PATTERN_CANDIDATE_SCHEMA_VERSION = (
    "historical-pattern-candidate-1.0.0"
)
HISTORICAL_PATTERN_SCHEMA_VERSION = "historical-pattern-1.0.0"
HISTORICAL_PATTERN_LIBRARY_SCHEMA_VERSION = (
    "historical-pattern-library-1.0.0"
)
HISTORICAL_PATTERN_VALIDATION_VERSION = (
    "historical-pattern-validation-1.0.0"
)
HISTORICAL_PATTERN_BUILD_SCHEMA_VERSION = (
    "historical-pattern-build-result-1.0.0"
)
DEFAULT_MINIMUM_PATTERN_CASES = 3


class PatternStatus(StrEnum):
    DRAFT = "draft"
    EXTRACTED = "extracted"
    REVIEWED = "reviewed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"
    INSUFFICIENT_SUPPORT = "insufficient_support"


class PatternDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    BIDIRECTIONAL = "bidirectional"
    CONTEXT_DEPENDENT = "context_dependent"


class PatternConfidence(StrEnum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNAVAILABLE = "unavailable"


class PatternChange(StrEnum):
    EXPANDING = "expanding"
    CONTRACTING = "contracting"
    STABLE = "stable"
    MIXED = "mixed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class PatternTendency(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    CONTEXT_DEPENDENT = "context_dependent"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class PatternRecoveryProfile(StrEnum):
    RAPID = "rapid"
    GRADUAL = "gradual"
    PARTIAL = "partial"
    FULL = "full"
    NONE = "none"
    MIXED = "mixed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class PatternConsistency(StrEnum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    MIXED = "mixed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNAVAILABLE = "unavailable"


class EvidenceIndependenceStatus(StrEnum):
    INDEPENDENT = "independent"
    MIXED = "mixed"
    CORRELATED = "correlated"
    UNKNOWN = "unknown"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class PatternOverlapRelationship(StrEnum):
    NONE = "none"
    OVERLAP = "overlap"
    NEAR_DUPLICATE = "near_duplicate"
    PARENT_CHILD = "parent_child"
    MUTUALLY_CONTRADICTORY = "mutually_contradictory"
    REGIME_DEPENDENT_OUTCOMES = "regime_dependent_outcomes"


_REGIME_FIELDS = (
    "interest_rate_regime",
    "inflation_regime",
    "liquidity_regime",
    "credit_regime",
    "equity_risk_appetite",
    "volatility_regime",
    "small_cap_regime",
    "growth_stock_regime",
    "sector_regime",
    "benchmark_trend",
)


def _normalize_timestamp(value: str | datetime, *, field_name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO timestamp.") from exc
    else:
        raise TypeError(f"{field_name} must be a datetime or ISO string.")
    aware = (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _temporal_point(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
            f"{field_name} must contain only {model_type.__name__} objects."
        )
    return result


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be a valid {enum_type.__name__}."
        ) from exc


def _enum_tuple(
    value: Sequence[Any] | None,
    enum_type: type[StrEnum],
    *,
    field_name: str,
) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    return tuple(
        _enum_value(item, enum_type, field_name=field_name) for item in value
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
    result = dict(value)
    if not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in result.items()
    ):
        raise TypeError(f"{field_name} must contain string keys and values.")
    return MappingProxyType(dict(sorted(result.items())))


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
        f"JSON contract cannot contain {type(value).__name__} instances."
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
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _content_hash(value: Any) -> str:
    payload = dict(value.to_dict())
    payload.pop("content_hash", None)
    return _hash_payload(payload)


def _mapping(value: Mapping[str, Any], *, model_name: str) -> dict[str, Any]:
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


def _duplicates(values: Sequence[Any]) -> set[Any]:
    counts = Counter(values)
    return {value for value, count in counts.items() if count > 1}


def _normalized_token(value: str) -> str:
    return re.sub(
        r"_+",
        "_",
        re.sub(r"[^a-z0-9]+", "_", value.casefold()),
    ).strip("_")


def _pattern_ref(pattern_id: str, pattern_version: int) -> str:
    return f"{pattern_id}@{pattern_version}"


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class PatternOutcomeProfile(_JsonContract):
    expected_move_types: tuple[HistoricalMoveType, ...]
    expected_direction: PatternDirection
    magnitude_bucket: MagnitudeBucket
    duration_bucket: DurationBucket
    expected_volatility_change: PatternChange
    expected_volume_change: PatternChange
    expected_benchmark_relative_behavior: RelativePerformanceBucket
    expected_sector_relative_behavior: RelativePerformanceBucket
    continuation_tendency: PatternTendency
    reversal_tendency: PatternTendency
    typical_recovery_profile: PatternRecoveryProfile
    uncertainty_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expected_move_types",
            _enum_tuple(
                self.expected_move_types,
                HistoricalMoveType,
                field_name="expected_move_types",
            ),
        )
        for field_name, enum_type in (
            ("expected_direction", PatternDirection),
            ("magnitude_bucket", MagnitudeBucket),
            ("duration_bucket", DurationBucket),
            ("expected_volatility_change", PatternChange),
            ("expected_volume_change", PatternChange),
            (
                "expected_benchmark_relative_behavior",
                RelativePerformanceBucket,
            ),
            ("expected_sector_relative_behavior", RelativePerformanceBucket),
            ("continuation_tendency", PatternTendency),
            ("reversal_tendency", PatternTendency),
            ("typical_recovery_profile", PatternRecoveryProfile),
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(
                    getattr(self, field_name),
                    enum_type,
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "uncertainty_notes",
            _string_tuple(
                self.uncertainty_notes,
                field_name="uncertainty_notes",
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PatternOutcomeProfile":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class PatternSupportMetrics(_JsonContract):
    supporting_case_count: int
    contradicting_case_count: int
    exception_case_count: int
    unique_symbol_count: int
    unique_sector_count: int
    unique_regime_count: int
    unique_move_type_count: int
    independent_source_group_count: int
    earliest_case_start: str
    latest_case_end: str
    temporal_span_days: float
    outcome_consistency: PatternConsistency
    mechanism_consistency: PatternConsistency
    regime_diversity: PatternConsistency
    symbol_concentration: float
    sector_concentration: float
    evidence_independence_status: EvidenceIndependenceStatus

    def __post_init__(self) -> None:
        for field_name in (
            "supporting_case_count",
            "contradicting_case_count",
            "exception_case_count",
            "unique_symbol_count",
            "unique_sector_count",
            "unique_regime_count",
            "unique_move_type_count",
            "independent_source_group_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")
        object.__setattr__(
            self,
            "earliest_case_start",
            _normalize_timestamp(
                self.earliest_case_start,
                field_name="earliest_case_start",
            ),
        )
        object.__setattr__(
            self,
            "latest_case_end",
            _normalize_timestamp(
                self.latest_case_end,
                field_name="latest_case_end",
            ),
        )
        for field_name in (
            "temporal_span_days",
            "symbol_concentration",
            "sector_concentration",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{field_name} must be finite.")
            object.__setattr__(self, field_name, float(value))
        for field_name in (
            "outcome_consistency",
            "mechanism_consistency",
            "regime_diversity",
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(
                    getattr(self, field_name),
                    PatternConsistency,
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "evidence_independence_status",
            _enum_value(
                self.evidence_independence_status,
                EvidenceIndependenceStatus,
                field_name="evidence_independence_status",
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PatternSupportMetrics":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class PatternCandidate(_JsonContract):
    candidate_id: str
    case_ids: tuple[str, ...]
    causal_analysis_hashes: Mapping[str, str]
    grouping_basis: tuple[str, ...]
    shared_move_types: tuple[HistoricalMoveType, ...]
    shared_exposure_types: tuple[ExposureType, ...]
    shared_transmission_mechanisms: tuple[TransmissionMechanism, ...]
    shared_event_types: tuple[str, ...]
    shared_market_regime_features: tuple[str, ...]
    differing_features: tuple[str, ...]
    candidate_support_metrics: PatternSupportMetrics
    candidate_warnings: tuple[str, ...]
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""
    schema_version: str = HISTORICAL_PATTERN_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "case_ids",
            "grouping_basis",
            "shared_event_types",
            "shared_market_regime_features",
            "differing_features",
            "candidate_warnings",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "causal_analysis_hashes",
            _string_mapping(
                self.causal_analysis_hashes,
                field_name="causal_analysis_hashes",
            ),
        )
        for field_name, enum_type in (
            ("shared_move_types", HistoricalMoveType),
            ("shared_exposure_types", ExposureType),
            (
                "shared_transmission_mechanisms",
                TransmissionMechanism,
            ),
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_tuple(
                    getattr(self, field_name),
                    enum_type,
                    field_name=field_name,
                ),
            )
        if not isinstance(
            self.candidate_support_metrics,
            PatternSupportMetrics,
        ):
            raise TypeError(
                "candidate_support_metrics must be PatternSupportMetrics."
            )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PatternCandidate":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["candidate_support_metrics"] = PatternSupportMetrics.from_dict(
            raw["candidate_support_metrics"]
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalPatternCandidateBuildResult(_JsonContract):
    success: bool
    candidates: tuple[PatternCandidate, ...]
    rejected_case_ids: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    minimum_supporting_cases: int
    generated_at: str
    input_packet_hashes: Mapping[str, str]
    content_hash: str = ""
    schema_version: str = HISTORICAL_PATTERN_BUILD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidates",
            _model_tuple(
                self.candidates,
                PatternCandidate,
                field_name="candidates",
            ),
        )
        for field_name in ("rejected_case_ids", "errors", "warnings"):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if (
            isinstance(self.minimum_supporting_cases, bool)
            or not isinstance(self.minimum_supporting_cases, int)
            or self.minimum_supporting_cases < 2
        ):
            raise ValueError(
                "minimum_supporting_cases must be an integer of at least two."
            )
        object.__setattr__(
            self,
            "generated_at",
            _normalize_timestamp(
                self.generated_at,
                field_name="generated_at",
            ),
        )
        object.__setattr__(
            self,
            "input_packet_hashes",
            _string_mapping(
                self.input_packet_hashes,
                field_name="input_packet_hashes",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "HistoricalPatternCandidateBuildResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["candidates"] = tuple(
            PatternCandidate.from_dict(item)
            for item in raw.get("candidates", ())
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalPattern(_JsonContract):
    pattern_id: str
    pattern_version: int
    name: str
    description: str
    invariant_features: tuple[str, ...]
    common_features: tuple[str, ...]
    optional_features: tuple[str, ...]
    disqualifying_features: tuple[str, ...]
    pattern_status: PatternStatus
    pattern_direction: PatternDirection
    applicable_move_types: tuple[HistoricalMoveType, ...]
    initiating_exposure_types: tuple[ExposureType, ...]
    initiating_conditions: tuple[str, ...]
    trigger_event_types: tuple[str, ...]
    no_single_trigger: bool
    transmission_mechanisms: tuple[TransmissionMechanism, ...]
    amplifying_exposure_types: tuple[ExposureType, ...]
    amplifiers: tuple[str, ...]
    dampening_exposure_types: tuple[ExposureType, ...]
    dampeners: tuple[str, ...]
    required_market_regimes: tuple[str, ...]
    optional_market_regimes: tuple[str, ...]
    incompatible_market_regimes: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    expected_outcome_profile: PatternOutcomeProfile
    supporting_case_ids: tuple[str, ...]
    contradicting_case_ids: tuple[str, ...]
    exception_case_ids: tuple[str, ...]
    no_contradicting_cases_explanation: str
    source_analysis_hashes: Mapping[str, str]
    support_metrics: PatternSupportMetrics
    confidence: PatternConfidence
    limitations: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    analyst_type: HistoricalAnalystType
    model_name: str
    prompt_version: str
    generated_at: str
    applicable_cutoff: str
    replacement_pattern_ref: str | None = None
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""
    schema_version: str = HISTORICAL_PATTERN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.pattern_version, bool)
            or not isinstance(self.pattern_version, int)
            or self.pattern_version < 1
        ):
            raise ValueError("pattern_version must be a positive integer.")
        for field_name in (
            "invariant_features",
            "common_features",
            "optional_features",
            "disqualifying_features",
            "initiating_conditions",
            "trigger_event_types",
            "amplifiers",
            "dampeners",
            "required_market_regimes",
            "optional_market_regimes",
            "incompatible_market_regimes",
            "failure_conditions",
            "supporting_case_ids",
            "contradicting_case_ids",
            "exception_case_ids",
            "limitations",
            "missing_evidence",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        for field_name, enum_type in (
            ("applicable_move_types", HistoricalMoveType),
            ("initiating_exposure_types", ExposureType),
            ("transmission_mechanisms", TransmissionMechanism),
            ("amplifying_exposure_types", ExposureType),
            ("dampening_exposure_types", ExposureType),
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_tuple(
                    getattr(self, field_name),
                    enum_type,
                    field_name=field_name,
                ),
            )
        for field_name, enum_type in (
            ("pattern_status", PatternStatus),
            ("pattern_direction", PatternDirection),
            ("confidence", PatternConfidence),
            ("analyst_type", HistoricalAnalystType),
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(
                    getattr(self, field_name),
                    enum_type,
                    field_name=field_name,
                ),
            )
        if not isinstance(
            self.expected_outcome_profile,
            PatternOutcomeProfile,
        ):
            raise TypeError(
                "expected_outcome_profile must be PatternOutcomeProfile."
            )
        if not isinstance(self.support_metrics, PatternSupportMetrics):
            raise TypeError("support_metrics must be PatternSupportMetrics.")
        object.__setattr__(
            self,
            "source_analysis_hashes",
            _string_mapping(
                self.source_analysis_hashes,
                field_name="source_analysis_hashes",
            ),
        )
        for field_name in (
            "name",
            "description",
            "no_contradicting_cases_explanation",
            "model_name",
            "prompt_version",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string.")
        if self.replacement_pattern_ref is not None and not isinstance(
            self.replacement_pattern_ref,
            str,
        ):
            raise TypeError("replacement_pattern_ref must be a string or null.")
        object.__setattr__(
            self,
            "generated_at",
            _normalize_timestamp(
                self.generated_at,
                field_name="generated_at",
            ),
        )
        object.__setattr__(
            self,
            "applicable_cutoff",
            _normalize_timestamp(
                self.applicable_cutoff,
                field_name="applicable_cutoff",
            ),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")

    @property
    def pattern_ref(self) -> str:
        return _pattern_ref(self.pattern_id, self.pattern_version)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoricalPattern":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["expected_outcome_profile"] = PatternOutcomeProfile.from_dict(
            raw["expected_outcome_profile"]
        )
        raw["support_metrics"] = PatternSupportMetrics.from_dict(
            raw["support_metrics"]
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class PatternOverlapDiagnostic(_JsonContract):
    first_pattern_ref: str
    second_pattern_ref: str
    relationship: PatternOverlapRelationship
    shared_exposure_types: tuple[ExposureType, ...]
    shared_transmission_mechanisms: tuple[TransmissionMechanism, ...]
    shared_event_types: tuple[str, ...]
    shared_market_regimes: tuple[str, ...]
    shared_supporting_case_ids: tuple[str, ...]
    outcome_profile_match: bool
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relationship",
            _enum_value(
                self.relationship,
                PatternOverlapRelationship,
                field_name="relationship",
            ),
        )
        for field_name, enum_type in (
            ("shared_exposure_types", ExposureType),
            (
                "shared_transmission_mechanisms",
                TransmissionMechanism,
            ),
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_tuple(
                    getattr(self, field_name),
                    enum_type,
                    field_name=field_name,
                ),
            )
        for field_name in (
            "shared_event_types",
            "shared_market_regimes",
            "shared_supporting_case_ids",
            "warnings",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "PatternOverlapDiagnostic":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalPatternLibrary(_JsonContract):
    library_id: str
    library_schema_version: str
    created_at: str
    applicable_cutoff: str
    patterns: tuple[HistoricalPattern, ...]
    deprecated_pattern_refs: tuple[str, ...]
    rejected_candidate_refs: tuple[str, ...]
    source_case_hashes: Mapping[str, str]
    source_analysis_hashes: Mapping[str, str]
    build_metadata: Mapping[str, Any]
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "applicable_cutoff",
            _normalize_timestamp(
                self.applicable_cutoff,
                field_name="applicable_cutoff",
            ),
        )
        object.__setattr__(
            self,
            "patterns",
            _model_tuple(
                self.patterns,
                HistoricalPattern,
                field_name="patterns",
            ),
        )
        for field_name in (
            "deprecated_pattern_refs",
            "rejected_candidate_refs",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        for field_name in (
            "source_case_hashes",
            "source_analysis_hashes",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_mapping(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if not isinstance(self.build_metadata, Mapping):
            raise TypeError("build_metadata must be a mapping.")
        object.__setattr__(
            self,
            "build_metadata",
            _freeze_json(self.build_metadata),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "HistoricalPatternLibrary":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["patterns"] = tuple(
            HistoricalPattern.from_dict(item)
            for item in raw.get("patterns", ())
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalPatternLibraryBuildResult(_JsonContract):
    success: bool
    library: HistoricalPatternLibrary | None
    accepted_pattern_refs: tuple[str, ...]
    rejected_pattern_refs: tuple[str, ...]
    overlap_diagnostics: tuple[PatternOverlapDiagnostic, ...]
    warnings: tuple[str, ...]
    validation_result: ValidationResult
    generated_at: str
    content_hash: str = ""
    schema_version: str = HISTORICAL_PATTERN_BUILD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.library is not None and not isinstance(
            self.library,
            HistoricalPatternLibrary,
        ):
            raise TypeError(
                "library must be HistoricalPatternLibrary or null."
            )
        for field_name in (
            "accepted_pattern_refs",
            "rejected_pattern_refs",
            "warnings",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "overlap_diagnostics",
            _model_tuple(
                self.overlap_diagnostics,
                PatternOverlapDiagnostic,
                field_name="overlap_diagnostics",
            ),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")
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
    ) -> "HistoricalPatternLibraryBuildResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("library") is not None:
            raw["library"] = HistoricalPatternLibrary.from_dict(
                raw["library"]
            )
        raw["overlap_diagnostics"] = tuple(
            PatternOverlapDiagnostic.from_dict(item)
            for item in raw.get("overlap_diagnostics", ())
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


_VALIDATION_RULE_ORDER = (
    "schema_version",
    "content_hash",
    "pattern_identity",
    "pattern_provenance",
    "pattern_case_support",
    "pattern_case_references",
    "pattern_analysis_references",
    "pattern_source_hashes",
    "pattern_case_overlap",
    "pattern_candidate_accounting",
    "pattern_controlled_values",
    "pattern_falsifiability",
    "pattern_expected_outcome",
    "pattern_certainty_language",
    "pattern_causal_law_language",
    "pattern_statistical_claims",
    "pattern_concentration",
    "pattern_support_metrics",
    "pattern_cutoff",
    "pattern_replacement",
    "candidate_identity",
    "candidate_case_support",
    "candidate_case_references",
    "candidate_analysis_references",
    "candidate_shared_features",
    "library_identity",
    "library_ordering",
    "library_duplicates",
    "library_pattern_validity",
    "library_references",
    "library_cutoff",
)


class _ValidationCollector:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []
        self.seen: set[str] = set()
        self.failed: set[str] = set()

    def require(
        self,
        rule: str,
        condition: bool,
        path: str,
        message: str,
    ) -> None:
        self.seen.add(rule)
        if condition:
            return
        self.failed.add(rule)
        self.errors.append(
            ValidationIssue(
                code=rule,
                severity="error",
                path=path,
                message=message,
            )
        )

    def warning(self, code: str, path: str, message: str) -> None:
        self.warnings.append(
            ValidationIssue(
                code=code,
                severity="warning",
                path=path,
                message=message,
            )
        )

    def result(self) -> ValidationResult:
        ordered = tuple(
            rule for rule in _VALIDATION_RULE_ORDER if rule in self.seen
        ) + tuple(sorted(self.seen - set(_VALIDATION_RULE_ORDER)))
        failed = tuple(rule for rule in ordered if rule in self.failed)
        passed = tuple(rule for rule in ordered if rule not in self.failed)
        score = round(len(passed) / len(ordered), 6) if ordered else 1.0
        return ValidationResult(
            is_valid=not self.errors,
            score=score,
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            failed_rules=failed,
            passed_rules=passed,
            validation_version=HISTORICAL_PATTERN_VALIDATION_VERSION,
        )


def pattern_candidate_content_hash(value: PatternCandidate) -> str:
    if not isinstance(value, PatternCandidate):
        raise TypeError("value must be PatternCandidate.")
    return _content_hash(value)


def historical_pattern_content_hash(value: HistoricalPattern) -> str:
    if not isinstance(value, HistoricalPattern):
        raise TypeError("value must be HistoricalPattern.")
    return _content_hash(value)


def historical_pattern_library_content_hash(
    value: HistoricalPatternLibrary,
) -> str:
    if not isinstance(value, HistoricalPatternLibrary):
        raise TypeError("value must be HistoricalPatternLibrary.")
    return _content_hash(value)


def historical_pattern_candidate_build_result_content_hash(
    value: HistoricalPatternCandidateBuildResult,
) -> str:
    if not isinstance(value, HistoricalPatternCandidateBuildResult):
        raise TypeError(
            "value must be HistoricalPatternCandidateBuildResult."
        )
    return _content_hash(value)


def historical_pattern_library_build_result_content_hash(
    value: HistoricalPatternLibraryBuildResult,
) -> str:
    if not isinstance(value, HistoricalPatternLibraryBuildResult):
        raise TypeError(
            "value must be HistoricalPatternLibraryBuildResult."
        )
    return _content_hash(value)


def _regime_features(regime: MarketRegime) -> frozenset[str]:
    values: set[str] = set()
    for field_name in _REGIME_FIELDS:
        state = getattr(regime, field_name)
        if state in {
            MarketRegimeState.UNKNOWN,
            MarketRegimeState.UNAVAILABLE,
        }:
            continue
        values.add(f"{field_name}={state.value}")
    return frozenset(values)


def _regime_signature(regime: MarketRegime) -> tuple[str, ...]:
    return tuple(
        f"{field_name}={getattr(regime, field_name).value}"
        for field_name in _REGIME_FIELDS
    )


def _source_hash_values(packet: HistoricalEvidencePacket) -> frozenset[str]:
    return frozenset(packet.historical_move.source_hashes.values())


def _source_group_signature(packet: HistoricalEvidencePacket) -> str:
    return _hash_payload(sorted(_source_hash_values(packet)))


def _source_independence(
    packets: Sequence[HistoricalEvidencePacket],
) -> tuple[int, EvidenceIndependenceStatus]:
    if len(packets) < 2:
        return (
            len(packets),
            EvidenceIndependenceStatus.INSUFFICIENT_EVIDENCE,
        )
    remaining = set(range(len(packets)))
    component_count = 0
    overlap_found = False
    while remaining:
        component_count += 1
        pending = [remaining.pop()]
        while pending:
            current = pending.pop()
            current_sources = _source_hash_values(packets[current])
            connected = {
                candidate
                for candidate in remaining
                if current_sources
                & _source_hash_values(packets[candidate])
            }
            if connected:
                overlap_found = True
                remaining.difference_update(connected)
                pending.extend(sorted(connected))
    if not overlap_found:
        status = EvidenceIndependenceStatus.INDEPENDENT
    elif component_count == 1:
        status = EvidenceIndependenceStatus.CORRELATED
    else:
        status = EvidenceIndependenceStatus.MIXED
    return component_count, status


def _consistency_from_ratio(ratio: float) -> PatternConsistency:
    if ratio >= 0.75:
        return PatternConsistency.HIGH
    if ratio >= 0.4:
        return PatternConsistency.MODERATE
    if ratio > 0:
        return PatternConsistency.LOW
    return PatternConsistency.MIXED


def _outcome_consistency(
    packets: Sequence[HistoricalEvidencePacket],
) -> PatternConsistency:
    if len(packets) < 2:
        return PatternConsistency.INSUFFICIENT_EVIDENCE
    directions = {
        packet.historical_move.direction for packet in packets
    }
    move_types = {
        packet.historical_move.move_type for packet in packets
    }
    if len(directions) == 1 and len(move_types) == 1:
        return PatternConsistency.HIGH
    if len(directions) == 1:
        return PatternConsistency.MODERATE
    return PatternConsistency.MIXED


def _mechanism_consistency(
    analyses: Sequence[HistoricalCausalAnalysis],
) -> PatternConsistency:
    if len(analyses) < 2:
        return PatternConsistency.INSUFFICIENT_EVIDENCE
    mechanism_sets = [
        set(analysis.transmission_mechanisms) for analysis in analyses
    ]
    union = set().union(*mechanism_sets)
    if not union:
        return PatternConsistency.UNAVAILABLE
    intersection = set.intersection(*mechanism_sets)
    return _consistency_from_ratio(len(intersection) / len(union))


def _regime_diversity(unique_regime_count: int) -> PatternConsistency:
    if unique_regime_count >= 3:
        return PatternConsistency.HIGH
    if unique_regime_count == 2:
        return PatternConsistency.MODERATE
    if unique_regime_count == 1:
        return PatternConsistency.LOW
    return PatternConsistency.UNAVAILABLE


def _concentration(values: Sequence[str]) -> float:
    if not values:
        return 0.0
    maximum = max(Counter(values).values())
    return round(maximum / len(values), 6)


def compute_pattern_support_metrics(
    supporting_packets: Sequence[HistoricalEvidencePacket],
    supporting_analyses: Sequence[HistoricalCausalAnalysis],
    *,
    contradicting_case_count: int = 0,
    exception_case_count: int = 0,
) -> PatternSupportMetrics:
    packets = tuple(supporting_packets)
    analyses = tuple(supporting_analyses)
    if not packets:
        raise ValueError("At least one supporting packet is required.")
    packet_case_ids = {
        packet.historical_move.case_id for packet in packets
    }
    analysis_case_ids = {analysis.case_id for analysis in analyses}
    if packet_case_ids != analysis_case_ids:
        raise ValueError(
            "Supporting packets and causal analyses must cover the same cases."
        )
    starts = [
        _temporal_point(packet.historical_move.start_at)
        for packet in packets
    ]
    ends = [
        _temporal_point(packet.historical_move.end_at)
        for packet in packets
    ]
    earliest = min(starts)
    latest = max(ends)
    symbols = [packet.historical_move.symbol for packet in packets]
    sectors = [
        packet.historical_move.sector_symbol
        for packet in packets
        if packet.historical_move.sector_symbol
    ]
    regimes = {
        _regime_signature(packet.market_regime) for packet in packets
    }
    source_group_count, independence = _source_independence(packets)
    return PatternSupportMetrics(
        supporting_case_count=len(packets),
        contradicting_case_count=contradicting_case_count,
        exception_case_count=exception_case_count,
        unique_symbol_count=len(set(symbols)),
        unique_sector_count=len(set(sectors)),
        unique_regime_count=len(regimes),
        unique_move_type_count=len(
            {
                packet.historical_move.move_type
                for packet in packets
            }
        ),
        independent_source_group_count=source_group_count,
        earliest_case_start=earliest.isoformat(timespec="seconds"),
        latest_case_end=latest.isoformat(timespec="seconds"),
        temporal_span_days=round(
            (latest - earliest).total_seconds() / 86_400.0,
            6,
        ),
        outcome_consistency=_outcome_consistency(packets),
        mechanism_consistency=_mechanism_consistency(analyses),
        regime_diversity=_regime_diversity(len(regimes)),
        symbol_concentration=_concentration(symbols),
        sector_concentration=_concentration(sectors),
        evidence_independence_status=independence,
    )


def magnitude_bucket_for_move(
    packet: HistoricalEvidencePacket,
) -> MagnitudeBucket:
    magnitude = abs(packet.historical_move.percentage_move)
    if magnitude < 10:
        return MagnitudeBucket.UNDER_10_PERCENT
    if magnitude < 20:
        return MagnitudeBucket.FROM_10_TO_20_PERCENT
    if magnitude < 40:
        return MagnitudeBucket.FROM_20_TO_40_PERCENT
    if magnitude < 60:
        return MagnitudeBucket.FROM_40_TO_60_PERCENT
    return MagnitudeBucket.OVER_60_PERCENT


def duration_bucket_for_move(
    packet: HistoricalEvidencePacket,
) -> DurationBucket:
    days = packet.historical_move.duration_days
    if days < 1:
        return DurationBucket.INTRADAY
    if days < 7:
        return DurationBucket.DAYS
    if days < 45:
        return DurationBucket.WEEKS
    if days < 730:
        return DurationBucket.MONTHS
    return DurationBucket.YEARS


def pattern_direction_for_cases(
    packets: Sequence[HistoricalEvidencePacket],
) -> PatternDirection:
    directions = {
        packet.historical_move.direction for packet in packets
    }
    if directions == {ScenarioDirection.UP}:
        return PatternDirection.BULLISH
    if directions == {ScenarioDirection.DOWN}:
        return PatternDirection.BEARISH
    if directions <= {ScenarioDirection.UP, ScenarioDirection.DOWN}:
        return PatternDirection.BIDIRECTIONAL
    return PatternDirection.CONTEXT_DEPENDENT


def _case_exposure_types(
    packet: HistoricalEvidencePacket,
) -> frozenset[ExposureType]:
    return frozenset(
        exposure.exposure_type
        for exposure in (
            packet.exposures_before_move
            + packet.exposures_during_move
        )
    )


def _case_event_types(
    packet: HistoricalEvidencePacket,
) -> frozenset[str]:
    return frozenset(
        _normalized_token(item.event.event_type)
        for item in packet.material_events
        if _normalized_token(item.event.event_type)
    )


def _set_intersection(values: Sequence[set[Any]]) -> set[Any]:
    return set.intersection(*values) if values else set()


def _candidate_actual_shared(
    packets: Sequence[HistoricalEvidencePacket],
    analyses: Sequence[HistoricalCausalAnalysis],
) -> tuple[
    set[HistoricalMoveType],
    set[ExposureType],
    set[TransmissionMechanism],
    set[str],
    set[str],
]:
    return (
        _set_intersection(
            [{packet.historical_move.move_type} for packet in packets]
        ),
        _set_intersection(
            [set(_case_exposure_types(packet)) for packet in packets]
        ),
        _set_intersection(
            [set(analysis.transmission_mechanisms) for analysis in analyses]
        ),
        _set_intersection(
            [set(_case_event_types(packet)) for packet in packets]
        ),
        _set_intersection(
            [set(_regime_features(packet.market_regime)) for packet in packets]
        ),
    )


def validate_pattern_candidate(
    candidate: PatternCandidate,
    *,
    packets: Sequence[HistoricalEvidencePacket] = (),
    analyses: Sequence[HistoricalCausalAnalysis] = (),
    minimum_supporting_cases: int = DEFAULT_MINIMUM_PATTERN_CASES,
) -> ValidationResult:
    if not isinstance(candidate, PatternCandidate):
        raise TypeError("candidate must be PatternCandidate.")
    if (
        isinstance(minimum_supporting_cases, bool)
        or not isinstance(minimum_supporting_cases, int)
        or minimum_supporting_cases < 2
    ):
        raise ValueError(
            "minimum_supporting_cases must be an integer of at least two."
        )
    result = _ValidationCollector()
    result.require(
        "schema_version",
        candidate.schema_version
        == HISTORICAL_PATTERN_CANDIDATE_SCHEMA_VERSION,
        "schema_version",
        "Pattern-candidate schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(candidate.content_hash)
        and candidate.content_hash == pattern_candidate_content_hash(candidate),
        "content_hash",
        "Pattern-candidate content hash is missing or mismatched.",
    )
    identity_valid = (
        bool(candidate.candidate_id.strip())
        and candidate.candidate_id == _normalized_token(candidate.candidate_id)
        and not _duplicates(candidate.case_ids)
        and tuple(sorted(candidate.case_ids)) == candidate.case_ids
    )
    result.require(
        "candidate_identity",
        identity_valid,
        "candidate_id",
        "Candidate identity and case ordering must be stable and unique.",
    )
    result.require(
        "candidate_case_support",
        len(candidate.case_ids) >= minimum_supporting_cases,
        "case_ids",
        "Candidate support is below the configured minimum.",
    )
    result.require(
        "candidate_analysis_references",
        set(candidate.causal_analysis_hashes) == set(candidate.case_ids)
        and all(candidate.causal_analysis_hashes.values()),
        "causal_analysis_hashes",
        "Each candidate case requires exactly one causal-analysis hash.",
    )
    duplicate_features = any(
        _duplicates(values)
        for values in (
            candidate.grouping_basis,
            candidate.shared_move_types,
            candidate.shared_exposure_types,
            candidate.shared_transmission_mechanisms,
            candidate.shared_event_types,
            candidate.shared_market_regime_features,
            candidate.differing_features,
            candidate.candidate_warnings,
        )
    )
    ordered_features = all(
        tuple(sorted(values, key=lambda item: str(item))) == values
        for values in (
            candidate.grouping_basis,
            candidate.shared_move_types,
            candidate.shared_exposure_types,
            candidate.shared_transmission_mechanisms,
            candidate.shared_event_types,
            candidate.shared_market_regime_features,
            candidate.differing_features,
            candidate.candidate_warnings,
        )
    )
    result.require(
        "candidate_shared_features",
        bool(candidate.grouping_basis)
        and not duplicate_features
        and ordered_features,
        "grouping_basis",
        "Candidate features must be non-empty, unique, and deterministic.",
    )
    packet_map = {
        packet.historical_move.case_id: packet for packet in packets
    }
    analysis_map = {analysis.case_id: analysis for analysis in analyses}
    if packets or analyses:
        result.require(
            "candidate_case_references",
            set(candidate.case_ids) <= set(packet_map)
            and set(candidate.case_ids) <= set(analysis_map),
            "case_ids",
            "Candidate contains case IDs without supplied immutable inputs.",
        )
        if set(candidate.case_ids) <= set(packet_map) and set(
            candidate.case_ids
        ) <= set(analysis_map):
            selected_packets = tuple(
                packet_map[case_id] for case_id in candidate.case_ids
            )
            selected_analyses = tuple(
                analysis_map[case_id] for case_id in candidate.case_ids
            )
            expected_hashes = {
                case_id: analysis_map[case_id].content_hash
                for case_id in candidate.case_ids
            }
            result.require(
                "candidate_analysis_references",
                dict(candidate.causal_analysis_hashes) == expected_hashes,
                "causal_analysis_hashes",
                "Candidate causal-analysis hashes do not match source records.",
            )
            shared = _candidate_actual_shared(
                selected_packets,
                selected_analyses,
            )
            observed_shared = (
                set(candidate.shared_move_types),
                set(candidate.shared_exposure_types),
                set(candidate.shared_transmission_mechanisms),
                set(candidate.shared_event_types),
                set(candidate.shared_market_regime_features),
            )
            result.require(
                "candidate_shared_features",
                shared == observed_shared,
                "shared_transmission_mechanisms",
                "Candidate shared features do not match their source cases.",
            )
            expected_metrics = compute_pattern_support_metrics(
                selected_packets,
                selected_analyses,
            )
            result.require(
                "pattern_support_metrics",
                candidate.candidate_support_metrics == expected_metrics,
                "candidate_support_metrics",
                "Candidate support metrics are inconsistent with source cases.",
            )
    else:
        result.require(
            "pattern_support_metrics",
            candidate.candidate_support_metrics.supporting_case_count
            == len(candidate.case_ids),
            "candidate_support_metrics.supporting_case_count",
            "Candidate support count is inconsistent.",
        )
    return result.result()


def finalize_pattern_candidate(
    candidate: PatternCandidate,
    *,
    packets: Sequence[HistoricalEvidencePacket] = (),
    analyses: Sequence[HistoricalCausalAnalysis] = (),
    minimum_supporting_cases: int = DEFAULT_MINIMUM_PATTERN_CASES,
) -> PatternCandidate:
    if not isinstance(candidate, PatternCandidate):
        raise TypeError("candidate must be PatternCandidate.")
    seed = replace(
        candidate,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(seed, content_hash=pattern_candidate_content_hash(seed))
    validation = validate_pattern_candidate(
        seed,
        packets=packets,
        analyses=analyses,
        minimum_supporting_cases=minimum_supporting_cases,
    )
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=pattern_candidate_content_hash(finalized),
    )
    repeated = validate_pattern_candidate(
        finalized,
        packets=packets,
        analyses=analyses,
        minimum_supporting_cases=minimum_supporting_cases,
    )
    if repeated != validation:
        raise RuntimeError("Pattern-candidate validation did not stabilize.")
    return finalized


def _historical_move_signature(
    packet: HistoricalEvidencePacket,
) -> tuple[Any, ...]:
    move = packet.historical_move
    return (
        move.symbol,
        move.exchange,
        move.start_at,
        move.end_at,
        round(move.start_price, 10),
        round(move.end_price, 10),
    )


def _candidate_differences(
    packets: Sequence[HistoricalEvidencePacket],
    analyses: Sequence[HistoricalCausalAnalysis],
    features: Mapping[str, HistoricalSimilarityFeatures],
) -> tuple[str, ...]:
    values: list[str] = []

    def add(name: str, items: set[str]) -> None:
        if len(items) > 1:
            values.append(f"{name}=" + "|".join(sorted(items)))

    add(
        "direction",
        {packet.historical_move.direction.value for packet in packets},
    )
    add(
        "move_type",
        {packet.historical_move.move_type.value for packet in packets},
    )
    add(
        "exposure",
        {
            item.value
            for packet in packets
            for item in _case_exposure_types(packet)
        },
    )
    add(
        "transmission",
        {
            item.value
            for analysis in analyses
            for item in analysis.transmission_mechanisms
        },
    )
    add(
        "event",
        {
            item
            for packet in packets
            for item in _case_event_types(packet)
        },
    )
    selected_features = [
        features[packet.historical_move.case_id]
        for packet in packets
        if packet.historical_move.case_id in features
    ]
    if selected_features:
        add(
            "magnitude",
            {item.magnitude_bucket.value for item in selected_features},
        )
        add(
            "duration",
            {item.duration_bucket.value for item in selected_features},
        )
        add(
            "technical_structure",
            {item.technical_structure for item in selected_features},
        )
    return tuple(sorted(values))


def _shared_parent_event(
    packets: Sequence[HistoricalEvidencePacket],
) -> bool:
    signatures: list[tuple[str, str | None, str]] = []
    for packet in packets:
        for item in packet.material_events:
            event = item.event
            signatures.append(
                (
                    _normalized_token(event.event_type),
                    event.scheduled_at,
                    event.source,
                )
            )
    return bool(_duplicates(signatures))


def _overlapping_source_groups(
    packets: Sequence[HistoricalEvidencePacket],
) -> bool:
    return any(
        bool(_source_hash_values(first) & _source_hash_values(second))
        for first, second in combinations(packets, 2)
    )


def _candidate_warnings(
    packets: Sequence[HistoricalEvidencePacket],
    metrics: PatternSupportMetrics,
    *,
    duplicate_move_excluded: bool,
) -> tuple[str, ...]:
    warnings: set[str] = set()
    if metrics.symbol_concentration == 1.0:
        warnings.add("one_symbol_concentration")
    known_sectors = [
        packet.historical_move.sector_symbol
        for packet in packets
        if packet.historical_move.sector_symbol
    ]
    if not known_sectors:
        warnings.add("sector_data_unavailable")
    elif (
        len(known_sectors) == len(packets)
        and metrics.sector_concentration == 1.0
    ):
        warnings.add("one_sector_concentration")
    if _overlapping_source_groups(packets):
        warnings.add("overlapping_source_groups")
    if _shared_parent_event(packets):
        warnings.add("shared_parent_event")
    if metrics.unique_regime_count < 2:
        warnings.add("insufficient_regime_diversity")
    if metrics.temporal_span_days < 365:
        warnings.add("insufficient_temporal_diversity")
    if metrics.outcome_consistency in {
        PatternConsistency.HIGH,
        PatternConsistency.MODERATE,
    }:
        warnings.add("no_negative_examples_available")
    if duplicate_move_excluded:
        warnings.add("duplicate_historical_move_excluded")
    return tuple(sorted(warnings))


class HistoricalPatternCandidateBuilder:
    """Build deterministic grouping proposals from immutable historical cases."""

    def __init__(
        self,
        *,
        minimum_supporting_cases: int = DEFAULT_MINIMUM_PATTERN_CASES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(minimum_supporting_cases, bool)
            or not isinstance(minimum_supporting_cases, int)
            or minimum_supporting_cases < 2
        ):
            raise ValueError(
                "minimum_supporting_cases must be an integer of at least two."
            )
        self.minimum_supporting_cases = minimum_supporting_cases
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _result(
        self,
        *,
        success: bool,
        candidates: Sequence[PatternCandidate],
        rejected_case_ids: Sequence[str],
        errors: Sequence[str],
        warnings: Sequence[str],
        packet_hashes: Mapping[str, str],
    ) -> HistoricalPatternCandidateBuildResult:
        seed = HistoricalPatternCandidateBuildResult(
            success=success,
            candidates=tuple(candidates),
            rejected_case_ids=tuple(sorted(set(rejected_case_ids))),
            errors=tuple(errors),
            warnings=tuple(sorted(set(warnings))),
            minimum_supporting_cases=self.minimum_supporting_cases,
            generated_at=self.clock(),
            input_packet_hashes=packet_hashes,
        )
        return replace(
            seed,
            content_hash=(
                historical_pattern_candidate_build_result_content_hash(seed)
            ),
        )

    def build(
        self,
        packets: Sequence[HistoricalEvidencePacket],
        analyses: Sequence[HistoricalCausalAnalysis],
        similarity_features: Sequence[HistoricalSimilarityFeatures] = (),
    ) -> HistoricalPatternCandidateBuildResult:
        if isinstance(packets, (str, bytes)) or not isinstance(
            packets,
            Sequence,
        ):
            raise TypeError("packets must be a sequence.")
        if isinstance(analyses, (str, bytes)) or not isinstance(
            analyses,
            Sequence,
        ):
            raise TypeError("analyses must be a sequence.")
        if isinstance(similarity_features, (str, bytes)) or not isinstance(
            similarity_features,
            Sequence,
        ):
            raise TypeError("similarity_features must be a sequence.")
        packet_items = tuple(packets)
        analysis_items = tuple(analyses)
        feature_items = tuple(similarity_features)
        if not all(
            isinstance(item, HistoricalEvidencePacket)
            for item in packet_items
        ):
            raise TypeError(
                "packets must contain HistoricalEvidencePacket objects."
            )
        if not all(
            isinstance(item, HistoricalCausalAnalysis)
            for item in analysis_items
        ):
            raise TypeError(
                "analyses must contain HistoricalCausalAnalysis objects."
            )
        if not all(
            isinstance(item, HistoricalSimilarityFeatures)
            for item in feature_items
        ):
            raise TypeError(
                "similarity_features must contain HistoricalSimilarityFeatures."
            )

        packet_ids = [
            item.historical_move.case_id for item in packet_items
        ]
        analysis_ids = [item.case_id for item in analysis_items]
        feature_ids = [item.case_id for item in feature_items]
        packet_hashes = {
            item.historical_move.case_id: item.content_hash
            for item in packet_items
        }
        errors: list[str] = []
        if _duplicates(packet_ids):
            errors.append("Duplicate packet case IDs are not permitted.")
        if _duplicates(analysis_ids):
            errors.append(
                "Multiple causal analyses for one case are not permitted."
            )
        if _duplicates(feature_ids):
            errors.append(
                "Multiple similarity-feature records for one case are not permitted."
            )
        packet_map = {
            item.historical_move.case_id: item for item in packet_items
        }
        analysis_map = {item.case_id: item for item in analysis_items}
        feature_map = {item.case_id: item for item in feature_items}
        if set(packet_map) != set(analysis_map):
            errors.append(
                "Packets and causal analyses must cover exactly the same cases."
            )
        if not set(feature_map) <= set(packet_map):
            errors.append(
                "Similarity features reference cases outside the packet set."
            )
        for case_id, packet in packet_map.items():
            validation = validate_historical_evidence_packet(packet)
            if (
                not validation.is_valid
                or packet.validation_result != validation
                or packet.content_hash
                != historical_evidence_packet_content_hash(packet)
            ):
                errors.append(
                    f"Historical packet {case_id!r} is invalid or not finalized."
                )
        for case_id, analysis in analysis_map.items():
            packet = packet_map.get(case_id)
            if packet is None:
                continue
            validation = validate_historical_causal_analysis(
                analysis,
                packet=packet,
            )
            if (
                not validation.is_valid
                or analysis.validation_result != validation
                or analysis.content_hash
                != historical_causal_analysis_content_hash(analysis)
            ):
                errors.append(
                    f"Causal analysis {case_id!r} is invalid or not finalized."
                )
        for case_id, item in feature_map.items():
            validation = validate_historical_similarity_features(item)
            if (
                not validation.is_valid
                or item.content_hash
                != historical_similarity_features_content_hash(item)
            ):
                errors.append(
                    f"Similarity features {case_id!r} are invalid."
                )
        if errors:
            return self._result(
                success=False,
                candidates=(),
                rejected_case_ids=(),
                errors=errors,
                warnings=(),
                packet_hashes=packet_hashes,
            )

        move_groups: dict[tuple[Any, ...], list[str]] = defaultdict(list)
        for case_id, packet in packet_map.items():
            move_groups[_historical_move_signature(packet)].append(case_id)
        rejected_duplicates: set[str] = set()
        for case_ids in move_groups.values():
            ordered = sorted(case_ids)
            rejected_duplicates.update(ordered[1:])
        active_case_ids = tuple(
            case_id
            for case_id in sorted(packet_map)
            if case_id not in rejected_duplicates
        )
        active_packets = {
            case_id: packet_map[case_id] for case_id in active_case_ids
        }
        active_analyses = {
            case_id: analysis_map[case_id] for case_id in active_case_ids
        }

        token_cases: dict[str, set[str]] = defaultdict(set)
        for case_id in active_case_ids:
            packet = active_packets[case_id]
            analysis = active_analyses[case_id]
            token_cases[
                f"move_type:{packet.historical_move.move_type.value}"
            ].add(case_id)
            for item in _case_exposure_types(packet):
                token_cases[f"exposure:{item.value}"].add(case_id)
            for item in analysis.transmission_mechanisms:
                token_cases[f"transmission:{item.value}"].add(case_id)
            for item in _case_event_types(packet):
                token_cases[f"event:{item}"].add(case_id)
            for item in _regime_features(packet.market_regime):
                token_cases[f"regime:{item}"].add(case_id)
            feature = feature_map.get(case_id)
            if feature is not None:
                token_cases[
                    f"magnitude:{feature.magnitude_bucket.value}"
                ].add(case_id)
                token_cases[
                    f"duration:{feature.duration_bucket.value}"
                ].add(case_id)
                token_cases[
                    "technical:"
                    + _normalized_token(feature.technical_structure)
                ].add(case_id)

        grouped_bases: dict[tuple[str, ...], set[str]] = defaultdict(set)
        for token, case_ids in sorted(token_cases.items()):
            if len(case_ids) < self.minimum_supporting_cases:
                continue
            grouped_bases[tuple(sorted(case_ids))].add(token)

        candidates: list[PatternCandidate] = []
        duplicate_move_excluded = bool(rejected_duplicates)
        for case_ids, bases in sorted(grouped_bases.items()):
            selected_packets = tuple(
                active_packets[case_id] for case_id in case_ids
            )
            selected_analyses = tuple(
                active_analyses[case_id] for case_id in case_ids
            )
            shared = _candidate_actual_shared(
                selected_packets,
                selected_analyses,
            )
            metrics = compute_pattern_support_metrics(
                selected_packets,
                selected_analyses,
            )
            candidate_id = "candidate_" + _hash_payload(case_ids)[:20]
            draft = PatternCandidate(
                candidate_id=candidate_id,
                case_ids=case_ids,
                causal_analysis_hashes={
                    case_id: active_analyses[case_id].content_hash
                    for case_id in case_ids
                },
                grouping_basis=tuple(sorted(bases)),
                shared_move_types=tuple(
                    sorted(shared[0], key=lambda item: item.value)
                ),
                shared_exposure_types=tuple(
                    sorted(shared[1], key=lambda item: item.value)
                ),
                shared_transmission_mechanisms=tuple(
                    sorted(shared[2], key=lambda item: item.value)
                ),
                shared_event_types=tuple(sorted(shared[3])),
                shared_market_regime_features=tuple(sorted(shared[4])),
                differing_features=_candidate_differences(
                    selected_packets,
                    selected_analyses,
                    feature_map,
                ),
                candidate_support_metrics=metrics,
                candidate_warnings=_candidate_warnings(
                    selected_packets,
                    metrics,
                    duplicate_move_excluded=duplicate_move_excluded,
                ),
            )
            finalized = finalize_pattern_candidate(
                draft,
                packets=selected_packets,
                analyses=selected_analyses,
                minimum_supporting_cases=self.minimum_supporting_cases,
            )
            candidates.append(finalized)

        build_warnings: list[str] = []
        if rejected_duplicates:
            build_warnings.append(
                "Duplicate historical moves were excluded from candidate support."
            )
        if not candidates:
            build_warnings.append(
                "No deterministic grouping reached the configured support minimum."
            )
        return self._result(
            success=True,
            candidates=tuple(
                sorted(candidates, key=lambda item: item.candidate_id)
            ),
            rejected_case_ids=tuple(sorted(rejected_duplicates)),
            errors=(),
            warnings=build_warnings,
            packet_hashes=packet_hashes,
        )


_PATTERN_CERTAINTY_PATTERNS = (
    re.compile(r"\b(?:definitely|certainly|undeniably)\s+caused\b", re.I),
    re.compile(r"\bcertainly\s+happened\s+because\b", re.I),
    re.compile(r"\bguaranteed\b", re.I),
    re.compile(r"\bprove(?:s|d)?\s+that\b", re.I),
    re.compile(r"\bmust\s+have\s+caused\b", re.I),
)
_PATTERN_CAUSAL_LAW_PATTERNS = (
    re.compile(r"\b(?:always|universally|inevitably)\b", re.I),
    re.compile(r"\bcausal\s+law\b", re.I),
    re.compile(r"\bguarantees?\s+(?:a|the|that)\b", re.I),
    re.compile(r"\bmust\s+(?:produce|cause|lead\s+to)\b", re.I),
)
_PATTERN_STATISTICAL_PATTERNS = (
    re.compile(r"\bstatistically\s+significant\b", re.I),
    re.compile(r"\bp[- ]?value\b", re.I),
    re.compile(r"\bconfidence\s+interval\b", re.I),
    re.compile(r"\bprobabilit(?:y|ies)\b", re.I),
    re.compile(r"\b(?:odds|risk ratio|correlation coefficient)\b", re.I),
)
_PATTERN_CIRCULAR_PATTERNS = (
    re.compile(r"\bbad\s+news\s+causes?\s+crashes?\b", re.I),
    re.compile(r"\bbullish\s+conditions?\s+causes?\s+rall(?:y|ies)\b", re.I),
    re.compile(
        r"\bstocks?\s+fall(?:s)?\s+when\s+sellers?\s+dominate\b",
        re.I,
    ),
    re.compile(r"\bmomentum\s+causes?\s+momentum\b", re.I),
)


def _pattern_text(pattern: HistoricalPattern) -> str:
    profile = pattern.expected_outcome_profile
    values = (
        (pattern.name, pattern.description)
        + pattern.invariant_features
        + pattern.common_features
        + pattern.optional_features
        + pattern.disqualifying_features
        + pattern.initiating_conditions
        + pattern.amplifiers
        + pattern.dampeners
        + pattern.failure_conditions
        + pattern.limitations
        + pattern.missing_evidence
        + profile.uncertainty_notes
    )
    return " ".join(values)


def _regime_tokens_valid(values: Sequence[str]) -> bool:
    allowed_fields = set(_REGIME_FIELDS)
    for value in values:
        if value.count("=") != 1:
            return False
        field_name, state_value = value.split("=", 1)
        if field_name not in allowed_fields:
            return False
        try:
            MarketRegimeState(state_value)
        except ValueError:
            return False
    return True


def _expected_profile_valid(
    pattern: HistoricalPattern,
    supporting_packets: Sequence[HistoricalEvidencePacket],
) -> bool:
    profile = pattern.expected_outcome_profile
    if not profile.expected_move_types:
        return False
    if profile.expected_direction != pattern.pattern_direction:
        return False
    if not set(profile.expected_move_types) <= set(
        pattern.applicable_move_types
    ):
        return False
    if not supporting_packets:
        return True
    observed_move_types = {
        packet.historical_move.move_type for packet in supporting_packets
    }
    if not set(profile.expected_move_types) <= observed_move_types:
        return False
    expected_direction = pattern_direction_for_cases(supporting_packets)
    if pattern.pattern_direction != expected_direction:
        return False
    magnitude_buckets = {
        magnitude_bucket_for_move(packet) for packet in supporting_packets
    }
    if len(magnitude_buckets) == 1:
        if profile.magnitude_bucket not in {
            next(iter(magnitude_buckets)),
            MagnitudeBucket.UNAVAILABLE,
        }:
            return False
    elif profile.magnitude_bucket is not MagnitudeBucket.UNAVAILABLE:
        return False
    duration_buckets = {
        duration_bucket_for_move(packet) for packet in supporting_packets
    }
    if len(duration_buckets) == 1:
        if profile.duration_bucket not in {
            next(iter(duration_buckets)),
            DurationBucket.UNAVAILABLE,
        }:
            return False
    elif profile.duration_bucket is not DurationBucket.UNAVAILABLE:
        return False
    return True


def _concentration_disclosed(
    limitations: Sequence[str],
    *tokens: str,
) -> bool:
    text = " ".join(limitations).casefold()
    return any(token in text for token in tokens)


def validate_historical_pattern(
    pattern: HistoricalPattern,
    *,
    candidate: PatternCandidate | None = None,
    packets: Sequence[HistoricalEvidencePacket] = (),
    analyses: Sequence[HistoricalCausalAnalysis] = (),
    minimum_supporting_cases: int = DEFAULT_MINIMUM_PATTERN_CASES,
) -> ValidationResult:
    if not isinstance(pattern, HistoricalPattern):
        raise TypeError("pattern must be HistoricalPattern.")
    if candidate is not None and not isinstance(candidate, PatternCandidate):
        raise TypeError("candidate must be PatternCandidate or null.")
    if (
        isinstance(minimum_supporting_cases, bool)
        or not isinstance(minimum_supporting_cases, int)
        or minimum_supporting_cases < 2
    ):
        raise ValueError(
            "minimum_supporting_cases must be an integer of at least two."
        )
    result = _ValidationCollector()
    result.require(
        "schema_version",
        pattern.schema_version == HISTORICAL_PATTERN_SCHEMA_VERSION,
        "schema_version",
        "Historical-pattern schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(pattern.content_hash)
        and pattern.content_hash == historical_pattern_content_hash(pattern),
        "content_hash",
        "Historical-pattern content hash is missing or mismatched.",
    )
    identity_valid = (
        bool(pattern.pattern_id.strip())
        and pattern.pattern_id == _normalized_token(pattern.pattern_id)
        and pattern.pattern_version >= 1
        and bool(pattern.name.strip())
        and bool(pattern.description.strip())
    )
    result.require(
        "pattern_identity",
        identity_valid,
        "pattern_id",
        "Pattern ID, version, name, and description are required.",
    )
    provenance_valid = (
        bool(pattern.model_name.strip())
        and bool(pattern.prompt_version.strip())
        and _temporal_point(pattern.applicable_cutoff)
        <= _temporal_point(pattern.generated_at)
    )
    result.require(
        "pattern_provenance",
        provenance_valid,
        "model_name",
        "Pattern model, prompt, generated time, and cutoff are required.",
    )

    support_ids = set(pattern.supporting_case_ids)
    contradict_ids = set(pattern.contradicting_case_ids)
    exception_ids = set(pattern.exception_case_ids)
    all_ids = support_ids | contradict_ids | exception_ids
    no_duplicates = not any(
        _duplicates(values)
        for values in (
            pattern.supporting_case_ids,
            pattern.contradicting_case_ids,
            pattern.exception_case_ids,
        )
    )
    result.require(
        "pattern_case_support",
        len(pattern.supporting_case_ids) >= minimum_supporting_cases
        and no_duplicates,
        "supporting_case_ids",
        "Pattern support is below the configured minimum or contains duplicates.",
    )
    result.require(
        "pattern_case_overlap",
        not (support_ids & contradict_ids)
        and not (support_ids & exception_ids)
        and not (contradict_ids & exception_ids),
        "supporting_case_ids",
        "Supporting, contradicting, and exception cases must be disjoint.",
    )
    result.require(
        "pattern_analysis_references",
        set(pattern.source_analysis_hashes) == all_ids
        and all(pattern.source_analysis_hashes.values()),
        "source_analysis_hashes",
        "Every referenced case requires exactly one causal-analysis hash.",
    )
    if not contradict_ids:
        result.require(
            "pattern_falsifiability",
            bool(pattern.no_contradicting_cases_explanation.strip()),
            "no_contradicting_cases_explanation",
            "Absence of contradicting cases requires an explicit explanation.",
        )

    duplicate_controlled_values = any(
        _duplicates(values)
        for values in (
            pattern.applicable_move_types,
            pattern.initiating_exposure_types,
            pattern.trigger_event_types,
            pattern.transmission_mechanisms,
            pattern.amplifying_exposure_types,
            pattern.dampening_exposure_types,
            pattern.required_market_regimes,
            pattern.optional_market_regimes,
            pattern.incompatible_market_regimes,
        )
    )
    regime_overlap = (
        set(pattern.required_market_regimes)
        & set(pattern.optional_market_regimes)
        or set(pattern.required_market_regimes)
        & set(pattern.incompatible_market_regimes)
        or set(pattern.optional_market_regimes)
        & set(pattern.incompatible_market_regimes)
    )
    triggers_valid = (
        bool(pattern.trigger_event_types) or pattern.no_single_trigger
    )
    result.require(
        "pattern_controlled_values",
        bool(pattern.applicable_move_types)
        and triggers_valid
        and not duplicate_controlled_values
        and not regime_overlap
        and all(
            item == _normalized_token(item)
            for item in pattern.trigger_event_types
        )
        and _regime_tokens_valid(
            pattern.required_market_regimes
            + pattern.optional_market_regimes
            + pattern.incompatible_market_regimes
        ),
        "transmission_mechanisms",
        "Pattern controlled values are duplicated, malformed, or inconsistent.",
    )
    result.require(
        "pattern_falsifiability",
        bool(pattern.invariant_features)
        and bool(pattern.disqualifying_features)
        and bool(pattern.initiating_conditions)
        and bool(pattern.transmission_mechanisms)
        and bool(pattern.limitations)
        and bool(pattern.missing_evidence)
        and bool(
            pattern.failure_conditions
            or pattern.incompatible_market_regimes
        ),
        "failure_conditions",
        "Pattern requires initiation, transmission, limitations, missing evidence, and failure conditions.",
    )

    packet_map = {
        packet.historical_move.case_id: packet for packet in packets
    }
    analysis_map = {analysis.case_id: analysis for analysis in analyses}
    supporting_packets: tuple[HistoricalEvidencePacket, ...] = ()
    supporting_analyses: tuple[HistoricalCausalAnalysis, ...] = ()
    if packets or analyses:
        references_valid = all_ids <= set(packet_map) and all_ids <= set(
            analysis_map
        )
        result.require(
            "pattern_case_references",
            references_valid,
            "supporting_case_ids",
            "Pattern contains dangling case references.",
        )
        if references_valid:
            expected_hashes = {
                case_id: analysis_map[case_id].content_hash
                for case_id in all_ids
            }
            result.require(
                "pattern_source_hashes",
                dict(pattern.source_analysis_hashes) == expected_hashes,
                "source_analysis_hashes",
                "Pattern analysis hashes do not match immutable sources.",
            )
            supporting_packets = tuple(
                packet_map[case_id]
                for case_id in pattern.supporting_case_ids
            )
            supporting_analyses = tuple(
                analysis_map[case_id]
                for case_id in pattern.supporting_case_ids
            )
            source_exposures = {
                item
                for case_id in all_ids
                for item in _case_exposure_types(packet_map[case_id])
            }
            source_mechanisms = {
                item
                for case_id in all_ids
                for item in analysis_map[case_id].transmission_mechanisms
            }
            source_events = {
                item
                for case_id in all_ids
                for item in _case_event_types(packet_map[case_id])
            }
            source_regimes = {
                item
                for case_id in all_ids
                for item in _regime_features(packet_map[case_id].market_regime)
            }
            pattern_exposures = (
                set(pattern.initiating_exposure_types)
                | set(pattern.amplifying_exposure_types)
                | set(pattern.dampening_exposure_types)
            )
            controlled_valid = (
                pattern_exposures <= source_exposures
                and set(pattern.transmission_mechanisms)
                <= source_mechanisms
                and set(pattern.trigger_event_types) <= source_events
                and set(pattern.required_market_regimes)
                <= source_regimes
                and set(pattern.optional_market_regimes) <= source_regimes
                and set(pattern.incompatible_market_regimes)
                <= source_regimes
            )
            result.require(
                "pattern_controlled_values",
                controlled_valid,
                "initiating_exposure_types",
                "Pattern introduces mechanisms, events, or regimes absent from its source cases.",
            )
            expected_metrics = compute_pattern_support_metrics(
                supporting_packets,
                supporting_analyses,
                contradicting_case_count=len(contradict_ids),
                exception_case_count=len(exception_ids),
            )
            result.require(
                "pattern_support_metrics",
                pattern.support_metrics == expected_metrics,
                "support_metrics",
                "Pattern support metrics are inconsistent with source cases.",
            )
            cutoff = _temporal_point(pattern.applicable_cutoff)
            cutoff_valid = all(
                _temporal_point(packet_map[case_id].historical_move.market_data_cutoff)
                <= cutoff
                and _temporal_point(analysis_map[case_id].applicable_cutoff)
                <= cutoff
                for case_id in all_ids
            )
            result.require(
                "pattern_cutoff",
                cutoff_valid,
                "applicable_cutoff",
                "Pattern uses a source record after its applicable cutoff.",
            )
    else:
        metrics = pattern.support_metrics
        result.require(
            "pattern_support_metrics",
            metrics.supporting_case_count == len(support_ids)
            and metrics.contradicting_case_count == len(contradict_ids)
            and metrics.exception_case_count == len(exception_ids),
            "support_metrics",
            "Pattern support counts are inconsistent.",
        )

    if candidate is not None:
        candidate_validation = validate_pattern_candidate(
            candidate,
            packets=packets,
            analyses=analyses,
            minimum_supporting_cases=minimum_supporting_cases,
        )
        result.require(
            "pattern_candidate_accounting",
            candidate_validation.is_valid
            and all_ids == set(candidate.case_ids),
            "supporting_case_ids",
            "Pattern must account for every case in its validated candidate.",
        )
        result.require(
            "pattern_source_hashes",
            dict(pattern.source_analysis_hashes)
            == dict(candidate.causal_analysis_hashes),
            "source_analysis_hashes",
            "Pattern provenance must match the candidate analysis hashes.",
        )

    result.require(
        "pattern_expected_outcome",
        _expected_profile_valid(pattern, supporting_packets),
        "expected_outcome_profile",
        "Expected outcome is missing or inconsistent with supporting cases.",
    )
    text = _pattern_text(pattern)
    result.require(
        "pattern_certainty_language",
        not any(
            expression.search(text)
            for expression in _PATTERN_CERTAINTY_PATTERNS
        ),
        "description",
        "Pattern contains unsupported certainty language.",
    )
    result.require(
        "pattern_causal_law_language",
        not any(
            expression.search(text)
            for expression in (
                _PATTERN_CAUSAL_LAW_PATTERNS
                + _PATTERN_CIRCULAR_PATTERNS
            )
        ),
        "description",
        "Pattern is universal, circular, tautological, or outcome-defined.",
    )
    result.require(
        "pattern_statistical_claims",
        not any(
            expression.search(text)
            for expression in _PATTERN_STATISTICAL_PATTERNS
        ),
        "description",
        "Pattern makes statistical claims unsupported by descriptive metrics.",
    )

    metrics = pattern.support_metrics
    symbol_disclosed = _concentration_disclosed(
        pattern.limitations,
        "symbol",
        "specialist",
        "concentration",
    )
    sector_disclosed = _concentration_disclosed(
        pattern.limitations,
        "sector",
        "specialist",
        "concentration",
    )
    concentrated_confidence = pattern.confidence in {
        PatternConfidence.LOW,
        PatternConfidence.INSUFFICIENT_EVIDENCE,
        PatternConfidence.UNAVAILABLE,
    }
    concentration_valid = (
        (
            metrics.symbol_concentration < 1.0
            or (symbol_disclosed and concentrated_confidence)
        )
        and (
            metrics.sector_concentration < 1.0
            or metrics.unique_sector_count == 0
            or (sector_disclosed and concentrated_confidence)
        )
        and (
            metrics.evidence_independence_status
            is not EvidenceIndependenceStatus.CORRELATED
            or concentrated_confidence
        )
    )
    if pattern.confidence is PatternConfidence.HIGH:
        concentration_valid = concentration_valid and (
            metrics.unique_symbol_count >= 2
            and metrics.independent_source_group_count >= 2
            and metrics.unique_regime_count >= 2
            and metrics.symbol_concentration < 1.0
            and metrics.evidence_independence_status
            is EvidenceIndependenceStatus.INDEPENDENT
        )
    result.require(
        "pattern_concentration",
        concentration_valid,
        "confidence",
        "Pattern confidence does not reflect concentration or source dependence.",
    )

    replacement_valid = (
        pattern.replacement_pattern_ref is None
        or (
            bool(pattern.replacement_pattern_ref.strip())
            and pattern.replacement_pattern_ref != pattern.pattern_ref
        )
    )
    result.require(
        "pattern_replacement",
        replacement_valid,
        "replacement_pattern_ref",
        "Replacement reference is empty or self-referential.",
    )
    return result.result()


def finalize_historical_pattern(
    pattern: HistoricalPattern,
    *,
    candidate: PatternCandidate | None = None,
    packets: Sequence[HistoricalEvidencePacket] = (),
    analyses: Sequence[HistoricalCausalAnalysis] = (),
    minimum_supporting_cases: int = DEFAULT_MINIMUM_PATTERN_CASES,
) -> HistoricalPattern:
    if not isinstance(pattern, HistoricalPattern):
        raise TypeError("pattern must be HistoricalPattern.")
    seed = replace(
        pattern,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(seed, content_hash=historical_pattern_content_hash(seed))
    validation = validate_historical_pattern(
        seed,
        candidate=candidate,
        packets=packets,
        analyses=analyses,
        minimum_supporting_cases=minimum_supporting_cases,
    )
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=historical_pattern_content_hash(finalized),
    )
    repeated = validate_historical_pattern(
        finalized,
        candidate=candidate,
        packets=packets,
        analyses=analyses,
        minimum_supporting_cases=minimum_supporting_cases,
    )
    if repeated != validation:
        raise RuntimeError("Historical-pattern validation did not stabilize.")
    return finalized


def _pattern_exposures(pattern: HistoricalPattern) -> set[ExposureType]:
    return (
        set(pattern.initiating_exposure_types)
        | set(pattern.amplifying_exposure_types)
        | set(pattern.dampening_exposure_types)
    )


def _jaccard(first: set[Any], second: set[Any]) -> float:
    union = first | second
    if not union:
        return 1.0
    return len(first & second) / len(union)


def analyze_pattern_overlaps(
    patterns: Sequence[HistoricalPattern],
) -> tuple[PatternOverlapDiagnostic, ...]:
    if isinstance(patterns, (str, bytes)) or not isinstance(
        patterns,
        Sequence,
    ):
        raise TypeError("patterns must be a sequence.")
    values = tuple(patterns)
    if not all(isinstance(item, HistoricalPattern) for item in values):
        raise TypeError("patterns must contain HistoricalPattern objects.")
    diagnostics: list[PatternOverlapDiagnostic] = []
    ordered = sorted(
        values,
        key=lambda item: (item.pattern_id, item.pattern_version),
    )
    for first, second in combinations(ordered, 2):
        first_exposures = _pattern_exposures(first)
        second_exposures = _pattern_exposures(second)
        first_mechanisms = set(first.transmission_mechanisms)
        second_mechanisms = set(second.transmission_mechanisms)
        first_events = set(first.trigger_event_types)
        second_events = set(second.trigger_event_types)
        first_regimes = set(
            first.required_market_regimes + first.optional_market_regimes
        )
        second_regimes = set(
            second.required_market_regimes + second.optional_market_regimes
        )
        shared_exposures = first_exposures & second_exposures
        shared_mechanisms = first_mechanisms & second_mechanisms
        shared_events = first_events & second_events
        shared_regimes = first_regimes & second_regimes
        shared_cases = set(first.supporting_case_ids) & set(
            second.supporting_case_ids
        )
        outcome_match = (
            first.expected_outcome_profile
            == second.expected_outcome_profile
        )
        opposite = {
            first.pattern_direction,
            second.pattern_direction,
        } == {
            PatternDirection.BULLISH,
            PatternDirection.BEARISH,
        }
        core_overlap = bool(shared_mechanisms) and bool(shared_exposures)
        first_core = (
            first_exposures,
            first_mechanisms,
            first_events,
            first_regimes,
        )
        second_core = (
            second_exposures,
            second_mechanisms,
            second_events,
            second_regimes,
        )
        subset = all(
            left <= right for left, right in zip(first_core, second_core)
        ) or all(
            right <= left for left, right in zip(first_core, second_core)
        )
        scores = (
            _jaccard(first_exposures, second_exposures),
            _jaccard(first_mechanisms, second_mechanisms),
            _jaccard(first_events, second_events),
            _jaccard(first_regimes, second_regimes),
            _jaccard(
                set(first.supporting_case_ids),
                set(second.supporting_case_ids),
            ),
        )
        warnings: list[str] = []
        if core_overlap and opposite and first_regimes != second_regimes:
            relationship = (
                PatternOverlapRelationship.REGIME_DEPENDENT_OUTCOMES
            )
            warnings.append(
                "Same core mechanisms have different regime-dependent outcomes."
            )
        elif core_overlap and opposite:
            relationship = (
                PatternOverlapRelationship.MUTUALLY_CONTRADICTORY
            )
            warnings.append(
                "Patterns share core inputs but specify opposing outcomes."
            )
        elif outcome_match and sum(scores) / len(scores) >= 0.8:
            relationship = PatternOverlapRelationship.NEAR_DUPLICATE
            warnings.append(
                "Patterns are near duplicates; no automatic merge was performed."
            )
        elif outcome_match and subset and core_overlap:
            relationship = PatternOverlapRelationship.PARENT_CHILD
            warnings.append(
                "One pattern appears structurally narrower than the other."
            )
        elif any(
            (
                shared_exposures,
                shared_mechanisms,
                shared_events,
                shared_regimes,
                shared_cases,
            )
        ):
            relationship = PatternOverlapRelationship.OVERLAP
        else:
            relationship = PatternOverlapRelationship.NONE
        diagnostics.append(
            PatternOverlapDiagnostic(
                first_pattern_ref=first.pattern_ref,
                second_pattern_ref=second.pattern_ref,
                relationship=relationship,
                shared_exposure_types=tuple(
                    sorted(shared_exposures, key=lambda item: item.value)
                ),
                shared_transmission_mechanisms=tuple(
                    sorted(shared_mechanisms, key=lambda item: item.value)
                ),
                shared_event_types=tuple(sorted(shared_events)),
                shared_market_regimes=tuple(sorted(shared_regimes)),
                shared_supporting_case_ids=tuple(sorted(shared_cases)),
                outcome_profile_match=outcome_match,
                warnings=tuple(warnings),
            )
        )
    return tuple(diagnostics)


def validate_historical_pattern_library(
    library: HistoricalPatternLibrary,
) -> ValidationResult:
    if not isinstance(library, HistoricalPatternLibrary):
        raise TypeError("library must be HistoricalPatternLibrary.")
    result = _ValidationCollector()
    result.require(
        "schema_version",
        library.library_schema_version
        == HISTORICAL_PATTERN_LIBRARY_SCHEMA_VERSION,
        "library_schema_version",
        "Historical-pattern library schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(library.content_hash)
        and library.content_hash
        == historical_pattern_library_content_hash(library),
        "content_hash",
        "Historical-pattern library content hash is missing or mismatched.",
    )
    identity_valid = (
        bool(library.library_id.strip())
        and library.library_id == _normalized_token(library.library_id)
        and _temporal_point(library.applicable_cutoff)
        <= _temporal_point(library.created_at)
    )
    result.require(
        "library_identity",
        identity_valid,
        "library_id",
        "Library identity, creation time, or cutoff is invalid.",
    )
    expected_order = tuple(
        sorted(
            library.patterns,
            key=lambda item: (item.pattern_id, item.pattern_version),
        )
    )
    result.require(
        "library_ordering",
        library.patterns == expected_order,
        "patterns",
        "Library patterns must use deterministic ID/version ordering.",
    )
    refs = tuple(pattern.pattern_ref for pattern in library.patterns)
    result.require(
        "library_duplicates",
        not _duplicates(refs),
        "patterns",
        "Library contains duplicate pattern ID/version pairs.",
    )
    pattern_validity = all(
        pattern.validation_result.is_valid
        and pattern.content_hash == historical_pattern_content_hash(pattern)
        and validate_historical_pattern(pattern).is_valid
        and pattern.pattern_status
        in {
            PatternStatus.REVIEWED,
            PatternStatus.VALIDATED,
            PatternStatus.DEPRECATED,
        }
        for pattern in library.patterns
    )
    result.require(
        "library_pattern_validity",
        pattern_validity,
        "patterns",
        "Library contains an invalid or rejected pattern.",
    )
    ref_set = set(refs)
    deprecated_expected = {
        pattern.pattern_ref
        for pattern in library.patterns
        if pattern.pattern_status is PatternStatus.DEPRECATED
    }
    replacements = {
        pattern.replacement_pattern_ref
        for pattern in library.patterns
        if pattern.replacement_pattern_ref is not None
    }
    all_case_ids = {
        case_id
        for pattern in library.patterns
        for case_id in (
            pattern.supporting_case_ids
            + pattern.contradicting_case_ids
            + pattern.exception_case_ids
        )
    }
    analysis_hashes: dict[str, str] = {}
    analysis_conflict = False
    for pattern in library.patterns:
        for case_id, content_hash in pattern.source_analysis_hashes.items():
            existing = analysis_hashes.get(case_id)
            if existing is not None and existing != content_hash:
                analysis_conflict = True
            analysis_hashes[case_id] = content_hash
    references_valid = (
        set(library.deprecated_pattern_refs) == deprecated_expected
        and replacements <= ref_set
        and set(library.source_case_hashes) == all_case_ids
        and dict(library.source_analysis_hashes) == analysis_hashes
        and not analysis_conflict
        and not _duplicates(library.deprecated_pattern_refs)
        and not _duplicates(library.rejected_candidate_refs)
    )
    result.require(
        "library_references",
        references_valid,
        "deprecated_pattern_refs",
        "Library deprecation, source, or rejection references are inconsistent.",
    )
    cutoff = _temporal_point(library.applicable_cutoff)
    result.require(
        "library_cutoff",
        all(
            _temporal_point(pattern.applicable_cutoff) <= cutoff
            for pattern in library.patterns
        ),
        "applicable_cutoff",
        "Library includes a pattern after its applicable cutoff.",
    )
    return result.result()


def finalize_historical_pattern_library(
    library: HistoricalPatternLibrary,
) -> HistoricalPatternLibrary:
    if not isinstance(library, HistoricalPatternLibrary):
        raise TypeError("library must be HistoricalPatternLibrary.")
    seed = replace(
        library,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(
        seed,
        content_hash=historical_pattern_library_content_hash(seed),
    )
    validation = validate_historical_pattern_library(seed)
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=historical_pattern_library_content_hash(finalized),
    )
    repeated = validate_historical_pattern_library(finalized)
    if repeated != validation:
        raise RuntimeError("Pattern-library validation did not stabilize.")
    return finalized


def _failure_validation(code: str, message: str) -> ValidationResult:
    issue = ValidationIssue(
        code=code,
        severity="error",
        path="historical_pattern_library_builder",
        message=message,
    )
    return ValidationResult(
        is_valid=False,
        score=0.0,
        errors=(issue,),
        failed_rules=(code,),
        validation_version=HISTORICAL_PATTERN_VALIDATION_VERSION,
    )


class HistoricalPatternLibraryBuilder:
    """Build immutable in-memory pattern libraries without persistence."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _result(
        self,
        *,
        success: bool,
        library: HistoricalPatternLibrary | None,
        accepted: Sequence[str],
        rejected: Sequence[str],
        diagnostics: Sequence[PatternOverlapDiagnostic],
        warnings: Sequence[str],
        validation: ValidationResult,
    ) -> HistoricalPatternLibraryBuildResult:
        seed = HistoricalPatternLibraryBuildResult(
            success=success,
            library=library,
            accepted_pattern_refs=tuple(accepted),
            rejected_pattern_refs=tuple(rejected),
            overlap_diagnostics=tuple(diagnostics),
            warnings=tuple(warnings),
            validation_result=validation,
            generated_at=self.clock(),
        )
        return replace(
            seed,
            content_hash=(
                historical_pattern_library_build_result_content_hash(seed)
            ),
        )

    def build(
        self,
        patterns: Sequence[HistoricalPattern],
        *,
        library_id: str,
        source_case_hashes: Mapping[str, str],
        rejected_candidate_refs: Sequence[str] = (),
        build_metadata: Mapping[str, Any] | None = None,
        applicable_cutoff: str | datetime | None = None,
        existing_library: HistoricalPatternLibrary | None = None,
    ) -> HistoricalPatternLibraryBuildResult:
        if isinstance(patterns, (str, bytes)) or not isinstance(
            patterns,
            Sequence,
        ):
            raise TypeError("patterns must be a sequence.")
        new_patterns = tuple(patterns)
        if not all(
            isinstance(item, HistoricalPattern) for item in new_patterns
        ):
            raise TypeError(
                "patterns must contain HistoricalPattern objects."
            )
        if existing_library is not None and not isinstance(
            existing_library,
            HistoricalPatternLibrary,
        ):
            raise TypeError(
                "existing_library must be HistoricalPatternLibrary or null."
            )
        if not isinstance(source_case_hashes, Mapping):
            raise TypeError("source_case_hashes must be a mapping.")
        existing_patterns: tuple[HistoricalPattern, ...] = ()
        if existing_library is not None:
            existing_validation = validate_historical_pattern_library(
                existing_library
            )
            if (
                not existing_validation.is_valid
                or existing_library.validation_result != existing_validation
            ):
                return self._result(
                    success=False,
                    library=None,
                    accepted=(),
                    rejected=(),
                    diagnostics=(),
                    warnings=(),
                    validation=_failure_validation(
                        "invalid_existing_library",
                        "Existing pattern library is invalid or not finalized.",
                    ),
                )
            existing_patterns = existing_library.patterns

        existing_versions: dict[str, int] = defaultdict(int)
        for pattern in existing_patterns:
            existing_versions[pattern.pattern_id] = max(
                existing_versions[pattern.pattern_id],
                pattern.pattern_version,
            )
        accepted_new: list[HistoricalPattern] = []
        rejected_refs: list[str] = []
        warnings: list[str] = []
        for pattern in new_patterns:
            validation = validate_historical_pattern(pattern)
            valid = (
                validation.is_valid
                and pattern.validation_result.is_valid
                and pattern.content_hash
                == historical_pattern_content_hash(pattern)
                and pattern.pattern_status
                in {
                    PatternStatus.REVIEWED,
                    PatternStatus.VALIDATED,
                    PatternStatus.DEPRECATED,
                }
            )
            if (
                pattern.pattern_id in existing_versions
                and pattern.pattern_version
                <= existing_versions[pattern.pattern_id]
            ):
                valid = False
                warnings.append(
                    f"{pattern.pattern_ref} does not advance its existing version."
                )
            if valid:
                accepted_new.append(pattern)
            else:
                rejected_refs.append(pattern.pattern_ref)

        combined = tuple(existing_patterns) + tuple(accepted_new)
        refs = [pattern.pattern_ref for pattern in combined]
        if _duplicates(refs):
            return self._result(
                success=False,
                library=None,
                accepted=(),
                rejected=tuple(sorted(set(rejected_refs) | _duplicates(refs))),
                diagnostics=(),
                warnings=warnings,
                validation=_failure_validation(
                    "library_duplicates",
                    "Duplicate pattern ID/version pairs cannot be resolved silently.",
                ),
            )
        if not combined:
            return self._result(
                success=False,
                library=None,
                accepted=(),
                rejected=tuple(sorted(rejected_refs)),
                diagnostics=(),
                warnings=warnings,
                validation=_failure_validation(
                    "library_pattern_validity",
                    "No validated pattern was available for the library.",
                ),
            )
        all_case_ids = {
            case_id
            for pattern in combined
            for case_id in (
                pattern.supporting_case_ids
                + pattern.contradicting_case_ids
                + pattern.exception_case_ids
            )
        }
        supplied_case_hashes = dict(source_case_hashes)
        if set(supplied_case_hashes) != all_case_ids or not all(
            supplied_case_hashes.values()
        ):
            return self._result(
                success=False,
                library=None,
                accepted=(),
                rejected=tuple(sorted(rejected_refs)),
                diagnostics=(),
                warnings=warnings,
                validation=_failure_validation(
                    "library_references",
                    "Source case hashes must cover every pattern case exactly.",
                ),
            )
        analysis_hashes: dict[str, str] = {}
        for pattern in combined:
            for case_id, content_hash in pattern.source_analysis_hashes.items():
                existing = analysis_hashes.get(case_id)
                if existing is not None and existing != content_hash:
                    return self._result(
                        success=False,
                        library=None,
                        accepted=(),
                        rejected=tuple(sorted(rejected_refs)),
                        diagnostics=(),
                        warnings=warnings,
                        validation=_failure_validation(
                            "library_references",
                            "Patterns disagree about one source analysis hash.",
                        ),
                    )
                analysis_hashes[case_id] = content_hash
        cutoff = (
            _normalize_timestamp(
                applicable_cutoff,
                field_name="applicable_cutoff",
            )
            if applicable_cutoff is not None
            else max(
                combined,
                key=lambda item: _temporal_point(item.applicable_cutoff),
            ).applicable_cutoff
        )
        ordered_patterns = tuple(
            sorted(
                combined,
                key=lambda item: (item.pattern_id, item.pattern_version),
            )
        )
        deprecated_refs = tuple(
            pattern.pattern_ref
            for pattern in ordered_patterns
            if pattern.pattern_status is PatternStatus.DEPRECATED
        )
        draft = HistoricalPatternLibrary(
            library_id=_normalized_token(library_id),
            library_schema_version=HISTORICAL_PATTERN_LIBRARY_SCHEMA_VERSION,
            created_at=self.clock(),
            applicable_cutoff=cutoff,
            patterns=ordered_patterns,
            deprecated_pattern_refs=deprecated_refs,
            rejected_candidate_refs=tuple(
                sorted(set(rejected_candidate_refs))
            ),
            source_case_hashes=supplied_case_hashes,
            source_analysis_hashes=analysis_hashes,
            build_metadata=build_metadata or {},
        )
        library = finalize_historical_pattern_library(draft)
        diagnostics = analyze_pattern_overlaps(ordered_patterns)
        for item in diagnostics:
            warnings.extend(item.warnings)
        return self._result(
            success=library.validation_result.is_valid,
            library=library,
            accepted=tuple(
                pattern.pattern_ref for pattern in accepted_new
            ),
            rejected=tuple(sorted(rejected_refs)),
            diagnostics=diagnostics,
            warnings=tuple(sorted(set(warnings))),
            validation=library.validation_result,
        )


__all__ = [
    "DEFAULT_MINIMUM_PATTERN_CASES",
    "HISTORICAL_PATTERN_BUILD_SCHEMA_VERSION",
    "HISTORICAL_PATTERN_CANDIDATE_SCHEMA_VERSION",
    "HISTORICAL_PATTERN_LIBRARY_SCHEMA_VERSION",
    "HISTORICAL_PATTERN_SCHEMA_VERSION",
    "HISTORICAL_PATTERN_VALIDATION_VERSION",
    "EvidenceIndependenceStatus",
    "HistoricalPattern",
    "HistoricalPatternCandidateBuildResult",
    "HistoricalPatternCandidateBuilder",
    "HistoricalPatternLibrary",
    "HistoricalPatternLibraryBuildResult",
    "HistoricalPatternLibraryBuilder",
    "PatternCandidate",
    "PatternChange",
    "PatternConfidence",
    "PatternConsistency",
    "PatternDirection",
    "PatternOutcomeProfile",
    "PatternOverlapDiagnostic",
    "PatternOverlapRelationship",
    "PatternRecoveryProfile",
    "PatternStatus",
    "PatternSupportMetrics",
    "PatternTendency",
    "analyze_pattern_overlaps",
    "compute_pattern_support_metrics",
    "duration_bucket_for_move",
    "finalize_historical_pattern",
    "finalize_historical_pattern_library",
    "finalize_pattern_candidate",
    "historical_pattern_candidate_build_result_content_hash",
    "historical_pattern_content_hash",
    "historical_pattern_library_build_result_content_hash",
    "historical_pattern_library_content_hash",
    "magnitude_bucket_for_move",
    "pattern_candidate_content_hash",
    "pattern_direction_for_cases",
    "validate_historical_pattern",
    "validate_historical_pattern_library",
    "validate_pattern_candidate",
]
