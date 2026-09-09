"""Immutable contracts and validation for current market scenarios.

The contracts in this module are standalone. They consume frozen technical,
current-state, evidence, exposure, event, and retrieval inputs but do not call
providers, perform research, alter Elliott analysis, persist data, render
reports, or make trading recommendations.
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

from .current_state import (
    CurrentMarketState,
    current_market_state_content_hash,
    evidence_packet_content_hash,
    material_event_packet_content_hash,
    validate_current_market_state,
)
from .historical_market import (
    DurationBucket,
    HistoricalAnalystType,
    MagnitudeBucket,
    TransmissionMechanism,
)
from .market_scenario import (
    EXPOSURE_TYPE_CATEGORY,
    QUALITATIVE_CONFIDENCE_VALUES,
    EvidenceImplication,
    EvidenceItem,
    EvidenceStatus,
    ExposureCategory,
    ExposureItem,
    ExposureType,
    FrozenTechnicalPremise,
    MaterialEvent,
    ScenarioDirection,
    ValidationIssue,
    ValidationResult,
    frozen_technical_premise_content_hash,
)
from .pattern_retrieval import (
    MatchQuality,
    MatchRecommendation,
    PatternRetrievalResult,
    RetrievalLane,
    RetrievedPatternMatch,
    pattern_retrieval_result_content_hash,
    validate_pattern_retrieval_result,
)


CURRENT_SCENARIO_STEP_SCHEMA_VERSION = "current-scenario-step-1.0.0"
CURRENT_SCENARIO_NARRATIVE_SCHEMA_VERSION = (
    "current-scenario-narrative-1.0.0"
)
CURRENT_SCENARIO_RELATIONSHIP_SCHEMA_VERSION = (
    "current-scenario-relationship-1.0.0"
)
CURRENT_SCENARIO_SET_SCHEMA_VERSION = "current-scenario-set-1.0.0"
CURRENT_SCENARIO_ADVERSARIAL_SCHEMA_VERSION = (
    "current-scenario-adversarial-review-1.0.0"
)
CURRENT_SCENARIO_VALIDATION_VERSION = (
    "current-scenario-validation-1.0.0"
)
CURRENT_SCENARIO_GENERATION_RESULT_SCHEMA_VERSION = (
    "current-scenario-generation-result-1.0.0"
)


class ScenarioType(StrEnum):
    COMPANY_EXECUTION = "company_execution"
    EARNINGS_AND_GUIDANCE = "earnings_and_guidance"
    FINANCING_AND_DILUTION = "financing_and_dilution"
    VALUATION_REPRICING = "valuation_repricing"
    MACRO_AND_LIQUIDITY = "macro_and_liquidity"
    SECTOR_ROTATION = "sector_rotation"
    REGULATORY = "regulatory"
    EVENT_DRIVEN = "event_driven"
    POSITIONING_UNWIND = "positioning_unwind"
    SHORT_SQUEEZE = "short_squeeze"
    MULTI_FACTOR = "multi_factor"
    TECHNICAL_INVALIDATION = "technical_invalidation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class TechnicalPremiseCompatibility(StrEnum):
    STRONGLY_CONSISTENT = "strongly_consistent"
    CONSISTENT = "consistent"
    PARTIALLY_CONSISTENT = "partially_consistent"
    WEAKLY_CONSISTENT = "weakly_consistent"
    CONTRADICTORY = "contradictory"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ScenarioConfidence(StrEnum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNAVAILABLE = "unavailable"


class ScenarioStepType(StrEnum):
    PRECONDITION = "precondition"
    TRIGGER = "trigger"
    INITIAL_REPRICING = "initial_repricing"
    AMPLIFICATION = "amplification"
    CONTINUATION = "continuation"
    EXHAUSTION = "exhaustion"
    REVERSAL = "reversal"
    INVALIDATION = "invalidation"


class ScenarioRelationshipType(StrEnum):
    MUTUALLY_EXCLUSIVE = "mutually_exclusive"
    COMPATIBLE = "compatible"
    PREREQUISITE = "prerequisite"
    SEQUENTIAL = "sequential"
    ALTERNATIVE_TRIGGER = "alternative_trigger"
    AMPLIFICATION_OF = "amplification_of"
    INVALIDATES = "invalidates"
    COUNTER_SCENARIO = "counter_scenario"


class ScenarioAdversarialRecommendation(StrEnum):
    KEEP = "keep"
    REVISE = "revise"
    REJECT = "reject"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


_SYMMETRIC_RELATIONSHIPS = frozenset(
    {
        ScenarioRelationshipType.MUTUALLY_EXCLUSIVE,
        ScenarioRelationshipType.COMPATIBLE,
        ScenarioRelationshipType.ALTERNATIVE_TRIGGER,
        ScenarioRelationshipType.COUNTER_SCENARIO,
    }
)

_CONDITIONAL_LANGUAGE = re.compile(
    r"\b(?:could|may|might|would|is consistent with|"
    r"would be consistent with|plausibly)\b",
    re.IGNORECASE,
)
_EXACT_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_PROHIBITED_CERTAINTY = re.compile(
    r"\b(?:guarantees?|certainly|definitely|must happen|"
    r"will inevitably|cannot fail|chart proves|technical count proves|"
    r"elliott(?: wave)? (?:count )?predicts?)\b",
    re.IGNORECASE,
)
_TECHNICAL_EVENT_PREDICTION = re.compile(
    r"\b(?:chart|technical analysis|technical count|"
    r"elliott(?: wave)? count)\b.{0,70}\b"
    r"(?:predicts?|guarantees?|proves?)\b.{0,70}\b"
    r"(?:event|news|announcement|earnings|guidance|merger|"
    r"bankruptcy|financing|regulatory)\b",
    re.IGNORECASE,
)
_TRADE_RECOMMENDATION = re.compile(
    r"\b(?:buy (?:the )?(?:stock|shares)|sell (?:the )?(?:stock|shares)|"
    r"go long|go short|enter (?:a|the) position|take profit|"
    r"stop[- ]loss|recommended? (?:trade|entry|exit|buy|sell)|"
    r"position size)\b",
    re.IGNORECASE,
)
_PROBABILITY_LANGUAGE = re.compile(
    r"\b(?:probability|odds|expected return|success rate|"
    r"\d+(?:\.\d+)?\s*%\s+(?:chance|probability))\b",
    re.IGNORECASE,
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
        elif re.fullmatch(r"\d{4}-\d{2}", raw):
            parsed = datetime.fromisoformat(raw + "-01")
        else:
            quarter = re.fullmatch(r"(\d{4})-Q([1-4])", raw.upper())
            if quarter:
                month = 1 + (int(quarter.group(2)) - 1) * 3
                parsed = datetime(int(quarter.group(1)), month, 1)
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


def _string_mapping(
    value: Mapping[str, str],
    *,
    field_name: str,
) -> Mapping[str, str]:
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


def _duplicates(values: Sequence[Any]) -> set[Any]:
    return {
        value
        for value, count in Counter(values).items()
        if count > 1
    }


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class HypotheticalFutureEvent(_JsonContract):
    hypothetical_event_id: str
    event_type: str
    description: str
    timing_window: str
    assumptions: tuple[str, ...]
    uncertainty: str
    explicitly_hypothetical: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assumptions",
            _string_tuple(
                self.assumptions,
                field_name="assumptions",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "HypotheticalFutureEvent":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ScenarioPatternGrounding(_JsonContract):
    pattern_ref: str
    lane: RetrievalLane
    recommendation: MatchRecommendation
    match_quality: MatchQuality
    reused_causal_features: tuple[str, ...]
    acknowledged_differences: tuple[str, ...]
    acknowledged_warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lane",
            _enum_value(
                self.lane,
                RetrievalLane,
                field_name="lane",
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
            "match_quality",
            _enum_value(
                self.match_quality,
                MatchQuality,
                field_name="match_quality",
            ),
        )
        for field_name in (
            "reused_causal_features",
            "acknowledged_differences",
            "acknowledged_warnings",
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
    ) -> "ScenarioPatternGrounding":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ScenarioStep(_JsonContract):
    step_number: int
    step_type: ScenarioStepType
    description: str
    current_evidence_ids: tuple[str, ...]
    current_exposure_types: tuple[ExposureType, ...]
    event_ids: tuple[str, ...]
    hypothetical_event_ids: tuple[str, ...]
    transmission_mechanisms: tuple[TransmissionMechanism, ...]
    expected_market_effect: str
    timing_relation: str
    assumptions: tuple[str, ...]
    uncertainty: str
    content_hash: str = ""
    schema_version: str = CURRENT_SCENARIO_STEP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "step_type",
            _enum_value(
                self.step_type,
                ScenarioStepType,
                field_name="step_type",
            ),
        )
        object.__setattr__(
            self,
            "current_exposure_types",
            _enum_tuple(
                self.current_exposure_types,
                ExposureType,
                field_name="current_exposure_types",
            ),
        )
        object.__setattr__(
            self,
            "transmission_mechanisms",
            _enum_tuple(
                self.transmission_mechanisms,
                TransmissionMechanism,
                field_name="transmission_mechanisms",
            ),
        )
        for field_name in (
            "current_evidence_ids",
            "event_ids",
            "hypothetical_event_ids",
            "assumptions",
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
    ) -> "ScenarioStep":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CurrentScenarioNarrative(_JsonContract):
    scenario_id: str
    title: str
    scenario_type: ScenarioType
    direction: ScenarioDirection
    summary: str
    initiating_conditions: tuple[str, ...]
    required_current_exposure_types: tuple[ExposureType, ...]
    optional_current_exposure_types: tuple[ExposureType, ...]
    triggering_events: tuple[str, ...]
    hypothetical_future_events: tuple[HypotheticalFutureEvent, ...]
    transmission_mechanisms: tuple[TransmissionMechanism, ...]
    exposure_interactions: tuple[str, ...]
    amplifiers: tuple[str, ...]
    dampeners: tuple[str, ...]
    market_regime_requirements: tuple[str, ...]
    timeline_sequence: tuple[ScenarioStep, ...]
    target_linkage: str
    duration_linkage: str
    linked_target_low: float | None
    linked_target_high: float | None
    linked_magnitude_bucket: MagnitudeBucket
    linked_duration_bucket: DurationBucket
    supporting_current_evidence_ids: tuple[str, ...]
    contradicting_current_evidence_ids: tuple[str, ...]
    supporting_pattern_refs: tuple[str, ...]
    counter_pattern_refs: tuple[str, ...]
    pattern_grounding: tuple[ScenarioPatternGrounding, ...]
    assumptions: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    technical_premise_compatibility: TechnicalPremiseCompatibility
    confidence: ScenarioConfidence
    limitations: tuple[str, ...]
    applicable_cutoff: str
    analyst_type: HistoricalAnalystType
    model_name: str
    prompt_version: str
    generated_at: str
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""
    schema_version: str = CURRENT_SCENARIO_NARRATIVE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name, enum_type in (
            ("scenario_type", ScenarioType),
            ("direction", ScenarioDirection),
            ("linked_magnitude_bucket", MagnitudeBucket),
            ("linked_duration_bucket", DurationBucket),
            (
                "technical_premise_compatibility",
                TechnicalPremiseCompatibility,
            ),
            ("confidence", ScenarioConfidence),
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
        for field_name in (
            "required_current_exposure_types",
            "optional_current_exposure_types",
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_tuple(
                    getattr(self, field_name),
                    ExposureType,
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "transmission_mechanisms",
            _enum_tuple(
                self.transmission_mechanisms,
                TransmissionMechanism,
                field_name="transmission_mechanisms",
            ),
        )
        object.__setattr__(
            self,
            "hypothetical_future_events",
            _model_tuple(
                self.hypothetical_future_events,
                HypotheticalFutureEvent,
                field_name="hypothetical_future_events",
            ),
        )
        object.__setattr__(
            self,
            "timeline_sequence",
            _model_tuple(
                self.timeline_sequence,
                ScenarioStep,
                field_name="timeline_sequence",
            ),
        )
        object.__setattr__(
            self,
            "pattern_grounding",
            _model_tuple(
                self.pattern_grounding,
                ScenarioPatternGrounding,
                field_name="pattern_grounding",
            ),
        )
        for field_name in (
            "initiating_conditions",
            "triggering_events",
            "exposure_interactions",
            "amplifiers",
            "dampeners",
            "market_regime_requirements",
            "supporting_current_evidence_ids",
            "contradicting_current_evidence_ids",
            "supporting_pattern_refs",
            "counter_pattern_refs",
            "assumptions",
            "missing_evidence",
            "invalidation_conditions",
            "limitations",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        for field_name in ("linked_target_low", "linked_target_high"):
            value = getattr(self, field_name)
            if value is not None:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise ValueError(
                        f"{field_name} must be finite or null."
                    )
                object.__setattr__(self, field_name, float(value))
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
            "generated_at",
            _normalize_timestamp(
                self.generated_at,
                field_name="generated_at",
            ),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CurrentScenarioNarrative":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["hypothetical_future_events"] = tuple(
            HypotheticalFutureEvent.from_dict(item)
            for item in raw.get("hypothetical_future_events", ())
        )
        raw["timeline_sequence"] = tuple(
            ScenarioStep.from_dict(item)
            for item in raw.get("timeline_sequence", ())
        )
        raw["pattern_grounding"] = tuple(
            ScenarioPatternGrounding.from_dict(item)
            for item in raw.get("pattern_grounding", ())
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ScenarioRelationship(_JsonContract):
    source_scenario_id: str
    target_scenario_id: str
    relationship_type: ScenarioRelationshipType
    rationale: str
    content_hash: str = ""
    schema_version: str = CURRENT_SCENARIO_RELATIONSHIP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relationship_type",
            _enum_value(
                self.relationship_type,
                ScenarioRelationshipType,
                field_name="relationship_type",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ScenarioRelationship":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CurrentScenarioSet(_JsonContract):
    scenario_set_id: str
    symbol: str
    applicable_cutoff: str
    frozen_technical_premise_hash: str
    current_state_hash: str
    retrieval_result_hash: str
    scenarios: tuple[CurrentScenarioNarrative, ...]
    scenario_relationships: tuple[ScenarioRelationship, ...]
    overall_missing_evidence: tuple[str, ...]
    overall_limitations: tuple[str, ...]
    adversarial_summary: str
    insufficient_evidence_reason: str | None
    analyst_type: HistoricalAnalystType
    model_name: str
    prompt_version: str
    generated_at: str
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""
    schema_version: str = CURRENT_SCENARIO_SET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scenarios",
            _model_tuple(
                self.scenarios,
                CurrentScenarioNarrative,
                field_name="scenarios",
            ),
        )
        object.__setattr__(
            self,
            "scenario_relationships",
            _model_tuple(
                self.scenario_relationships,
                ScenarioRelationship,
                field_name="scenario_relationships",
            ),
        )
        for field_name in (
            "overall_missing_evidence",
            "overall_limitations",
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
            "analyst_type",
            _enum_value(
                self.analyst_type,
                HistoricalAnalystType,
                field_name="analyst_type",
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
        object.__setattr__(
            self,
            "generated_at",
            _normalize_timestamp(
                self.generated_at,
                field_name="generated_at",
            ),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")
        if self.insufficient_evidence_reason is not None and not isinstance(
            self.insufficient_evidence_reason,
            str,
        ):
            raise TypeError(
                "insufficient_evidence_reason must be a string or null."
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CurrentScenarioSet":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["scenarios"] = tuple(
            CurrentScenarioNarrative.from_dict(item)
            for item in raw.get("scenarios", ())
        )
        raw["scenario_relationships"] = tuple(
            ScenarioRelationship.from_dict(item)
            for item in raw.get("scenario_relationships", ())
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CurrentScenarioAdversarialReview(_JsonContract):
    recommendation: ScenarioAdversarialRecommendation
    summary: str
    duplicated_scenario_findings: tuple[str, ...]
    unsupported_future_event_findings: tuple[str, ...]
    invented_fact_findings: tuple[str, ...]
    dangling_reference_findings: tuple[str, ...]
    hindsight_leakage_findings: tuple[str, ...]
    historical_analogue_overreliance_findings: tuple[str, ...]
    ignored_counter_pattern_findings: tuple[str, ...]
    missing_contradicting_evidence_findings: tuple[str, ...]
    weak_causal_transmission_findings: tuple[str, ...]
    target_magnitude_mismatch_findings: tuple[str, ...]
    duration_mismatch_findings: tuple[str, ...]
    timing_contradiction_findings: tuple[str, ...]
    excessive_certainty_findings: tuple[str, ...]
    single_catalyst_findings: tuple[str, ...]
    scheduled_hypothetical_confusion_findings: tuple[str, ...]
    simpler_explanations: tuple[str, ...]
    strongest_counterargument: str
    referenced_scenario_ids: tuple[str, ...]
    referenced_current_evidence_ids: tuple[str, ...]
    referenced_pattern_refs: tuple[str, ...]
    replacement_scenario_set: CurrentScenarioSet | None
    provider: str
    model_name: str
    prompt_version: str
    generated_at: str
    applicable_cutoff: str
    content_hash: str = ""
    schema_version: str = CURRENT_SCENARIO_ADVERSARIAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "recommendation",
            _enum_value(
                self.recommendation,
                ScenarioAdversarialRecommendation,
                field_name="recommendation",
            ),
        )
        for field_name in (
            "duplicated_scenario_findings",
            "unsupported_future_event_findings",
            "invented_fact_findings",
            "dangling_reference_findings",
            "hindsight_leakage_findings",
            "historical_analogue_overreliance_findings",
            "ignored_counter_pattern_findings",
            "missing_contradicting_evidence_findings",
            "weak_causal_transmission_findings",
            "target_magnitude_mismatch_findings",
            "duration_mismatch_findings",
            "timing_contradiction_findings",
            "excessive_certainty_findings",
            "single_catalyst_findings",
            "scheduled_hypothetical_confusion_findings",
            "simpler_explanations",
            "referenced_scenario_ids",
            "referenced_current_evidence_ids",
            "referenced_pattern_refs",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if self.replacement_scenario_set is not None and not isinstance(
            self.replacement_scenario_set,
            CurrentScenarioSet,
        ):
            raise TypeError(
                "replacement_scenario_set must be CurrentScenarioSet or null."
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
            "applicable_cutoff",
            _normalize_timestamp(
                self.applicable_cutoff,
                field_name="applicable_cutoff",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CurrentScenarioAdversarialReview":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("replacement_scenario_set") is not None:
            raw["replacement_scenario_set"] = CurrentScenarioSet.from_dict(
                raw["replacement_scenario_set"]
            )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CurrentScenarioGenerationResult(_JsonContract):
    success: bool
    scenario_set: CurrentScenarioSet | None
    validation_result: ValidationResult
    provider: str
    model_name: str
    prompt_version: str
    attempts: int
    used_adversarial_pass: bool
    adversarial_recommendation: ScenarioAdversarialRecommendation | None
    adversarial_review: CurrentScenarioAdversarialReview | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    input_hashes: Mapping[str, str]
    output_hash: str
    generated_at: str
    content_hash: str = ""
    schema_version: str = CURRENT_SCENARIO_GENERATION_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.scenario_set is not None and not isinstance(
            self.scenario_set,
            CurrentScenarioSet,
        ):
            raise TypeError(
                "scenario_set must be CurrentScenarioSet or null."
            )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")
        if self.adversarial_recommendation is not None:
            object.__setattr__(
                self,
                "adversarial_recommendation",
                _enum_value(
                    self.adversarial_recommendation,
                    ScenarioAdversarialRecommendation,
                    field_name="adversarial_recommendation",
                ),
            )
        if self.adversarial_review is not None and not isinstance(
            self.adversarial_review,
            CurrentScenarioAdversarialReview,
        ):
            raise TypeError(
                "adversarial_review must be "
                "CurrentScenarioAdversarialReview or null."
            )
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
            "input_hashes",
            _string_mapping(
                self.input_hashes,
                field_name="input_hashes",
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
    ) -> "CurrentScenarioGenerationResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("scenario_set") is not None:
            raw["scenario_set"] = CurrentScenarioSet.from_dict(
                raw["scenario_set"]
            )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        if raw.get("adversarial_review") is not None:
            raw["adversarial_review"] = (
                CurrentScenarioAdversarialReview.from_dict(
                    raw["adversarial_review"]
                )
            )
        return cls(**raw)


def scenario_step_content_hash(step: ScenarioStep) -> str:
    if not isinstance(step, ScenarioStep):
        raise TypeError("step must be ScenarioStep.")
    return _contract_hash(step)


def scenario_relationship_content_hash(
    relationship: ScenarioRelationship,
) -> str:
    if not isinstance(relationship, ScenarioRelationship):
        raise TypeError("relationship must be ScenarioRelationship.")
    return _contract_hash(relationship)


def current_scenario_narrative_content_hash(
    scenario: CurrentScenarioNarrative,
) -> str:
    if not isinstance(scenario, CurrentScenarioNarrative):
        raise TypeError("scenario must be CurrentScenarioNarrative.")
    return _contract_hash(scenario)


def current_scenario_set_content_hash(
    scenario_set: CurrentScenarioSet,
) -> str:
    if not isinstance(scenario_set, CurrentScenarioSet):
        raise TypeError("scenario_set must be CurrentScenarioSet.")
    return _contract_hash(scenario_set)


def current_scenario_adversarial_review_content_hash(
    review: CurrentScenarioAdversarialReview,
) -> str:
    if not isinstance(review, CurrentScenarioAdversarialReview):
        raise TypeError(
            "review must be CurrentScenarioAdversarialReview."
        )
    return _contract_hash(review)


def current_scenario_generation_result_content_hash(
    result: CurrentScenarioGenerationResult,
) -> str:
    if not isinstance(result, CurrentScenarioGenerationResult):
        raise TypeError(
            "result must be CurrentScenarioGenerationResult."
        )
    return _contract_hash(result)


_INPUT_RULE_ORDER = (
    "input_types",
    "technical_premise",
    "current_state",
    "technical_state_linkage",
    "evidence_bundle",
    "exposure_bundle",
    "event_bundle",
    "cutoff_isolation",
    "retrieval_result",
    "retrieval_linkage",
)

_SCENARIO_RULE_ORDER = (
    "scenario_schema",
    "scenario_hash",
    "scenario_identity",
    "scenario_provenance",
    "scenario_cutoff",
    "scenario_direction",
    "scenario_target_linkage",
    "scenario_duration_linkage",
    "scenario_evidence_references",
    "scenario_exposure_references",
    "scenario_event_references",
    "scenario_hypothetical_events",
    "scenario_pattern_references",
    "scenario_pattern_grounding",
    "scenario_causal_sequence",
    "scenario_independent_drivers",
    "scenario_contradicting_evidence",
    "scenario_invalidation",
    "scenario_conditional_language",
    "scenario_prohibited_language",
    "scenario_completeness",
)

_SET_RULE_ORDER = (
    "set_schema",
    "set_hash",
    "set_identity",
    "set_provenance",
    "set_scenario_validation",
    "set_scenario_count",
    "set_scenario_uniqueness",
    "set_relationships",
    "set_diversity",
    "set_counter_patterns",
    "set_technical_invalidation",
    "set_completeness",
)


class _ValidationCollector:
    def __init__(self, rule_order: Sequence[str]) -> None:
        self.rule_order = tuple(rule_order)
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []
        self.failed: set[str] = set()

    def require(
        self,
        code: str,
        condition: bool,
        path: str,
        message: str,
    ) -> None:
        if condition:
            return
        self.failed.add(code)
        self.errors.append(
            ValidationIssue(
                code=code,
                severity="error",
                path=path,
                message=message,
            )
        )

    def warn(
        self,
        code: str,
        condition: bool,
        path: str,
        message: str,
    ) -> None:
        if condition:
            return
        self.warnings.append(
            ValidationIssue(
                code=code,
                severity="warning",
                path=path,
                message=message,
            )
        )

    def result(self) -> ValidationResult:
        passed = tuple(
            rule for rule in self.rule_order if rule not in self.failed
        )
        failed = tuple(
            rule for rule in self.rule_order if rule in self.failed
        )
        score = (
            round(100.0 * len(passed) / len(self.rule_order), 2)
            if self.rule_order
            else 100.0
        )
        return ValidationResult(
            is_valid=not self.errors,
            score=score,
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            failed_rules=failed,
            passed_rules=passed,
            validation_version=CURRENT_SCENARIO_VALIDATION_VERSION,
        )


def _valid_hash(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _float_equal(
    first: float | None,
    second: float | None,
) -> bool:
    if first is None or second is None:
        return first is second
    return math.isclose(
        float(first),
        float(second),
        rel_tol=0.0,
        abs_tol=1e-9,
    )


def _expected_state_direction(
    direction: ScenarioDirection,
) -> str:
    return {
        ScenarioDirection.UP: "bullish",
        ScenarioDirection.DOWN: "bearish",
        ScenarioDirection.SIDEWAYS: "neutral",
        ScenarioDirection.UNKNOWN: "unavailable",
    }[direction]


def _publication_at_or_before_cutoff(
    publication_date: str | None,
    cutoff: str,
) -> bool:
    if publication_date is None:
        return True
    return _temporal_point(publication_date) <= _temporal_point(cutoff)


def validate_current_scenario_inputs(
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    retrieval: PatternRetrievalResult,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    events: Sequence[MaterialEvent] = (),
) -> ValidationResult:
    if not isinstance(premise, FrozenTechnicalPremise):
        raise TypeError("premise must be FrozenTechnicalPremise.")
    if not isinstance(state, CurrentMarketState):
        raise TypeError("state must be CurrentMarketState.")
    if not isinstance(retrieval, PatternRetrievalResult):
        raise TypeError("retrieval must be PatternRetrievalResult.")
    evidence_items = tuple(evidence)
    exposure_items = tuple(exposures)
    event_items = tuple(events)
    if not all(isinstance(item, EvidenceItem) for item in evidence_items):
        raise TypeError("evidence must contain EvidenceItem values.")
    if not all(isinstance(item, ExposureItem) for item in exposure_items):
        raise TypeError("exposures must contain ExposureItem values.")
    if not all(isinstance(item, MaterialEvent) for item in event_items):
        raise TypeError("events must contain MaterialEvent values.")

    result = _ValidationCollector(_INPUT_RULE_ORDER)
    result.require(
        "input_types",
        True,
        "inputs",
        "All inputs must use the Phase 7 controlled contracts.",
    )

    premise_hash = frozen_technical_premise_content_hash(premise)
    premise_ok = (
        _valid_hash(premise.frozen_content_hash)
        and premise.frozen_content_hash == premise_hash
        and bool(premise.resolution_id.strip())
        and bool(premise.symbol.strip())
        and bool(premise.source_hashes)
        and all(_valid_hash(value) for value in premise.source_hashes.values())
        and _temporal_point(premise.market_data_cutoff)
        <= _temporal_point(premise.analysed_at)
    )
    result.require(
        "technical_premise",
        premise_ok,
        "premise",
        "Frozen technical premise identity, cutoff, source hashes, or content hash is invalid.",
    )

    state_validation = validate_current_market_state(
        state,
        premise=premise,
    )
    state_hash = current_market_state_content_hash(state)
    result.require(
        "current_state",
        (
            state.validation_result.is_valid
            and state_validation.is_valid
            and state.content_hash == state_hash
        ),
        "state",
        "Current market state is not finalized, valid, or hash-stable.",
    )
    result.require(
        "technical_state_linkage",
        (
            state.frozen_technical_premise_hash == premise_hash
            and state.symbol == premise.symbol
            and (
                premise.exchange is None
                or state.exchange == premise.exchange
            )
            and state.technical_direction.value
            == _expected_state_direction(premise.direction)
            and _temporal_point(premise.market_data_cutoff)
            <= _temporal_point(state.applicable_cutoff)
        ),
        "state.frozen_technical_premise_hash",
        "Current state does not exactly preserve the frozen technical premise.",
    )

    evidence_ids = tuple(item.evidence_id for item in evidence_items)
    evidence_id_set = set(evidence_ids)
    result.require(
        "evidence_bundle",
        (
            not _duplicates(evidence_ids)
            and tuple(sorted(evidence_ids)) == state.evidence_ids
            and state.evidence_packet_hash
            == evidence_packet_content_hash(evidence_items)
            and all(
                bool(item.evidence_id.strip())
                and item.applicable_cutoff == state.applicable_cutoff
                and _publication_at_or_before_cutoff(
                    item.publication_date,
                    item.applicable_cutoff,
                )
                for item in evidence_items
            )
        ),
        "evidence",
        "Current evidence identities, hashes, provenance, or cutoff ordering are invalid.",
    )

    exposure_ids = tuple(item.exposure_id for item in exposure_items)
    exposure_types = {
        item.exposure_type for item in exposure_items
    }
    exposure_ok = (
        not _duplicates(exposure_ids)
        and tuple(sorted(exposure_ids)) == state.exposure_ids
        and exposure_types == set(state.current_exposure_types)
        and all(
            EXPOSURE_TYPE_CATEGORY.get(item.exposure_type)
            is item.category
            and set(item.supporting_evidence_ids) <= evidence_id_set
            and set(item.contradicting_evidence_ids) <= evidence_id_set
            and not (
                set(item.supporting_evidence_ids)
                & set(item.contradicting_evidence_ids)
            )
            and item.confidence in QUALITATIVE_CONFIDENCE_VALUES
            for item in exposure_items
        )
    )
    result.require(
        "exposure_bundle",
        exposure_ok,
        "exposures",
        "Current exposures contain invalid identities, categories, evidence references, or state linkage.",
    )

    event_ids = tuple(item.event_id for item in event_items)
    event_hash = material_event_packet_content_hash(event_items)
    event_ok = (
        not _duplicates(event_ids)
        and tuple(sorted(event_ids)) == state.event_ids
        and state.provenance.get("material_event_packet_hash") == event_hash
        and all(
            set(item.related_evidence_ids) <= evidence_id_set
            and item.applicable_cutoff == state.applicable_cutoff
            and _publication_at_or_before_cutoff(
                item.publication_date,
                item.applicable_cutoff,
            )
            and (
                _temporal_point(item.scheduled_at) is None
                or _temporal_point(item.scheduled_at)
                <= _temporal_point(state.applicable_cutoff)
                or item.status is EvidenceStatus.SCHEDULED
            )
            for item in event_items
        )
    )
    result.require(
        "event_bundle",
        event_ok,
        "events",
        "Material events contain invalid references, provenance, status, or cutoff ordering.",
    )

    cutoff = _temporal_point(state.applicable_cutoff)
    result.require(
        "cutoff_isolation",
        (
            all(
                _temporal_point(item.applicable_cutoff) <= cutoff
                for item in evidence_items
            )
            and all(
                _temporal_point(item.applicable_cutoff) <= cutoff
                for item in event_items
            )
            and _temporal_point(retrieval.generated_at) >= cutoff
        ),
        "applicable_cutoff",
        "An input crosses the current-state decision-time cutoff.",
    )

    retrieval_validation = validate_pattern_retrieval_result(
        retrieval,
        state=state,
    )
    retrieval_hash = pattern_retrieval_result_content_hash(retrieval)
    result.require(
        "retrieval_result",
        (
            retrieval.success
            and retrieval.validation_result.is_valid
            and retrieval_validation.is_valid
            and retrieval.content_hash == retrieval_hash
        ),
        "retrieval",
        "Pattern retrieval result is unsuccessful, invalid, or hash-mismatched.",
    )
    result.require(
        "retrieval_linkage",
        (
            retrieval.query.current_state_hash == state_hash
            and retrieval.query.current_state_hash == state.content_hash
            and all(
                item.recommendation
                in {
                    MatchRecommendation.INCLUDE,
                    MatchRecommendation.INCLUDE_WITH_WARNING,
                }
                for item in retrieval.matches
                + retrieval.counter_patterns
            )
        ),
        "retrieval.query.current_state_hash",
        "Pattern retrieval is not bound to the exact current state or contains ineligible matches.",
    )
    return result.result()


def technical_invalidation_tokens(
    premise: FrozenTechnicalPremise,
) -> tuple[str, ...]:
    if not isinstance(premise, FrozenTechnicalPremise):
        raise TypeError("premise must be FrozenTechnicalPremise.")
    if premise.invalidation_level is None:
        return ()
    return (
        "price_invalidation_level:"
        + format(float(premise.invalidation_level), ".15g"),
    )


def _all_text(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(
            text
            for item in value.values()
            for text in _all_text(item)
        )
    if isinstance(value, (tuple, list)):
        return tuple(
            text for item in value for text in _all_text(item)
        )
    return ()


def _scenario_feature_signature(
    scenario: CurrentScenarioNarrative,
) -> tuple[Any, ...]:
    trigger_types = tuple(
        sorted(
            {
                _normalized_token(item)
                for item in scenario.triggering_events
                if item.strip()
            }
            | {
                _normalized_token(item.event_type)
                for item in scenario.hypothetical_future_events
                if item.event_type.strip()
            }
        )
    )
    return (
        tuple(
            sorted(
                item.value
                for item in scenario.required_current_exposure_types
            )
        ),
        trigger_types,
        tuple(
            sorted(
                item.value
                for item in scenario.transmission_mechanisms
            )
        ),
        tuple(
            sorted(
                _normalized_token(item)
                for item in scenario.market_regime_requirements
            )
        ),
        tuple(
            item.step_type.value for item in scenario.timeline_sequence
        ),
        tuple(sorted(scenario.supporting_pattern_refs)),
        tuple(sorted(scenario.counter_pattern_refs)),
    )


def _driver_families(
    scenario: CurrentScenarioNarrative,
    *,
    evidence_by_id: Mapping[str, EvidenceItem],
) -> set[str]:
    families: set[str] = set()
    for exposure_type in scenario.required_current_exposure_types:
        category = EXPOSURE_TYPE_CATEGORY.get(exposure_type)
        if category is not None:
            families.add(category.value)
    for evidence_id in scenario.supporting_current_evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            continue
        category = _normalized_token(item.category)
        for token, family in (
            ("company", "company"),
            ("sector", "sector_and_competitors"),
            ("competitor", "sector_and_competitors"),
            ("macro", "macro_and_cross_asset"),
            ("liquidity", "macro_and_cross_asset"),
            ("valuation", "valuation_and_positioning"),
            ("position", "valuation_and_positioning"),
            ("event", "event_calendar"),
            ("earnings", "event_calendar"),
        ):
            if token in category:
                families.add(family)
    if any(step.event_ids for step in scenario.timeline_sequence):
        families.add("event_calendar")
    if scenario.hypothetical_future_events:
        families.add("hypothetical_event")
    if scenario.market_regime_requirements:
        families.add("market_regime")
    return families


def _scenario_has_conditional_linkage(
    scenario: CurrentScenarioNarrative,
) -> bool:
    return bool(
        scenario.target_linkage.strip()
        and scenario.duration_linkage.strip()
        and _CONDITIONAL_LANGUAGE.search(scenario.target_linkage)
        and _CONDITIONAL_LANGUAGE.search(scenario.duration_linkage)
    )


def _grounding_differences(
    match: RetrievedPatternMatch,
) -> set[str]:
    return set(
        match.missing_required_features
        + match.contradicted_required_features
        + match.incompatible_features
        + match.contradictory_current_features
    )


def validate_current_scenario_narrative(
    scenario: CurrentScenarioNarrative,
    *,
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    retrieval: PatternRetrievalResult,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    events: Sequence[MaterialEvent] = (),
) -> ValidationResult:
    if not isinstance(scenario, CurrentScenarioNarrative):
        raise TypeError("scenario must be CurrentScenarioNarrative.")
    evidence_items = tuple(evidence)
    exposure_items = tuple(exposures)
    event_items = tuple(events)
    evidence_by_id = {item.evidence_id: item for item in evidence_items}
    exposure_types = {item.exposure_type for item in exposure_items}
    event_by_id = {item.event_id: item for item in event_items}
    primary_by_ref = {
        item.pattern_ref: item for item in retrieval.matches
    }
    counter_by_ref = {
        item.pattern_ref: item for item in retrieval.counter_patterns
    }
    match_by_ref = {**primary_by_ref, **counter_by_ref}
    result = _ValidationCollector(_SCENARIO_RULE_ORDER)

    result.require(
        "scenario_schema",
        scenario.schema_version
        == CURRENT_SCENARIO_NARRATIVE_SCHEMA_VERSION,
        "schema_version",
        "Current-scenario narrative schema version is unsupported.",
    )
    result.require(
        "scenario_hash",
        bool(scenario.content_hash)
        and scenario.content_hash
        == current_scenario_narrative_content_hash(scenario),
        "content_hash",
        "Current-scenario narrative content hash is missing or mismatched.",
    )
    result.require(
        "scenario_identity",
        (
            bool(scenario.scenario_id)
            and scenario.scenario_id
            == _normalized_token(scenario.scenario_id)
            and bool(scenario.title.strip())
        ),
        "scenario_id",
        "Scenario ID must be a stable normalized token and title is required.",
    )
    result.require(
        "scenario_provenance",
        (
            scenario.analyst_type is HistoricalAnalystType.LLM
            and bool(scenario.model_name.strip())
            and bool(scenario.prompt_version.strip())
            and _temporal_point(scenario.generated_at)
            >= _temporal_point(scenario.applicable_cutoff)
        ),
        "analyst_type",
        "Scenario provenance must identify the LLM, prompt, and generation time.",
    )
    result.require(
        "scenario_cutoff",
        scenario.applicable_cutoff == state.applicable_cutoff,
        "applicable_cutoff",
        "Scenario cutoff must exactly match the frozen current state.",
    )

    direction_ok = (
        scenario.direction is premise.direction
        if scenario.scenario_type
        is not ScenarioType.TECHNICAL_INVALIDATION
        else (
            scenario.direction is not premise.direction
            and scenario.technical_premise_compatibility
            is TechnicalPremiseCompatibility.CONTRADICTORY
        )
    )
    result.require(
        "scenario_direction",
        direction_ok,
        "direction",
        "Normal scenarios must preserve technical direction; an invalidation scenario must be explicitly contradictory.",
    )
    result.require(
        "scenario_target_linkage",
        (
            _float_equal(
                scenario.linked_target_low,
                premise.target_low,
            )
            and _float_equal(
                scenario.linked_target_high,
                premise.target_high,
            )
            and scenario.linked_magnitude_bucket
            is state.expected_magnitude_bucket
            and bool(scenario.target_linkage.strip())
        ),
        "target_linkage",
        "Scenario target and magnitude linkage must preserve the frozen technical values.",
    )
    result.require(
        "scenario_duration_linkage",
        (
            scenario.linked_duration_bucket
            is state.expected_duration_bucket
            and bool(scenario.duration_linkage.strip())
        ),
        "duration_linkage",
        "Scenario duration linkage must preserve the frozen duration bucket.",
    )

    supporting_ids = set(scenario.supporting_current_evidence_ids)
    contradicting_ids = set(
        scenario.contradicting_current_evidence_ids
    )
    referenced_evidence = supporting_ids | contradicting_ids
    usable_evidence_statuses = {
        EvidenceStatus.CONFIRMED,
        EvidenceStatus.SCHEDULED,
        EvidenceStatus.INTERPRETATION,
    }
    result.require(
        "scenario_evidence_references",
        (
            referenced_evidence <= set(evidence_by_id)
            and not (supporting_ids & contradicting_ids)
            and not _duplicates(
                scenario.supporting_current_evidence_ids
            )
            and not _duplicates(
                scenario.contradicting_current_evidence_ids
            )
            and all(
                evidence_by_id[evidence_id].status
                in usable_evidence_statuses
                for evidence_id in referenced_evidence
            )
        ),
        "supporting_current_evidence_ids",
        "Scenario evidence references are dangling, duplicated, overlapping, or unavailable as current evidence.",
    )

    required_exposures = set(
        scenario.required_current_exposure_types
    )
    optional_exposures = set(
        scenario.optional_current_exposure_types
    )
    result.require(
        "scenario_exposure_references",
        (
            required_exposures <= exposure_types
            and optional_exposures <= exposure_types
            and required_exposures <= set(state.current_exposure_types)
            and optional_exposures <= set(state.current_exposure_types)
            and not (required_exposures & optional_exposures)
            and not _duplicates(
                scenario.required_current_exposure_types
            )
            and not _duplicates(
                scenario.optional_current_exposure_types
            )
        ),
        "required_current_exposure_types",
        "Scenario exposure types must exist in the frozen current state and cannot be both required and optional.",
    )

    step_event_ids = {
        event_id
        for step in scenario.timeline_sequence
        for event_id in step.event_ids
    }
    result.require(
        "scenario_event_references",
        step_event_ids <= set(event_by_id),
        "timeline_sequence.event_ids",
        "Scenario timeline contains an unknown material-event ID.",
    )

    hypothetical_ids = tuple(
        item.hypothetical_event_id
        for item in scenario.hypothetical_future_events
    )
    hypothetical_id_set = set(hypothetical_ids)
    hypothetical_ok = (
        not _duplicates(hypothetical_ids)
        and all(
            item.hypothetical_event_id
            == _normalized_token(item.hypothetical_event_id)
            and bool(item.event_type.strip())
            and bool(item.description.strip())
            and bool(item.timing_window.strip())
            and bool(item.assumptions)
            and bool(item.uncertainty.strip())
            and item.explicitly_hypothetical
            and not _EXACT_DATE.search(item.description)
            and not _EXACT_DATE.search(item.timing_window)
            and bool(
                _CONDITIONAL_LANGUAGE.search(item.description)
                or _CONDITIONAL_LANGUAGE.search(item.timing_window)
            )
            for item in scenario.hypothetical_future_events
        )
    )
    result.require(
        "scenario_hypothetical_events",
        hypothetical_ok,
        "hypothetical_future_events",
        "Hypothetical events require explicit labels, assumptions, uncertainty, conditional wording, and no fabricated exact dates.",
    )

    support_refs = set(scenario.supporting_pattern_refs)
    counter_refs = set(scenario.counter_pattern_refs)
    pattern_refs = support_refs | counter_refs
    result.require(
        "scenario_pattern_references",
        (
            support_refs <= set(primary_by_ref)
            and counter_refs <= set(counter_by_ref)
            and not (support_refs & counter_refs)
            and (
                bool(pattern_refs)
                or scenario.scenario_type
                is ScenarioType.INSUFFICIENT_EVIDENCE
            )
            and all(
                match_by_ref[pattern_ref].recommendation
                in {
                    MatchRecommendation.INCLUDE,
                    MatchRecommendation.INCLUDE_WITH_WARNING,
                }
                for pattern_ref in pattern_refs
            )
        ),
        "supporting_pattern_refs",
        "Scenario patterns must be eligible matches from their correct retrieval lanes.",
    )

    grounding_by_ref = {
        item.pattern_ref: item for item in scenario.pattern_grounding
    }
    grounding_ok = (
        not _duplicates(
            tuple(item.pattern_ref for item in scenario.pattern_grounding)
        )
        and set(grounding_by_ref) == pattern_refs
        and pattern_refs <= set(match_by_ref)
        and all(
            (
                grounding.lane is match_by_ref[pattern_ref].lane
                and grounding.recommendation
                is match_by_ref[pattern_ref].recommendation
                and grounding.match_quality
                is match_by_ref[pattern_ref].match_quality
                and bool(grounding.reused_causal_features)
                and set(grounding.reused_causal_features)
                <= set(
                    match_by_ref[pattern_ref].matched_required_features
                    + match_by_ref[pattern_ref].matched_optional_features
                )
                and _grounding_differences(
                    match_by_ref[pattern_ref]
                )
                <= set(grounding.acknowledged_differences)
                and set(match_by_ref[pattern_ref].warnings)
                <= set(grounding.acknowledged_warnings)
            )
            for pattern_ref, grounding in grounding_by_ref.items()
        )
    )
    result.require(
        "scenario_pattern_grounding",
        grounding_ok,
        "pattern_grounding",
        "Pattern grounding must preserve lane, quality, recommendation, matched causal features, differences, and warnings.",
    )

    step_numbers = tuple(
        step.step_number for step in scenario.timeline_sequence
    )
    step_stage = {
        ScenarioStepType.PRECONDITION: 0,
        ScenarioStepType.TRIGGER: 1,
        ScenarioStepType.INITIAL_REPRICING: 2,
        ScenarioStepType.AMPLIFICATION: 3,
        ScenarioStepType.CONTINUATION: 3,
        ScenarioStepType.EXHAUSTION: 4,
        ScenarioStepType.REVERSAL: 5,
        ScenarioStepType.INVALIDATION: 5,
    }
    stages = tuple(
        step_stage[step.step_type]
        for step in scenario.timeline_sequence
    )
    step_evidence = {
        item
        for step in scenario.timeline_sequence
        for item in step.current_evidence_ids
    }
    step_exposures = {
        item
        for step in scenario.timeline_sequence
        for item in step.current_exposure_types
    }
    step_hypothetical_ids = {
        item
        for step in scenario.timeline_sequence
        for item in step.hypothetical_event_ids
    }
    step_mechanisms = {
        item
        for step in scenario.timeline_sequence
        for item in step.transmission_mechanisms
    }
    causal_ok = (
        step_numbers
        == tuple(range(1, len(scenario.timeline_sequence) + 1))
        and bool(step_numbers)
        and stages == tuple(sorted(stages))
        and step_evidence <= set(evidence_by_id)
        and step_exposures <= exposure_types
        and step_event_ids <= set(event_by_id)
        and step_hypothetical_ids <= hypothetical_id_set
        and hypothetical_id_set <= step_hypothetical_ids
        and step_mechanisms <= set(scenario.transmission_mechanisms)
        and any(
            step.step_type
            in {
                ScenarioStepType.PRECONDITION,
                ScenarioStepType.TRIGGER,
            }
            for step in scenario.timeline_sequence
        )
        and any(
            step.step_type
            in {
                ScenarioStepType.INITIAL_REPRICING,
                ScenarioStepType.AMPLIFICATION,
                ScenarioStepType.CONTINUATION,
                ScenarioStepType.REVERSAL,
                ScenarioStepType.INVALIDATION,
            }
            for step in scenario.timeline_sequence
        )
        and all(
            bool(step.description.strip())
            and bool(step.expected_market_effect.strip())
            and bool(step.timing_relation.strip())
            and bool(step.uncertainty.strip())
            and step.content_hash
            == scenario_step_content_hash(step)
            and step.schema_version
            == CURRENT_SCENARIO_STEP_SCHEMA_VERSION
            for step in scenario.timeline_sequence
        )
    )
    if (
        scenario.scenario_type
        is ScenarioType.TECHNICAL_INVALIDATION
    ):
        causal_ok = causal_ok and any(
            step.step_type is ScenarioStepType.INVALIDATION
            for step in scenario.timeline_sequence
        )
    result.require(
        "scenario_causal_sequence",
        causal_ok,
        "timeline_sequence",
        "Scenario steps must be contiguous, chronological, hash-valid, and use only supplied references.",
    )

    driver_families = _driver_families(
        scenario,
        evidence_by_id=evidence_by_id,
    )
    requires_multi_driver = scenario.scenario_type not in {
        ScenarioType.TECHNICAL_INVALIDATION,
        ScenarioType.INSUFFICIENT_EVIDENCE,
    }
    result.require(
        "scenario_independent_drivers",
        not requires_multi_driver or len(driver_families) >= 2,
        "initiating_conditions",
        "Each primary scenario requires at least two independently traceable driver families.",
    )

    available_contradictions = {
        item.evidence_id
        for item in evidence_items
        if item.implication is EvidenceImplication.CONTRADICTORY
        and item.status in usable_evidence_statuses
    }
    result.require(
        "scenario_contradicting_evidence",
        (
            bool(contradicting_ids & available_contradictions)
            if available_contradictions
            else bool(scenario.missing_evidence)
        ),
        "contradicting_current_evidence_ids",
        "A scenario must include available contradictory evidence or explicitly record that it is missing.",
    )

    invalidation_tokens = set(technical_invalidation_tokens(premise))
    invalidation_ok = bool(scenario.invalidation_conditions)
    if (
        scenario.scenario_type
        is ScenarioType.TECHNICAL_INVALIDATION
    ):
        invalidation_ok = invalidation_ok and (
            invalidation_tokens
            <= set(scenario.invalidation_conditions)
            if invalidation_tokens
            else any(
                "unavailable" in item.casefold()
                for item in scenario.invalidation_conditions
            )
        )
    result.require(
        "scenario_invalidation",
        invalidation_ok,
        "invalidation_conditions",
        "Every scenario needs invalidation conditions; the technical-invalidation scenario must preserve the frozen technical condition exactly.",
    )
    result.require(
        "scenario_conditional_language",
        _scenario_has_conditional_linkage(scenario),
        "target_linkage",
        "Target and duration linkage must use explicitly conditional language.",
    )

    text = "\n".join(_all_text(scenario.to_dict()))
    result.require(
        "scenario_prohibited_language",
        (
            not _PROHIBITED_CERTAINTY.search(text)
            and not _TECHNICAL_EVENT_PREDICTION.search(text)
            and not _TRADE_RECOMMENDATION.search(text)
            and not _PROBABILITY_LANGUAGE.search(text)
        ),
        "scenario",
        "Scenario contains prohibited certainty, event prediction, probability, or trade language.",
    )
    result.require(
        "scenario_completeness",
        (
            bool(scenario.summary.strip())
            and bool(scenario.initiating_conditions)
            and bool(scenario.transmission_mechanisms)
            and bool(scenario.timeline_sequence)
            and bool(scenario.limitations)
            and (
                bool(scenario.supporting_current_evidence_ids)
                or scenario.scenario_type
                is ScenarioType.INSUFFICIENT_EVIDENCE
            )
            and scenario.confidence.value
            in QUALITATIVE_CONFIDENCE_VALUES
        ),
        "scenario",
        "Scenario summary, conditions, mechanisms, timeline, limitations, and qualitative confidence are required.",
    )
    return result.result()


def _relationship_key(
    relationship: ScenarioRelationship,
) -> tuple[str, str, str]:
    first = relationship.source_scenario_id
    second = relationship.target_scenario_id
    if relationship.relationship_type in _SYMMETRIC_RELATIONSHIPS:
        first, second = sorted((first, second))
    return (first, second, relationship.relationship_type.value)


def _relevant_exposure_categories(
    state: CurrentMarketState,
) -> set[ExposureCategory]:
    return {
        EXPOSURE_TYPE_CATEGORY[item]
        for item in state.current_exposure_types
        if item in EXPOSURE_TYPE_CATEGORY
    }


def validate_current_scenario_set(
    scenario_set: CurrentScenarioSet,
    *,
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    retrieval: PatternRetrievalResult,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    events: Sequence[MaterialEvent] = (),
) -> ValidationResult:
    if not isinstance(scenario_set, CurrentScenarioSet):
        raise TypeError("scenario_set must be CurrentScenarioSet.")
    result = _ValidationCollector(_SET_RULE_ORDER)
    result.require(
        "set_schema",
        scenario_set.schema_version == CURRENT_SCENARIO_SET_SCHEMA_VERSION,
        "schema_version",
        "Current-scenario-set schema version is unsupported.",
    )
    result.require(
        "set_hash",
        bool(scenario_set.content_hash)
        and scenario_set.content_hash
        == current_scenario_set_content_hash(scenario_set),
        "content_hash",
        "Current-scenario-set content hash is missing or mismatched.",
    )
    result.require(
        "set_identity",
        (
            bool(scenario_set.scenario_set_id)
            and scenario_set.scenario_set_id
            == _normalized_token(scenario_set.scenario_set_id)
            and scenario_set.symbol == state.symbol == premise.symbol
            and scenario_set.applicable_cutoff
            == state.applicable_cutoff
        ),
        "scenario_set_id",
        "Scenario-set identity, symbol, or cutoff is invalid.",
    )
    result.require(
        "set_provenance",
        (
            scenario_set.frozen_technical_premise_hash
            == premise.frozen_content_hash
            and scenario_set.current_state_hash == state.content_hash
            and scenario_set.retrieval_result_hash
            == retrieval.content_hash
            and scenario_set.analyst_type
            is HistoricalAnalystType.LLM
            and bool(scenario_set.model_name.strip())
            and bool(scenario_set.prompt_version.strip())
            and _temporal_point(scenario_set.generated_at)
            >= _temporal_point(scenario_set.applicable_cutoff)
        ),
        "frozen_technical_premise_hash",
        "Scenario-set immutable references or application provenance are invalid.",
    )

    scenario_validations = tuple(
        validate_current_scenario_narrative(
            scenario,
            premise=premise,
            state=state,
            retrieval=retrieval,
            evidence=evidence,
            exposures=exposures,
            events=events,
        )
        for scenario in scenario_set.scenarios
    )
    result.require(
        "set_scenario_validation",
        all(
            validation.is_valid
            and scenario.validation_result == validation
            for scenario, validation in zip(
                scenario_set.scenarios,
                scenario_validations,
                strict=True,
            )
        ),
        "scenarios",
        "One or more scenarios failed deterministic validation or carry stale validation.",
    )

    scenario_count = len(scenario_set.scenarios)
    structured_insufficient = (
        bool(
            scenario_set.insufficient_evidence_reason
            and scenario_set.insufficient_evidence_reason.strip()
        )
        and any(
            item.scenario_type
            is ScenarioType.INSUFFICIENT_EVIDENCE
            for item in scenario_set.scenarios
        )
    )
    result.require(
        "set_scenario_count",
        (
            3 <= scenario_count <= 6
            or (
                1 <= scenario_count < 3
                and structured_insufficient
            )
        ),
        "scenarios",
        "Scenario set requires three to six scenarios, unless a smaller structured insufficient-evidence set explains the limitation.",
    )

    scenario_ids = tuple(
        item.scenario_id for item in scenario_set.scenarios
    )
    result.require(
        "set_scenario_uniqueness",
        (
            not _duplicates(scenario_ids)
            and not _duplicates(
                tuple(
                    item.title.casefold().strip()
                    for item in scenario_set.scenarios
                )
            )
        ),
        "scenarios",
        "Scenario IDs and titles must be unique.",
    )

    scenario_id_set = set(scenario_ids)
    relationship_keys = tuple(
        _relationship_key(item)
        for item in scenario_set.scenario_relationships
    )
    relationship_pairs: dict[
        tuple[str, str], set[ScenarioRelationshipType]
    ] = {}
    relationships_valid = not _duplicates(relationship_keys)
    for relationship in scenario_set.scenario_relationships:
        pair = tuple(
            sorted(
                (
                    relationship.source_scenario_id,
                    relationship.target_scenario_id,
                )
            )
        )
        relationship_pairs.setdefault(pair, set()).add(
            relationship.relationship_type
        )
        relationships_valid = relationships_valid and (
            relationship.schema_version
            == CURRENT_SCENARIO_RELATIONSHIP_SCHEMA_VERSION
            and relationship.content_hash
            == scenario_relationship_content_hash(relationship)
            and relationship.source_scenario_id in scenario_id_set
            and relationship.target_scenario_id in scenario_id_set
            and relationship.source_scenario_id
            != relationship.target_scenario_id
            and bool(relationship.rationale.strip())
        )
    incompatible_combinations = (
        {
            ScenarioRelationshipType.COMPATIBLE,
            ScenarioRelationshipType.MUTUALLY_EXCLUSIVE,
        },
        {
            ScenarioRelationshipType.COMPATIBLE,
            ScenarioRelationshipType.INVALIDATES,
        },
        {
            ScenarioRelationshipType.PREREQUISITE,
            ScenarioRelationshipType.MUTUALLY_EXCLUSIVE,
        },
        {
            ScenarioRelationshipType.SEQUENTIAL,
            ScenarioRelationshipType.MUTUALLY_EXCLUSIVE,
        },
    )
    relationships_valid = relationships_valid and all(
        not any(
            conflict <= relationship_types
            for conflict in incompatible_combinations
        )
        for relationship_types in relationship_pairs.values()
    )
    result.require(
        "set_relationships",
        relationships_valid,
        "scenario_relationships",
        "Scenario relationships are dangling, duplicated, self-referential, hash-invalid, or mutually incompatible.",
    )

    ordinary = tuple(
        item
        for item in scenario_set.scenarios
        if item.scenario_type
        is not ScenarioType.INSUFFICIENT_EVIDENCE
    )
    signatures = tuple(
        _scenario_feature_signature(item) for item in ordinary
    )
    exposure_categories = _relevant_exposure_categories(state)
    has_company_support = bool(
        exposure_categories
        & {
            ExposureCategory.COMPANY,
            ExposureCategory.EVENT_CALENDAR,
        }
        or state.event_ids
    )
    has_broad_support = bool(
        exposure_categories
        & {
            ExposureCategory.SECTOR_AND_COMPETITORS,
            ExposureCategory.MACRO_AND_CROSS_ASSET,
            ExposureCategory.VALUATION_AND_POSITIONING,
        }
        or state.market_regime is not None
    )
    has_multi_factor_support = len(exposure_categories) >= 2
    has_company_scenario = any(
        item.scenario_type
        in {
            ScenarioType.COMPANY_EXECUTION,
            ScenarioType.EARNINGS_AND_GUIDANCE,
            ScenarioType.FINANCING_AND_DILUTION,
            ScenarioType.REGULATORY,
            ScenarioType.EVENT_DRIVEN,
        }
        for item in ordinary
    )
    has_broad_scenario = any(
        item.scenario_type
        in {
            ScenarioType.MACRO_AND_LIQUIDITY,
            ScenarioType.VALUATION_REPRICING,
            ScenarioType.SECTOR_ROTATION,
            ScenarioType.POSITIONING_UNWIND,
            ScenarioType.SHORT_SQUEEZE,
        }
        for item in ordinary
    )
    has_multi_factor = any(
        item.scenario_type is ScenarioType.MULTI_FACTOR
        for item in ordinary
    )
    broad_scenario_types = {
        ScenarioType.MACRO_AND_LIQUIDITY,
        ScenarioType.VALUATION_REPRICING,
        ScenarioType.SECTOR_ROTATION,
        ScenarioType.POSITIONING_UNWIND,
        ScenarioType.SHORT_SQUEEZE,
    }
    has_non_company_specific_path = any(
        item.scenario_type in broad_scenario_types
        and all(
            EXPOSURE_TYPE_CATEGORY.get(exposure_type)
            is not ExposureCategory.COMPANY
            for exposure_type in item.required_current_exposure_types
        )
        for item in ordinary
    )
    diversity_ok = (
        not _duplicates(signatures)
        and (
            not has_company_support
            or has_company_scenario
            or structured_insufficient
        )
        and (
            not has_broad_support
            or has_broad_scenario
            or structured_insufficient
        )
        and (
            not has_multi_factor_support
            or has_multi_factor
            or structured_insufficient
        )
        and (
            not has_broad_support
            or has_non_company_specific_path
            or structured_insufficient
        )
    )
    result.require(
        "set_diversity",
        diversity_ok,
        "scenarios",
        "Scenarios must have distinct structured causal signatures and cover supported company, broad-market, and multi-factor lanes.",
    )
    if len(ordinary) > 1:
        result.warn(
            "set_diversity_warning",
            len(
                {
                    tuple(
                        sorted(
                            item.value
                            for item in scenario.transmission_mechanisms
                        )
                    )
                    for scenario in ordinary
                }
            )
            > 1,
            "scenarios.transmission_mechanisms",
            "All scenarios use the same transmission-mechanism set.",
        )
        result.warn(
            "set_pattern_concentration",
            len(
                {
                    tuple(sorted(item.supporting_pattern_refs))
                    for item in ordinary
                }
            )
            > 1,
            "scenarios.supporting_pattern_refs",
            "All scenarios rely on the same supporting-pattern set.",
        )

    used_counter_refs = {
        pattern_ref
        for scenario in scenario_set.scenarios
        for pattern_ref in scenario.counter_pattern_refs
    }
    result.require(
        "set_counter_patterns",
        (
            not retrieval.counter_patterns
            or bool(used_counter_refs)
            or structured_insufficient
        ),
        "scenarios.counter_pattern_refs",
        "At least one retrieved counter-pattern must challenge the scenario set.",
    )

    invalidation_scenarios = tuple(
        item
        for item in scenario_set.scenarios
        if item.scenario_type
        is ScenarioType.TECHNICAL_INVALIDATION
    )
    invalidation_relationships = tuple(
        item
        for item in scenario_set.scenario_relationships
        if item.relationship_type
        in {
            ScenarioRelationshipType.INVALIDATES,
            ScenarioRelationshipType.COUNTER_SCENARIO,
        }
        and (
            item.source_scenario_id
            in {value.scenario_id for value in invalidation_scenarios}
            or item.target_scenario_id
            in {value.scenario_id for value in invalidation_scenarios}
        )
    )
    result.require(
        "set_technical_invalidation",
        (
            (
                len(invalidation_scenarios) == 1
                and bool(invalidation_relationships)
            )
            or structured_insufficient
        ),
        "scenarios",
        "Exactly one explicit technical-invalidation scenario must be related to a primary scenario.",
    )
    result.require(
        "set_completeness",
        (
            bool(scenario_set.overall_limitations)
            and bool(scenario_set.adversarial_summary.strip())
        ),
        "overall_limitations",
        "Scenario-set limitations and adversarial status summary are required.",
    )
    return result.result()


def finalize_current_scenario_narrative(
    scenario: CurrentScenarioNarrative,
    *,
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    retrieval: PatternRetrievalResult,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    events: Sequence[MaterialEvent] = (),
) -> CurrentScenarioNarrative:
    if not isinstance(scenario, CurrentScenarioNarrative):
        raise TypeError("scenario must be CurrentScenarioNarrative.")
    finalized_steps = tuple(
        replace(
            step,
            content_hash=scenario_step_content_hash(
                replace(step, content_hash="")
            ),
        )
        for step in scenario.timeline_sequence
    )
    seed = replace(
        scenario,
        timeline_sequence=finalized_steps,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(
        seed,
        content_hash=current_scenario_narrative_content_hash(seed),
    )
    validation = validate_current_scenario_narrative(
        seed,
        premise=premise,
        state=state,
        retrieval=retrieval,
        evidence=evidence,
        exposures=exposures,
        events=events,
    )
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=current_scenario_narrative_content_hash(
            finalized
        ),
    )
    repeated = validate_current_scenario_narrative(
        finalized,
        premise=premise,
        state=state,
        retrieval=retrieval,
        evidence=evidence,
        exposures=exposures,
        events=events,
    )
    if repeated != validation:
        raise RuntimeError(
            "Current-scenario narrative validation did not stabilize."
        )
    return finalized


def finalize_current_scenario_set(
    scenario_set: CurrentScenarioSet,
    *,
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    retrieval: PatternRetrievalResult,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    events: Sequence[MaterialEvent] = (),
) -> CurrentScenarioSet:
    if not isinstance(scenario_set, CurrentScenarioSet):
        raise TypeError("scenario_set must be CurrentScenarioSet.")
    finalized_scenarios = tuple(
        finalize_current_scenario_narrative(
            scenario,
            premise=premise,
            state=state,
            retrieval=retrieval,
            evidence=evidence,
            exposures=exposures,
            events=events,
        )
        for scenario in scenario_set.scenarios
    )
    finalized_relationships = tuple(
        replace(
            relationship,
            content_hash=scenario_relationship_content_hash(
                replace(relationship, content_hash="")
            ),
        )
        for relationship in scenario_set.scenario_relationships
    )
    seed = replace(
        scenario_set,
        scenarios=finalized_scenarios,
        scenario_relationships=finalized_relationships,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(
        seed,
        content_hash=current_scenario_set_content_hash(seed),
    )
    validation = validate_current_scenario_set(
        seed,
        premise=premise,
        state=state,
        retrieval=retrieval,
        evidence=evidence,
        exposures=exposures,
        events=events,
    )
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=current_scenario_set_content_hash(finalized),
    )
    repeated = validate_current_scenario_set(
        finalized,
        premise=premise,
        state=state,
        retrieval=retrieval,
        evidence=evidence,
        exposures=exposures,
        events=events,
    )
    if repeated != validation:
        raise RuntimeError(
            "Current-scenario-set validation did not stabilize."
        )
    return finalized


def validate_current_scenario_adversarial_review(
    review: CurrentScenarioAdversarialReview,
    *,
    draft_scenario_set: CurrentScenarioSet,
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    retrieval: PatternRetrievalResult,
    evidence: Sequence[EvidenceItem],
    exposures: Sequence[ExposureItem],
    events: Sequence[MaterialEvent] = (),
) -> ValidationResult:
    if not isinstance(review, CurrentScenarioAdversarialReview):
        raise TypeError(
            "review must be CurrentScenarioAdversarialReview."
        )
    result = _ValidationCollector(
        (
            "review_schema",
            "review_hash",
            "review_provenance",
            "review_references",
            "review_completeness",
            "review_recommendation",
            "review_replacement",
            "review_language",
        )
    )
    result.require(
        "review_schema",
        review.schema_version == CURRENT_SCENARIO_ADVERSARIAL_SCHEMA_VERSION,
        "schema_version",
        "Current-scenario adversarial-review schema is unsupported.",
    )
    result.require(
        "review_hash",
        bool(review.content_hash)
        and review.content_hash
        == current_scenario_adversarial_review_content_hash(review),
        "content_hash",
        "Adversarial-review hash is missing or mismatched.",
    )
    result.require(
        "review_provenance",
        (
            review.applicable_cutoff == state.applicable_cutoff
            and bool(review.provider.strip())
            and bool(review.model_name.strip())
            and bool(review.prompt_version.strip())
            and _temporal_point(review.generated_at)
            >= _temporal_point(review.applicable_cutoff)
        ),
        "provider",
        "Adversarial review has invalid provider, prompt, time, or cutoff provenance.",
    )
    scenario_ids = {
        item.scenario_id for item in draft_scenario_set.scenarios
    }
    evidence_ids = {item.evidence_id for item in evidence}
    pattern_refs = {
        item.pattern_ref
        for item in retrieval.matches + retrieval.counter_patterns
    }
    result.require(
        "review_references",
        (
            set(review.referenced_scenario_ids) == scenario_ids
            and set(review.referenced_current_evidence_ids)
            <= evidence_ids
            and set(review.referenced_pattern_refs) <= pattern_refs
            and not _duplicates(review.referenced_scenario_ids)
            and not _duplicates(
                review.referenced_current_evidence_ids
            )
            and not _duplicates(review.referenced_pattern_refs)
        ),
        "referenced_scenario_ids",
        "Adversarial review must cover every draft scenario and use only supplied evidence and patterns.",
    )
    result.require(
        "review_completeness",
        (
            bool(review.summary.strip())
            and bool(review.strongest_counterargument.strip())
            and bool(review.simpler_explanations)
        ),
        "summary",
        "Adversarial review requires a summary, strongest counterargument, and simpler explanation.",
    )
    has_replacement = review.replacement_scenario_set is not None
    result.require(
        "review_recommendation",
        (
            review.recommendation
            is ScenarioAdversarialRecommendation.REVISE
        )
        == has_replacement,
        "replacement_scenario_set",
        "A full replacement scenario set is required only for revise.",
    )
    replacement_ok = True
    if review.replacement_scenario_set is not None:
        replacement = review.replacement_scenario_set
        replacement_validation = validate_current_scenario_set(
            replacement,
            premise=premise,
            state=state,
            retrieval=retrieval,
            evidence=evidence,
            exposures=exposures,
            events=events,
        )
        replacement_ok = (
            replacement_validation.is_valid
            and replacement.validation_result
            == replacement_validation
            and replacement.frozen_technical_premise_hash
            == draft_scenario_set.frozen_technical_premise_hash
            and replacement.current_state_hash
            == draft_scenario_set.current_state_hash
            and replacement.retrieval_result_hash
            == draft_scenario_set.retrieval_result_hash
            and replacement.applicable_cutoff
            == draft_scenario_set.applicable_cutoff
            and replacement.symbol == draft_scenario_set.symbol
        )
    result.require(
        "review_replacement",
        replacement_ok,
        "replacement_scenario_set",
        "Replacement scenario set is invalid or changes an immutable input boundary.",
    )
    text = "\n".join(_all_text(review.to_dict()))
    result.require(
        "review_language",
        (
            not _PROHIBITED_CERTAINTY.search(text)
            and not _TECHNICAL_EVENT_PREDICTION.search(text)
            and not _TRADE_RECOMMENDATION.search(text)
            and not _PROBABILITY_LANGUAGE.search(text)
        ),
        "review",
        "Adversarial review contains prohibited certainty, prediction, probability, or trade language.",
    )
    return result.result()


def finalize_current_scenario_adversarial_review(
    review: CurrentScenarioAdversarialReview,
) -> CurrentScenarioAdversarialReview:
    if not isinstance(review, CurrentScenarioAdversarialReview):
        raise TypeError(
            "review must be CurrentScenarioAdversarialReview."
        )
    seed = replace(review, content_hash="")
    return replace(
        seed,
        content_hash=current_scenario_adversarial_review_content_hash(
            seed
        ),
    )


def validate_current_scenario_generation_result(
    result_value: CurrentScenarioGenerationResult,
    *,
    premise: FrozenTechnicalPremise | None = None,
    state: CurrentMarketState | None = None,
    retrieval: PatternRetrievalResult | None = None,
) -> ValidationResult:
    if not isinstance(
        result_value,
        CurrentScenarioGenerationResult,
    ):
        raise TypeError(
            "result_value must be CurrentScenarioGenerationResult."
        )
    result = _ValidationCollector(
        (
            "result_schema",
            "result_hash",
            "result_attempts",
            "result_output",
            "result_recommendation",
            "result_input_hashes",
        )
    )
    result.require(
        "result_schema",
        result_value.schema_version
        == CURRENT_SCENARIO_GENERATION_RESULT_SCHEMA_VERSION,
        "schema_version",
        "Current-scenario generation-result schema is unsupported.",
    )
    result.require(
        "result_hash",
        bool(result_value.content_hash)
        and result_value.content_hash
        == current_scenario_generation_result_content_hash(result_value),
        "content_hash",
        "Current-scenario generation-result hash is missing or mismatched.",
    )
    result.require(
        "result_attempts",
        (
            isinstance(result_value.attempts, int)
            and not isinstance(result_value.attempts, bool)
            and result_value.attempts >= 0
        ),
        "attempts",
        "Generation attempts must be a non-negative integer.",
    )
    result.require(
        "result_output",
        (
            result_value.output_hash
            == (
                result_value.scenario_set.content_hash
                if result_value.scenario_set is not None
                else ""
            )
            and (
                not result_value.success
                or (
                    result_value.scenario_set is not None
                    and result_value.validation_result.is_valid
                )
            )
        ),
        "output_hash",
        "Generation result output hash or success state is inconsistent.",
    )
    review = result_value.adversarial_review
    result.require(
        "result_recommendation",
        (
            (
                not result_value.used_adversarial_pass
                and review is None
                and result_value.adversarial_recommendation is None
            )
            or (
                result_value.used_adversarial_pass
                and (
                    (
                        review is None
                        and result_value.adversarial_recommendation
                        is None
                    )
                    or (
                        review is not None
                        and result_value.adversarial_recommendation
                        is review.recommendation
                    )
                )
            )
        ),
        "adversarial_recommendation",
        "Adversarial pass state, review, and recommendation are inconsistent.",
    )
    expected_hashes: dict[str, str] = {}
    if premise is not None:
        expected_hashes["frozen_technical_premise"] = (
            premise.frozen_content_hash
        )
    if state is not None:
        expected_hashes["current_market_state"] = state.content_hash
        expected_hashes["evidence_packet"] = (
            state.evidence_packet_hash
        )
        expected_hashes["exposure_report"] = (
            state.exposure_report_hash
        )
        expected_hashes["material_event_packet"] = str(
            state.provenance.get(
                "material_event_packet_hash",
                "",
            )
        )
    if retrieval is not None:
        expected_hashes["pattern_retrieval_result"] = (
            retrieval.content_hash
        )
    result.require(
        "result_input_hashes",
        all(
            result_value.input_hashes.get(key) == value
            for key, value in expected_hashes.items()
        ),
        "input_hashes",
        "Generation result does not preserve an immutable input hash.",
    )
    return result.result()


def finalize_current_scenario_generation_result(
    result: CurrentScenarioGenerationResult,
) -> CurrentScenarioGenerationResult:
    if not isinstance(result, CurrentScenarioGenerationResult):
        raise TypeError(
            "result must be CurrentScenarioGenerationResult."
        )
    seed = replace(result, content_hash="")
    return replace(
        seed,
        content_hash=current_scenario_generation_result_content_hash(
            seed
        ),
    )


__all__ = [
    "CURRENT_SCENARIO_ADVERSARIAL_SCHEMA_VERSION",
    "CURRENT_SCENARIO_GENERATION_RESULT_SCHEMA_VERSION",
    "CURRENT_SCENARIO_NARRATIVE_SCHEMA_VERSION",
    "CURRENT_SCENARIO_RELATIONSHIP_SCHEMA_VERSION",
    "CURRENT_SCENARIO_SET_SCHEMA_VERSION",
    "CURRENT_SCENARIO_STEP_SCHEMA_VERSION",
    "CURRENT_SCENARIO_VALIDATION_VERSION",
    "CurrentScenarioAdversarialReview",
    "CurrentScenarioGenerationResult",
    "CurrentScenarioNarrative",
    "CurrentScenarioSet",
    "HypotheticalFutureEvent",
    "ScenarioAdversarialRecommendation",
    "ScenarioConfidence",
    "ScenarioPatternGrounding",
    "ScenarioRelationship",
    "ScenarioRelationshipType",
    "ScenarioStep",
    "ScenarioStepType",
    "ScenarioType",
    "TechnicalPremiseCompatibility",
    "current_scenario_adversarial_review_content_hash",
    "current_scenario_generation_result_content_hash",
    "current_scenario_narrative_content_hash",
    "current_scenario_set_content_hash",
    "finalize_current_scenario_adversarial_review",
    "finalize_current_scenario_generation_result",
    "finalize_current_scenario_narrative",
    "finalize_current_scenario_set",
    "scenario_relationship_content_hash",
    "scenario_step_content_hash",
    "technical_invalidation_tokens",
    "validate_current_scenario_adversarial_review",
    "validate_current_scenario_generation_result",
    "validate_current_scenario_inputs",
    "validate_current_scenario_narrative",
    "validate_current_scenario_set",
]
