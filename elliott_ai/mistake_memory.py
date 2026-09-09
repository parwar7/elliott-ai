"""Human-reviewed forecast outcomes and cutoff-safe mistake memory.

Phase 11C consumes immutable Phase 11B outcome evaluations.  It records human
interpretation without rewriting deterministic results, and exposes approved
lessons only as post-count audit warnings.  It performs no model call, wave
counting, probability calculation, or active-pipeline integration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TypeVar

from .forecast_outcomes import ForecastOutcomeEvaluation, OutcomeStatus
from .forecast_records import canonical_sha256


OUTCOME_REVIEW_SCHEMA_VERSION = "forecast-outcome-review-1.0.0"
FAILURE_DIAGNOSIS_SCHEMA_VERSION = "forecast-failure-diagnosis-1.0.0"
LESSON_SCHEMA_VERSION = "mistake-memory-lesson-1.0.0"
LESSON_SCOPE_SCHEMA_VERSION = "mistake-memory-scope-1.0.0"
LESSON_SOURCE_SCHEMA_VERSION = "mistake-memory-source-1.0.0"
LESSON_EVENT_SCHEMA_VERSION = "mistake-memory-event-1.0.0"
LESSON_RETRIEVAL_QUERY_SCHEMA_VERSION = "mistake-memory-query-1.0.0"
RETRIEVED_LESSON_SCHEMA_VERSION = "mistake-memory-retrieval-1.0.0"
MISTAKE_MEMORY_POLICY_VERSION = "mistake-memory-policy-1.0.0"
MISTAKE_MEMORY_CALCULATION_VERSION = "mistake-memory-deterministic-1.0.0"


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    REVISED = "revised"
    REJECTED = "rejected"
    NEEDS_MORE_DATA = "needs_more_data"


class FailureDiagnosisType(StrEnum):
    WRONG_DEGREE = "wrong_degree"
    WRONG_WAVE_FAMILY = "wrong_wave_family"
    INCORRECT_IMPULSE_CORRECTION = "incorrect_impulse_correction_classification"
    PREMATURE_COMPLETION = "premature_wave_completion_assumption"
    INVALID_FIBONACCI_ANCHORS_OR_SCALE = "invalid_fibonacci_anchors_or_scale"
    ALTERNATIVE_IMPROPERLY_REJECTED = "alternative_count_improperly_rejected"
    TIMING_WINDOW_ERROR = "timing_window_error"
    TARGET_ERROR = "target_error"
    RSI_OVERWEIGHTED = "rsi_overweighted"
    VOLUME_OVERWEIGHTED = "volume_overweighted"
    EWO_OVERWEIGHTED = "ewo_overweighted"
    MISSING_CONTRADICTORY_EVIDENCE = "missing_contradictory_evidence"
    DATA_QUALITY_PROBLEM = "data_quality_problem"
    AMBIGUOUS_OR_INCOMPARABLE = "ambiguous_or_incomparable_outcome"
    OTHER = "other_human_documented_diagnosis"


class LessonScopeType(StrEnum):
    EXACT_CASE = "exact_case"
    SCOPED = "scoped"
    GENERAL = "general"


class LessonSourceRole(StrEnum):
    SUPPORTING = "supporting"
    COUNTEREXAMPLE = "counterexample"


class LessonStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_INELIGIBLE_LESSON_OUTCOMES = frozenset(
    {
        OutcomeStatus.UNRESOLVED,
        OutcomeStatus.INSUFFICIENT_DATA,
        OutcomeStatus.INCOMPARABLE,
    }
)
_ELIGIBLE_FORECAST_ERROR_OUTCOMES = frozenset(
    {OutcomeStatus.FAILED, OutcomeStatus.PARTIAL}
)
_ALLOWED_LESSON_TRANSITIONS: Mapping[LessonStatus | None, frozenset[LessonStatus]] = {
    None: frozenset({LessonStatus.PROPOSED}),
    LessonStatus.PROPOSED: frozenset(
        {LessonStatus.ACTIVE, LessonStatus.REJECTED, LessonStatus.RETIRED}
    ),
    LessonStatus.ACTIVE: frozenset(
        {LessonStatus.SUPERSEDED, LessonStatus.RETIRED}
    ),
    LessonStatus.REJECTED: frozenset(),
    LessonStatus.SUPERSEDED: frozenset(),
    LessonStatus.RETIRED: frozenset(),
}
T = TypeVar("T")


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numeric values must be finite.")
        return value
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}.")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON mappings require string keys.")
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numeric values must be finite.")
        return value
    raise TypeError(f"Unsupported JSON contract value: {type(value).__name__}.")


def _normalize_utc(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit UTC offset.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


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


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _string_tuple(
    value: Sequence[str], *, field_name: str, sort_unique: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(_require_text(item, field_name=field_name) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicate values.")
    return tuple(sorted(result)) if sort_unique else result


def _string_mapping(value: Mapping[str, str], *, field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    result = {
        _require_text(key, field_name=f"{field_name} key"): _require_text(
            item, field_name=f"{field_name}[{key!r}]"
        )
        for key, item in value.items()
    }
    return MappingProxyType(dict(sorted(result.items())))


def _model_tuple(
    value: Sequence[Any], model_type: type[T], *, field_name: str
) -> tuple[T, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    result: list[T] = []
    for item in value:
        if isinstance(item, model_type):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(model_type.from_dict(item))  # type: ignore[attr-defined]
        else:
            raise TypeError(
                f"{field_name} items must be {model_type.__name__} objects or mappings."
            )
    return tuple(result)


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], *, model_name: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{model_name} contains unknown fields: " + ", ".join(unknown) + "."
        )


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class FailureDiagnosis(_JsonContract):
    diagnosis_id: str
    diagnosis_type: FailureDiagnosisType
    summary: str
    affected_hypothesis_ids: tuple[str, ...] = ()
    affected_claim_ids: tuple[str, ...] = ()
    evidence_reference_ids: tuple[str, ...] = ()
    notes: str | None = None
    schema_version: str = FAILURE_DIAGNOSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnosis_id", _require_text(self.diagnosis_id, field_name="diagnosis_id"))
        object.__setattr__(self, "diagnosis_type", _enum_value(self.diagnosis_type, FailureDiagnosisType, field_name="diagnosis_type"))
        object.__setattr__(self, "summary", _require_text(self.summary, field_name="summary"))
        for name in ("affected_hypothesis_ids", "affected_claim_ids", "evidence_reference_ids"):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=True))
        object.__setattr__(self, "notes", _optional_text(self.notes, field_name="notes"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.diagnosis_type is FailureDiagnosisType.OTHER and not self.notes:
            raise ValueError("An other diagnosis requires human-authored notes.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FailureDiagnosis":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ForecastOutcomeReview(_JsonContract):
    review_id: str
    review_version: int
    parent_review_id: str | None
    evaluation_id: str
    evaluation_content_hash: str
    forecast_id: str
    forecast_content_hash: str
    reviewed_hypothesis_id: str
    reviewed_outcome_status: OutcomeStatus
    reviewer_reference: str
    reviewed_at_utc: str
    decision: ReviewDecision
    diagnoses: tuple[FailureDiagnosis, ...]
    corrected_interpretation: str | None
    evidence_reference_ids: tuple[str, ...]
    notes: str
    deterministic_scoring_error: bool = False
    required_superseding_evaluation_id: str | None = None
    source_hashes: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = OUTCOME_REVIEW_SCHEMA_VERSION
    policy_version: str = MISTAKE_MEMORY_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "review_id", "evaluation_id", "forecast_id", "reviewed_hypothesis_id",
            "reviewer_reference", "notes", "schema_version", "policy_version",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        if not isinstance(self.review_version, int) or isinstance(self.review_version, bool) or self.review_version < 1:
            raise ValueError("review_version must be a positive integer.")
        object.__setattr__(self, "parent_review_id", _optional_text(self.parent_review_id, field_name="parent_review_id"))
        if self.parent_review_id == self.review_id:
            raise ValueError("An outcome review cannot be its own parent.")
        for name in ("evaluation_content_hash", "forecast_content_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "reviewed_outcome_status", _enum_value(self.reviewed_outcome_status, OutcomeStatus, field_name="reviewed_outcome_status"))
        object.__setattr__(self, "reviewed_at_utc", _normalize_utc(self.reviewed_at_utc, field_name="reviewed_at_utc"))
        object.__setattr__(self, "decision", _enum_value(self.decision, ReviewDecision, field_name="decision"))
        object.__setattr__(self, "diagnoses", _model_tuple(self.diagnoses, FailureDiagnosis, field_name="diagnoses"))
        if len({item.diagnosis_id for item in self.diagnoses}) != len(self.diagnoses):
            raise ValueError("diagnoses must have unique diagnosis IDs.")
        object.__setattr__(self, "corrected_interpretation", _optional_text(self.corrected_interpretation, field_name="corrected_interpretation"))
        object.__setattr__(self, "evidence_reference_ids", _string_tuple(self.evidence_reference_ids, field_name="evidence_reference_ids", sort_unique=True))
        if not isinstance(self.deterministic_scoring_error, bool):
            raise TypeError("deterministic_scoring_error must be boolean.")
        object.__setattr__(self, "required_superseding_evaluation_id", _optional_text(self.required_superseding_evaluation_id, field_name="required_superseding_evaluation_id"))
        if self.deterministic_scoring_error != bool(self.required_superseding_evaluation_id):
            raise ValueError("A deterministic scoring error requires a superseding evaluation ID, and that ID is otherwise forbidden.")
        if self.decision is ReviewDecision.REVISED and not self.corrected_interpretation:
            raise ValueError("A revised review requires corrected_interpretation.")
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        for key, item in source_hashes.items():
            _require_hash(item, field_name=f"source_hashes[{key!r}]")
        object.__setattr__(self, "source_hashes", source_hashes)
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "ForecastOutcomeReview":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("review_id"):
            seed = {
                "evaluation_id": values.get("evaluation_id"),
                "review_version": values.get("review_version", 1),
                "parent_review_id": values.get("parent_review_id"),
                "reviewer_reference": values.get("reviewer_reference"),
                "reviewed_at_utc": values.get("reviewed_at_utc"),
            }
            values["review_id"] = f"forecast_review_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=forecast_outcome_review_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ForecastOutcomeReview":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != forecast_outcome_review_content_hash(result):
            raise ValueError("ForecastOutcomeReview content_hash does not match its payload.")
        return result


def forecast_outcome_review_content_hash(value: ForecastOutcomeReview | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ForecastOutcomeReview) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class LessonScope(_JsonContract):
    scope_type: LessonScopeType
    exact_forecast_ids: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    asset_classes: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    degrees: tuple[str, ...] = ()
    wave_roles: tuple[str, ...] = ()
    pattern_families: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    indicator_regimes: tuple[str, ...] = ()
    market_regimes: tuple[str, ...] = ()
    applicability_conditions: tuple[str, ...] = ()
    non_applicability_conditions: tuple[str, ...] = ()
    schema_version: str = LESSON_SCOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_type", _enum_value(self.scope_type, LessonScopeType, field_name="scope_type"))
        for name in (
            "exact_forecast_ids", "symbols", "asset_classes", "timeframes", "degrees",
            "wave_roles", "pattern_families", "directions", "indicator_regimes",
            "market_regimes", "applicability_conditions", "non_applicability_conditions",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=True))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.scope_type is LessonScopeType.EXACT_CASE:
            required = {
                "exact_forecast_ids": self.exact_forecast_ids,
                "symbols": self.symbols,
                "timeframes": self.timeframes,
                "degrees": self.degrees,
                "wave_roles": self.wave_roles,
                "pattern_families": self.pattern_families,
                "directions": self.directions,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "An exact-case lesson must remain narrowly scoped; missing: "
                    + ", ".join(missing)
                    + "."
                )
            plural = [name for name, value in required.items() if len(value) != 1]
            if plural:
                raise ValueError(
                    "An exact-case lesson requires exactly one value for: "
                    + ", ".join(plural)
                    + "."
                )
        elif self.exact_forecast_ids:
            raise ValueError("Only exact-case scopes may name exact forecast IDs.")
        if self.scope_type is LessonScopeType.SCOPED and not any(
            (
                self.symbols, self.asset_classes, self.timeframes, self.degrees,
                self.wave_roles, self.pattern_families, self.directions,
                self.indicator_regimes, self.market_regimes,
            )
        ):
            raise ValueError("A scoped lesson requires at least one explicit matching field.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LessonScope":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MistakeMemoryLesson(_JsonContract):
    lesson_id: str
    lesson_version: int
    supersedes_lesson_id: str | None
    proposed_by: str
    proposed_at_utc: str
    title: str
    scope: LessonScope
    warning_text: str
    recommended_audit_check: str
    supporting_source_ids: tuple[str, ...]
    counterexample_source_ids: tuple[str, ...]
    possible_duplicate_lesson_ids: tuple[str, ...]
    human_exception_reason: str | None = None
    initial_status: LessonStatus = LessonStatus.PROPOSED
    deduplication_key: str = ""
    schema_version: str = LESSON_SCHEMA_VERSION
    policy_version: str = MISTAKE_MEMORY_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "lesson_id", "proposed_by", "title", "warning_text",
            "recommended_audit_check", "schema_version", "policy_version",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        if not isinstance(self.lesson_version, int) or isinstance(self.lesson_version, bool) or self.lesson_version < 1:
            raise ValueError("lesson_version must be a positive integer.")
        object.__setattr__(self, "supersedes_lesson_id", _optional_text(self.supersedes_lesson_id, field_name="supersedes_lesson_id"))
        if self.supersedes_lesson_id == self.lesson_id:
            raise ValueError("A lesson cannot supersede itself.")
        object.__setattr__(self, "proposed_at_utc", _normalize_utc(self.proposed_at_utc, field_name="proposed_at_utc"))
        if isinstance(self.scope, Mapping):
            object.__setattr__(self, "scope", LessonScope.from_dict(self.scope))
        elif not isinstance(self.scope, LessonScope):
            raise TypeError("scope must be a LessonScope.")
        for name in (
            "supporting_source_ids", "counterexample_source_ids",
            "possible_duplicate_lesson_ids",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=True))
        if set(self.supporting_source_ids) & set(self.counterexample_source_ids):
            raise ValueError("A lesson source cannot be both supporting and a counterexample.")
        if not self.supporting_source_ids:
            raise ValueError("A proposed lesson requires at least one supporting source ID.")
        if self.lesson_id in self.possible_duplicate_lesson_ids:
            raise ValueError("A lesson cannot identify itself as a possible duplicate.")
        object.__setattr__(self, "human_exception_reason", _optional_text(self.human_exception_reason, field_name="human_exception_reason"))
        object.__setattr__(self, "initial_status", _enum_value(self.initial_status, LessonStatus, field_name="initial_status"))
        if self.initial_status is not LessonStatus.PROPOSED:
            raise ValueError("A lesson record must begin as proposed; later status is event-derived.")
        if self.deduplication_key:
            object.__setattr__(self, "deduplication_key", _require_hash(self.deduplication_key, field_name="deduplication_key"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "MistakeMemoryLesson":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        scope_value = values.get("scope")
        scope = LessonScope.from_dict(scope_value) if isinstance(scope_value, Mapping) else scope_value
        if not isinstance(scope, LessonScope):
            raise TypeError("scope must be supplied to create a lesson.")
        values["scope"] = scope
        expected_deduplication_key = lesson_deduplication_key(
            scope=scope,
            warning_text=values.get("warning_text"),
            recommended_audit_check=values.get("recommended_audit_check"),
        )
        supplied_key = values.pop("deduplication_key", "")
        if supplied_key and supplied_key != expected_deduplication_key:
            raise ValueError("Supplied lesson deduplication_key does not match.")
        values["deduplication_key"] = expected_deduplication_key
        if not values.get("lesson_id"):
            seed = {
                "deduplication_key": expected_deduplication_key,
                "supporting_source_ids": values.get("supporting_source_ids", ()),
                "lesson_version": values.get("lesson_version", 1),
                "supersedes_lesson_id": values.get("supersedes_lesson_id"),
                "proposed_by": values.get("proposed_by"),
                "proposed_at_utc": values.get("proposed_at_utc"),
            }
            values["lesson_id"] = f"mistake_lesson_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=mistake_memory_lesson_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "MistakeMemoryLesson":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != mistake_memory_lesson_content_hash(result):
            raise ValueError("MistakeMemoryLesson content_hash does not match its payload.")
        return result


def lesson_deduplication_key(
    *, scope: LessonScope | Mapping[str, Any], warning_text: Any, recommended_audit_check: Any
) -> str:
    normalized_scope = LessonScope.from_dict(scope) if isinstance(scope, Mapping) else scope
    if not isinstance(normalized_scope, LessonScope):
        raise TypeError("scope must be a LessonScope.")
    return canonical_sha256(
        {
            "scope": normalized_scope.to_dict(),
            "warning_text": _require_text(warning_text, field_name="warning_text").casefold(),
            "recommended_audit_check": _require_text(recommended_audit_check, field_name="recommended_audit_check").casefold(),
            "policy_version": MISTAKE_MEMORY_POLICY_VERSION,
        }
    )


def mistake_memory_lesson_content_hash(value: MistakeMemoryLesson | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, MistakeMemoryLesson) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class LessonSource(_JsonContract):
    source_id: str
    lesson_id: str
    lesson_content_hash: str
    source_role: LessonSourceRole
    review_id: str
    review_content_hash: str
    evaluation_id: str
    evaluation_content_hash: str
    forecast_id: str
    forecast_content_hash: str
    symbol: str
    reviewed_at_utc: str
    evaluated_through_utc: str
    added_by: str
    added_at_utc: str
    notes: str | None = None
    schema_version: str = LESSON_SOURCE_SCHEMA_VERSION
    policy_version: str = MISTAKE_MEMORY_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "source_id", "lesson_id", "review_id", "evaluation_id", "forecast_id",
            "symbol", "added_by", "schema_version", "policy_version",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        for name in (
            "lesson_content_hash", "review_content_hash", "evaluation_content_hash",
            "forecast_content_hash",
        ):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "source_role", _enum_value(self.source_role, LessonSourceRole, field_name="source_role"))
        for name in ("reviewed_at_utc", "evaluated_through_utc", "added_at_utc"):
            object.__setattr__(self, name, _normalize_utc(getattr(self, name), field_name=name))
        if _utc(self.reviewed_at_utc) < _utc(self.evaluated_through_utc):
            raise ValueError("A human review cannot predate the evaluated outcome horizon.")
        if _utc(self.added_at_utc) < _utc(self.reviewed_at_utc):
            raise ValueError("A lesson source cannot be added before its review.")
        object.__setattr__(self, "notes", _optional_text(self.notes, field_name="notes"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "LessonSource":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("source_id"):
            seed = {
                "lesson_id": values.get("lesson_id"),
                "review_id": values.get("review_id"),
                "source_role": _json_value(values.get("source_role")),
            }
            values["source_id"] = f"mistake_source_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=lesson_source_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "LessonSource":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != lesson_source_content_hash(result):
            raise ValueError("LessonSource content_hash does not match its payload.")
        return result


def lesson_source_content_hash(value: LessonSource | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, LessonSource) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class LessonEvent(_JsonContract):
    event_id: str
    lesson_id: str
    lesson_content_hash: str
    sequence_number: int
    parent_event_id: str | None
    from_status: LessonStatus | None
    to_status: LessonStatus
    actor_reference: str
    recorded_at_utc: str
    reason: str
    source_ids_considered: tuple[str, ...]
    related_lesson_id: str | None = None
    human_exception_reason: str | None = None
    schema_version: str = LESSON_EVENT_SCHEMA_VERSION
    policy_version: str = MISTAKE_MEMORY_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("event_id", "lesson_id", "actor_reference", "reason", "schema_version", "policy_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "lesson_content_hash", _require_hash(self.lesson_content_hash, field_name="lesson_content_hash"))
        if not isinstance(self.sequence_number, int) or isinstance(self.sequence_number, bool) or self.sequence_number < 1:
            raise ValueError("sequence_number must be a positive integer.")
        object.__setattr__(self, "parent_event_id", _optional_text(self.parent_event_id, field_name="parent_event_id"))
        object.__setattr__(self, "from_status", None if self.from_status is None else _enum_value(self.from_status, LessonStatus, field_name="from_status"))
        object.__setattr__(self, "to_status", _enum_value(self.to_status, LessonStatus, field_name="to_status"))
        if self.to_status not in _ALLOWED_LESSON_TRANSITIONS.get(self.from_status, frozenset()):
            raise ValueError(f"Illegal lesson transition: {self.from_status!r} -> {self.to_status.value!r}.")
        if self.sequence_number == 1:
            if self.parent_event_id is not None or self.from_status is not None or self.to_status is not LessonStatus.PROPOSED:
                raise ValueError("The first lesson event must establish proposed status.")
        elif self.parent_event_id is None or self.from_status is None:
            raise ValueError("Every later lesson event requires a parent and from_status.")
        object.__setattr__(self, "recorded_at_utc", _normalize_utc(self.recorded_at_utc, field_name="recorded_at_utc"))
        object.__setattr__(self, "source_ids_considered", _string_tuple(self.source_ids_considered, field_name="source_ids_considered", sort_unique=True))
        object.__setattr__(self, "related_lesson_id", _optional_text(self.related_lesson_id, field_name="related_lesson_id"))
        object.__setattr__(self, "human_exception_reason", _optional_text(self.human_exception_reason, field_name="human_exception_reason"))
        if self.to_status is LessonStatus.SUPERSEDED and not self.related_lesson_id:
            raise ValueError("A supersession event requires related_lesson_id.")
        if self.to_status is not LessonStatus.SUPERSEDED and self.related_lesson_id:
            raise ValueError("related_lesson_id is reserved for supersession events.")
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "LessonEvent":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("event_id"):
            seed = {
                "lesson_id": values.get("lesson_id"),
                "sequence_number": values.get("sequence_number"),
                "parent_event_id": values.get("parent_event_id"),
                "to_status": _json_value(values.get("to_status")),
                "actor_reference": values.get("actor_reference"),
                "recorded_at_utc": values.get("recorded_at_utc"),
            }
            values["event_id"] = f"mistake_event_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=lesson_event_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "LessonEvent":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != lesson_event_content_hash(result):
            raise ValueError("LessonEvent content_hash does not match its payload.")
        return result


def lesson_event_content_hash(value: LessonEvent | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, LessonEvent) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class LessonRetrievalQuery(_JsonContract):
    query_id: str
    analysis_cutoff_utc: str
    blind_mode: bool
    current_forecast_id: str | None = None
    symbol: str | None = None
    asset_class: str | None = None
    timeframe: str | None = None
    degree: str | None = None
    wave_role: str | None = None
    pattern_family: str | None = None
    direction: str | None = None
    indicator_regimes: tuple[str, ...] = ()
    market_regimes: tuple[str, ...] = ()
    schema_version: str = LESSON_RETRIEVAL_QUERY_SCHEMA_VERSION
    policy_version: str = MISTAKE_MEMORY_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_id", _require_text(self.query_id, field_name="query_id"))
        object.__setattr__(self, "analysis_cutoff_utc", _normalize_utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        if not isinstance(self.blind_mode, bool):
            raise TypeError("blind_mode must be boolean.")
        for name in (
            "current_forecast_id", "symbol", "asset_class", "timeframe", "degree",
            "wave_role", "pattern_family", "direction",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "indicator_regimes", _string_tuple(self.indicator_regimes, field_name="indicator_regimes", sort_unique=True))
        object.__setattr__(self, "market_regimes", _string_tuple(self.market_regimes, field_name="market_regimes", sort_unique=True))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "LessonRetrievalQuery":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("query_id"):
            seed = dict(values)
            seed.pop("query_id", None)
            values["query_id"] = f"mistake_query_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=lesson_retrieval_query_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "LessonRetrievalQuery":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != lesson_retrieval_query_content_hash(result):
            raise ValueError("LessonRetrievalQuery content_hash does not match its payload.")
        return result


def lesson_retrieval_query_content_hash(value: LessonRetrievalQuery | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, LessonRetrievalQuery) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class RetrievedLesson(_JsonContract):
    retrieval_id: str
    query_id: str
    query_content_hash: str
    rank: int
    lesson_id: str
    lesson_content_hash: str
    retrieval_scope: LessonScopeType
    effective_event_id: str
    effective_event_content_hash: str
    approved_at_utc: str
    matched_dimensions: tuple[str, ...]
    matching_reasons: tuple[str, ...]
    source_ids: tuple[str, ...]
    source_provenance: tuple[Mapping[str, Any], ...]
    warning_text: str
    recommended_audit_check: str
    applicability_conditions: tuple[str, ...]
    non_applicability_conditions: tuple[str, ...]
    audit_only: bool = True
    schema_version: str = RETRIEVED_LESSON_SCHEMA_VERSION
    policy_version: str = MISTAKE_MEMORY_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "retrieval_id", "query_id", "lesson_id", "effective_event_id",
            "warning_text", "recommended_audit_check", "schema_version", "policy_version",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        for name in ("query_content_hash", "lesson_content_hash", "effective_event_content_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        if not isinstance(self.rank, int) or isinstance(self.rank, bool) or self.rank < 1:
            raise ValueError("rank must be a positive integer.")
        object.__setattr__(self, "retrieval_scope", _enum_value(self.retrieval_scope, LessonScopeType, field_name="retrieval_scope"))
        object.__setattr__(self, "approved_at_utc", _normalize_utc(self.approved_at_utc, field_name="approved_at_utc"))
        for name in (
            "matched_dimensions", "matching_reasons", "source_ids",
            "applicability_conditions", "non_applicability_conditions",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=name in {"matched_dimensions", "source_ids"}))
        if isinstance(self.source_provenance, (str, bytes)) or not isinstance(self.source_provenance, Sequence):
            raise TypeError("source_provenance must be a sequence of mappings.")
        provenance: list[Mapping[str, Any]] = []
        for item in self.source_provenance:
            if not isinstance(item, Mapping):
                raise TypeError("source_provenance items must be mappings.")
            provenance.append(_freeze_json(item))
        object.__setattr__(self, "source_provenance", tuple(provenance))
        if not isinstance(self.audit_only, bool) or not self.audit_only:
            raise ValueError("Retrieved lessons are audit-only and cannot resolve wave counts.")
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "RetrievedLesson":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("retrieval_id"):
            seed = {
                "query_id": values.get("query_id"),
                "lesson_id": values.get("lesson_id"),
                "rank": values.get("rank"),
            }
            values["retrieval_id"] = f"retrieved_lesson_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=retrieved_lesson_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "RetrievedLesson":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != retrieved_lesson_content_hash(result):
            raise ValueError("RetrievedLesson content_hash does not match its payload.")
        return result


def retrieved_lesson_content_hash(value: RetrievedLesson | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, RetrievedLesson) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _hypothesis_outcome(
    evaluation: ForecastOutcomeEvaluation, hypothesis_id: str
) -> Any:
    candidates = (
        evaluation.main_hypothesis_outcome,
        *evaluation.alternative_hypothesis_outcomes,
    )
    for item in candidates:
        if item.hypothesis_id == hypothesis_id:
            return item
    raise ValueError(
        f"Hypothesis {hypothesis_id!r} is not present in evaluation "
        f"{evaluation.evaluation_id}."
    )


def _evaluation_reference_ids(
    evaluation: ForecastOutcomeEvaluation,
) -> frozenset[str]:
    result = {
        evaluation.evaluation_id,
        evaluation.forecast_id,
        evaluation.observation_set_id,
        *evaluation.lower_timeframe_observation_set_ids,
    }
    for hypothesis in (
        evaluation.main_hypothesis_outcome,
        *evaluation.alternative_hypothesis_outcomes,
    ):
        result.add(hypothesis.hypothesis_id)
        result.update(hypothesis.supporting_candle_ids)
        for claim in hypothesis.claim_outcomes:
            result.add(claim.claim_id)
            result.update(claim.supporting_candle_ids)
    return frozenset(result)


def validate_forecast_outcome_review(
    review: ForecastOutcomeReview,
    *,
    evaluation: ForecastOutcomeEvaluation,
    parent_review: ForecastOutcomeReview | None = None,
    superseding_evaluation: ForecastOutcomeEvaluation | None = None,
) -> tuple[str, ...]:
    """Validate a review without granting it power to rewrite Phase 11B."""

    errors: list[str] = []
    if review.schema_version != OUTCOME_REVIEW_SCHEMA_VERSION:
        errors.append("Unsupported outcome-review schema_version.")
    if review.policy_version != MISTAKE_MEMORY_POLICY_VERSION:
        errors.append("Unsupported outcome-review policy_version.")
    if review.content_hash != forecast_outcome_review_content_hash(review):
        errors.append("Outcome-review content_hash does not match its payload.")
    if evaluation.content_hash != review.evaluation_content_hash:
        errors.append("Outcome review references a different evaluation hash.")
    if evaluation.evaluation_id != review.evaluation_id:
        errors.append("Outcome review references a different evaluation ID.")
    if evaluation.forecast_id != review.forecast_id:
        errors.append("Outcome review references a different forecast ID.")
    if evaluation.forecast_content_hash != review.forecast_content_hash:
        errors.append("Outcome review references a different forecast hash.")
    if _utc(review.reviewed_at_utc) < _utc(evaluation.evaluated_through_utc):
        errors.append("An outcome review cannot predate the evaluated outcome horizon.")
    try:
        hypothesis = _hypothesis_outcome(evaluation, review.reviewed_hypothesis_id)
    except ValueError as exc:
        errors.append(str(exc))
        hypothesis = None
    if hypothesis is not None and hypothesis.outcome_status is not review.reviewed_outcome_status:
        errors.append("The reviewed outcome status differs from the deterministic evaluation.")
    references = _evaluation_reference_ids(evaluation)
    unknown_review_refs = set(review.evidence_reference_ids) - references
    if unknown_review_refs:
        errors.append(
            "Outcome review contains unknown evidence references: "
            + ", ".join(sorted(unknown_review_refs))
            + "."
        )
    hypothesis_ids = {
        evaluation.main_hypothesis_outcome.hypothesis_id,
        *(item.hypothesis_id for item in evaluation.alternative_hypothesis_outcomes),
    }
    for diagnosis in review.diagnoses:
        unknown = (
            set(diagnosis.affected_hypothesis_ids)
            | set(diagnosis.affected_claim_ids)
            | set(diagnosis.evidence_reference_ids)
        ) - references
        if unknown:
            errors.append(
                f"Diagnosis {diagnosis.diagnosis_id} contains unknown references: "
                + ", ".join(sorted(unknown))
                + "."
            )
        if set(diagnosis.affected_hypothesis_ids) - hypothesis_ids:
            errors.append(
                f"Diagnosis {diagnosis.diagnosis_id} references an unknown hypothesis."
            )
    if parent_review is None:
        if review.review_version != 1 or review.parent_review_id is not None:
            errors.append("An initial review must have version 1 and no parent.")
    else:
        if review.parent_review_id != parent_review.review_id:
            errors.append("Review revision does not reference the supplied parent.")
        if review.review_version != parent_review.review_version + 1:
            errors.append("Review revision must increment its parent version by one.")
        if review.forecast_id != parent_review.forecast_id:
            errors.append("A review revision must remain in the same forecast lineage.")
        if _utc(review.reviewed_at_utc) < _utc(parent_review.reviewed_at_utc):
            errors.append("A review revision cannot predate its parent.")
        if review.evaluation_id != parent_review.evaluation_id:
            if (
                parent_review.required_superseding_evaluation_id
                != review.evaluation_id
            ):
                errors.append(
                    "A review may move only to the superseding evaluation required by its parent."
                )
    if review.deterministic_scoring_error:
        if superseding_evaluation is None:
            errors.append(
                "A deterministic scoring error requires a new supplied superseding evaluation."
            )
        else:
            if review.required_superseding_evaluation_id != superseding_evaluation.evaluation_id:
                errors.append("Required superseding evaluation ID does not match.")
            if superseding_evaluation.supersedes_evaluation_id != evaluation.evaluation_id:
                errors.append("The supplied evaluation does not supersede the reviewed evaluation.")
            if superseding_evaluation.forecast_id != evaluation.forecast_id:
                errors.append("A scoring correction must remain in the same forecast lineage.")
            if superseding_evaluation.evaluation_version != evaluation.evaluation_version + 1:
                errors.append("A scoring correction must increment the evaluation version by one.")
    elif superseding_evaluation is not None:
        errors.append("A superseding evaluation is valid only for a declared scoring error.")
    required_hashes = {
        "forecast_outcome_evaluation": evaluation.content_hash,
        "forecast_record": evaluation.forecast_content_hash,
    }
    if review.deterministic_scoring_error and superseding_evaluation is not None:
        required_hashes["required_superseding_evaluation"] = superseding_evaluation.content_hash
    for key, expected in required_hashes.items():
        if review.source_hashes.get(key) != expected:
            errors.append(f"Outcome-review source_hashes[{key!r}] does not match.")
    return tuple(errors)


def create_forecast_outcome_review(
    evaluation: ForecastOutcomeEvaluation,
    *,
    reviewer_reference: str,
    reviewed_at_utc: str,
    decision: ReviewDecision | str,
    notes: str,
    reviewed_hypothesis_id: str | None = None,
    diagnoses: Sequence[FailureDiagnosis | Mapping[str, Any]] = (),
    corrected_interpretation: str | None = None,
    evidence_reference_ids: Sequence[str] = (),
    parent_review: ForecastOutcomeReview | None = None,
    deterministic_scoring_error: bool = False,
    superseding_evaluation: ForecastOutcomeEvaluation | None = None,
    review_id: str | None = None,
) -> ForecastOutcomeReview:
    """Create one human review over a frozen deterministic evaluation."""

    if not isinstance(evaluation, ForecastOutcomeEvaluation):
        raise TypeError("evaluation must be a ForecastOutcomeEvaluation.")
    selected_hypothesis_id = reviewed_hypothesis_id or evaluation.main_hypothesis_outcome.hypothesis_id
    hypothesis = _hypothesis_outcome(evaluation, selected_hypothesis_id)
    source_hashes = {
        "forecast_outcome_evaluation": evaluation.content_hash,
        "forecast_record": evaluation.forecast_content_hash,
    }
    if superseding_evaluation is not None:
        source_hashes["required_superseding_evaluation"] = superseding_evaluation.content_hash
    review = ForecastOutcomeReview.create(
        review_id=review_id or "",
        review_version=(parent_review.review_version + 1 if parent_review else 1),
        parent_review_id=(parent_review.review_id if parent_review else None),
        evaluation_id=evaluation.evaluation_id,
        evaluation_content_hash=evaluation.content_hash,
        forecast_id=evaluation.forecast_id,
        forecast_content_hash=evaluation.forecast_content_hash,
        reviewed_hypothesis_id=selected_hypothesis_id,
        reviewed_outcome_status=hypothesis.outcome_status,
        reviewer_reference=reviewer_reference,
        reviewed_at_utc=reviewed_at_utc,
        decision=decision,
        diagnoses=tuple(diagnoses),
        corrected_interpretation=corrected_interpretation,
        evidence_reference_ids=tuple(evidence_reference_ids),
        notes=notes,
        deterministic_scoring_error=deterministic_scoring_error,
        required_superseding_evaluation_id=(
            superseding_evaluation.evaluation_id if superseding_evaluation else None
        ),
        source_hashes=source_hashes,
    )
    errors = validate_forecast_outcome_review(
        review,
        evaluation=evaluation,
        parent_review=parent_review,
        superseding_evaluation=superseding_evaluation,
    )
    if errors:
        raise ValueError("Invalid ForecastOutcomeReview: " + " ".join(errors))
    return review


def revise_forecast_outcome_review(
    parent_review: ForecastOutcomeReview,
    evaluation: ForecastOutcomeEvaluation,
    **changes: Any,
) -> ForecastOutcomeReview:
    """Create a linear immutable review revision."""

    return create_forecast_outcome_review(
        evaluation,
        parent_review=parent_review,
        **changes,
    )


# A concise verb for callers presenting an evaluation to a human reviewer.
review_forecast_outcome = create_forecast_outcome_review


def _review_is_eligible_support(
    review: ForecastOutcomeReview, evaluation: ForecastOutcomeEvaluation
) -> tuple[bool, str | None]:
    errors = validate_forecast_outcome_review(review, evaluation=evaluation)
    # Existing revision parents are a persistence concern; source eligibility
    # needs the record and its immutable evaluation linkage only.
    errors = tuple(
        item for item in errors if "initial review" not in item.lower()
    )
    if errors:
        return False, "Review validation failed: " + " ".join(errors)
    if review.decision not in {ReviewDecision.APPROVED, ReviewDecision.REVISED}:
        return False, "Only approved or revised human reviews may support a lesson."
    if review.deterministic_scoring_error:
        return False, "A review awaiting deterministic re-evaluation cannot support a lesson."
    if review.reviewed_outcome_status in _INELIGIBLE_LESSON_OUTCOMES:
        return False, "Unresolved, insufficient, or incomparable outcomes cannot support forecast-error lessons."
    if review.reviewed_outcome_status not in _ELIGIBLE_FORECAST_ERROR_OUTCOMES:
        return False, "Only failed or partial forecast outcomes may support a forecast-error lesson."
    return True, None


def create_lesson_source(
    lesson: MistakeMemoryLesson,
    review: ForecastOutcomeReview,
    evaluation: ForecastOutcomeEvaluation,
    *,
    symbol: str,
    source_role: LessonSourceRole | str,
    added_by: str,
    added_at_utc: str,
    notes: str | None = None,
    source_id: str | None = None,
) -> LessonSource:
    """Create one immutable supporting or counterexample link."""

    if evaluation.evaluation_id != review.evaluation_id or evaluation.content_hash != review.evaluation_content_hash:
        raise ValueError("Lesson source review/evaluation linkage does not match.")
    if lesson.content_hash != mistake_memory_lesson_content_hash(lesson):
        raise ValueError("Lesson content hash does not match.")
    role = _enum_value(source_role, LessonSourceRole, field_name="source_role")
    if role is LessonSourceRole.SUPPORTING:
        eligible, reason = _review_is_eligible_support(review, evaluation)
        if not eligible:
            raise ValueError(reason)
    elif review.decision not in {ReviewDecision.APPROVED, ReviewDecision.REVISED}:
        raise ValueError("A counterexample requires an approved or revised human review.")
    return LessonSource.create(
        source_id=source_id or "",
        lesson_id=lesson.lesson_id,
        lesson_content_hash=lesson.content_hash,
        source_role=role,
        review_id=review.review_id,
        review_content_hash=review.content_hash,
        evaluation_id=evaluation.evaluation_id,
        evaluation_content_hash=evaluation.content_hash,
        forecast_id=evaluation.forecast_id,
        forecast_content_hash=evaluation.forecast_content_hash,
        symbol=symbol,
        reviewed_at_utc=review.reviewed_at_utc,
        evaluated_through_utc=evaluation.evaluated_through_utc,
        added_by=added_by,
        added_at_utc=added_at_utc,
        notes=notes,
    )


def _planned_source_id(
    *, lesson_id: str, review_id: str, source_role: LessonSourceRole
) -> str:
    return f"mistake_source_{canonical_sha256({'lesson_id': lesson_id, 'review_id': review_id, 'source_role': source_role.value})[:32]}"


def propose_mistake_memory_lesson(
    reviews: Sequence[ForecastOutcomeReview],
    evaluations: Mapping[str, ForecastOutcomeEvaluation],
    *,
    forecast_symbols: Mapping[str, str],
    scope: LessonScope | Mapping[str, Any],
    title: str,
    warning_text: str,
    recommended_audit_check: str,
    proposed_by: str,
    proposed_at_utc: str,
    counterexample_reviews: Sequence[ForecastOutcomeReview] = (),
    existing_lessons: Sequence[MistakeMemoryLesson] = (),
    human_exception_reason: str | None = None,
    supersedes_lesson_id: str | None = None,
    lesson_version: int = 1,
    lesson_id: str | None = None,
) -> tuple[MistakeMemoryLesson, tuple[LessonSource, ...], LessonEvent]:
    """Propose, but never activate, a lesson from approved reviewed outcomes."""

    normalized_scope = LessonScope.from_dict(scope) if isinstance(scope, Mapping) else scope
    if not isinstance(normalized_scope, LessonScope):
        raise TypeError("scope must be a LessonScope.")
    supporting_reviews = tuple(reviews)
    counter_reviews = tuple(counterexample_reviews)
    if not supporting_reviews:
        raise ValueError("At least one supporting review is required.")
    all_reviews = supporting_reviews + counter_reviews
    if len({item.review_id for item in all_reviews}) != len(all_reviews):
        raise ValueError("Lesson proposal reviews must be unique.")
    for review in supporting_reviews:
        evaluation = evaluations.get(review.evaluation_id)
        if evaluation is None:
            raise ValueError(f"Evaluation {review.evaluation_id} is unavailable.")
        eligible, reason = _review_is_eligible_support(review, evaluation)
        if not eligible:
            raise ValueError(reason)
    for review in counter_reviews:
        evaluation = evaluations.get(review.evaluation_id)
        if evaluation is None:
            raise ValueError(f"Evaluation {review.evaluation_id} is unavailable.")
        if review.decision not in {ReviewDecision.APPROVED, ReviewDecision.REVISED}:
            raise ValueError("Counterexample reviews must be approved or revised.")
    for review in all_reviews:
        if review.forecast_id not in forecast_symbols:
            raise ValueError(f"A symbol is required for forecast {review.forecast_id}.")
    deduplication_key = lesson_deduplication_key(
        scope=normalized_scope,
        warning_text=warning_text,
        recommended_audit_check=recommended_audit_check,
    )
    duplicate_ids = tuple(
        sorted(
            item.lesson_id
            for item in existing_lessons
            if item.deduplication_key == deduplication_key
        )
    )
    resolved_lesson_id = lesson_id or (
        "mistake_lesson_"
        + canonical_sha256(
            {
                "deduplication_key": deduplication_key,
                "review_ids": sorted(item.review_id for item in all_reviews),
                "lesson_version": lesson_version,
                "supersedes_lesson_id": supersedes_lesson_id,
                "proposed_by": proposed_by,
                "proposed_at_utc": proposed_at_utc,
            }
        )[:32]
    )
    supporting_ids = tuple(
        sorted(
            _planned_source_id(
                lesson_id=resolved_lesson_id,
                review_id=item.review_id,
                source_role=LessonSourceRole.SUPPORTING,
            )
            for item in supporting_reviews
        )
    )
    counterexample_ids = tuple(
        sorted(
            _planned_source_id(
                lesson_id=resolved_lesson_id,
                review_id=item.review_id,
                source_role=LessonSourceRole.COUNTEREXAMPLE,
            )
            for item in counter_reviews
        )
    )
    lesson = MistakeMemoryLesson.create(
        lesson_id=resolved_lesson_id,
        lesson_version=lesson_version,
        supersedes_lesson_id=supersedes_lesson_id,
        proposed_by=proposed_by,
        proposed_at_utc=proposed_at_utc,
        title=title,
        scope=normalized_scope,
        warning_text=warning_text,
        recommended_audit_check=recommended_audit_check,
        supporting_source_ids=supporting_ids,
        counterexample_source_ids=counterexample_ids,
        possible_duplicate_lesson_ids=duplicate_ids,
        human_exception_reason=human_exception_reason,
        deduplication_key=deduplication_key,
    )
    sources: list[LessonSource] = []
    for role, selected_reviews in (
        (LessonSourceRole.SUPPORTING, supporting_reviews),
        (LessonSourceRole.COUNTEREXAMPLE, counter_reviews),
    ):
        for review in selected_reviews:
            sources.append(
                create_lesson_source(
                    lesson,
                    review,
                    evaluations[review.evaluation_id],
                    symbol=forecast_symbols[review.forecast_id],
                    source_role=role,
                    added_by=proposed_by,
                    added_at_utc=proposed_at_utc,
                    source_id=_planned_source_id(
                        lesson_id=lesson.lesson_id,
                        review_id=review.review_id,
                        source_role=role,
                    ),
                )
            )
    event = LessonEvent.create(
        lesson_id=lesson.lesson_id,
        lesson_content_hash=lesson.content_hash,
        sequence_number=1,
        parent_event_id=None,
        from_status=None,
        to_status=LessonStatus.PROPOSED,
        actor_reference=proposed_by,
        recorded_at_utc=proposed_at_utc,
        reason="Human proposed this audit lesson; no activation occurred.",
        source_ids_considered=tuple(item.source_id for item in sources),
    )
    return lesson, tuple(sorted(sources, key=lambda item: item.source_id)), event


def validate_lesson_event_chain(
    lesson: MistakeMemoryLesson,
    events: Sequence[LessonEvent | Mapping[str, Any]],
) -> tuple[str, ...]:
    errors: list[str] = []
    normalized: list[LessonEvent] = []
    for item in events:
        try:
            event = item if isinstance(item, LessonEvent) else LessonEvent.from_dict(item)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        normalized.append(event)
    normalized.sort(key=lambda item: (item.sequence_number, item.event_id))
    previous: LessonEvent | None = None
    seen_ids: set[str] = set()
    for expected_sequence, event in enumerate(normalized, start=1):
        if event.event_id in seen_ids:
            errors.append(f"Duplicate lesson event ID {event.event_id}.")
        seen_ids.add(event.event_id)
        if event.lesson_id != lesson.lesson_id or event.lesson_content_hash != lesson.content_hash:
            errors.append(f"Lesson event {event.event_id} references a different lesson.")
        if event.sequence_number != expected_sequence:
            errors.append(f"Lesson event {event.event_id} has a noncontiguous sequence.")
        expected_parent = previous.event_id if previous else None
        expected_status = previous.to_status if previous else None
        if event.parent_event_id != expected_parent:
            errors.append(f"Lesson event {event.event_id} breaks the parent chain.")
        if event.from_status is not expected_status:
            errors.append(f"Lesson event {event.event_id} breaks the status chain.")
        if previous and _utc(event.recorded_at_utc) < _utc(previous.recorded_at_utc):
            errors.append(f"Lesson event {event.event_id} predates its parent.")
        previous = event
    return tuple(errors)


def lesson_current_status(
    lesson: MistakeMemoryLesson,
    events: Sequence[LessonEvent | Mapping[str, Any]],
    *,
    at_or_before_utc: str | None = None,
) -> LessonStatus | None:
    errors = validate_lesson_event_chain(lesson, events)
    if errors:
        raise ValueError("Invalid lesson event chain: " + " ".join(errors))
    normalized = tuple(
        item if isinstance(item, LessonEvent) else LessonEvent.from_dict(item)
        for item in events
    )
    if at_or_before_utc is not None:
        cutoff = _utc(_normalize_utc(at_or_before_utc, field_name="at_or_before_utc"))
        normalized = tuple(item for item in normalized if _utc(item.recorded_at_utc) <= cutoff)
    return normalized[-1].to_status if normalized else None


def _validate_source_for_lesson(
    source: LessonSource, lesson: MistakeMemoryLesson
) -> tuple[str, ...]:
    errors: list[str] = []
    if source.content_hash != lesson_source_content_hash(source):
        errors.append(f"Lesson source {source.source_id} hash does not match.")
    if source.lesson_id != lesson.lesson_id or source.lesson_content_hash != lesson.content_hash:
        errors.append(f"Lesson source {source.source_id} references a different lesson.")
    return tuple(errors)


def _activation_errors(
    lesson: MistakeMemoryLesson,
    sources: Sequence[LessonSource],
    *,
    human_exception_reason: str | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    supporting = tuple(
        item for item in sources if item.source_role is LessonSourceRole.SUPPORTING
    )
    if not supporting:
        errors.append("Lesson activation requires at least one supporting source.")
        return tuple(errors)
    distinct_forecasts = {item.forecast_id for item in supporting}
    distinct_symbols = {item.symbol.casefold() for item in supporting}
    scope_type = lesson.scope.scope_type
    if scope_type is LessonScopeType.EXACT_CASE:
        if len(distinct_forecasts) != 1:
            errors.append("An exact-case warning must remain tied to one forecast source.")
        if not distinct_forecasts.issubset(set(lesson.scope.exact_forecast_ids)):
            errors.append("Exact-case source forecast is outside the lesson scope.")
    elif scope_type is LessonScopeType.SCOPED:
        if len(distinct_forecasts) < 2:
            errors.append(
                "A lesson supported by one forecast may activate only as exact_case; "
                "a scoped lesson requires two independent forecasts."
            )
    elif len(distinct_forecasts) < 3 or len(distinct_symbols) < 2:
        if not _optional_text(human_exception_reason, field_name="human_exception_reason"):
            errors.append(
                "A general lesson requires three independent forecasts across two symbols "
                "or an explicit human exception reason."
            )
    return tuple(errors)


def build_lesson_event(
    lesson: MistakeMemoryLesson,
    existing_events: Sequence[LessonEvent | Mapping[str, Any]],
    sources: Sequence[LessonSource],
    *,
    to_status: LessonStatus | str,
    actor_reference: str,
    recorded_at_utc: str,
    reason: str,
    related_lesson: MistakeMemoryLesson | None = None,
    human_exception_reason: str | None = None,
) -> LessonEvent:
    """Build a human-authored transition after deterministic policy checks."""

    chain_errors = validate_lesson_event_chain(lesson, existing_events)
    if chain_errors:
        raise ValueError("Invalid lesson event chain: " + " ".join(chain_errors))
    normalized_events = sorted(
        (
            item if isinstance(item, LessonEvent) else LessonEvent.from_dict(item)
            for item in existing_events
        ),
        key=lambda item: item.sequence_number,
    )
    if not normalized_events:
        raise ValueError("Use proposal creation to establish the first lesson event.")
    current = normalized_events[-1]
    target = _enum_value(to_status, LessonStatus, field_name="to_status")
    normalized_sources = tuple(sources)
    if len({item.source_id for item in normalized_sources}) != len(normalized_sources):
        raise ValueError("Lesson event sources must be unique.")
    source_errors = tuple(
        error
        for source in normalized_sources
        for error in _validate_source_for_lesson(source, lesson)
    )
    if source_errors:
        raise ValueError("Invalid lesson source: " + " ".join(source_errors))
    recorded = _normalize_utc(recorded_at_utc, field_name="recorded_at_utc")
    if _utc(recorded) < _utc(current.recorded_at_utc):
        raise ValueError("A lesson event cannot predate its parent.")
    if any(_utc(source.added_at_utc) > _utc(recorded) for source in normalized_sources):
        raise ValueError("A lesson event cannot consider a source added in the future.")
    if target is LessonStatus.ACTIVE:
        activation_errors = _activation_errors(
            lesson,
            normalized_sources,
            human_exception_reason=human_exception_reason,
        )
        if activation_errors:
            raise ValueError("Lesson activation failed: " + " ".join(activation_errors))
    if target is LessonStatus.SUPERSEDED:
        if related_lesson is None:
            raise ValueError("Lesson supersession requires the replacement lesson.")
        if related_lesson.supersedes_lesson_id != lesson.lesson_id:
            raise ValueError("Replacement lesson does not explicitly supersede this lesson.")
        if related_lesson.lesson_version != lesson.lesson_version + 1:
            raise ValueError("Replacement lesson version must increment by one.")
    elif related_lesson is not None:
        raise ValueError("A related lesson is accepted only for supersession.")
    event = LessonEvent.create(
        lesson_id=lesson.lesson_id,
        lesson_content_hash=lesson.content_hash,
        sequence_number=current.sequence_number + 1,
        parent_event_id=current.event_id,
        from_status=current.to_status,
        to_status=target,
        actor_reference=actor_reference,
        recorded_at_utc=recorded,
        reason=reason,
        source_ids_considered=tuple(item.source_id for item in normalized_sources),
        related_lesson_id=(related_lesson.lesson_id if related_lesson else None),
        human_exception_reason=human_exception_reason,
    )
    final_errors = validate_lesson_event_chain(
        lesson, (*normalized_events, event)
    )
    if final_errors:
        raise ValueError("Invalid lesson transition: " + " ".join(final_errors))
    return event


def approve_lesson(
    lesson: MistakeMemoryLesson,
    existing_events: Sequence[LessonEvent | Mapping[str, Any]],
    sources: Sequence[LessonSource],
    **human_action: Any,
) -> LessonEvent:
    return build_lesson_event(
        lesson,
        existing_events,
        sources,
        to_status=LessonStatus.ACTIVE,
        **human_action,
    )


def reject_lesson(
    lesson: MistakeMemoryLesson,
    existing_events: Sequence[LessonEvent | Mapping[str, Any]],
    sources: Sequence[LessonSource],
    **human_action: Any,
) -> LessonEvent:
    return build_lesson_event(
        lesson,
        existing_events,
        sources,
        to_status=LessonStatus.REJECTED,
        **human_action,
    )


def supersede_lesson(
    lesson: MistakeMemoryLesson,
    existing_events: Sequence[LessonEvent | Mapping[str, Any]],
    sources: Sequence[LessonSource],
    *,
    replacement_lesson: MistakeMemoryLesson,
    **human_action: Any,
) -> LessonEvent:
    return build_lesson_event(
        lesson,
        existing_events,
        sources,
        to_status=LessonStatus.SUPERSEDED,
        related_lesson=replacement_lesson,
        **human_action,
    )


def retire_lesson(
    lesson: MistakeMemoryLesson,
    existing_events: Sequence[LessonEvent | Mapping[str, Any]],
    sources: Sequence[LessonSource],
    **human_action: Any,
) -> LessonEvent:
    return build_lesson_event(
        lesson,
        existing_events,
        sources,
        to_status=LessonStatus.RETIRED,
        **human_action,
    )


def _scope_match(
    scope: LessonScope, query: LessonRetrievalQuery
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    matched: list[str] = []
    reasons: list[str] = []
    scalar_checks = (
        ("exact_forecast_id", scope.exact_forecast_ids, query.current_forecast_id),
        ("symbol", scope.symbols, query.symbol),
        ("asset_class", scope.asset_classes, query.asset_class),
        ("timeframe", scope.timeframes, query.timeframe),
        ("degree", scope.degrees, query.degree),
        ("wave_role", scope.wave_roles, query.wave_role),
        ("pattern_family", scope.pattern_families, query.pattern_family),
        ("direction", scope.directions, query.direction),
    )
    for name, allowed, actual in scalar_checks:
        if not allowed:
            continue
        if actual is None or actual.casefold() not in {item.casefold() for item in allowed}:
            return False, (), ()
        matched.append(name)
        reasons.append(f"{name} matched the approved lesson scope.")
    set_checks = (
        ("indicator_regime", scope.indicator_regimes, query.indicator_regimes),
        ("market_regime", scope.market_regimes, query.market_regimes),
    )
    for name, allowed, actual in set_checks:
        if not allowed:
            continue
        overlap = {item.casefold() for item in allowed} & {
            item.casefold() for item in actual
        }
        if not overlap:
            return False, (), ()
        matched.append(name)
        reasons.append(f"{name} had an explicit compatible value.")
    if scope.applicability_conditions:
        reasons.append("Applicability conditions remain a human audit check; they were not auto-inferred.")
    if scope.non_applicability_conditions:
        reasons.append("Non-applicability conditions must be checked by the human auditor.")
    return True, tuple(sorted(matched)), tuple(reasons)


def retrieve_applicable_lessons(
    query: LessonRetrievalQuery,
    *,
    lessons: Sequence[MistakeMemoryLesson],
    sources: Sequence[LessonSource],
    events: Sequence[LessonEvent],
) -> tuple[RetrievedLesson, ...]:
    """Retrieve active cutoff-valid lessons as audit warnings only."""

    if query.content_hash != lesson_retrieval_query_content_hash(query):
        raise ValueError("Lesson retrieval query hash does not match.")
    if query.blind_mode:
        return ()
    cutoff = _utc(query.analysis_cutoff_utc)
    by_lesson_events: dict[str, list[LessonEvent]] = {}
    for event in events:
        by_lesson_events.setdefault(event.lesson_id, []).append(event)
    by_lesson_sources: dict[str, list[LessonSource]] = {}
    for source in sources:
        by_lesson_sources.setdefault(source.lesson_id, []).append(source)
    candidates: list[
        tuple[int, int, str, MistakeMemoryLesson, LessonEvent, tuple[str, ...], tuple[str, ...], tuple[LessonSource, ...]]
    ] = []
    scope_priority = {
        LessonScopeType.EXACT_CASE: 0,
        LessonScopeType.SCOPED: 1,
        LessonScopeType.GENERAL: 2,
    }
    for lesson in lessons:
        if lesson.content_hash != mistake_memory_lesson_content_hash(lesson):
            raise ValueError(f"Lesson {lesson.lesson_id} hash does not match.")
        if _utc(lesson.proposed_at_utc) > cutoff:
            continue
        lesson_events = sorted(
            by_lesson_events.get(lesson.lesson_id, []),
            key=lambda item: (item.sequence_number, item.event_id),
        )
        chain_errors = validate_lesson_event_chain(lesson, lesson_events)
        if chain_errors:
            raise ValueError(
                f"Lesson {lesson.lesson_id} has an invalid event chain: "
                + " ".join(chain_errors)
            )
        eligible_events = tuple(
            item for item in lesson_events if _utc(item.recorded_at_utc) <= cutoff
        )
        if not eligible_events or eligible_events[-1].to_status is not LessonStatus.ACTIVE:
            continue
        effective_event = eligible_events[-1]
        available_sources = tuple(
            sorted(
                (
                    item
                    for item in by_lesson_sources.get(lesson.lesson_id, [])
                    if _utc(item.added_at_utc) <= cutoff
                    and _utc(item.reviewed_at_utc) <= cutoff
                    and _utc(item.evaluated_through_utc) <= cutoff
                ),
                key=lambda item: item.source_id,
            )
        )
        if set(effective_event.source_ids_considered) - {
            item.source_id for item in available_sources
        }:
            # The approval depended on evidence that did not yet exist at this
            # replay cutoff, so the lesson is not historically available.
            continue
        source_errors = tuple(
            error
            for source in available_sources
            for error in _validate_source_for_lesson(source, lesson)
        )
        if source_errors:
            raise ValueError("Invalid retrieval source: " + " ".join(source_errors))
        matches, dimensions, reasons = _scope_match(lesson.scope, query)
        if not matches:
            continue
        candidates.append(
            (
                scope_priority[lesson.scope.scope_type],
                -len(dimensions),
                lesson.lesson_id,
                lesson,
                effective_event,
                dimensions,
                reasons,
                available_sources,
            )
        )
    candidates.sort(key=lambda item: item[:3])
    result: list[RetrievedLesson] = []
    for rank, candidate in enumerate(candidates, start=1):
        _, _, _, lesson, event, dimensions, reasons, available_sources = candidate
        provenance = tuple(
            {
                "source_id": item.source_id,
                "source_role": item.source_role.value,
                "review_id": item.review_id,
                "review_content_hash": item.review_content_hash,
                "evaluation_id": item.evaluation_id,
                "evaluation_content_hash": item.evaluation_content_hash,
                "forecast_id": item.forecast_id,
                "forecast_content_hash": item.forecast_content_hash,
                "symbol": item.symbol,
                "reviewed_at_utc": item.reviewed_at_utc,
                "evaluated_through_utc": item.evaluated_through_utc,
            }
            for item in available_sources
        )
        result.append(
            RetrievedLesson.create(
                query_id=query.query_id,
                query_content_hash=query.content_hash,
                rank=rank,
                lesson_id=lesson.lesson_id,
                lesson_content_hash=lesson.content_hash,
                retrieval_scope=lesson.scope.scope_type,
                effective_event_id=event.event_id,
                effective_event_content_hash=event.content_hash,
                approved_at_utc=event.recorded_at_utc,
                matched_dimensions=dimensions,
                matching_reasons=reasons,
                source_ids=tuple(item.source_id for item in available_sources),
                source_provenance=provenance,
                warning_text=lesson.warning_text,
                recommended_audit_check=lesson.recommended_audit_check,
                applicability_conditions=lesson.scope.applicability_conditions,
                non_applicability_conditions=lesson.scope.non_applicability_conditions,
            )
        )
    return tuple(result)


def replay_lesson_retrieval(
    query: LessonRetrievalQuery | Mapping[str, Any],
    *,
    lessons: Sequence[MistakeMemoryLesson | Mapping[str, Any]],
    sources: Sequence[LessonSource | Mapping[str, Any]],
    events: Sequence[LessonEvent | Mapping[str, Any]],
    expected_results: Sequence[RetrievedLesson | Mapping[str, Any]] | None = None,
) -> tuple[RetrievedLesson, ...]:
    """Replay retrieval from serialized immutable inputs and optionally verify it."""

    normalized_query = query if isinstance(query, LessonRetrievalQuery) else LessonRetrievalQuery.from_dict(query)
    normalized_lessons = tuple(
        item if isinstance(item, MistakeMemoryLesson) else MistakeMemoryLesson.from_dict(item)
        for item in lessons
    )
    normalized_sources = tuple(
        item if isinstance(item, LessonSource) else LessonSource.from_dict(item)
        for item in sources
    )
    normalized_events = tuple(
        item if isinstance(item, LessonEvent) else LessonEvent.from_dict(item)
        for item in events
    )
    result = retrieve_applicable_lessons(
        normalized_query,
        lessons=normalized_lessons,
        sources=normalized_sources,
        events=normalized_events,
    )
    if expected_results is not None:
        expected = tuple(
            item if isinstance(item, RetrievedLesson) else RetrievedLesson.from_dict(item)
            for item in expected_results
        )
        if tuple(item.content_hash for item in result) != tuple(
            item.content_hash for item in expected
        ):
            raise ValueError("Lesson retrieval replay does not match expected hashes.")
    return result


MISTAKE_MEMORY_MIGRATION_SQL = (
    """
    CREATE TABLE forecast_outcome_reviews (
        review_id TEXT PRIMARY KEY
            CHECK (length(trim(review_id)) > 0),
        review_version INTEGER NOT NULL
            CHECK (review_version >= 1),
        parent_review_id TEXT
            REFERENCES forecast_outcome_reviews(review_id)
            ON DELETE RESTRICT,
        evaluation_id TEXT NOT NULL
            REFERENCES forecast_outcome_evaluations(evaluation_id)
            ON DELETE RESTRICT,
        required_superseding_evaluation_id TEXT
            REFERENCES forecast_outcome_evaluations(evaluation_id)
            ON DELETE RESTRICT,
        forecast_id TEXT NOT NULL
            REFERENCES forecast_records(forecast_id)
            ON DELETE RESTRICT,
        decision TEXT NOT NULL
            CHECK (decision IN ('approved', 'revised', 'rejected', 'needs_more_data')),
        reviewed_outcome_status TEXT NOT NULL
            CHECK (
                reviewed_outcome_status IN (
                    'succeeded', 'failed', 'partial', 'unresolved',
                    'insufficient_data', 'incomparable'
                )
            ),
        reviewer_reference TEXT NOT NULL
            CHECK (length(trim(reviewer_reference)) > 0),
        reviewed_at_utc TEXT NOT NULL
            CHECK (length(trim(reviewed_at_utc)) > 0),
        schema_version TEXT NOT NULL
            CHECK (length(trim(schema_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL
            CHECK (length(trim(record_json)) > 1),
        CHECK (parent_review_id IS NULL OR parent_review_id <> review_id),
        CHECK (
            (required_superseding_evaluation_id IS NULL)
            OR required_superseding_evaluation_id <> evaluation_id
        )
    )
    """,
    """
    CREATE TABLE mistake_memory_lessons (
        lesson_id TEXT PRIMARY KEY
            CHECK (length(trim(lesson_id)) > 0),
        lesson_version INTEGER NOT NULL
            CHECK (lesson_version >= 1),
        supersedes_lesson_id TEXT
            REFERENCES mistake_memory_lessons(lesson_id)
            ON DELETE RESTRICT,
        scope_type TEXT NOT NULL
            CHECK (scope_type IN ('exact_case', 'scoped', 'general')),
        initial_status TEXT NOT NULL DEFAULT 'proposed'
            CHECK (initial_status = 'proposed'),
        proposed_by TEXT NOT NULL
            CHECK (length(trim(proposed_by)) > 0),
        proposed_at_utc TEXT NOT NULL
            CHECK (length(trim(proposed_at_utc)) > 0),
        deduplication_key TEXT NOT NULL
            CHECK (
                length(deduplication_key) = 64
                AND deduplication_key NOT GLOB '*[^0-9a-f]*'
            ),
        schema_version TEXT NOT NULL
            CHECK (length(trim(schema_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL
            CHECK (length(trim(record_json)) > 1),
        CHECK (supersedes_lesson_id IS NULL OR supersedes_lesson_id <> lesson_id)
    )
    """,
    """
    CREATE TABLE mistake_memory_sources (
        source_id TEXT PRIMARY KEY
            CHECK (length(trim(source_id)) > 0),
        lesson_id TEXT NOT NULL
            REFERENCES mistake_memory_lessons(lesson_id)
            ON DELETE RESTRICT,
        review_id TEXT NOT NULL
            REFERENCES forecast_outcome_reviews(review_id)
            ON DELETE RESTRICT,
        evaluation_id TEXT NOT NULL
            REFERENCES forecast_outcome_evaluations(evaluation_id)
            ON DELETE RESTRICT,
        forecast_id TEXT NOT NULL
            REFERENCES forecast_records(forecast_id)
            ON DELETE RESTRICT,
        source_role TEXT NOT NULL
            CHECK (source_role IN ('supporting', 'counterexample')),
        symbol TEXT NOT NULL
            CHECK (length(trim(symbol)) > 0),
        added_by TEXT NOT NULL
            CHECK (length(trim(added_by)) > 0),
        added_at_utc TEXT NOT NULL
            CHECK (length(trim(added_at_utc)) > 0),
        schema_version TEXT NOT NULL
            CHECK (length(trim(schema_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL
            CHECK (length(trim(record_json)) > 1),
        UNIQUE (lesson_id, review_id)
    )
    """,
    """
    CREATE TABLE mistake_memory_events (
        event_id TEXT PRIMARY KEY
            CHECK (length(trim(event_id)) > 0),
        lesson_id TEXT NOT NULL
            REFERENCES mistake_memory_lessons(lesson_id)
            ON DELETE RESTRICT,
        sequence_number INTEGER NOT NULL
            CHECK (sequence_number >= 1),
        parent_event_id TEXT
            REFERENCES mistake_memory_events(event_id)
            ON DELETE RESTRICT,
        related_lesson_id TEXT
            REFERENCES mistake_memory_lessons(lesson_id)
            ON DELETE RESTRICT,
        from_status TEXT
            CHECK (
                from_status IS NULL OR from_status IN (
                    'proposed', 'active', 'rejected', 'superseded', 'retired'
                )
            ),
        to_status TEXT NOT NULL
            CHECK (
                to_status IN (
                    'proposed', 'active', 'rejected', 'superseded', 'retired'
                )
            ),
        actor_reference TEXT NOT NULL
            CHECK (length(trim(actor_reference)) > 0),
        recorded_at_utc TEXT NOT NULL
            CHECK (length(trim(recorded_at_utc)) > 0),
        reason TEXT NOT NULL
            CHECK (length(trim(reason)) > 0),
        schema_version TEXT NOT NULL
            CHECK (length(trim(schema_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL
            CHECK (length(trim(record_json)) > 1),
        UNIQUE (lesson_id, sequence_number),
        CHECK (parent_event_id IS NULL OR parent_event_id <> event_id),
        CHECK (related_lesson_id IS NULL OR related_lesson_id <> lesson_id)
    )
    """,
    "CREATE INDEX forecast_outcome_reviews_evaluation_idx ON forecast_outcome_reviews(evaluation_id, reviewed_at_utc, review_version)",
    "CREATE INDEX forecast_outcome_reviews_forecast_idx ON forecast_outcome_reviews(forecast_id, reviewed_at_utc)",
    "CREATE UNIQUE INDEX forecast_outcome_reviews_parent_idx ON forecast_outcome_reviews(parent_review_id) WHERE parent_review_id IS NOT NULL",
    "CREATE INDEX mistake_memory_lessons_scope_idx ON mistake_memory_lessons(scope_type, proposed_at_utc, lesson_id)",
    "CREATE INDEX mistake_memory_lessons_dedup_idx ON mistake_memory_lessons(deduplication_key, lesson_id)",
    "CREATE UNIQUE INDEX mistake_memory_lessons_supersedes_idx ON mistake_memory_lessons(supersedes_lesson_id) WHERE supersedes_lesson_id IS NOT NULL",
    "CREATE INDEX mistake_memory_sources_lesson_idx ON mistake_memory_sources(lesson_id, source_role, source_id)",
    "CREATE INDEX mistake_memory_sources_forecast_idx ON mistake_memory_sources(forecast_id, evaluation_id, review_id)",
    "CREATE INDEX mistake_memory_events_lesson_idx ON mistake_memory_events(lesson_id, sequence_number, recorded_at_utc)",
    "CREATE UNIQUE INDEX mistake_memory_events_parent_idx ON mistake_memory_events(parent_event_id) WHERE parent_event_id IS NOT NULL",
    """
    CREATE TRIGGER forecast_outcome_reviews_no_update
    BEFORE UPDATE ON forecast_outcome_reviews
    BEGIN
        SELECT RAISE(ABORT, 'forecast_outcome_reviews is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_outcome_reviews_no_delete
    BEFORE DELETE ON forecast_outcome_reviews
    BEGIN
        SELECT RAISE(ABORT, 'forecast_outcome_reviews is append-only');
    END
    """,
    """
    CREATE TRIGGER mistake_memory_lessons_no_update
    BEFORE UPDATE ON mistake_memory_lessons
    BEGIN
        SELECT RAISE(ABORT, 'mistake_memory_lessons is append-only');
    END
    """,
    """
    CREATE TRIGGER mistake_memory_lessons_no_delete
    BEFORE DELETE ON mistake_memory_lessons
    BEGIN
        SELECT RAISE(ABORT, 'mistake_memory_lessons is append-only');
    END
    """,
    """
    CREATE TRIGGER mistake_memory_sources_no_update
    BEFORE UPDATE ON mistake_memory_sources
    BEGIN
        SELECT RAISE(ABORT, 'mistake_memory_sources is append-only');
    END
    """,
    """
    CREATE TRIGGER mistake_memory_sources_no_delete
    BEFORE DELETE ON mistake_memory_sources
    BEGIN
        SELECT RAISE(ABORT, 'mistake_memory_sources is append-only');
    END
    """,
    """
    CREATE TRIGGER mistake_memory_events_no_update
    BEFORE UPDATE ON mistake_memory_events
    BEGIN
        SELECT RAISE(ABORT, 'mistake_memory_events is append-only');
    END
    """,
    """
    CREATE TRIGGER mistake_memory_events_no_delete
    BEFORE DELETE ON mistake_memory_events
    BEGIN
        SELECT RAISE(ABORT, 'mistake_memory_events is append-only');
    END
    """,
)

MISTAKE_MEMORY_TABLES = frozenset(
    {
        "forecast_outcome_reviews",
        "mistake_memory_lessons",
        "mistake_memory_sources",
        "mistake_memory_events",
    }
)
MISTAKE_MEMORY_INDEXES = frozenset(
    {
        "forecast_outcome_reviews_evaluation_idx",
        "forecast_outcome_reviews_forecast_idx",
        "forecast_outcome_reviews_parent_idx",
        "mistake_memory_lessons_scope_idx",
        "mistake_memory_lessons_dedup_idx",
        "mistake_memory_lessons_supersedes_idx",
        "mistake_memory_sources_lesson_idx",
        "mistake_memory_sources_forecast_idx",
        "mistake_memory_events_lesson_idx",
        "mistake_memory_events_parent_idx",
    }
)
MISTAKE_MEMORY_TRIGGERS = frozenset(
    {
        "forecast_outcome_reviews_no_update",
        "forecast_outcome_reviews_no_delete",
        "mistake_memory_lessons_no_update",
        "mistake_memory_lessons_no_delete",
        "mistake_memory_sources_no_update",
        "mistake_memory_sources_no_delete",
        "mistake_memory_events_no_update",
        "mistake_memory_events_no_delete",
    }
)


__all__ = [
    "FAILURE_DIAGNOSIS_SCHEMA_VERSION",
    "FailureDiagnosis",
    "FailureDiagnosisType",
    "ForecastOutcomeReview",
    "LESSON_EVENT_SCHEMA_VERSION",
    "LESSON_RETRIEVAL_QUERY_SCHEMA_VERSION",
    "LESSON_SCHEMA_VERSION",
    "LESSON_SCOPE_SCHEMA_VERSION",
    "LESSON_SOURCE_SCHEMA_VERSION",
    "LessonEvent",
    "LessonRetrievalQuery",
    "LessonScope",
    "LessonScopeType",
    "LessonSource",
    "LessonSourceRole",
    "LessonStatus",
    "MISTAKE_MEMORY_CALCULATION_VERSION",
    "MISTAKE_MEMORY_INDEXES",
    "MISTAKE_MEMORY_MIGRATION_SQL",
    "MISTAKE_MEMORY_POLICY_VERSION",
    "MISTAKE_MEMORY_TABLES",
    "MISTAKE_MEMORY_TRIGGERS",
    "MistakeMemoryLesson",
    "OUTCOME_REVIEW_SCHEMA_VERSION",
    "RETRIEVED_LESSON_SCHEMA_VERSION",
    "RetrievedLesson",
    "ReviewDecision",
    "approve_lesson",
    "build_lesson_event",
    "create_forecast_outcome_review",
    "create_lesson_source",
    "forecast_outcome_review_content_hash",
    "lesson_current_status",
    "lesson_deduplication_key",
    "lesson_event_content_hash",
    "lesson_retrieval_query_content_hash",
    "lesson_source_content_hash",
    "mistake_memory_lesson_content_hash",
    "propose_mistake_memory_lesson",
    "reject_lesson",
    "replay_lesson_retrieval",
    "retrieve_applicable_lessons",
    "retrieved_lesson_content_hash",
    "retire_lesson",
    "revise_forecast_outcome_review",
    "review_forecast_outcome",
    "supersede_lesson",
    "validate_forecast_outcome_review",
    "validate_lesson_event_chain",
]
