"""Deterministic retrieval of validated historical market patterns.

This module consumes a frozen ``CurrentMarketState`` and an immutable Phase 5
``HistoricalPatternLibrary``. It performs no research, model call, narrative
generation, Elliott analysis, prediction, persistence, reporting, or trading
decision.
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
from typing import Any, Callable, Mapping, Sequence

from .current_state import (
    CurrentMarketState,
    FeatureAvailability,
    TechnicalDirection,
    current_market_state_content_hash,
    validate_current_market_state,
)
from .historical_market import (
    DurationBucket,
    HistoricalMoveType,
    MagnitudeBucket,
    MarketRegimeState,
    RelativePerformanceBucket,
    TransmissionMechanism,
)
from .historical_patterns import (
    DEFAULT_MINIMUM_PATTERN_CASES,
    EvidenceIndependenceStatus,
    HistoricalPattern,
    HistoricalPatternLibrary,
    PatternDirection,
    PatternOverlapDiagnostic,
    PatternOverlapRelationship,
    PatternStatus,
    analyze_pattern_overlaps,
    historical_pattern_content_hash,
    historical_pattern_library_content_hash,
    validate_historical_pattern,
    validate_historical_pattern_library,
)
from .market_scenario import (
    EXPOSURE_CATEGORY_COMPATIBILITY,
    ExposureType,
    ValidationIssue,
    ValidationResult,
)


PATTERN_RETRIEVAL_QUERY_SCHEMA_VERSION = (
    "pattern-retrieval-query-1.0.0"
)
RETRIEVED_PATTERN_MATCH_SCHEMA_VERSION = (
    "retrieved-pattern-match-1.0.0"
)
PATTERN_RETRIEVAL_RESULT_SCHEMA_VERSION = (
    "pattern-retrieval-result-1.0.0"
)
PATTERN_RETRIEVAL_EXECUTION_RESULT_SCHEMA_VERSION = (
    "pattern-retrieval-execution-result-1.0.0"
)
PATTERN_RETRIEVAL_VALIDATION_VERSION = (
    "pattern-retrieval-validation-1.0.0"
)
PATTERN_RETRIEVAL_SCORING_PROFILE_VERSION = (
    "pattern-retrieval-scoring-profile-1.0.0"
)
PATTERN_RETRIEVAL_ENGINE_VERSION = "pattern-retrieval-engine-1.0.0"

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

_COMPONENT_NAMES = (
    "exposure",
    "transmission",
    "regime",
    "event",
    "technical",
    "outcome",
)


class MatchQuality(StrEnum):
    VERY_STRONG = "very_strong"
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    VERY_WEAK = "very_weak"
    INSUFFICIENT_DATA = "insufficient_data"


class MatchRecommendation(StrEnum):
    INCLUDE = "include"
    INCLUDE_WITH_WARNING = "include_with_warning"
    EXCLUDE = "exclude"
    INSUFFICIENT_DATA = "insufficient_data"


class RetrievalLane(StrEnum):
    PRIMARY = "primary"
    COUNTER = "counter"


class MatchExplanationCode(StrEnum):
    REQUIRED_EXPOSURES_MATCH = "required_exposures_match"
    OPTIONAL_EXPOSURES_MATCH = "optional_exposures_match"
    TRANSMISSION_MATCH = "transmission_match"
    REGIME_MATCH = "regime_match"
    EVENT_MATCH = "event_match"
    TECHNICAL_DIRECTION_MATCH = "technical_direction_match"
    TECHNICAL_STRUCTURE_MATCH = "technical_structure_match"
    OUTCOME_PROFILE_MATCH = "outcome_profile_match"
    BENCHMARK_RELATIVE_MATCH = "benchmark_relative_match"
    SECTOR_RELATIVE_MATCH = "sector_relative_match"
    REQUIRED_EXPOSURE_MISSING = "required_exposure_missing"
    INCOMPATIBLE_REGIME = "incompatible_regime"
    CONTRADICTORY_EXPOSURE = "contradictory_exposure"
    DIRECTION_MISMATCH = "direction_mismatch"
    TECHNICAL_STRUCTURE_MISMATCH = "technical_structure_mismatch"
    EVENT_MISMATCH = "event_mismatch"
    TRANSMISSION_MISMATCH = "transmission_mismatch"
    INSUFFICIENT_CURRENT_DATA = "insufficient_current_data"
    CONCENTRATED_PATTERN_SUPPORT = "concentrated_pattern_support"
    WEAK_SOURCE_INDEPENDENCE = "weak_source_independence"
    PATTERN_LIMITATION_APPLIES = "pattern_limitation_applies"
    CUTOFF_INCOMPATIBILITY = "cutoff_incompatibility"
    OPTIONAL_REGIME_MATCH = "optional_regime_match"
    TECHNICAL_MOVE_TYPE_MATCH = "technical_move_type_match"
    MAGNITUDE_MATCH = "magnitude_match"
    DURATION_MATCH = "duration_match"
    COUNTER_PATTERN = "counter_pattern"
    CONTEXT_CONDITION_MATCH = "context_condition_match"
    BIDIRECTIONAL_PATTERN = "bidirectional_pattern"


class PatternExclusionCode(StrEnum):
    STATUS_NOT_ALLOWED = "status_not_allowed"
    REJECTED_PATTERN = "rejected_pattern"
    DEPRECATED_PATTERN = "deprecated_pattern"
    INVALID_PATTERN_HASH = "invalid_pattern_hash"
    INVALID_PATTERN = "invalid_pattern"
    INSUFFICIENT_PATTERN_SUPPORT = "insufficient_pattern_support"
    INVALID_SOURCE_REFERENCES = "invalid_source_references"
    CUTOFF_INCOMPATIBILITY = "cutoff_incompatibility"
    CONTEXT_CONDITION_NOT_MET = "context_condition_not_met"
    BIDIRECTIONAL_NOT_REQUESTED = "bidirectional_not_requested"
    BELOW_MINIMUM_QUALITY = "below_minimum_quality"
    OVERLAP_NEAR_DUPLICATE_SUPPRESSED = (
        "overlap_near_duplicate_suppressed"
    )
    RESULT_LIMIT = "result_limit"
    NO_MEANINGFUL_COUNTER_INPUT_OVERLAP = (
        "no_meaningful_counter_input_overlap"
    )
    INSUFFICIENT_CURRENT_DATA = "insufficient_current_data"


_QUALITY_ORDER = MappingProxyType(
    {
        MatchQuality.INSUFFICIENT_DATA: 0,
        MatchQuality.VERY_WEAK: 1,
        MatchQuality.WEAK: 2,
        MatchQuality.MODERATE: 3,
        MatchQuality.STRONG: 4,
        MatchQuality.VERY_STRONG: 5,
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


def _temporal_point(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
        _enum_value(item, enum_type, field_name=field_name)
        for item in value
    )


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


def _ordered_strings(value: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(value)))


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


def _normalized_token(value: str) -> str:
    return re.sub(
        r"_+",
        "_",
        re.sub(r"[^a-z0-9]+", "_", value.casefold()),
    ).strip("_")


def _duplicates(values: Sequence[Any]) -> set[Any]:
    return {
        value
        for value, count in Counter(values).items()
        if count > 1
    }


def _float_mapping(
    value: Mapping[str, float],
    *,
    field_name: str,
) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    result: dict[str, float] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key
            or isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            raise ValueError(
                f"{field_name} requires string keys and finite numbers."
            )
        result[key] = float(item)
    return MappingProxyType(dict(sorted(result.items())))


def _reason_mapping(
    value: Mapping[str, Sequence[Any]],
) -> Mapping[str, tuple[PatternExclusionCode, ...]]:
    if not isinstance(value, Mapping):
        raise TypeError("exclusion_reasons must be a mapping.")
    result: dict[str, tuple[PatternExclusionCode, ...]] = {}
    for key, reasons in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(
                "exclusion_reasons requires non-empty string keys."
            )
        result[key] = tuple(
            sorted(
                set(
                    _enum_tuple(
                        reasons,
                        PatternExclusionCode,
                        field_name="exclusion_reasons",
                    )
                ),
                key=lambda item: item.value,
            )
        )
    return MappingProxyType(dict(sorted(result.items())))


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class PatternRetrievalScoringProfile(_JsonContract):
    profile_id: str
    profile_version: str
    component_weights: Mapping[str, float]
    score_thresholds: Mapping[str, float]
    completeness_thresholds: Mapping[str, float]
    penalties: Mapping[str, float]
    support_adjustments: Mapping[str, float]
    minimum_supporting_cases: int
    maximum_results: int
    maximum_counter_results: int
    maximum_near_duplicates_per_cluster: int
    content_hash: str = ""
    schema_version: str = PATTERN_RETRIEVAL_SCORING_PROFILE_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "component_weights",
            "score_thresholds",
            "completeness_thresholds",
            "penalties",
            "support_adjustments",
        ):
            object.__setattr__(
                self,
                field_name,
                _float_mapping(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        for field_name, minimum in (
            ("minimum_supporting_cases", 2),
            ("maximum_results", 1),
            ("maximum_counter_results", 0),
            ("maximum_near_duplicates_per_cluster", 1),
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
            ):
                raise ValueError(
                    f"{field_name} must be an integer of at least {minimum}."
                )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "PatternRetrievalScoringProfile":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


def _default_scoring_profile() -> PatternRetrievalScoringProfile:
    seed = PatternRetrievalScoringProfile(
        profile_id="phase6_lexical_pattern_retrieval",
        profile_version=PATTERN_RETRIEVAL_SCORING_PROFILE_VERSION,
        component_weights={
            "exposure": 30.0,
            "transmission": 20.0,
            "regime": 15.0,
            "event": 10.0,
            "technical": 15.0,
            "outcome": 10.0,
        },
        score_thresholds={
            MatchQuality.VERY_STRONG.value: 85.0,
            MatchQuality.STRONG.value: 70.0,
            MatchQuality.MODERATE.value: 55.0,
            MatchQuality.WEAK.value: 40.0,
            MatchQuality.VERY_WEAK.value: 0.0,
        },
        completeness_thresholds={
            MatchQuality.VERY_STRONG.value: 0.85,
            MatchQuality.STRONG.value: 0.70,
            MatchQuality.MODERATE.value: 0.50,
            MatchQuality.WEAK.value: 0.35,
            MatchQuality.VERY_WEAK.value: 0.35,
            MatchQuality.INSUFFICIENT_DATA.value: 0.35,
        },
        penalties={
            "missing_required": 6.0,
            "unavailable_required": 3.0,
            "incompatible_regime": 12.0,
            "contradictory_feature": 7.0,
            "one_symbol_concentration": 4.0,
            "one_sector_concentration": 3.0,
            "correlated_sources": 5.0,
            "mixed_sources": 1.0,
            "unknown_sources": 2.0,
            "insufficient_sources": 3.0,
            "high_missing_share_maximum": 10.0,
            "applicable_limitation": 4.0,
            "maximum_very_strong_penalties": 3.0,
        },
        support_adjustments={
            "independent_diverse": 2.0,
            "independent": 1.0,
            "mixed": 0.5,
            "correlated": 0.0,
            "unknown": 0.0,
            "insufficient_evidence": 0.0,
        },
        minimum_supporting_cases=DEFAULT_MINIMUM_PATTERN_CASES,
        maximum_results=50,
        maximum_counter_results=20,
        maximum_near_duplicates_per_cluster=1,
    )
    return replace(seed, content_hash=_contract_hash(seed))


DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE = _default_scoring_profile()


def validate_pattern_retrieval_scoring_profile(
    profile: PatternRetrievalScoringProfile,
) -> ValidationResult:
    if not isinstance(profile, PatternRetrievalScoringProfile):
        raise TypeError(
            "profile must be PatternRetrievalScoringProfile."
        )
    errors: list[ValidationIssue] = []

    def require(code: str, condition: bool, path: str, message: str) -> None:
        if not condition:
            errors.append(
                ValidationIssue(
                    code=code,
                    severity="error",
                    path=path,
                    message=message,
                )
            )

    require(
        "scoring_profile_schema",
        profile.schema_version
        == PATTERN_RETRIEVAL_SCORING_PROFILE_VERSION,
        "schema_version",
        "Scoring-profile schema version is unsupported.",
    )
    require(
        "scoring_profile_version",
        profile.profile_version
        == PATTERN_RETRIEVAL_SCORING_PROFILE_VERSION,
        "profile_version",
        "Scoring-profile version is unsupported.",
    )
    require(
        "scoring_profile_hash",
        bool(profile.content_hash)
        and profile.content_hash == _contract_hash(profile),
        "content_hash",
        "Scoring-profile content hash is missing or mismatched.",
    )
    require(
        "scoring_profile_weights",
        set(profile.component_weights) == set(_COMPONENT_NAMES)
        and math.isclose(
            sum(profile.component_weights.values()),
            100.0,
            abs_tol=1e-9,
        )
        and all(
            0.0 <= value <= 100.0
            for value in profile.component_weights.values()
        ),
        "component_weights",
        "Component weights must cover all six components and sum to 100.",
    )
    quality_keys = {
        item.value
        for item in MatchQuality
        if item is not MatchQuality.INSUFFICIENT_DATA
    }
    require(
        "scoring_profile_thresholds",
        set(profile.score_thresholds) == quality_keys
        and set(profile.completeness_thresholds)
        == {item.value for item in MatchQuality}
        and all(
            0.0 <= value <= 100.0
            for value in profile.score_thresholds.values()
        )
        and all(
            0.0 <= value <= 1.0
            for value in profile.completeness_thresholds.values()
        ),
        "score_thresholds",
        "Quality and completeness thresholds are incomplete or invalid.",
    )
    require(
        "scoring_profile_penalties",
        all(value >= 0.0 for value in profile.penalties.values())
        and all(
            value >= 0.0
            for value in profile.support_adjustments.values()
        ),
        "penalties",
        "Penalties and support adjustments must be non-negative.",
    )
    rule_order = (
        "scoring_profile_schema",
        "scoring_profile_version",
        "scoring_profile_hash",
        "scoring_profile_weights",
        "scoring_profile_thresholds",
        "scoring_profile_penalties",
    )
    failed = tuple(
        code for code in rule_order if any(item.code == code for item in errors)
    )
    passed = tuple(code for code in rule_order if code not in failed)
    return ValidationResult(
        is_valid=not errors,
        score=round(100.0 * len(passed) / len(rule_order), 2),
        errors=tuple(errors),
        failed_rules=failed,
        passed_rules=passed,
        validation_version=PATTERN_RETRIEVAL_VALIDATION_VERSION,
    )


@dataclass(frozen=True, slots=True)
class PatternRetrievalQuery(_JsonContract):
    query_id: str
    current_state_hash: str
    library_hash: str
    requested_limit: int
    minimum_match_quality: MatchQuality
    include_context_dependent_patterns: bool
    include_bidirectional_patterns: bool
    allowed_pattern_statuses: tuple[PatternStatus, ...]
    scoring_profile_version: str
    generated_at: str
    requested_counter_limit: int = 3
    content_hash: str = ""
    schema_version: str = PATTERN_RETRIEVAL_QUERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_match_quality",
            _enum_value(
                self.minimum_match_quality,
                MatchQuality,
                field_name="minimum_match_quality",
            ),
        )
        object.__setattr__(
            self,
            "allowed_pattern_statuses",
            tuple(
                sorted(
                    set(
                        _enum_tuple(
                            self.allowed_pattern_statuses,
                            PatternStatus,
                            field_name="allowed_pattern_statuses",
                        )
                    ),
                    key=lambda item: item.value,
                )
            ),
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
    ) -> "PatternRetrievalQuery":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class RetrievedPatternMatch(_JsonContract):
    pattern_id: str
    pattern_version: int
    pattern_hash: str
    lane: RetrievalLane
    rank: int
    total_score: float
    match_quality: MatchQuality
    recommendation: MatchRecommendation
    required_feature_tokens: tuple[str, ...]
    optional_feature_tokens: tuple[str, ...]
    matched_required_features: tuple[str, ...]
    missing_required_features: tuple[str, ...]
    contradicted_required_features: tuple[str, ...]
    matched_optional_features: tuple[str, ...]
    incompatible_features: tuple[str, ...]
    contradictory_current_features: tuple[str, ...]
    exposure_match_score: float
    transmission_match_score: float
    regime_match_score: float
    event_match_score: float
    technical_match_score: float
    outcome_match_score: float
    concentration_penalty: float
    support_quality_adjustment: float
    missing_data_penalty: float
    incompatibility_penalty: float
    contradiction_penalty: float
    limitation_penalty: float
    information_completeness: float
    explanation_codes: tuple[MatchExplanationCode, ...]
    warnings: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = RETRIEVED_PATTERN_MATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lane",
            _enum_value(self.lane, RetrievalLane, field_name="lane"),
        )
        object.__setattr__(
            self,
            "match_quality",
            _enum_value(
                self.match_quality,
                MatchQuality,
                field_name="match_quality",
            ),
        )
        object.__setattr__(
            self,
            "recommendation",
            _enum_value(
                self.recommendation,
                MatchRecommendation,
                field_name="recommendation",
            ),
        )
        object.__setattr__(
            self,
            "explanation_codes",
            _enum_tuple(
                self.explanation_codes,
                MatchExplanationCode,
                field_name="explanation_codes",
            ),
        )
        for field_name in (
            "required_feature_tokens",
            "optional_feature_tokens",
            "matched_required_features",
            "missing_required_features",
            "contradicted_required_features",
            "matched_optional_features",
            "incompatible_features",
            "contradictory_current_features",
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
        for field_name in (
            "total_score",
            "exposure_match_score",
            "transmission_match_score",
            "regime_match_score",
            "event_match_score",
            "technical_match_score",
            "outcome_match_score",
            "concentration_penalty",
            "support_quality_adjustment",
            "missing_data_penalty",
            "incompatibility_penalty",
            "contradiction_penalty",
            "limitation_penalty",
            "information_completeness",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{field_name} must be finite.")
            object.__setattr__(self, field_name, float(value))

    @property
    def pattern_ref(self) -> str:
        return f"{self.pattern_id}@{self.pattern_version}"

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "RetrievedPatternMatch":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class PatternRetrievalResult(_JsonContract):
    success: bool
    query: PatternRetrievalQuery
    matches: tuple[RetrievedPatternMatch, ...]
    counter_patterns: tuple[RetrievedPatternMatch, ...]
    excluded_pattern_refs: tuple[str, ...]
    insufficient_data_pattern_refs: tuple[str, ...]
    suppressed_pattern_refs: tuple[str, ...]
    exclusion_reasons: Mapping[
        str, tuple[PatternExclusionCode, ...]
    ]
    overlap_diagnostics: tuple[PatternOverlapDiagnostic, ...]
    scoring_profile_version: str
    scoring_profile_hash: str
    validation_result: ValidationResult
    warnings: tuple[str, ...]
    generated_at: str
    content_hash: str = ""
    schema_version: str = PATTERN_RETRIEVAL_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.query, PatternRetrievalQuery):
            raise TypeError("query must be PatternRetrievalQuery.")
        for field_name in ("matches", "counter_patterns"):
            object.__setattr__(
                self,
                field_name,
                _model_tuple(
                    getattr(self, field_name),
                    RetrievedPatternMatch,
                    field_name=field_name,
                ),
            )
        for field_name in (
            "excluded_pattern_refs",
            "insufficient_data_pattern_refs",
            "suppressed_pattern_refs",
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
            "exclusion_reasons",
            _reason_mapping(self.exclusion_reasons),
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
    ) -> "PatternRetrievalResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["query"] = PatternRetrievalQuery.from_dict(raw["query"])
        for field_name in ("matches", "counter_patterns"):
            raw[field_name] = tuple(
                RetrievedPatternMatch.from_dict(item)
                for item in raw.get(field_name, ())
            )
        raw["overlap_diagnostics"] = tuple(
            PatternOverlapDiagnostic.from_dict(item)
            for item in raw.get("overlap_diagnostics", ())
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class PatternRetrievalExecutionResult(_JsonContract):
    success: bool
    retrieval_result: PatternRetrievalResult | None
    validation_result: ValidationResult
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    state_hash: str
    library_hash: str
    scoring_profile_version: str
    generated_at: str
    content_hash: str = ""
    schema_version: str = (
        PATTERN_RETRIEVAL_EXECUTION_RESULT_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        if self.retrieval_result is not None and not isinstance(
            self.retrieval_result,
            PatternRetrievalResult,
        ):
            raise TypeError(
                "retrieval_result must be PatternRetrievalResult or null."
            )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")
        for field_name in ("errors", "warnings"):
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
    ) -> "PatternRetrievalExecutionResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("retrieval_result") is not None:
            raw["retrieval_result"] = PatternRetrievalResult.from_dict(
                raw["retrieval_result"]
            )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


def pattern_retrieval_query_content_hash(
    query: PatternRetrievalQuery,
) -> str:
    if not isinstance(query, PatternRetrievalQuery):
        raise TypeError("query must be PatternRetrievalQuery.")
    return _contract_hash(query)


def retrieved_pattern_match_content_hash(
    match: RetrievedPatternMatch,
) -> str:
    if not isinstance(match, RetrievedPatternMatch):
        raise TypeError("match must be RetrievedPatternMatch.")
    return _contract_hash(match)


def pattern_retrieval_result_content_hash(
    result: PatternRetrievalResult,
) -> str:
    if not isinstance(result, PatternRetrievalResult):
        raise TypeError("result must be PatternRetrievalResult.")
    return _contract_hash(result)


def pattern_retrieval_execution_result_content_hash(
    result: PatternRetrievalExecutionResult,
) -> str:
    if not isinstance(result, PatternRetrievalExecutionResult):
        raise TypeError(
            "result must be PatternRetrievalExecutionResult."
        )
    return _contract_hash(result)


_QUERY_RULE_ORDER = (
    "query_schema",
    "query_hash",
    "query_identity",
    "query_limits",
    "query_quality",
    "query_statuses",
    "query_scoring_profile",
    "query_state_hash",
    "query_library_hash",
)

_MATCH_RULE_ORDER = (
    "match_schema",
    "match_hash",
    "match_identity",
    "match_rank",
    "match_scores",
    "match_score_reconciliation",
    "match_quality",
    "match_recommendation",
    "match_explanations",
    "match_required_accounting",
    "match_feature_disjointness",
    "match_pattern_reference",
)

_RESULT_RULE_ORDER = (
    "result_schema",
    "result_hash",
    "result_query",
    "result_matches",
    "result_ranks",
    "result_ordering",
    "result_uniqueness",
    "result_lane_separation",
    "result_limits",
    "result_reference_accounting",
    "result_overlap_diagnostics",
    "result_profile",
)


class _ValidationCollector:
    def __init__(self, rule_order: Sequence[str]) -> None:
        self.rule_order = tuple(rule_order)
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []
        self.failed: set[str] = set()

    def require(
        self,
        rule: str,
        condition: bool,
        path: str,
        message: str,
    ) -> None:
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
        failed = tuple(
            rule for rule in self.rule_order if rule in self.failed
        )
        passed = tuple(
            rule for rule in self.rule_order if rule not in self.failed
        )
        return ValidationResult(
            is_valid=not self.errors,
            score=round(
                100.0 * len(passed) / len(self.rule_order),
                2,
            ),
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            failed_rules=failed,
            passed_rules=passed,
            validation_version=PATTERN_RETRIEVAL_VALIDATION_VERSION,
        )


def _failure_validation(
    code: str,
    path: str,
    message: str,
) -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        score=0.0,
        errors=(
            ValidationIssue(
                code=code,
                severity="error",
                path=path,
                message=message,
            ),
        ),
        failed_rules=(code,),
        validation_version=PATTERN_RETRIEVAL_VALIDATION_VERSION,
    )


def create_pattern_retrieval_query(
    state: CurrentMarketState,
    library: HistoricalPatternLibrary,
    *,
    requested_limit: int = 10,
    requested_counter_limit: int = 3,
    minimum_match_quality: MatchQuality = MatchQuality.VERY_WEAK,
    include_context_dependent_patterns: bool = False,
    include_bidirectional_patterns: bool = False,
    allowed_pattern_statuses: Sequence[PatternStatus] = (
        PatternStatus.REVIEWED,
        PatternStatus.VALIDATED,
    ),
    scoring_profile: PatternRetrievalScoringProfile = (
        DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
    ),
    generated_at: str | datetime | None = None,
    query_id: str | None = None,
) -> PatternRetrievalQuery:
    if not isinstance(state, CurrentMarketState):
        raise TypeError("state must be CurrentMarketState.")
    if not isinstance(library, HistoricalPatternLibrary):
        raise TypeError("library must be HistoricalPatternLibrary.")
    if not isinstance(scoring_profile, PatternRetrievalScoringProfile):
        raise TypeError(
            "scoring_profile must be PatternRetrievalScoringProfile."
        )
    timestamp = _normalize_timestamp(
        generated_at or datetime.now(timezone.utc),
        field_name="generated_at",
    )
    selected_id = (
        _normalized_token(query_id)
        if query_id is not None
        else "query_"
        + _hash_payload(
            {
                "state_hash": state.content_hash,
                "library_hash": library.content_hash,
                "generated_at": timestamp,
            }
        )[:20]
    )
    seed = PatternRetrievalQuery(
        query_id=selected_id,
        current_state_hash=state.content_hash,
        library_hash=library.content_hash,
        requested_limit=requested_limit,
        requested_counter_limit=requested_counter_limit,
        minimum_match_quality=minimum_match_quality,
        include_context_dependent_patterns=(
            include_context_dependent_patterns
        ),
        include_bidirectional_patterns=include_bidirectional_patterns,
        allowed_pattern_statuses=tuple(allowed_pattern_statuses),
        scoring_profile_version=scoring_profile.profile_version,
        generated_at=timestamp,
    )
    return replace(
        seed,
        content_hash=pattern_retrieval_query_content_hash(seed),
    )


def validate_pattern_retrieval_query(
    query: PatternRetrievalQuery,
    *,
    state: CurrentMarketState | None = None,
    library: HistoricalPatternLibrary | None = None,
    scoring_profile: PatternRetrievalScoringProfile = (
        DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
    ),
) -> ValidationResult:
    if not isinstance(query, PatternRetrievalQuery):
        raise TypeError("query must be PatternRetrievalQuery.")
    if state is not None and not isinstance(state, CurrentMarketState):
        raise TypeError("state must be CurrentMarketState or null.")
    if library is not None and not isinstance(
        library,
        HistoricalPatternLibrary,
    ):
        raise TypeError(
            "library must be HistoricalPatternLibrary or null."
        )
    result = _ValidationCollector(_QUERY_RULE_ORDER)
    result.require(
        "query_schema",
        query.schema_version == PATTERN_RETRIEVAL_QUERY_SCHEMA_VERSION,
        "schema_version",
        "Pattern-retrieval query schema version is unsupported.",
    )
    result.require(
        "query_hash",
        bool(query.content_hash)
        and query.content_hash == pattern_retrieval_query_content_hash(query),
        "content_hash",
        "Pattern-retrieval query hash is missing or mismatched.",
    )
    result.require(
        "query_identity",
        bool(query.query_id)
        and query.query_id == _normalized_token(query.query_id)
        and bool(query.current_state_hash)
        and bool(query.library_hash),
        "query_id",
        "Query identity and immutable input hashes are required.",
    )
    result.require(
        "query_limits",
        isinstance(query.requested_limit, int)
        and not isinstance(query.requested_limit, bool)
        and 1
        <= query.requested_limit
        <= scoring_profile.maximum_results
        and isinstance(query.requested_counter_limit, int)
        and not isinstance(query.requested_counter_limit, bool)
        and 0
        <= query.requested_counter_limit
        <= scoring_profile.maximum_counter_results,
        "requested_limit",
        "Requested primary or counter limit is outside the profile bounds.",
    )
    result.require(
        "query_quality",
        query.minimum_match_quality is not MatchQuality.INSUFFICIENT_DATA,
        "minimum_match_quality",
        "Insufficient-data is not a selectable minimum match quality.",
    )
    supported_statuses = {
        PatternStatus.REVIEWED,
        PatternStatus.VALIDATED,
        PatternStatus.DEPRECATED,
    }
    result.require(
        "query_statuses",
        bool(query.allowed_pattern_statuses)
        and set(query.allowed_pattern_statuses) <= supported_statuses,
        "allowed_pattern_statuses",
        "Only reviewed, validated, and explicitly requested deprecated statuses are supported.",
    )
    profile_validation = validate_pattern_retrieval_scoring_profile(
        scoring_profile
    )
    result.require(
        "query_scoring_profile",
        profile_validation.is_valid
        and query.scoring_profile_version
        == scoring_profile.profile_version,
        "scoring_profile_version",
        "Query scoring profile is unavailable, invalid, or mismatched.",
    )
    result.require(
        "query_state_hash",
        state is None
        or (
            query.current_state_hash == state.content_hash
            and state.content_hash
            == current_market_state_content_hash(state)
        ),
        "current_state_hash",
        "Query current-state hash does not match the supplied state.",
    )
    result.require(
        "query_library_hash",
        library is None
        or (
            query.library_hash == library.content_hash
            and library.content_hash
            == historical_pattern_library_content_hash(library)
        ),
        "library_hash",
        "Query library hash does not match the supplied library.",
    )
    return result.result()


def _score_total(
    match: RetrievedPatternMatch,
) -> float:
    positive = (
        match.exposure_match_score
        + match.transmission_match_score
        + match.regime_match_score
        + match.event_match_score
        + match.technical_match_score
        + match.outcome_match_score
        + match.support_quality_adjustment
    )
    negative = (
        match.concentration_penalty
        + match.missing_data_penalty
        + match.incompatibility_penalty
        + match.contradiction_penalty
        + match.limitation_penalty
    )
    return round(min(100.0, max(0.0, positive - negative)), 4)


def validate_retrieved_pattern_match(
    match: RetrievedPatternMatch,
    *,
    pattern: HistoricalPattern | None = None,
    scoring_profile: PatternRetrievalScoringProfile = (
        DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
    ),
) -> ValidationResult:
    if not isinstance(match, RetrievedPatternMatch):
        raise TypeError("match must be RetrievedPatternMatch.")
    if pattern is not None and not isinstance(pattern, HistoricalPattern):
        raise TypeError("pattern must be HistoricalPattern or null.")
    result = _ValidationCollector(_MATCH_RULE_ORDER)
    result.require(
        "match_schema",
        match.schema_version == RETRIEVED_PATTERN_MATCH_SCHEMA_VERSION,
        "schema_version",
        "Retrieved-match schema version is unsupported.",
    )
    result.require(
        "match_hash",
        bool(match.content_hash)
        and match.content_hash == retrieved_pattern_match_content_hash(match),
        "content_hash",
        "Retrieved-match hash is missing or mismatched.",
    )
    result.require(
        "match_identity",
        bool(match.pattern_id)
        and match.pattern_id == _normalized_token(match.pattern_id)
        and match.pattern_version >= 1
        and bool(match.pattern_hash),
        "pattern_id",
        "Retrieved-match pattern identity is invalid.",
    )
    result.require(
        "match_rank",
        isinstance(match.rank, int)
        and not isinstance(match.rank, bool)
        and match.rank >= 1,
        "rank",
        "Retrieved-match rank must be a positive integer.",
    )
    component_values = {
        "exposure": match.exposure_match_score,
        "transmission": match.transmission_match_score,
        "regime": match.regime_match_score,
        "event": match.event_match_score,
        "technical": match.technical_match_score,
        "outcome": match.outcome_match_score,
    }
    result.require(
        "match_scores",
        0.0 <= match.total_score <= 100.0
        and 0.0 <= match.information_completeness <= 1.0
        and all(
            0.0 <= value <= scoring_profile.component_weights[name]
            for name, value in component_values.items()
        )
        and all(
            value >= 0.0
            for value in (
                match.concentration_penalty,
                match.support_quality_adjustment,
                match.missing_data_penalty,
                match.incompatibility_penalty,
                match.contradiction_penalty,
                match.limitation_penalty,
            )
        ),
        "total_score",
        "Match scores, penalties, adjustment, or completeness are invalid.",
    )
    result.require(
        "match_score_reconciliation",
        math.isclose(
            match.total_score,
            _score_total(match),
            abs_tol=1e-4,
        ),
        "total_score",
        "Total score does not reconcile with component scores and penalties.",
    )
    result.require(
        "match_quality",
        _quality_for_score(
            match.total_score,
            match.information_completeness,
            match,
            scoring_profile,
        )
        is match.match_quality,
        "match_quality",
        "Match quality is inconsistent with score and completeness.",
    )
    expected_recommendation = _recommendation_for_match(match)
    result.require(
        "match_recommendation",
        match.recommendation is expected_recommendation,
        "recommendation",
        "Match recommendation is inconsistent with match evidence.",
    )
    result.require(
        "match_explanations",
        not _duplicates(match.explanation_codes)
        and not _duplicates(match.warnings),
        "explanation_codes",
        "Explanation and warning codes must be unique.",
    )
    required = set(match.required_feature_tokens)
    accounted = (
        set(match.matched_required_features)
        | set(match.missing_required_features)
        | set(match.contradicted_required_features)
    )
    result.require(
        "match_required_accounting",
        required == accounted
        and set(match.contradicted_required_features)
        <= set(match.contradictory_current_features)
        and not any(
            _duplicates(values)
            for values in (
                match.required_feature_tokens,
                match.optional_feature_tokens,
                match.matched_required_features,
                match.missing_required_features,
                match.contradicted_required_features,
                match.matched_optional_features,
            )
        ),
        "required_feature_tokens",
        "Required feature states are incomplete, duplicated, or inconsistent.",
    )
    matched = set(match.matched_required_features) | set(
        match.matched_optional_features
    )
    negative = set(match.incompatible_features) | set(
        match.contradictory_current_features
    )
    result.require(
        "match_feature_disjointness",
        not (matched & negative)
        and not (
            set(match.required_feature_tokens)
            & set(match.optional_feature_tokens)
        ),
        "matched_required_features",
        "Matched, incompatible, contradictory, required, and optional features overlap.",
    )
    result.require(
        "match_pattern_reference",
        pattern is None
        or (
            match.pattern_id == pattern.pattern_id
            and match.pattern_version == pattern.pattern_version
            and match.pattern_hash == pattern.content_hash
            and pattern.content_hash
            == historical_pattern_content_hash(pattern)
        ),
        "pattern_hash",
        "Retrieved match does not reference the supplied immutable pattern.",
    )
    return result.result()


def _match_order_key(
    match: RetrievedPatternMatch,
) -> tuple[Any, ...]:
    return (
        -round(match.total_score, 4),
        -_QUALITY_ORDER[match.match_quality],
        -round(match.information_completeness, 6),
        match.pattern_id,
        -match.pattern_version,
    )


def validate_pattern_retrieval_result(
    retrieval: PatternRetrievalResult,
    *,
    state: CurrentMarketState | None = None,
    library: HistoricalPatternLibrary | None = None,
    scoring_profile: PatternRetrievalScoringProfile = (
        DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
    ),
) -> ValidationResult:
    if not isinstance(retrieval, PatternRetrievalResult):
        raise TypeError("retrieval must be PatternRetrievalResult.")
    if state is not None and not isinstance(state, CurrentMarketState):
        raise TypeError("state must be CurrentMarketState or null.")
    if library is not None and not isinstance(
        library,
        HistoricalPatternLibrary,
    ):
        raise TypeError(
            "library must be HistoricalPatternLibrary or null."
        )
    result = _ValidationCollector(_RESULT_RULE_ORDER)
    result.require(
        "result_schema",
        retrieval.schema_version == PATTERN_RETRIEVAL_RESULT_SCHEMA_VERSION,
        "schema_version",
        "Pattern-retrieval result schema version is unsupported.",
    )
    result.require(
        "result_hash",
        bool(retrieval.content_hash)
        and retrieval.content_hash
        == pattern_retrieval_result_content_hash(retrieval),
        "content_hash",
        "Pattern-retrieval result hash is missing or mismatched.",
    )
    query_validation = validate_pattern_retrieval_query(
        retrieval.query,
        state=state,
        library=library,
        scoring_profile=scoring_profile,
    )
    result.require(
        "result_query",
        query_validation.is_valid,
        "query",
        "Retrieval query is invalid for the supplied immutable inputs.",
    )
    pattern_map = (
        {pattern.pattern_ref: pattern for pattern in library.patterns}
        if library is not None
        else {}
    )
    all_matches = retrieval.matches + retrieval.counter_patterns
    match_valid = all(
        validate_retrieved_pattern_match(
            item,
            pattern=pattern_map.get(item.pattern_ref),
            scoring_profile=scoring_profile,
        ).is_valid
        for item in all_matches
    )
    result.require(
        "result_matches",
        match_valid
        and all(
            item.lane is RetrievalLane.PRIMARY
            for item in retrieval.matches
        )
        and all(
            item.lane is RetrievalLane.COUNTER
            for item in retrieval.counter_patterns
        ),
        "matches",
        "A retrieved match is invalid or assigned to the wrong lane.",
    )
    result.require(
        "result_ranks",
        tuple(item.rank for item in retrieval.matches)
        == tuple(range(1, len(retrieval.matches) + 1))
        and tuple(item.rank for item in retrieval.counter_patterns)
        == tuple(range(1, len(retrieval.counter_patterns) + 1)),
        "matches",
        "Primary and counter ranks must each be contiguous from one.",
    )
    result.require(
        "result_ordering",
        retrieval.matches
        == tuple(sorted(retrieval.matches, key=_match_order_key))
        and retrieval.counter_patterns
        == tuple(
            sorted(retrieval.counter_patterns, key=_match_order_key)
        ),
        "matches",
        "Retrieved matches are not in deterministic score order.",
    )
    primary_refs = tuple(item.pattern_ref for item in retrieval.matches)
    counter_refs = tuple(
        item.pattern_ref for item in retrieval.counter_patterns
    )
    result.require(
        "result_uniqueness",
        not _duplicates(primary_refs)
        and not _duplicates(counter_refs)
        and not _duplicates(retrieval.excluded_pattern_refs)
        and not _duplicates(
            retrieval.insufficient_data_pattern_refs
        )
        and not _duplicates(retrieval.suppressed_pattern_refs),
        "matches",
        "Retrieval result contains duplicate pattern references.",
    )
    result.require(
        "result_lane_separation",
        not (set(primary_refs) & set(counter_refs)),
        "counter_patterns",
        "A pattern cannot appear in both retrieval lanes.",
    )
    result.require(
        "result_limits",
        len(retrieval.matches) <= retrieval.query.requested_limit
        and len(retrieval.counter_patterns)
        <= retrieval.query.requested_counter_limit,
        "matches",
        "Retrieval result exceeds the requested lane limits.",
    )
    included_refs = set(primary_refs) | set(counter_refs)
    excluded_refs = set(retrieval.excluded_pattern_refs)
    insufficient_refs = set(
        retrieval.insufficient_data_pattern_refs
    )
    library_refs = set(pattern_map)
    included_patterns_eligible = True
    complete_library_accounting = True
    if library is not None:
        included_patterns_eligible = all(
            pattern_ref in pattern_map
            and pattern_map[pattern_ref].pattern_status
            in set(retrieval.query.allowed_pattern_statuses)
            and pattern_map[pattern_ref].content_hash
            == historical_pattern_content_hash(
                pattern_map[pattern_ref]
            )
            and pattern_map[pattern_ref].validation_result.is_valid
            and (
                state is None
                or _temporal_point(
                    pattern_map[pattern_ref].applicable_cutoff
                )
                <= _temporal_point(state.applicable_cutoff)
            )
            for pattern_ref in included_refs
        )
        complete_library_accounting = (
            included_refs | excluded_refs | insufficient_refs
            == library_refs
        )
    result.require(
        "result_reference_accounting",
        not (included_refs & excluded_refs)
        and not (included_refs & insufficient_refs)
        and not (excluded_refs & insufficient_refs)
        and set(retrieval.exclusion_reasons)
        == excluded_refs | insufficient_refs,
        "excluded_pattern_refs",
        "Included, excluded, and insufficient-data references are inconsistent.",
    )
    result.require(
        "result_reference_accounting",
        included_patterns_eligible
        and complete_library_accounting
        and set(retrieval.suppressed_pattern_refs) <= excluded_refs
        and all(retrieval.exclusion_reasons.values()),
        "excluded_pattern_refs",
        "Pattern eligibility, library accounting, suppression, or exclusion reasons are inconsistent.",
    )
    result.require(
        "result_overlap_diagnostics",
        all(
            item.first_pattern_ref in library_refs
            and item.second_pattern_ref in library_refs
            for item in retrieval.overlap_diagnostics
        )
        if library is not None
        else True,
        "overlap_diagnostics",
        "Overlap diagnostics contain dangling pattern references.",
    )
    profile_validation = validate_pattern_retrieval_scoring_profile(
        scoring_profile
    )
    result.require(
        "result_profile",
        profile_validation.is_valid
        and retrieval.scoring_profile_version
        == scoring_profile.profile_version
        and retrieval.scoring_profile_hash
        == scoring_profile.content_hash,
        "scoring_profile_version",
        "Retrieval result scoring profile is invalid or mismatched.",
    )
    return result.result()


def _quality_for_score(
    score: float,
    completeness: float,
    match: RetrievedPatternMatch,
    profile: PatternRetrievalScoringProfile,
) -> MatchQuality:
    insufficient_threshold = profile.completeness_thresholds[
        MatchQuality.INSUFFICIENT_DATA.value
    ]
    if completeness < insufficient_threshold:
        return MatchQuality.INSUFFICIENT_DATA
    total_penalties = (
        match.concentration_penalty
        + match.missing_data_penalty
        + match.incompatibility_penalty
        + match.contradiction_penalty
        + match.limitation_penalty
    )
    for quality in (
        MatchQuality.VERY_STRONG,
        MatchQuality.STRONG,
        MatchQuality.MODERATE,
        MatchQuality.WEAK,
        MatchQuality.VERY_WEAK,
    ):
        if (
            score
            >= profile.score_thresholds[quality.value]
            and completeness
            >= profile.completeness_thresholds[quality.value]
        ):
            if (
                quality is MatchQuality.VERY_STRONG
                and total_penalties
                > profile.penalties[
                    "maximum_very_strong_penalties"
                ]
            ):
                continue
            return quality
    return MatchQuality.VERY_WEAK


def _recommendation_for_match(
    match: RetrievedPatternMatch,
) -> MatchRecommendation:
    if match.match_quality is MatchQuality.INSUFFICIENT_DATA:
        return MatchRecommendation.INSUFFICIENT_DATA
    warning_condition = (
        match.lane is RetrievalLane.COUNTER
        or bool(match.warnings)
        or bool(match.missing_required_features)
        or bool(match.contradicted_required_features)
        or bool(match.incompatible_features)
        or bool(match.contradictory_current_features)
        or match.match_quality
        in {MatchQuality.WEAK, MatchQuality.VERY_WEAK}
    )
    return (
        MatchRecommendation.INCLUDE_WITH_WARNING
        if warning_condition
        else MatchRecommendation.INCLUDE
    )


def _exposure_category_for(
    exposure_type: ExposureType,
) -> str | None:
    categories = tuple(
        category.value
        for category, values in EXPOSURE_CATEGORY_COMPATIBILITY.items()
        if exposure_type in values
    )
    return categories[0] if len(categories) == 1 else None


def _regime_features(
    state: CurrentMarketState,
) -> tuple[set[str], dict[str, MarketRegimeState]]:
    if state.market_regime is None:
        return set(), {}
    values: set[str] = set()
    by_field: dict[str, MarketRegimeState] = {}
    for field_name in _REGIME_FIELDS:
        regime_value = getattr(state.market_regime, field_name)
        by_field[field_name] = regime_value
        if regime_value not in {
            MarketRegimeState.UNKNOWN,
            MarketRegimeState.UNAVAILABLE,
        }:
            values.add(f"{field_name}={regime_value.value}")
    return values, by_field


def _controlled_feature(
    value: str,
) -> tuple[str, str] | None:
    normalized = value.strip().casefold()
    match = re.fullmatch(
        r"(technical_structure|technical_move_type|event|exposure|"
        r"transmission|regime|limitation_applies):([a-z0-9_=:-]+)",
        normalized,
    )
    if match is None:
        return None
    return match.group(1), match.group(2)


def _controlled_features(
    values: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        item
        for item in (_controlled_feature(value) for value in values)
        if item is not None
    )


def _feature_token(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _current_feature_tokens(
    state: CurrentMarketState,
) -> set[str]:
    regimes, _ = _regime_features(state)
    result = {
        _feature_token("exposure", item.value)
        for item in state.current_exposure_types
    }
    result.update(
        _feature_token("transmission", item.value)
        for item in state.current_transmission_hypotheses
    )
    result.update(
        _feature_token("event", item)
        for item in state.current_event_types
    )
    result.update(
        _feature_token("regime", item) for item in regimes
    )
    if state.technical_direction is not TechnicalDirection.UNAVAILABLE:
        result.add(
            _feature_token(
                "technical_direction",
                state.technical_direction.value,
            )
        )
    result.update(
        _feature_token("technical_move_type", item.value)
        for item in state.technical_move_types
    )
    if state.technical_structure != "unavailable":
        result.add(
            _feature_token(
                "technical_structure",
                _normalized_token(state.technical_structure),
            )
        )
    if state.expected_magnitude_bucket is not MagnitudeBucket.UNAVAILABLE:
        result.add(
            _feature_token(
                "magnitude_bucket",
                state.expected_magnitude_bucket.value,
            )
        )
    if state.expected_duration_bucket is not DurationBucket.UNAVAILABLE:
        result.add(
            _feature_token(
                "duration_bucket",
                state.expected_duration_bucket.value,
            )
        )
    if (
        state.benchmark_relative_state
        is not RelativePerformanceBucket.UNAVAILABLE
    ):
        result.add(
            _feature_token(
                "benchmark_relative",
                state.benchmark_relative_state.value,
            )
        )
    if (
        state.sector_relative_state
        is not RelativePerformanceBucket.UNAVAILABLE
    ):
        result.add(
            _feature_token(
                "sector_relative",
                state.sector_relative_state.value,
            )
        )
    return result


def _required_exposure_state(
    exposure_type: ExposureType,
    state: CurrentMarketState,
) -> str:
    if exposure_type in state.current_exposure_types:
        availability = state.exposure_availability.get(
            exposure_type.value,
            FeatureAvailability.AVAILABLE,
        )
        return (
            "unknown"
            if availability
            in {
                FeatureAvailability.UNAVAILABLE,
                FeatureAvailability.ASSUMED,
            }
            else "matched"
        )
    category = _exposure_category_for(exposure_type)
    availability = (
        state.exposure_category_availability.get(category)
        if category is not None
        else None
    )
    if availability is FeatureAvailability.RESEARCHED_NO_SIGNAL:
        return "missing"
    return "unknown"


def _required_regime_state(
    token: str,
    current_tokens: set[str],
    regime_by_field: Mapping[str, MarketRegimeState],
) -> str:
    if token in current_tokens:
        return "matched"
    if "=" not in token:
        return "unknown"
    field_name, _ = token.split("=", 1)
    current = regime_by_field.get(field_name)
    if current is None or current in {
        MarketRegimeState.UNKNOWN,
        MarketRegimeState.UNAVAILABLE,
    }:
        return "unknown"
    return "contradicted"


def _technical_direction_state(
    pattern: HistoricalPattern,
    state: CurrentMarketState,
) -> str:
    if pattern.pattern_direction not in {
        PatternDirection.BULLISH,
        PatternDirection.BEARISH,
    }:
        return "not_applicable"
    if state.technical_direction in {
        TechnicalDirection.UNAVAILABLE,
        TechnicalDirection.MIXED,
    }:
        return "unknown"
    expected = (
        TechnicalDirection.BULLISH
        if pattern.pattern_direction is PatternDirection.BULLISH
        else TechnicalDirection.BEARISH
    )
    return (
        "matched"
        if state.technical_direction is expected
        else "contradicted"
    )


def _move_type_state(
    pattern_types: Sequence[HistoricalMoveType],
    state: CurrentMarketState,
) -> str:
    if not pattern_types:
        return "not_applicable"
    if not state.technical_move_types:
        return "unknown"
    return (
        "matched"
        if set(pattern_types) & set(state.technical_move_types)
        else "contradicted"
    )


def _technical_structure_state(
    expected: str,
    state: CurrentMarketState,
) -> str:
    availability = state.feature_availability.get(
        "technical_structure",
        FeatureAvailability.UNAVAILABLE,
    )
    if (
        availability
        in {
            FeatureAvailability.UNAVAILABLE,
            FeatureAvailability.ASSUMED,
            FeatureAvailability.DRAFT,
        }
        or state.technical_structure == "unavailable"
    ):
        return "unknown"
    return (
        "matched"
        if _normalized_token(state.technical_structure) == expected
        else "contradicted"
    )


def _component_score(
    weight: float,
    *,
    required_matches: int,
    required_count: int,
    optional_matches: int = 0,
    optional_count: int = 0,
    neutral_when_empty: float = 0.0,
) -> float:
    if required_count:
        required_ratio = required_matches / required_count
        if optional_count:
            ratio = (
                0.8 * required_ratio
                + 0.2 * optional_matches / optional_count
            )
        else:
            ratio = required_ratio
    elif optional_count:
        ratio = optional_matches / optional_count
    else:
        ratio = neutral_when_empty
    return round(weight * ratio, 4)


def _outcome_dimension(
    expected: StrEnum,
    current: StrEnum,
    unavailable: StrEnum,
) -> tuple[bool, bool]:
    if expected is unavailable:
        return False, False
    if current is unavailable:
        return True, False
    return True, expected is current


@dataclass(slots=True)
class _ScoreState:
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    matched_required: set[str] = field(default_factory=set)
    missing_required: set[str] = field(default_factory=set)
    contradicted_required: set[str] = field(default_factory=set)
    unknown_required: set[str] = field(default_factory=set)
    matched_optional: set[str] = field(default_factory=set)
    incompatible: set[str] = field(default_factory=set)
    contradictory: set[str] = field(default_factory=set)
    explanations: set[MatchExplanationCode] = field(
        default_factory=set
    )
    warnings: set[str] = field(default_factory=set)
    information_total: int = 0
    information_known: int = 0

    def required_state(self, token: str, status: str) -> None:
        self.required.add(token)
        self.information_total += 1
        if status == "matched":
            self.matched_required.add(token)
            self.information_known += 1
        elif status == "missing":
            self.missing_required.add(token)
            self.information_known += 1
        elif status == "contradicted":
            self.contradicted_required.add(token)
            self.contradictory.add(token)
            self.information_known += 1
        elif status == "unknown":
            self.missing_required.add(token)
            self.unknown_required.add(token)
        elif status != "not_applicable":
            raise ValueError(f"Unsupported required feature state: {status}.")

    def optional_state(self, token: str, status: str) -> None:
        self.optional.add(token)
        self.information_total += 1
        if status == "matched":
            self.matched_optional.add(token)
            self.information_known += 1
        elif status in {"missing", "contradicted"}:
            self.information_known += 1
            if status == "contradicted":
                self.contradictory.add(token)
        elif status not in {"unknown", "not_applicable"}:
            raise ValueError(f"Unsupported optional feature state: {status}.")


def _score_pattern(
    pattern: HistoricalPattern,
    state: CurrentMarketState,
    *,
    lane: RetrievalLane,
    profile: PatternRetrievalScoringProfile,
) -> RetrievedPatternMatch:
    score_state = _ScoreState()
    current_exposures = set(state.current_exposure_types)
    current_transmissions = set(
        state.current_transmission_hypotheses
    )
    current_events = set(state.current_event_types)
    current_regimes, regime_by_field = _regime_features(state)
    current_tokens = _current_feature_tokens(state)

    required_exposure_matches = 0
    for exposure in pattern.initiating_exposure_types:
        token = _feature_token("exposure", exposure.value)
        status = _required_exposure_state(exposure, state)
        score_state.required_state(token, status)
        if status == "matched":
            required_exposure_matches += 1
    optional_exposure_matches = 0
    for exposure in pattern.amplifying_exposure_types:
        token = _feature_token("exposure", exposure.value)
        status = (
            "matched"
            if exposure in current_exposures
            else _required_exposure_state(exposure, state)
        )
        score_state.optional_state(token, status)
        if status == "matched":
            optional_exposure_matches += 1
    for exposure in pattern.dampening_exposure_types:
        if exposure in current_exposures:
            token = _feature_token("exposure", exposure.value)
            score_state.contradictory.add(token)
            score_state.explanations.add(
                MatchExplanationCode.CONTRADICTORY_EXPOSURE
            )
    if required_exposure_matches:
        score_state.explanations.add(
            MatchExplanationCode.REQUIRED_EXPOSURES_MATCH
        )
    if optional_exposure_matches:
        score_state.explanations.add(
            MatchExplanationCode.OPTIONAL_EXPOSURES_MATCH
        )
    if any(
        token.startswith("exposure:")
        for token in score_state.missing_required
    ):
        score_state.explanations.add(
            MatchExplanationCode.REQUIRED_EXPOSURE_MISSING
        )
    exposure_score = _component_score(
        profile.component_weights["exposure"],
        required_matches=required_exposure_matches,
        required_count=len(pattern.initiating_exposure_types),
        optional_matches=optional_exposure_matches,
        optional_count=len(pattern.amplifying_exposure_types),
    )

    transmission_matches = 0
    for mechanism in pattern.transmission_mechanisms:
        token = _feature_token("transmission", mechanism.value)
        if mechanism in current_transmissions:
            status = "matched"
            transmission_matches += 1
        else:
            availability = state.feature_availability.get(
                token,
                FeatureAvailability.UNAVAILABLE,
            )
            status = (
                "missing"
                if availability
                is FeatureAvailability.RESEARCHED_NO_SIGNAL
                else "unknown"
            )
        score_state.required_state(token, status)
    if transmission_matches:
        score_state.explanations.add(
            MatchExplanationCode.TRANSMISSION_MATCH
        )
    if transmission_matches < len(pattern.transmission_mechanisms):
        score_state.explanations.add(
            MatchExplanationCode.TRANSMISSION_MISMATCH
        )
    transmission_score = _component_score(
        profile.component_weights["transmission"],
        required_matches=transmission_matches,
        required_count=len(pattern.transmission_mechanisms),
    )

    required_regime_matches = 0
    for regime in pattern.required_market_regimes:
        token = _feature_token("regime", regime)
        status = _required_regime_state(
            regime,
            current_regimes,
            regime_by_field,
        )
        score_state.required_state(token, status)
        if status == "matched":
            required_regime_matches += 1
    optional_regime_matches = 0
    for regime in pattern.optional_market_regimes:
        token = _feature_token("regime", regime)
        status = _required_regime_state(
            regime,
            current_regimes,
            regime_by_field,
        )
        score_state.optional_state(token, status)
        if status == "matched":
            optional_regime_matches += 1
    for regime in pattern.incompatible_market_regimes:
        if regime in current_regimes:
            score_state.incompatible.add(
                _feature_token("regime", regime)
            )
            score_state.explanations.add(
                MatchExplanationCode.INCOMPATIBLE_REGIME
            )
    if required_regime_matches:
        score_state.explanations.add(
            MatchExplanationCode.REGIME_MATCH
        )
    if optional_regime_matches:
        score_state.explanations.add(
            MatchExplanationCode.OPTIONAL_REGIME_MATCH
        )
    regime_score = _component_score(
        profile.component_weights["regime"],
        required_matches=required_regime_matches,
        required_count=len(pattern.required_market_regimes),
        optional_matches=optional_regime_matches,
        optional_count=len(pattern.optional_market_regimes),
        neutral_when_empty=0.5,
    )

    event_matches = 0
    if pattern.no_single_trigger:
        event_required_count = 0
        for event_type in pattern.trigger_event_types:
            token = _feature_token("event", event_type)
            status = (
                "matched"
                if event_type in current_events
                else "unknown"
            )
            score_state.optional_state(token, status)
            if status == "matched":
                event_matches += 1
        event_score = profile.component_weights["event"]
    else:
        event_required_count = len(pattern.trigger_event_types)
        for event_type in pattern.trigger_event_types:
            token = _feature_token("event", event_type)
            status = (
                "matched"
                if event_type in current_events
                else "unknown"
            )
            score_state.required_state(token, status)
            if status == "matched":
                event_matches += 1
        event_score = _component_score(
            profile.component_weights["event"],
            required_matches=event_matches,
            required_count=event_required_count,
        )
    if event_matches:
        score_state.explanations.add(
            MatchExplanationCode.EVENT_MATCH
        )
    if event_required_count and event_matches < event_required_count:
        score_state.explanations.add(
            MatchExplanationCode.EVENT_MISMATCH
        )

    technical_matches = 0
    technical_count = 0
    direction_status = _technical_direction_state(pattern, state)
    if direction_status != "not_applicable":
        direction_token = _feature_token(
            "technical_direction",
            pattern.pattern_direction.value,
        )
        score_state.required_state(direction_token, direction_status)
        technical_count += 1
        if direction_status == "matched":
            technical_matches += 1
            score_state.explanations.add(
                MatchExplanationCode.TECHNICAL_DIRECTION_MATCH
            )
        elif direction_status == "contradicted":
            score_state.explanations.add(
                MatchExplanationCode.DIRECTION_MISMATCH
            )
    move_token = "technical_move_type:any[" + ",".join(
        sorted(item.value for item in pattern.applicable_move_types)
    ) + "]"
    move_status = _move_type_state(
        pattern.applicable_move_types,
        state,
    )
    if move_status != "not_applicable":
        score_state.required_state(move_token, move_status)
        technical_count += 1
        if move_status == "matched":
            technical_matches += 1
            score_state.explanations.add(
                MatchExplanationCode.TECHNICAL_MOVE_TYPE_MATCH
            )
    technical_structures = {
        value
        for kind, value in _controlled_features(
            pattern.invariant_features
        )
        if kind == "technical_structure"
    }
    for structure in sorted(technical_structures):
        token = _feature_token("technical_structure", structure)
        structure_status = _technical_structure_state(
            structure,
            state,
        )
        score_state.required_state(token, structure_status)
        technical_count += 1
        if structure_status == "matched":
            technical_matches += 1
            score_state.explanations.add(
                MatchExplanationCode.TECHNICAL_STRUCTURE_MATCH
            )
        elif structure_status == "contradicted":
            score_state.explanations.add(
                MatchExplanationCode.TECHNICAL_STRUCTURE_MISMATCH
            )
    technical_score = _component_score(
        profile.component_weights["technical"],
        required_matches=technical_matches,
        required_count=technical_count,
    )

    outcome_comparable = 0
    outcome_matches = 0
    outcome_profile = pattern.expected_outcome_profile

    outcome_direction_status = _technical_direction_state(pattern, state)
    outcome_direction_token = _feature_token(
        "outcome_direction",
        outcome_profile.expected_direction.value,
    )
    if outcome_direction_status != "not_applicable":
        score_state.optional_state(
            outcome_direction_token,
            outcome_direction_status,
        )
        outcome_comparable += 1
        if outcome_direction_status == "matched":
            outcome_matches += 1

    expected_move_token = "outcome_move_type:any[" + ",".join(
        sorted(
            item.value
            for item in outcome_profile.expected_move_types
        )
    ) + "]"
    expected_move_status = _move_type_state(
        outcome_profile.expected_move_types,
        state,
    )
    if expected_move_status != "not_applicable":
        score_state.optional_state(
            expected_move_token,
            expected_move_status,
        )
        outcome_comparable += 1
        if expected_move_status == "matched":
            outcome_matches += 1

    for token_name, expected, current, unavailable, code in (
        (
            "magnitude_bucket",
            outcome_profile.magnitude_bucket,
            state.expected_magnitude_bucket,
            MagnitudeBucket.UNAVAILABLE,
            MatchExplanationCode.MAGNITUDE_MATCH,
        ),
        (
            "duration_bucket",
            outcome_profile.duration_bucket,
            state.expected_duration_bucket,
            DurationBucket.UNAVAILABLE,
            MatchExplanationCode.DURATION_MATCH,
        ),
        (
            "benchmark_relative",
            outcome_profile.expected_benchmark_relative_behavior,
            state.benchmark_relative_state,
            RelativePerformanceBucket.UNAVAILABLE,
            MatchExplanationCode.BENCHMARK_RELATIVE_MATCH,
        ),
        (
            "sector_relative",
            outcome_profile.expected_sector_relative_behavior,
            state.sector_relative_state,
            RelativePerformanceBucket.UNAVAILABLE,
            MatchExplanationCode.SECTOR_RELATIVE_MATCH,
        ),
    ):
        applicable, matched = _outcome_dimension(
            expected,
            current,
            unavailable,
        )
        if not applicable:
            continue
        token = _feature_token(token_name, expected.value)
        status = (
            "matched"
            if matched
            else (
                "unknown"
                if current is unavailable
                else "contradicted"
            )
        )
        score_state.optional_state(token, status)
        outcome_comparable += 1
        if matched:
            outcome_matches += 1
            score_state.explanations.add(code)
    if outcome_matches:
        score_state.explanations.add(
            MatchExplanationCode.OUTCOME_PROFILE_MATCH
        )
    outcome_score = _component_score(
        profile.component_weights["outcome"],
        required_matches=outcome_matches,
        required_count=outcome_comparable,
    )

    for kind, value in _controlled_features(
        pattern.common_features + pattern.optional_features
    ):
        if kind == "limitation_applies":
            continue
        token = _feature_token(kind, value)
        if token in score_state.required or token in score_state.optional:
            continue
        status = "matched" if token in current_tokens else "unknown"
        score_state.optional_state(token, status)
    for kind, value in _controlled_features(
        pattern.disqualifying_features
    ):
        token = _feature_token(kind, value)
        if token in current_tokens:
            score_state.incompatible.add(token)
            if kind == "exposure":
                score_state.explanations.add(
                    MatchExplanationCode.CONTRADICTORY_EXPOSURE
                )

    applicable_limitations = {
        _feature_token(kind, value)
        for kind, value in _controlled_features(pattern.limitations)
        if kind == "limitation_applies" and value in current_tokens
    }
    if applicable_limitations:
        score_state.explanations.add(
            MatchExplanationCode.PATTERN_LIMITATION_APPLIES
        )

    if score_state.unknown_required:
        score_state.explanations.add(
            MatchExplanationCode.INSUFFICIENT_CURRENT_DATA
        )
        score_state.warnings.add("unavailable_required_features")
    if lane is RetrievalLane.COUNTER:
        score_state.explanations.add(
            MatchExplanationCode.COUNTER_PATTERN
        )
        score_state.warnings.add("counter_pattern")
    if pattern.pattern_direction is PatternDirection.BIDIRECTIONAL:
        score_state.explanations.add(
            MatchExplanationCode.BIDIRECTIONAL_PATTERN
        )

    metrics = pattern.support_metrics
    concentration_penalty = 0.0
    if (
        metrics.unique_symbol_count <= 1
        or metrics.symbol_concentration >= 1.0
    ):
        concentration_penalty += profile.penalties[
            "one_symbol_concentration"
        ]
    if (
        metrics.unique_sector_count == 1
        or (
            metrics.unique_sector_count > 0
            and metrics.sector_concentration >= 1.0
        )
    ):
        concentration_penalty += profile.penalties[
            "one_sector_concentration"
        ]
    if concentration_penalty:
        score_state.explanations.add(
            MatchExplanationCode.CONCENTRATED_PATTERN_SUPPORT
        )
        score_state.warnings.add("concentrated_pattern_support")

    independence = metrics.evidence_independence_status
    weak_independence_penalty = {
        EvidenceIndependenceStatus.INDEPENDENT: 0.0,
        EvidenceIndependenceStatus.MIXED: profile.penalties[
            "mixed_sources"
        ],
        EvidenceIndependenceStatus.CORRELATED: profile.penalties[
            "correlated_sources"
        ],
        EvidenceIndependenceStatus.UNKNOWN: profile.penalties[
            "unknown_sources"
        ],
        EvidenceIndependenceStatus.INSUFFICIENT_EVIDENCE: (
            profile.penalties["insufficient_sources"]
        ),
    }[independence]
    concentration_penalty += weak_independence_penalty
    if weak_independence_penalty:
        score_state.explanations.add(
            MatchExplanationCode.WEAK_SOURCE_INDEPENDENCE
        )
        score_state.warnings.add("weak_source_independence")

    if independence is EvidenceIndependenceStatus.INDEPENDENT:
        support_quality_adjustment = profile.support_adjustments[
            (
                "independent_diverse"
                if metrics.unique_symbol_count >= 2
                and metrics.unique_regime_count >= 2
                else "independent"
            )
        ]
    elif independence is EvidenceIndependenceStatus.MIXED:
        support_quality_adjustment = profile.support_adjustments["mixed"]
    else:
        support_quality_adjustment = profile.support_adjustments.get(
            independence.value,
            0.0,
        )

    known_required_missing = (
        score_state.missing_required - score_state.unknown_required
    )
    missing_data_penalty = (
        len(known_required_missing)
        * profile.penalties["missing_required"]
        + len(score_state.unknown_required)
        * profile.penalties["unavailable_required"]
    )
    information_completeness = (
        score_state.information_known / score_state.information_total
        if score_state.information_total
        else 0.0
    )
    if information_completeness < 0.65:
        missing_data_penalty += (
            profile.penalties["high_missing_share_maximum"]
            * (1.0 - information_completeness)
        )
    incompatibility_penalty = (
        len(
            {
                item
                for item in score_state.incompatible
                if item.startswith("regime:")
            }
        )
        * profile.penalties["incompatible_regime"]
    )
    other_incompatible_count = len(score_state.incompatible) - len(
        {
            item
            for item in score_state.incompatible
            if item.startswith("regime:")
        }
    )
    incompatibility_penalty += (
        other_incompatible_count
        * profile.penalties["contradictory_feature"]
    )
    contradiction_penalty = (
        len(score_state.contradictory)
        * profile.penalties["contradictory_feature"]
    )
    limitation_penalty = (
        len(applicable_limitations)
        * profile.penalties["applicable_limitation"]
    )

    seed = RetrievedPatternMatch(
        pattern_id=pattern.pattern_id,
        pattern_version=pattern.pattern_version,
        pattern_hash=pattern.content_hash,
        lane=lane,
        rank=1,
        total_score=0.0,
        match_quality=MatchQuality.INSUFFICIENT_DATA,
        recommendation=MatchRecommendation.INSUFFICIENT_DATA,
        required_feature_tokens=_ordered_strings(
            tuple(score_state.required)
        ),
        optional_feature_tokens=_ordered_strings(
            tuple(score_state.optional)
        ),
        matched_required_features=_ordered_strings(
            tuple(score_state.matched_required)
        ),
        missing_required_features=_ordered_strings(
            tuple(score_state.missing_required)
        ),
        contradicted_required_features=_ordered_strings(
            tuple(score_state.contradicted_required)
        ),
        matched_optional_features=_ordered_strings(
            tuple(score_state.matched_optional)
        ),
        incompatible_features=_ordered_strings(
            tuple(score_state.incompatible)
        ),
        contradictory_current_features=_ordered_strings(
            tuple(score_state.contradictory)
        ),
        exposure_match_score=exposure_score,
        transmission_match_score=transmission_score,
        regime_match_score=regime_score,
        event_match_score=event_score,
        technical_match_score=technical_score,
        outcome_match_score=outcome_score,
        concentration_penalty=round(concentration_penalty, 4),
        support_quality_adjustment=round(
            support_quality_adjustment,
            4,
        ),
        missing_data_penalty=round(missing_data_penalty, 4),
        incompatibility_penalty=round(
            incompatibility_penalty,
            4,
        ),
        contradiction_penalty=round(
            contradiction_penalty,
            4,
        ),
        limitation_penalty=round(limitation_penalty, 4),
        information_completeness=round(
            information_completeness,
            6,
        ),
        explanation_codes=tuple(
            sorted(
                score_state.explanations,
                key=lambda item: item.value,
            )
        ),
        warnings=tuple(sorted(score_state.warnings)),
    )
    score = _score_total(seed)
    quality = _quality_for_score(
        score,
        seed.information_completeness,
        seed,
        profile,
    )
    with_quality = replace(
        seed,
        total_score=score,
        match_quality=quality,
    )
    completed = replace(
        with_quality,
        recommendation=_recommendation_for_match(with_quality),
    )
    completed = replace(
        completed,
        content_hash=retrieved_pattern_match_content_hash(completed),
    )
    validation = validate_retrieved_pattern_match(
        completed,
        pattern=pattern,
        scoring_profile=profile,
    )
    if not validation.is_valid:
        messages = "; ".join(
            f"{item.code}:{item.path}" for item in validation.errors
        )
        raise RuntimeError(
            "Deterministic pattern score failed validation: " + messages
        )
    return completed


def _lane_for_pattern(
    pattern: HistoricalPattern,
    state: CurrentMarketState,
) -> RetrievalLane:
    opposite = (
        (
            pattern.pattern_direction is PatternDirection.BULLISH
            and state.technical_direction is TechnicalDirection.BEARISH
        )
        or (
            pattern.pattern_direction is PatternDirection.BEARISH
            and state.technical_direction is TechnicalDirection.BULLISH
        )
    )
    return RetrievalLane.COUNTER if opposite else RetrievalLane.PRIMARY


def _meaningful_counter_overlap(
    match: RetrievedPatternMatch,
) -> bool:
    eligible_prefixes = (
        "exposure:",
        "transmission:",
        "regime:",
        "event:",
        "technical_structure:",
        "technical_move_type:",
    )
    return any(
        token.startswith(eligible_prefixes)
        for token in (
            match.matched_required_features
            + match.matched_optional_features
        )
    )


def _context_condition_met(
    match: RetrievedPatternMatch,
) -> bool:
    return any(
        token.startswith(("regime:", "event:"))
        for token in (
            match.matched_required_features
            + match.matched_optional_features
        )
    )


def _pattern_source_references_valid(
    pattern: HistoricalPattern,
    library: HistoricalPatternLibrary,
) -> bool:
    case_ids = set(
        pattern.supporting_case_ids
        + pattern.contradicting_case_ids
        + pattern.exception_case_ids
    )
    return (
        set(pattern.source_analysis_hashes) == case_ids
        and case_ids <= set(library.source_case_hashes)
        and all(
            library.source_analysis_hashes.get(case_id)
            == pattern.source_analysis_hashes[case_id]
            for case_id in case_ids
        )
    )


def _rank_matches(
    matches: Sequence[RetrievedPatternMatch],
) -> tuple[RetrievedPatternMatch, ...]:
    ranked: list[RetrievedPatternMatch] = []
    for rank, match in enumerate(
        sorted(matches, key=_match_order_key),
        start=1,
    ):
        seed = replace(match, rank=rank, content_hash="")
        ranked.append(
            replace(
                seed,
                content_hash=retrieved_pattern_match_content_hash(seed),
            )
        )
    return tuple(ranked)


def _near_duplicate_components(
    refs: set[str],
    diagnostics: Sequence[PatternOverlapDiagnostic],
) -> tuple[tuple[str, ...], ...]:
    adjacency = {ref: set() for ref in refs}
    for item in diagnostics:
        if (
            item.relationship
            is PatternOverlapRelationship.NEAR_DUPLICATE
            and item.first_pattern_ref in refs
            and item.second_pattern_ref in refs
        ):
            adjacency[item.first_pattern_ref].add(
                item.second_pattern_ref
            )
            adjacency[item.second_pattern_ref].add(
                item.first_pattern_ref
            )
    remaining = set(refs)
    components: list[tuple[str, ...]] = []
    while remaining:
        first = min(remaining)
        pending = [first]
        component: set[str] = set()
        remaining.remove(first)
        while pending:
            current = pending.pop()
            component.add(current)
            connected = adjacency[current] & remaining
            remaining.difference_update(connected)
            pending.extend(sorted(connected, reverse=True))
        components.append(tuple(sorted(component)))
    return tuple(sorted(components))


def _apply_overlap_diversity(
    matches: Sequence[RetrievedPatternMatch],
    diagnostics: Sequence[PatternOverlapDiagnostic],
    profile: PatternRetrievalScoringProfile,
) -> tuple[
    tuple[RetrievedPatternMatch, ...],
    tuple[str, ...],
]:
    ordered = tuple(sorted(matches, key=_match_order_key))
    match_map = {item.pattern_ref: item for item in ordered}
    suppressed: set[str] = set()
    for component in _near_duplicate_components(
        set(match_map),
        diagnostics,
    ):
        if len(component) <= profile.maximum_near_duplicates_per_cluster:
            continue
        component_matches = sorted(
            (match_map[ref] for ref in component),
            key=_match_order_key,
        )
        for item in component_matches[
            profile.maximum_near_duplicates_per_cluster :
        ]:
            suppressed.add(item.pattern_ref)
    return (
        tuple(
            item for item in ordered if item.pattern_ref not in suppressed
        ),
        tuple(sorted(suppressed)),
    )


def _minimum_quality_met(
    quality: MatchQuality,
    minimum: MatchQuality,
) -> bool:
    return _QUALITY_ORDER[quality] >= _QUALITY_ORDER[minimum]


def _finalize_retrieval_result(
    result: PatternRetrievalResult,
    *,
    state: CurrentMarketState,
    library: HistoricalPatternLibrary,
    scoring_profile: PatternRetrievalScoringProfile,
) -> PatternRetrievalResult:
    seed = replace(
        result,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(
        seed,
        content_hash=pattern_retrieval_result_content_hash(seed),
    )
    validation = validate_pattern_retrieval_result(
        seed,
        state=state,
        library=library,
        scoring_profile=scoring_profile,
    )
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=pattern_retrieval_result_content_hash(finalized),
    )
    repeated = validate_pattern_retrieval_result(
        finalized,
        state=state,
        library=library,
        scoring_profile=scoring_profile,
    )
    if repeated != validation:
        raise RuntimeError(
            "Pattern-retrieval result validation did not stabilize."
        )
    return finalized


class PatternRetrievalEngine:
    """Retrieve historical patterns through versioned deterministic rules."""

    def __init__(
        self,
        *,
        scoring_profile: PatternRetrievalScoringProfile = (
            DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
        ),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(
            scoring_profile,
            PatternRetrievalScoringProfile,
        ):
            raise TypeError(
                "scoring_profile must be PatternRetrievalScoringProfile."
            )
        self.scoring_profile = scoring_profile
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _execution_result(
        self,
        *,
        success: bool,
        retrieval_result: PatternRetrievalResult | None,
        validation: ValidationResult,
        errors: Sequence[str],
        warnings: Sequence[str],
        state_hash: str,
        library_hash: str,
        generated_at: str,
    ) -> PatternRetrievalExecutionResult:
        seed = PatternRetrievalExecutionResult(
            success=success,
            retrieval_result=retrieval_result,
            validation_result=validation,
            errors=tuple(errors),
            warnings=tuple(warnings),
            state_hash=state_hash,
            library_hash=library_hash,
            scoring_profile_version=(
                self.scoring_profile.profile_version
            ),
            generated_at=generated_at,
        )
        return replace(
            seed,
            content_hash=(
                pattern_retrieval_execution_result_content_hash(seed)
            ),
        )

    def _failure(
        self,
        *,
        validation: ValidationResult,
        state_hash: str,
        library_hash: str,
        generated_at: str,
        warnings: Sequence[str] = (),
    ) -> PatternRetrievalExecutionResult:
        errors = tuple(
            f"{item.code} at {item.path}: {item.message}"
            for item in validation.errors
        )
        return self._execution_result(
            success=False,
            retrieval_result=None,
            validation=validation,
            errors=errors,
            warnings=warnings,
            state_hash=state_hash,
            library_hash=library_hash,
            generated_at=generated_at,
        )

    def retrieve(
        self,
        state: CurrentMarketState,
        library: HistoricalPatternLibrary,
        query: PatternRetrievalQuery,
    ) -> PatternRetrievalExecutionResult:
        if not isinstance(state, CurrentMarketState):
            raise TypeError("state must be CurrentMarketState.")
        if not isinstance(library, HistoricalPatternLibrary):
            raise TypeError(
                "library must be HistoricalPatternLibrary."
            )
        if not isinstance(query, PatternRetrievalQuery):
            raise TypeError("query must be PatternRetrievalQuery.")
        generated_at = _normalize_timestamp(
            self.clock(),
            field_name="generated_at",
        )

        profile_validation = (
            validate_pattern_retrieval_scoring_profile(
                self.scoring_profile
            )
        )
        if not profile_validation.is_valid:
            return self._failure(
                validation=profile_validation,
                state_hash=state.content_hash,
                library_hash=library.content_hash,
                generated_at=generated_at,
            )

        state_validation = validate_current_market_state(state)
        if (
            not state_validation.is_valid
            or state.validation_result != state_validation
        ):
            validation = (
                state_validation
                if not state_validation.is_valid
                else _failure_validation(
                    "current_state_validation_mismatch",
                    "state.validation_result",
                    "Stored current-state validation is not canonical.",
                )
            )
            return self._failure(
                validation=validation,
                state_hash=state.content_hash,
                library_hash=library.content_hash,
                generated_at=generated_at,
            )

        query_validation = validate_pattern_retrieval_query(
            query,
            state=state,
            library=library,
            scoring_profile=self.scoring_profile,
        )
        if not query_validation.is_valid:
            return self._failure(
                validation=query_validation,
                state_hash=state.content_hash,
                library_hash=library.content_hash,
                generated_at=generated_at,
            )

        library_validation = validate_historical_pattern_library(
            library
        )
        fatal_library_failures = set(
            library_validation.failed_rules
        ) - {"library_pattern_validity"}
        if fatal_library_failures:
            return self._failure(
                validation=library_validation,
                state_hash=state.content_hash,
                library_hash=library.content_hash,
                generated_at=generated_at,
            )

        allowed_statuses = set(query.allowed_pattern_statuses)
        exclusions: dict[str, set[PatternExclusionCode]] = {}
        insufficient: set[str] = set()
        primary_candidates: list[RetrievedPatternMatch] = []
        counter_candidates: list[RetrievedPatternMatch] = []
        scored_patterns: list[HistoricalPattern] = []
        pattern_by_ref = {
            item.pattern_ref: item for item in library.patterns
        }
        state_cutoff = _temporal_point(state.applicable_cutoff)

        def exclude(
            pattern_ref: str,
            reason: PatternExclusionCode,
        ) -> None:
            exclusions.setdefault(pattern_ref, set()).add(reason)

        for pattern in library.patterns:
            pattern_ref = pattern.pattern_ref
            if pattern.pattern_status is PatternStatus.REJECTED:
                exclude(
                    pattern_ref,
                    PatternExclusionCode.REJECTED_PATTERN,
                )
                continue
            if (
                pattern.pattern_status is PatternStatus.DEPRECATED
                and PatternStatus.DEPRECATED not in allowed_statuses
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.DEPRECATED_PATTERN,
                )
                continue
            if pattern.pattern_status not in allowed_statuses:
                exclude(
                    pattern_ref,
                    PatternExclusionCode.STATUS_NOT_ALLOWED,
                )
                continue
            if (
                not pattern.content_hash
                or pattern.content_hash
                != historical_pattern_content_hash(pattern)
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.INVALID_PATTERN_HASH,
                )
                continue
            standalone_validation = validate_historical_pattern(
                pattern,
                minimum_supporting_cases=(
                    self.scoring_profile.minimum_supporting_cases
                ),
            )
            if (
                not standalone_validation.is_valid
                or not pattern.validation_result.is_valid
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.INVALID_PATTERN,
                )
                continue
            if (
                pattern.support_metrics.supporting_case_count
                < self.scoring_profile.minimum_supporting_cases
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.INSUFFICIENT_PATTERN_SUPPORT,
                )
                continue
            if not _pattern_source_references_valid(pattern, library):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.INVALID_SOURCE_REFERENCES,
                )
                continue
            if (
                _temporal_point(pattern.applicable_cutoff)
                > state_cutoff
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.CUTOFF_INCOMPATIBILITY,
                )
                continue
            if (
                pattern.pattern_direction
                is PatternDirection.CONTEXT_DEPENDENT
                and not query.include_context_dependent_patterns
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.CONTEXT_CONDITION_NOT_MET,
                )
                continue
            if (
                pattern.pattern_direction
                is PatternDirection.BIDIRECTIONAL
                and not query.include_bidirectional_patterns
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.BIDIRECTIONAL_NOT_REQUESTED,
                )
                continue

            lane = _lane_for_pattern(pattern, state)
            match = _score_pattern(
                pattern,
                state,
                lane=lane,
                profile=self.scoring_profile,
            )
            if (
                pattern.pattern_direction
                is PatternDirection.CONTEXT_DEPENDENT
                and not _context_condition_met(match)
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.CONTEXT_CONDITION_NOT_MET,
                )
                continue
            if (
                lane is RetrievalLane.COUNTER
                and not _meaningful_counter_overlap(match)
            ):
                exclude(
                    pattern_ref,
                    (
                        PatternExclusionCode
                        .NO_MEANINGFUL_COUNTER_INPUT_OVERLAP
                    ),
                )
                continue
            if match.match_quality is MatchQuality.INSUFFICIENT_DATA:
                insufficient.add(pattern_ref)
                exclude(
                    pattern_ref,
                    PatternExclusionCode.INSUFFICIENT_CURRENT_DATA,
                )
                scored_patterns.append(pattern)
                continue
            if not _minimum_quality_met(
                match.match_quality,
                query.minimum_match_quality,
            ):
                exclude(
                    pattern_ref,
                    PatternExclusionCode.BELOW_MINIMUM_QUALITY,
                )
                scored_patterns.append(pattern)
                continue
            scored_patterns.append(pattern)
            if lane is RetrievalLane.PRIMARY:
                primary_candidates.append(match)
            else:
                counter_candidates.append(match)

        overlap_diagnostics = analyze_pattern_overlaps(
            tuple(scored_patterns)
        )
        diverse_primary, suppressed_primary = (
            _apply_overlap_diversity(
                primary_candidates,
                overlap_diagnostics,
                self.scoring_profile,
            )
        )
        diverse_counter, suppressed_counter = (
            _apply_overlap_diversity(
                counter_candidates,
                overlap_diagnostics,
                self.scoring_profile,
            )
        )
        suppressed = set(suppressed_primary) | set(
            suppressed_counter
        )
        for pattern_ref in suppressed:
            exclude(
                pattern_ref,
                PatternExclusionCode.OVERLAP_NEAR_DUPLICATE_SUPPRESSED,
            )

        selected_primary = diverse_primary[: query.requested_limit]
        selected_counter = diverse_counter[
            : query.requested_counter_limit
        ]
        for match in diverse_primary[query.requested_limit :]:
            exclude(
                match.pattern_ref,
                PatternExclusionCode.RESULT_LIMIT,
            )
        for match in diverse_counter[
            query.requested_counter_limit :
        ]:
            exclude(
                match.pattern_ref,
                PatternExclusionCode.RESULT_LIMIT,
            )

        ranked_primary = _rank_matches(selected_primary)
        ranked_counter = _rank_matches(selected_counter)
        excluded_refs = set(exclusions) - insufficient
        warnings: list[str] = []
        if not library.patterns:
            warnings.append("empty_pattern_library")
        if not ranked_primary:
            warnings.append("no_primary_matches")
        if ranked_counter:
            warnings.append("counter_patterns_present")
        if suppressed:
            warnings.append("overlap_suppression_applied")
        if exclusions:
            warnings.append("ineligible_patterns_excluded")

        draft = PatternRetrievalResult(
            success=True,
            query=query,
            matches=ranked_primary,
            counter_patterns=ranked_counter,
            excluded_pattern_refs=tuple(sorted(excluded_refs)),
            insufficient_data_pattern_refs=tuple(
                sorted(insufficient)
            ),
            suppressed_pattern_refs=tuple(sorted(suppressed)),
            exclusion_reasons={
                key: tuple(
                    sorted(value, key=lambda item: item.value)
                )
                for key, value in exclusions.items()
            },
            overlap_diagnostics=overlap_diagnostics,
            scoring_profile_version=(
                self.scoring_profile.profile_version
            ),
            scoring_profile_hash=self.scoring_profile.content_hash,
            validation_result=ValidationResult.unvalidated(),
            warnings=tuple(warnings),
            generated_at=generated_at,
        )
        retrieval = _finalize_retrieval_result(
            draft,
            state=state,
            library=library,
            scoring_profile=self.scoring_profile,
        )
        if not retrieval.validation_result.is_valid:
            return self._failure(
                validation=retrieval.validation_result,
                state_hash=state.content_hash,
                library_hash=library.content_hash,
                generated_at=generated_at,
                warnings=retrieval.warnings,
            )
        return self._execution_result(
            success=True,
            retrieval_result=retrieval,
            validation=retrieval.validation_result,
            errors=(),
            warnings=retrieval.warnings,
            state_hash=state.content_hash,
            library_hash=library.content_hash,
            generated_at=generated_at,
        )


__all__ = [
    "DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE",
    "MatchExplanationCode",
    "MatchQuality",
    "MatchRecommendation",
    "PATTERN_RETRIEVAL_ENGINE_VERSION",
    "PATTERN_RETRIEVAL_EXECUTION_RESULT_SCHEMA_VERSION",
    "PATTERN_RETRIEVAL_QUERY_SCHEMA_VERSION",
    "PATTERN_RETRIEVAL_RESULT_SCHEMA_VERSION",
    "PATTERN_RETRIEVAL_SCORING_PROFILE_VERSION",
    "PATTERN_RETRIEVAL_VALIDATION_VERSION",
    "PatternExclusionCode",
    "PatternRetrievalEngine",
    "PatternRetrievalExecutionResult",
    "PatternRetrievalQuery",
    "PatternRetrievalResult",
    "PatternRetrievalScoringProfile",
    "RETRIEVED_PATTERN_MATCH_SCHEMA_VERSION",
    "RetrievalLane",
    "RetrievedPatternMatch",
    "create_pattern_retrieval_query",
    "pattern_retrieval_execution_result_content_hash",
    "pattern_retrieval_query_content_hash",
    "pattern_retrieval_result_content_hash",
    "retrieved_pattern_match_content_hash",
    "validate_pattern_retrieval_query",
    "validate_pattern_retrieval_result",
    "validate_pattern_retrieval_scoring_profile",
    "validate_retrieved_pattern_match",
]
