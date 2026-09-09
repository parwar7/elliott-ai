"""Immutable Phase 11A forecast contracts and legacy-record adapter.

This module freezes technical forecasts for later outcome evaluation. It does
not evaluate outcomes, learn from mistakes, call a model, or alter Elliott Wave
analysis. All normalization is deterministic and uses only supplied or stored
decision-time data.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TypeVar

from .market_scenario import (
    FrozenTechnicalPremise,
    ScenarioDirection,
    frozen_technical_premise_content_hash,
)


FORECAST_SCHEMA_VERSION = "forecast-record-1.0.0"
FORECAST_CALCULATION_VERSION = "forecast-record-canonicalization-1.0.0"
FORECAST_POLICY_VERSION = "forecast-evaluation-policy-1.0.0"
DATASET_CUTOFF_SCHEMA_VERSION = "forecast-dataset-cutoff-1.0.0"
HYPOTHESIS_SCHEMA_VERSION = "forecast-hypothesis-1.0.0"
TARGET_CLAIM_SCHEMA_VERSION = "forecast-target-claim-1.0.0"
INVALIDATION_CLAIM_SCHEMA_VERSION = "forecast-invalidation-claim-1.0.0"
CONFIRMATION_CLAIM_SCHEMA_VERSION = "forecast-confirmation-claim-1.0.0"
COMPLETION_WINDOW_SCHEMA_VERSION = "forecast-completion-window-1.0.0"
FORECAST_EVIDENCE_SCHEMA_VERSION = "forecast-evidence-1.0.0"
EVALUATION_ELIGIBILITY_SCHEMA_VERSION = "forecast-evaluation-eligibility-1.0.0"


class EvaluationEligibilityStatus(StrEnum):
    EVALUABLE = "evaluable"
    LEGACY_UNSCORABLE = "legacy_unscorable"


class ForecastRecordState(StrEnum):
    FINAL = "final"
    PROVISIONAL = "provisional"


class ForecastDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"
    UNKNOWN = "unknown"


class ClaimEvaluationBasis(StrEnum):
    INTRABAR_TOUCH = "intrabar_touch"
    INTRABAR_TOUCH_OR_BREACH = "intrabar_touch_or_breach"
    CANDLE_CLOSE = "candle_close"
    HUMAN_STRUCTURAL_REVIEW = "human_structural_review"


class InvalidationOperator(StrEnum):
    AT_OR_ABOVE = "at_or_above"
    AT_OR_BELOW = "at_or_below"
    ABOVE = "above"
    BELOW = "below"
    STRUCTURAL_CONDITION = "structural_condition"


class ConfirmationOperator(StrEnum):
    AT_OR_ABOVE = "at_or_above"
    AT_OR_BELOW = "at_or_below"
    ABOVE = "above"
    BELOW = "below"
    STRUCTURAL_CONDITION = "structural_condition"


class ForecastEvidenceStatus(StrEnum):
    SUPPORTING = "supporting"
    CONTRADICTORY = "contradictory"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"
    INCOMPARABLE = "incomparable"


class ForecastEvidenceKind(StrEnum):
    PRICE_STRUCTURE = "price_structure"
    ELLIOTT_RULE = "elliott_rule"
    FIBONACCI = "fibonacci"
    VOLUME = "volume"
    RSI = "rsi"
    EWO = "ewo"
    MACD = "macd"
    DURATION = "duration"
    SCALE = "scale"
    CHANNEL = "channel"
    DATA_QUALITY = "data_quality"
    OTHER_TECHNICAL = "other_technical"


class EvidenceRuleClass(StrEnum):
    HARD_STRUCTURAL = "hard_structural"
    SOFT_TECHNICAL = "soft_technical"


class DatasetHashScope(StrEnum):
    STORED_DATASET_SUMMARY = "stored_dataset_summary"
    SOURCE_DOCUMENT = "source_document"


class SameCandleCollisionPolicy(StrEnum):
    INCOMPARABLE_WITHOUT_LOWER_TIMEFRAME_ORDER = (
        "incomparable_without_lower_timeframe_order"
    )


_SOFT_EVIDENCE_KINDS = frozenset(
    {
        ForecastEvidenceKind.FIBONACCI,
        ForecastEvidenceKind.VOLUME,
        ForecastEvidenceKind.RSI,
        ForecastEvidenceKind.EWO,
        ForecastEvidenceKind.MACD,
        ForecastEvidenceKind.DURATION,
        ForecastEvidenceKind.SCALE,
        ForecastEvidenceKind.CHANNEL,
    }
)
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_FROZEN_TECHNICAL_KEYS = frozenset(
    {
        "company_knowledge",
        "company_fundamentals",
        "fundamentals",
        "fundamental_analysis",
        "news",
        "market_news",
        "macro_news",
        "market_scenario",
        "scenario_narratives",
    }
)


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


def canonical_forecast_json(value: Any) -> str:
    """Return the canonical JSON representation used by Phase 11A hashes."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_forecast_json(value).encode("utf-8")).hexdigest()


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


def _normalize_optional_utc(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _normalize_utc(value, field_name=field_name)


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _string_tuple(value: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(value)
    if not all(isinstance(item, str) and item.strip() for item in result):
        raise ValueError(f"{field_name} must contain only non-empty strings.")
    return result


def _string_mapping(value: Mapping[str, str], *, field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    result = dict(value)
    if not all(
        isinstance(key, str)
        and key.strip()
        and isinstance(item, str)
        and item.strip()
        for key, item in result.items()
    ):
        raise ValueError(f"{field_name} requires non-empty string keys and values.")
    return MappingProxyType(dict(sorted(result.items())))


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


T = TypeVar("T")


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
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class DatasetCutoff(_JsonContract):
    dataset_id: str
    source_reference: str
    timeframe: str
    cutoff_utc: str
    dataset_hash: str
    hash_scope: DatasetHashScope
    provider: str
    feed_identity: str
    requested_symbol: str
    resolved_symbol: str
    exchange: str
    session: str
    timezone: str
    adjustment: Any
    price_basis: str
    completed_candles_only: bool = True
    source_document_hash: str | None = None
    captured_at_utc: str | None = None
    bar_count: int | None = None
    schema_version: str = DATASET_CUTOFF_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "dataset_id",
            "source_reference",
            "timeframe",
            "provider",
            "feed_identity",
            "requested_symbol",
            "resolved_symbol",
            "exchange",
            "session",
            "timezone",
            "price_basis",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self, "cutoff_utc", _normalize_utc(self.cutoff_utc, field_name="cutoff_utc")
        )
        object.__setattr__(
            self,
            "captured_at_utc",
            _normalize_optional_utc(
                self.captured_at_utc, field_name="captured_at_utc"
            ),
        )
        object.__setattr__(
            self,
            "hash_scope",
            _enum_value(self.hash_scope, DatasetHashScope, field_name="hash_scope"),
        )
        object.__setattr__(
            self,
            "dataset_hash",
            _require_hash(self.dataset_hash, field_name="dataset_hash"),
        )
        if self.source_document_hash is not None:
            object.__setattr__(
                self,
                "source_document_hash",
                _require_hash(
                    self.source_document_hash, field_name="source_document_hash"
                ),
            )
        if self.hash_scope is DatasetHashScope.SOURCE_DOCUMENT:
            if self.source_document_hash != self.dataset_hash:
                raise ValueError(
                    "A source_document dataset hash must match source_document_hash."
                )
        if not isinstance(self.completed_candles_only, bool):
            raise TypeError("completed_candles_only must be boolean.")
        if self.bar_count is not None and (
            not isinstance(self.bar_count, int) or self.bar_count <= 0
        ):
            raise ValueError("bar_count must be a positive integer when supplied.")
        object.__setattr__(self, "adjustment", _freeze_json(self.adjustment))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatasetCutoff":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ForecastEvidence(_JsonContract):
    evidence_id: str
    hypothesis_ids: tuple[str, ...]
    kind: ForecastEvidenceKind
    status: ForecastEvidenceStatus
    statement: str
    rule_class: EvidenceRuleClass = EvidenceRuleClass.SOFT_TECHNICAL
    source_dataset_ids: tuple[str, ...] = ()
    source_wave_ids: tuple[str, ...] = ()
    comparison_reference: str | None = None
    reason: str | None = None
    schema_version: str = FORECAST_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("evidence_id", "statement", "schema_version"):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "hypothesis_ids",
            _string_tuple(self.hypothesis_ids, field_name="hypothesis_ids"),
        )
        object.__setattr__(
            self,
            "source_dataset_ids",
            _string_tuple(self.source_dataset_ids, field_name="source_dataset_ids"),
        )
        object.__setattr__(
            self,
            "source_wave_ids",
            _string_tuple(self.source_wave_ids, field_name="source_wave_ids"),
        )
        object.__setattr__(
            self,
            "kind",
            _enum_value(self.kind, ForecastEvidenceKind, field_name="kind"),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(self.status, ForecastEvidenceStatus, field_name="status"),
        )
        object.__setattr__(
            self,
            "rule_class",
            _enum_value(self.rule_class, EvidenceRuleClass, field_name="rule_class"),
        )
        if self.kind in _SOFT_EVIDENCE_KINDS and self.rule_class is not EvidenceRuleClass.SOFT_TECHNICAL:
            raise ValueError(
                f"{self.kind.value} evidence is soft and cannot be a structural invalidation."
            )
        if self.status in {
            ForecastEvidenceStatus.UNAVAILABLE,
            ForecastEvidenceStatus.INCOMPARABLE,
        } and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValueError("Unavailable or incomparable evidence requires a reason.")
        if self.comparison_reference is not None:
            object.__setattr__(
                self,
                "comparison_reference",
                _require_text(
                    self.comparison_reference, field_name="comparison_reference"
                ),
            )
        if self.reason is not None:
            object.__setattr__(
                self, "reason", _require_text(self.reason, field_name="reason")
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ForecastEvidence":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class TargetClaim(_JsonContract):
    claim_id: str
    hypothesis_id: str
    target_low: float
    target_high: float
    direction: ForecastDirection
    timeframe: str
    price_basis: str
    rationale: str
    evaluation_basis: ClaimEvaluationBasis = ClaimEvaluationBasis.INTRABAR_TOUCH
    evidence_ids: tuple[str, ...] = ()
    source_wave_ids: tuple[str, ...] = ()
    schema_version: str = TARGET_CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "hypothesis_id",
            "timeframe",
            "price_basis",
            "rationale",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        for name in ("target_low", "target_high"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite.")
            object.__setattr__(self, name, float(value))
        if self.target_low > self.target_high:
            raise ValueError("target_low must be less than or equal to target_high.")
        object.__setattr__(
            self,
            "direction",
            _enum_value(self.direction, ForecastDirection, field_name="direction"),
        )
        object.__setattr__(
            self,
            "evaluation_basis",
            _enum_value(
                self.evaluation_basis,
                ClaimEvaluationBasis,
                field_name="evaluation_basis",
            ),
        )
        if self.evaluation_basis not in {
            ClaimEvaluationBasis.INTRABAR_TOUCH,
            ClaimEvaluationBasis.CANDLE_CLOSE,
        }:
            raise ValueError("Target claims require touch or candle-close evaluation.")
        object.__setattr__(
            self, "evidence_ids", _string_tuple(self.evidence_ids, field_name="evidence_ids")
        )
        object.__setattr__(
            self,
            "source_wave_ids",
            _string_tuple(self.source_wave_ids, field_name="source_wave_ids"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TargetClaim":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class InvalidationClaim(_JsonContract):
    claim_id: str
    hypothesis_id: str
    operator: InvalidationOperator
    condition: str
    timeframe: str
    price_basis: str
    price_level: float | None = None
    evaluation_basis: ClaimEvaluationBasis = (
        ClaimEvaluationBasis.INTRABAR_TOUCH_OR_BREACH
    )
    evidence_ids: tuple[str, ...] = ()
    source_wave_ids: tuple[str, ...] = ()
    schema_version: str = INVALIDATION_CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "hypothesis_id",
            "condition",
            "timeframe",
            "price_basis",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "operator",
            _enum_value(self.operator, InvalidationOperator, field_name="operator"),
        )
        object.__setattr__(
            self,
            "evaluation_basis",
            _enum_value(
                self.evaluation_basis,
                ClaimEvaluationBasis,
                field_name="evaluation_basis",
            ),
        )
        if self.evaluation_basis not in {
            ClaimEvaluationBasis.INTRABAR_TOUCH_OR_BREACH,
            ClaimEvaluationBasis.CANDLE_CLOSE,
            ClaimEvaluationBasis.HUMAN_STRUCTURAL_REVIEW,
        }:
            raise ValueError("Invalidation claims use breach, close, or structural review.")
        if self.price_level is not None:
            if not isinstance(self.price_level, (int, float)) or not math.isfinite(
                float(self.price_level)
            ):
                raise ValueError("price_level must be finite when supplied.")
            object.__setattr__(self, "price_level", float(self.price_level))
        if self.operator is InvalidationOperator.STRUCTURAL_CONDITION:
            if self.evaluation_basis is not ClaimEvaluationBasis.HUMAN_STRUCTURAL_REVIEW:
                raise ValueError(
                    "A structural invalidation must use human_structural_review."
                )
        elif self.price_level is None:
            raise ValueError("A price invalidation operator requires price_level.")
        object.__setattr__(
            self, "evidence_ids", _string_tuple(self.evidence_ids, field_name="evidence_ids")
        )
        object.__setattr__(
            self,
            "source_wave_ids",
            _string_tuple(self.source_wave_ids, field_name="source_wave_ids"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InvalidationClaim":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ConfirmationClaim(_JsonContract):
    claim_id: str
    hypothesis_id: str
    operator: ConfirmationOperator
    condition: str
    timeframe: str
    price_basis: str
    price_level: float | None = None
    evaluation_basis: ClaimEvaluationBasis = ClaimEvaluationBasis.CANDLE_CLOSE
    evidence_ids: tuple[str, ...] = ()
    source_wave_ids: tuple[str, ...] = ()
    schema_version: str = CONFIRMATION_CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "hypothesis_id",
            "condition",
            "timeframe",
            "price_basis",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "operator",
            _enum_value(self.operator, ConfirmationOperator, field_name="operator"),
        )
        object.__setattr__(
            self,
            "evaluation_basis",
            _enum_value(
                self.evaluation_basis,
                ClaimEvaluationBasis,
                field_name="evaluation_basis",
            ),
        )
        if self.evaluation_basis not in {
            ClaimEvaluationBasis.CANDLE_CLOSE,
            ClaimEvaluationBasis.INTRABAR_TOUCH,
            ClaimEvaluationBasis.HUMAN_STRUCTURAL_REVIEW,
        }:
            raise ValueError("Confirmation claims use close, touch, or structural review.")
        if self.price_level is not None:
            if not isinstance(self.price_level, (int, float)) or not math.isfinite(
                float(self.price_level)
            ):
                raise ValueError("price_level must be finite when supplied.")
            object.__setattr__(self, "price_level", float(self.price_level))
        if self.operator is ConfirmationOperator.STRUCTURAL_CONDITION:
            if self.evaluation_basis is not ClaimEvaluationBasis.HUMAN_STRUCTURAL_REVIEW:
                raise ValueError(
                    "A structural confirmation must use human_structural_review."
                )
        elif self.price_level is None:
            raise ValueError("A price confirmation operator requires price_level.")
        object.__setattr__(
            self, "evidence_ids", _string_tuple(self.evidence_ids, field_name="evidence_ids")
        )
        object.__setattr__(
            self,
            "source_wave_ids",
            _string_tuple(self.source_wave_ids, field_name="source_wave_ids"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ConfirmationClaim":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ExpectedCompletionWindow(_JsonContract):
    window_id: str
    hypothesis_id: str
    start_utc: str
    end_utc: str
    timeframe: str
    start_rule: str
    end_rule: str
    rationale: str
    schema_version: str = COMPLETION_WINDOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "window_id",
            "hypothesis_id",
            "timeframe",
            "start_rule",
            "end_rule",
            "rationale",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self, "start_utc", _normalize_utc(self.start_utc, field_name="start_utc")
        )
        object.__setattr__(
            self, "end_utc", _normalize_utc(self.end_utc, field_name="end_utc")
        )
        if self.start_utc > self.end_utc:
            raise ValueError("Expected completion start must not be after its end.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExpectedCompletionWindow":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MainHypothesis(_JsonContract):
    hypothesis_id: str
    label: str
    direction: ForecastDirection
    degree: str
    wave_label: str
    pattern_family: str
    completion_status: str
    summary: str
    count: Any
    target_claim_ids: tuple[str, ...] = ()
    invalidation_claim_ids: tuple[str, ...] = ()
    confirmation_claim_ids: tuple[str, ...] = ()
    completion_window_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    original_uncalibrated_confidence: Any = None
    schema_version: str = HYPOTHESIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _normalize_hypothesis(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MainHypothesis":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class AlternativeHypothesis(_JsonContract):
    hypothesis_id: str
    label: str
    direction: ForecastDirection
    degree: str
    wave_label: str
    pattern_family: str
    completion_status: str
    summary: str
    count: Any
    activation_conditions: tuple[str, ...] = ()
    distinguishing_evidence_needed: tuple[str, ...] = ()
    target_claim_ids: tuple[str, ...] = ()
    invalidation_claim_ids: tuple[str, ...] = ()
    confirmation_claim_ids: tuple[str, ...] = ()
    completion_window_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    original_uncalibrated_confidence: Any = None
    schema_version: str = HYPOTHESIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _normalize_hypothesis(self)
        object.__setattr__(
            self,
            "activation_conditions",
            _string_tuple(
                self.activation_conditions, field_name="activation_conditions"
            ),
        )
        object.__setattr__(
            self,
            "distinguishing_evidence_needed",
            _string_tuple(
                self.distinguishing_evidence_needed,
                field_name="distinguishing_evidence_needed",
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AlternativeHypothesis":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


def _normalize_hypothesis(value: MainHypothesis | AlternativeHypothesis) -> None:
    for name in (
        "hypothesis_id",
        "label",
        "degree",
        "wave_label",
        "pattern_family",
        "completion_status",
        "summary",
        "schema_version",
    ):
        object.__setattr__(
            value, name, _require_text(getattr(value, name), field_name=name)
        )
    object.__setattr__(
        value,
        "direction",
        _enum_value(value.direction, ForecastDirection, field_name="direction"),
    )
    object.__setattr__(value, "count", _freeze_json(value.count))
    object.__setattr__(
        value,
        "original_uncalibrated_confidence",
        _freeze_json(value.original_uncalibrated_confidence),
    )
    for name in (
        "target_claim_ids",
        "invalidation_claim_ids",
        "confirmation_claim_ids",
        "completion_window_ids",
        "evidence_ids",
    ):
        object.__setattr__(
            value,
            name,
            _string_tuple(getattr(value, name), field_name=name),
        )


@dataclass(frozen=True, slots=True)
class EvaluationEligibility(_JsonContract):
    status: EvaluationEligibilityStatus
    record_state: ForecastRecordState
    reason_codes: tuple[str, ...]
    typed_claims_present: bool
    price_accuracy_evaluable: bool
    timing_accuracy_evaluable: bool
    schema_version: str = EVALUATION_ELIGIBILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                EvaluationEligibilityStatus,
                field_name="status",
            ),
        )
        object.__setattr__(
            self,
            "record_state",
            _enum_value(
                self.record_state, ForecastRecordState, field_name="record_state"
            ),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _string_tuple(self.reason_codes, field_name="reason_codes"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )
        for name in (
            "typed_claims_present",
            "price_accuracy_evaluable",
            "timing_accuracy_evaluable",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        if self.status is EvaluationEligibilityStatus.LEGACY_UNSCORABLE:
            if self.typed_claims_present:
                raise ValueError("legacy_unscorable records cannot claim typed inputs.")
            if self.price_accuracy_evaluable or self.timing_accuracy_evaluable:
                raise ValueError("legacy_unscorable records cannot be marked evaluable.")
        elif not self.typed_claims_present:
            raise ValueError("Evaluable records require typed claims.")
        if self.record_state is ForecastRecordState.PROVISIONAL and not self.reason_codes:
            raise ValueError("Provisional records require an explicit reason code.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationEligibility":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ForecastRecord(_JsonContract):
    forecast_id: str
    forecast_version: int
    supersedes_forecast_id: str | None
    source_analysis_run_id: int
    source_degree_resolution_id: int
    created_at_utc: str
    analysis_cutoff_utc: str
    symbol: str
    exchange: str
    provider: str
    model: str
    feed: str
    session: str
    timezone: str
    adjustment: Any
    price_basis: str
    dataset_cutoffs: tuple[DatasetCutoff, ...]
    frozen_technical_premise: FrozenTechnicalPremise
    main_hypothesis: MainHypothesis
    alternative_hypotheses: tuple[AlternativeHypothesis, ...]
    target_claims: tuple[TargetClaim, ...]
    invalidation_claims: tuple[InvalidationClaim, ...]
    confirmation_claims: tuple[ConfirmationClaim, ...]
    expected_completion_windows: tuple[ExpectedCompletionWindow, ...]
    evidence: tuple[ForecastEvidence, ...]
    evaluation_eligibility: EvaluationEligibility
    provider_versions: Mapping[str, str]
    model_versions: Mapping[str, str]
    prompt_versions: Mapping[str, str]
    policy_versions: Mapping[str, str]
    original_uncalibrated_confidence: Any
    source_hashes: Mapping[str, str]
    same_candle_collision_policy: SameCandleCollisionPolicy = (
        SameCandleCollisionPolicy.INCOMPARABLE_WITHOUT_LOWER_TIMEFRAME_ORDER
    )
    schema_version: str = FORECAST_SCHEMA_VERSION
    calculation_version: str = FORECAST_CALCULATION_VERSION
    policy_version: str = FORECAST_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "forecast_id",
            "symbol",
            "exchange",
            "provider",
            "model",
            "feed",
            "session",
            "timezone",
            "price_basis",
            "schema_version",
            "calculation_version",
            "policy_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        if not isinstance(self.forecast_version, int) or self.forecast_version < 1:
            raise ValueError("forecast_version must be a positive integer.")
        for name in ("source_analysis_run_id", "source_degree_resolution_id"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.supersedes_forecast_id is not None:
            object.__setattr__(
                self,
                "supersedes_forecast_id",
                _require_text(
                    self.supersedes_forecast_id,
                    field_name="supersedes_forecast_id",
                ),
            )
            if self.supersedes_forecast_id == self.forecast_id:
                raise ValueError("A forecast cannot supersede itself.")
        object.__setattr__(
            self,
            "created_at_utc",
            _normalize_utc(self.created_at_utc, field_name="created_at_utc"),
        )
        object.__setattr__(
            self,
            "analysis_cutoff_utc",
            _normalize_utc(
                self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"
            ),
        )
        object.__setattr__(self, "adjustment", _freeze_json(self.adjustment))
        object.__setattr__(
            self,
            "dataset_cutoffs",
            _model_tuple(
                self.dataset_cutoffs, DatasetCutoff, field_name="dataset_cutoffs"
            ),
        )
        if isinstance(self.frozen_technical_premise, Mapping):
            object.__setattr__(
                self,
                "frozen_technical_premise",
                FrozenTechnicalPremise.from_dict(self.frozen_technical_premise),
            )
        elif not isinstance(self.frozen_technical_premise, FrozenTechnicalPremise):
            raise TypeError("frozen_technical_premise has an invalid type.")
        if isinstance(self.main_hypothesis, Mapping):
            object.__setattr__(
                self,
                "main_hypothesis",
                MainHypothesis.from_dict(self.main_hypothesis),
            )
        elif not isinstance(self.main_hypothesis, MainHypothesis):
            raise TypeError("main_hypothesis has an invalid type.")
        for name, model_type in (
            ("alternative_hypotheses", AlternativeHypothesis),
            ("target_claims", TargetClaim),
            ("invalidation_claims", InvalidationClaim),
            ("confirmation_claims", ConfirmationClaim),
            ("expected_completion_windows", ExpectedCompletionWindow),
            ("evidence", ForecastEvidence),
        ):
            object.__setattr__(
                self,
                name,
                _model_tuple(getattr(self, name), model_type, field_name=name),
            )
        if isinstance(self.evaluation_eligibility, Mapping):
            object.__setattr__(
                self,
                "evaluation_eligibility",
                EvaluationEligibility.from_dict(self.evaluation_eligibility),
            )
        elif not isinstance(self.evaluation_eligibility, EvaluationEligibility):
            raise TypeError("evaluation_eligibility has an invalid type.")
        for name in (
            "provider_versions",
            "model_versions",
            "prompt_versions",
            "policy_versions",
        ):
            object.__setattr__(
                self,
                name,
                _string_mapping(getattr(self, name), field_name=name),
            )
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        for key, value in source_hashes.items():
            _require_hash(value, field_name=f"source_hashes[{key!r}]")
        object.__setattr__(self, "source_hashes", source_hashes)
        object.__setattr__(
            self,
            "same_candle_collision_policy",
            _enum_value(
                self.same_candle_collision_policy,
                SameCandleCollisionPolicy,
                field_name="same_candle_collision_policy",
            ),
        )
        object.__setattr__(
            self,
            "original_uncalibrated_confidence",
            _freeze_json(self.original_uncalibrated_confidence),
        )
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(cls, **values: Any) -> "ForecastRecord":
        supplied_hash = values.pop("content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("forecast_id"):
            main = values.get("main_hypothesis")
            main_id = (
                main.hypothesis_id
                if isinstance(main, MainHypothesis)
                else str(dict(main or {}).get("hypothesis_id") or "unknown")
            )
            identity_seed = {
                "schema_version": values.get(
                    "schema_version", FORECAST_SCHEMA_VERSION
                ),
                "source_analysis_run_id": values.get("source_analysis_run_id"),
                "source_degree_resolution_id": values.get(
                    "source_degree_resolution_id"
                ),
                "analysis_cutoff_utc": _normalize_utc(
                    values.get("analysis_cutoff_utc"),
                    field_name="analysis_cutoff_utc",
                ),
                "forecast_version": values.get("forecast_version"),
                "supersedes_forecast_id": values.get("supersedes_forecast_id"),
                "main_hypothesis_id": main_id,
            }
            values["forecast_id"] = f"forecast_{canonical_sha256(identity_seed)[:32]}"
        record = cls(**values, content_hash="")
        record = replace(record, content_hash=forecast_record_content_hash(record))
        errors = validate_forecast_record(record)
        if errors:
            raise ValueError("Invalid ForecastRecord: " + " ".join(errors))
        return record

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ForecastRecord":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        record = cls(**raw)
        if verify_hash:
            errors = validate_forecast_record(record)
            if errors:
                raise ValueError("Invalid ForecastRecord: " + " ".join(errors))
        return record

    @property
    def supporting_evidence(self) -> tuple[ForecastEvidence, ...]:
        return tuple(
            item
            for item in self.evidence
            if item.status is ForecastEvidenceStatus.SUPPORTING
        )

    @property
    def contradictory_evidence(self) -> tuple[ForecastEvidence, ...]:
        return tuple(
            item
            for item in self.evidence
            if item.status is ForecastEvidenceStatus.CONTRADICTORY
        )

    @property
    def unavailable_evidence(self) -> tuple[ForecastEvidence, ...]:
        return tuple(
            item
            for item in self.evidence
            if item.status is ForecastEvidenceStatus.UNAVAILABLE
        )

    @property
    def incomparable_evidence(self) -> tuple[ForecastEvidence, ...]:
        return tuple(
            item
            for item in self.evidence
            if item.status is ForecastEvidenceStatus.INCOMPARABLE
        )


def forecast_record_content_hash(value: ForecastRecord | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ForecastRecord) else _json_value(value)
    if not isinstance(payload, dict):
        raise TypeError("ForecastRecord hash input must be a mapping.")
    payload = dict(payload)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def stored_record_content_hash(value: Mapping[str, Any]) -> str:
    """Hash one decoded stored source row without consulting current files."""

    return canonical_sha256(dict(value))


def _duplicates(values: Sequence[str]) -> set[str]:
    seen: set[str] = set()
    duplicated: set[str] = set()
    for value in values:
        if value in seen:
            duplicated.add(value)
        seen.add(value)
    return duplicated


def _find_forbidden_keys(value: Any, path: str = "technical_structure") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if str(key).casefold() in _FORBIDDEN_FROZEN_TECHNICAL_KEYS:
                findings.append(child_path)
            findings.extend(_find_forbidden_keys(item, child_path))
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            findings.extend(_find_forbidden_keys(item, f"{path}[{index}]"))
    return findings


def validate_forecast_record(
    value: ForecastRecord | Mapping[str, Any],
) -> tuple[str, ...]:
    """Return deterministic validation errors, including canonical hash failures."""

    if isinstance(value, ForecastRecord):
        record = value
    else:
        try:
            record = ForecastRecord.from_dict(value, verify_hash=False)
        except (TypeError, ValueError) as exc:
            return (str(exc),)
    errors: list[str] = []
    if record.schema_version != FORECAST_SCHEMA_VERSION:
        errors.append("Unsupported forecast schema_version.")
    if record.calculation_version != FORECAST_CALCULATION_VERSION:
        errors.append("Unsupported forecast calculation_version.")
    if record.policy_version != FORECAST_POLICY_VERSION:
        errors.append("Unsupported forecast policy_version.")
    if record.supersedes_forecast_id is None and record.forecast_version != 1:
        errors.append("A forecast without a predecessor must have version 1.")
    if record.supersedes_forecast_id is not None and record.forecast_version == 1:
        errors.append("A superseding forecast must have a version greater than 1.")
    if not record.content_hash:
        errors.append("Forecast content_hash is required.")
    elif record.content_hash != forecast_record_content_hash(record):
        errors.append("Forecast content_hash does not match its canonical payload.")
    premise = record.frozen_technical_premise
    if premise.frozen_content_hash != frozen_technical_premise_content_hash(premise):
        errors.append("Frozen technical premise hash does not match its payload.")
    if premise.symbol.casefold() != record.symbol.casefold():
        errors.append("Frozen technical premise symbol does not match the forecast.")
    if premise.market_data_cutoff != record.analysis_cutoff_utc:
        errors.append("Frozen technical premise cutoff does not match the forecast cutoff.")
    if str(premise.resolution_id) != str(record.source_degree_resolution_id):
        errors.append("Frozen technical premise resolution reference does not match.")
    forbidden = _find_forbidden_keys(premise.technical_structure)
    if forbidden:
        errors.append(
            "Frozen technical premise contains post-technical context: "
            + ", ".join(forbidden)
            + "."
        )
    for required_hash in ("analysis_run", "degree_resolution"):
        if required_hash not in record.source_hashes:
            errors.append(f"source_hashes is missing {required_hash}.")

    dataset_ids = [item.dataset_id for item in record.dataset_cutoffs]
    if not dataset_ids:
        errors.append("At least one decision-time dataset cutoff is required.")
    duplicates = _duplicates(dataset_ids)
    if duplicates:
        errors.append("Duplicate dataset IDs: " + ", ".join(sorted(duplicates)) + ".")
    analysis_cutoff = datetime.fromisoformat(record.analysis_cutoff_utc)
    if datetime.fromisoformat(record.created_at_utc) < analysis_cutoff:
        errors.append("Forecast creation time cannot precede its analysis cutoff.")
    for dataset in record.dataset_cutoffs:
        if datetime.fromisoformat(dataset.cutoff_utc) > analysis_cutoff:
            errors.append(
                f"Dataset {dataset.dataset_id} extends beyond the analysis cutoff."
            )
        if dataset.requested_symbol.casefold() != record.symbol.casefold():
            errors.append(
                f"Dataset {dataset.dataset_id} requested symbol does not match the forecast."
            )
    has_incomplete_candle = any(
        not item.completed_candles_only for item in record.dataset_cutoffs
    )
    if (
        has_incomplete_candle
        and record.evaluation_eligibility.record_state
        is not ForecastRecordState.PROVISIONAL
    ):
        errors.append("A forecast using an incomplete candle must be provisional.")

    hypotheses = (record.main_hypothesis,) + record.alternative_hypotheses
    hypothesis_ids = [item.hypothesis_id for item in hypotheses]
    duplicates = _duplicates(hypothesis_ids)
    if duplicates:
        errors.append("Duplicate hypothesis IDs: " + ", ".join(sorted(duplicates)) + ".")
    known_hypotheses = set(hypothesis_ids)
    claim_groups: tuple[tuple[str, Sequence[Any]], ...] = (
        ("target", record.target_claims),
        ("invalidation", record.invalidation_claims),
        ("confirmation", record.confirmation_claims),
    )
    all_claim_ids: list[str] = []
    for kind, claims in claim_groups:
        ids = [item.claim_id for item in claims]
        all_claim_ids.extend(ids)
        duplicates = _duplicates(ids)
        if duplicates:
            errors.append(
                f"Duplicate {kind} claim IDs: " + ", ".join(sorted(duplicates)) + "."
            )
        for claim in claims:
            if claim.hypothesis_id not in known_hypotheses:
                errors.append(
                    f"Claim {claim.claim_id} references an unknown hypothesis."
                )
    duplicates = _duplicates(all_claim_ids)
    if duplicates:
        errors.append("Claim IDs collide across claim types: " + ", ".join(sorted(duplicates)) + ".")

    window_ids = [item.window_id for item in record.expected_completion_windows]
    duplicates = _duplicates(window_ids)
    if duplicates:
        errors.append("Duplicate completion-window IDs: " + ", ".join(sorted(duplicates)) + ".")
    for window in record.expected_completion_windows:
        if window.hypothesis_id not in known_hypotheses:
            errors.append(
                f"Completion window {window.window_id} references an unknown hypothesis."
            )
        if datetime.fromisoformat(window.start_utc) < analysis_cutoff:
            errors.append(
                f"Completion window {window.window_id} begins before the analysis cutoff."
            )

    evidence_ids = [item.evidence_id for item in record.evidence]
    duplicates = _duplicates(evidence_ids)
    if duplicates:
        errors.append("Duplicate evidence IDs: " + ", ".join(sorted(duplicates)) + ".")
    known_evidence = set(evidence_ids)
    known_datasets = set(dataset_ids)
    for item in record.evidence:
        unknown_hypotheses = sorted(set(item.hypothesis_ids) - known_hypotheses)
        if unknown_hypotheses:
            errors.append(
                f"Evidence {item.evidence_id} references unknown hypotheses: "
                + ", ".join(unknown_hypotheses)
                + "."
            )
        unknown_datasets = sorted(set(item.source_dataset_ids) - known_datasets)
        if unknown_datasets:
            errors.append(
                f"Evidence {item.evidence_id} references unknown datasets: "
                + ", ".join(unknown_datasets)
                + "."
            )
    claim_by_id = {
        item.claim_id: item
        for group in (record.target_claims, record.invalidation_claims, record.confirmation_claims)
        for item in group
    }
    windows_by_id = {item.window_id: item for item in record.expected_completion_windows}
    hypothesis_by_id = {item.hypothesis_id: item for item in hypotheses}
    for hypothesis in hypotheses:
        for attribute in (
            "target_claim_ids",
            "invalidation_claim_ids",
            "confirmation_claim_ids",
        ):
            for claim_id in getattr(hypothesis, attribute):
                claim = claim_by_id.get(claim_id)
                if claim is None:
                    errors.append(
                        f"Hypothesis {hypothesis.hypothesis_id} references unknown claim {claim_id}."
                    )
                elif claim.hypothesis_id != hypothesis.hypothesis_id:
                    errors.append(
                        f"Claim {claim_id} belongs to a different hypothesis."
                    )
        for window_id in hypothesis.completion_window_ids:
            window = windows_by_id.get(window_id)
            if window is None:
                errors.append(
                    f"Hypothesis {hypothesis.hypothesis_id} references unknown window {window_id}."
                )
            elif window.hypothesis_id != hypothesis.hypothesis_id:
                errors.append(f"Window {window_id} belongs to a different hypothesis.")
        unknown_evidence = sorted(set(hypothesis.evidence_ids) - known_evidence)
        if unknown_evidence:
            errors.append(
                f"Hypothesis {hypothesis.hypothesis_id} references unknown evidence: "
                + ", ".join(unknown_evidence)
                + "."
            )
    for claim in claim_by_id.values():
        unknown_evidence = sorted(set(claim.evidence_ids) - known_evidence)
        if unknown_evidence:
            errors.append(
                f"Claim {claim.claim_id} references unknown evidence: "
                + ", ".join(unknown_evidence)
                + "."
            )
        owner = hypothesis_by_id[claim.hypothesis_id]
        expected_attribute = (
            "target_claim_ids"
            if isinstance(claim, TargetClaim)
            else (
                "invalidation_claim_ids"
                if isinstance(claim, InvalidationClaim)
                else "confirmation_claim_ids"
            )
        )
        if claim.claim_id not in getattr(owner, expected_attribute):
            errors.append(
                f"Claim {claim.claim_id} is not linked by its owning hypothesis."
            )
    for window in record.expected_completion_windows:
        owner = hypothesis_by_id[window.hypothesis_id]
        if window.window_id not in owner.completion_window_ids:
            errors.append(
                f"Window {window.window_id} is not linked by its owning hypothesis."
            )

    eligibility = record.evaluation_eligibility
    typed_claims_present = bool(
        record.target_claims
        or record.invalidation_claims
        or record.confirmation_claims
        or record.expected_completion_windows
    )
    if eligibility.typed_claims_present != typed_claims_present:
        errors.append("Evaluation eligibility disagrees with typed-claim presence.")
    if eligibility.status is EvaluationEligibilityStatus.LEGACY_UNSCORABLE:
        if typed_claims_present:
            errors.append("legacy_unscorable forecasts cannot contain reconstructed claims.")
    else:
        if not record.target_claims:
            errors.append("An evaluable forecast requires at least one target claim.")
        if not record.invalidation_claims:
            errors.append("An evaluable forecast requires at least one invalidation claim.")
        if not record.expected_completion_windows:
            errors.append("An evaluable forecast requires an expected completion window.")
        if not eligibility.price_accuracy_evaluable:
            errors.append("Typed forecasts must enable separate price evaluation.")
        if not eligibility.timing_accuracy_evaluable:
            errors.append("Typed forecasts must enable separate timing evaluation.")
    if record.original_uncalibrated_confidence is None:
        errors.append("Original uncalibrated confidence must be preserved explicitly.")
    return tuple(errors)


def _direction(value: Any) -> ForecastDirection:
    try:
        return ForecastDirection(str(value).casefold())
    except ValueError:
        return ForecastDirection.UNKNOWN


def _safe_timestamp(value: Any, *, field_name: str) -> str:
    return _normalize_utc(value, field_name=field_name)


def _legacy_version(label: str, value: Any) -> str:
    return f"legacy_unversioned:{label}:{canonical_sha256(value)[:16]}"


def dataset_cutoff_from_stored_summary(
    summary: Mapping[str, Any], *, requested_symbol: str
) -> DatasetCutoff:
    snapshot = summary.get("snapshot")
    snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    source = str(summary.get("source") or "stored_market_data_summary")
    timeframe = str(summary.get("timeframe") or "unknown")
    cutoff = _safe_timestamp(summary.get("end_date"), field_name="market_data.end_date")
    summary_hash = canonical_sha256(summary)
    source_document_hash = snapshot.get("source_document_hash") or summary.get(
        "source_document_hash"
    )
    if not isinstance(source_document_hash, str) or _HASH_PATTERN.fullmatch(
        source_document_hash
    ) is None:
        source_document_hash = None
    provider = str(
        snapshot.get("provider")
        or snapshot.get("source_provider")
        or snapshot.get("source_mode")
        or "stored_snapshot"
    )
    resolved_symbol = str(
        snapshot.get("resolved_symbol")
        or snapshot.get("provider_symbol")
        or snapshot.get("chart_symbol")
        or snapshot.get("requested_symbol")
        or requested_symbol
    )
    exchange = str(
        snapshot.get("listed_exchange")
        or snapshot.get("exchange")
        or "unknown"
    )
    session = str(snapshot.get("session") or "unknown")
    adjustment = snapshot.get("adjustment_basis", snapshot.get("adjustment", "unknown"))
    timezone_name = str(
        snapshot.get("chart_timezone")
        or snapshot.get("exchange_timezone")
        or "unknown"
    )
    feed_identity = str(
        snapshot.get("feed_identity")
        or "|".join(
            (
                "legacy_unversioned",
                provider,
                exchange,
                resolved_symbol,
                timeframe,
                session,
                canonical_sha256(adjustment)[:12],
            )
        )
    )
    captured = snapshot.get("captured_at")
    captured_at = (
        _safe_timestamp(captured, field_name="market_data.captured_at")
        if isinstance(captured, str) and captured.strip()
        else None
    )
    explicitly_incomplete = bool(
        snapshot.get("incomplete_candle")
        or snapshot.get("includes_incomplete_candle")
        or summary.get("incomplete_candle")
    )
    dataset_seed = {
        "source": source,
        "timeframe": timeframe,
        "cutoff": cutoff,
        "summary_hash": summary_hash,
    }
    return DatasetCutoff(
        dataset_id=f"dataset_{canonical_sha256(dataset_seed)[:24]}",
        source_reference=source,
        timeframe=timeframe,
        cutoff_utc=cutoff,
        dataset_hash=summary_hash,
        hash_scope=DatasetHashScope.STORED_DATASET_SUMMARY,
        provider=provider,
        feed_identity=feed_identity,
        requested_symbol=str(snapshot.get("requested_symbol") or requested_symbol),
        resolved_symbol=resolved_symbol,
        exchange=exchange,
        session=session,
        timezone=timezone_name,
        adjustment=adjustment,
        price_basis=str(snapshot.get("price_basis") or "ohlc"),
        completed_candles_only=not explicitly_incomplete,
        source_document_hash=source_document_hash,
        captured_at_utc=captured_at,
        bar_count=(
            int(summary["bar_count"])
            if isinstance(summary.get("bar_count"), int)
            and int(summary["bar_count"]) > 0
            else None
        ),
    )


def _active_wave(response: Mapping[str, Any]) -> Mapping[str, Any]:
    active = response.get("active_position")
    wave_ids = active.get("wave_ids", ()) if isinstance(active, Mapping) else ()
    active_id = wave_ids[-1] if isinstance(wave_ids, Sequence) and wave_ids else None
    hierarchy = response.get("degree_hierarchy")
    if isinstance(hierarchy, Sequence):
        for item in reversed(hierarchy):
            if isinstance(item, Mapping) and item.get("wave_id") == active_id:
                return item
        for item in reversed(hierarchy):
            if isinstance(item, Mapping) and item.get("completion_status") == "active":
                return item
    return {}


def _legacy_hypotheses(
    response: Mapping[str, Any],
) -> tuple[MainHypothesis, tuple[AlternativeHypothesis, ...]]:
    active_position = response.get("active_position")
    active_position = dict(active_position) if isinstance(active_position, Mapping) else {}
    active_wave = dict(_active_wave(response))
    main_count = {
        "degree_hierarchy": response.get("degree_hierarchy", ()),
        "active_position": active_position,
    }
    main_id = f"main_{canonical_sha256(main_count)[:24]}"
    main = MainHypothesis(
        hypothesis_id=main_id,
        label=str(active_position.get("summary") or "Stored preferred technical count"),
        direction=_direction(active_wave.get("direction")),
        degree=str(active_wave.get("degree") or "Unassigned"),
        wave_label=str(active_wave.get("wave") or "Unassigned"),
        pattern_family=str(active_wave.get("structure") or "unresolved"),
        completion_status=str(active_wave.get("completion_status") or "unresolved"),
        summary=str(
            active_position.get("summary")
            or response.get("scope")
            or "Stored preferred technical count."
        ),
        count=main_count,
        original_uncalibrated_confidence=response.get("confidence"),
    )
    alternatives: list[AlternativeHypothesis] = []
    raw_alternatives = response.get("alternate_counts")
    if isinstance(raw_alternatives, Sequence) and not isinstance(
        raw_alternatives, (str, bytes)
    ):
        for index, item in enumerate(raw_alternatives, start=1):
            payload = dict(item) if isinstance(item, Mapping) else {"description": str(item)}
            description = str(payload.get("description") or f"Stored alternate {index}")
            alternatives.append(
                AlternativeHypothesis(
                    hypothesis_id=f"alternate_{canonical_sha256(payload)[:24]}",
                    label=f"Stored alternate {index}",
                    direction=ForecastDirection.UNKNOWN,
                    degree="Unassigned",
                    wave_label="Unassigned",
                    pattern_family="unresolved",
                    completion_status="unresolved",
                    summary=description,
                    count=payload,
                    activation_conditions=(str(payload["trigger"]),)
                    if payload.get("trigger")
                    else (),
                    distinguishing_evidence_needed=(str(payload["invalidation"]),)
                    if payload.get("invalidation")
                    else (),
                    original_uncalibrated_confidence=None,
                )
            )
    return main, tuple(alternatives)


def _legacy_evidence(
    response: Mapping[str, Any],
    readiness: Mapping[str, Any],
    *,
    main_hypothesis_id: str,
) -> tuple[ForecastEvidence, ...]:
    result: list[ForecastEvidence] = []
    source_groups = (
        (
            response.get("confirmations"),
            ForecastEvidenceStatus.SUPPORTING,
            ForecastEvidenceKind.PRICE_STRUCTURE,
            None,
        ),
        (
            response.get("unresolved_items"),
            ForecastEvidenceStatus.UNAVAILABLE,
            ForecastEvidenceKind.DATA_QUALITY,
            "The stored resolution marked this item unresolved.",
        ),
        (
            readiness.get("blockers"),
            ForecastEvidenceStatus.UNAVAILABLE,
            ForecastEvidenceKind.DATA_QUALITY,
            "The deterministic readiness audit marked this item unavailable.",
        ),
        (
            readiness.get("warnings"),
            ForecastEvidenceStatus.INCOMPARABLE,
            ForecastEvidenceKind.DATA_QUALITY,
            "The deterministic readiness audit marked this evidence limited or incomparable.",
        ),
    )
    seen: set[tuple[str, str]] = set()
    for values, status, kind, reason in source_groups:
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        for item in values:
            statement = str(item).strip()
            key = (status.value, statement)
            if not statement or key in seen:
                continue
            seen.add(key)
            seed = {"status": status.value, "statement": statement}
            result.append(
                ForecastEvidence(
                    evidence_id=f"evidence_{canonical_sha256(seed)[:24]}",
                    hypothesis_ids=(main_hypothesis_id,),
                    kind=kind,
                    status=status,
                    statement=statement,
                    rule_class=EvidenceRuleClass.SOFT_TECHNICAL,
                    reason=reason,
                )
            )
    return tuple(result)


def build_forecast_record_from_stored_records(
    analysis_run: Mapping[str, Any],
    degree_resolution: Mapping[str, Any],
    *,
    dataset_cutoffs: Sequence[DatasetCutoff | Mapping[str, Any]] | None = None,
    main_hypothesis: MainHypothesis | Mapping[str, Any] | None = None,
    alternative_hypotheses: Sequence[
        AlternativeHypothesis | Mapping[str, Any]
    ] | None = None,
    target_claims: Sequence[TargetClaim | Mapping[str, Any]] = (),
    invalidation_claims: Sequence[InvalidationClaim | Mapping[str, Any]] = (),
    confirmation_claims: Sequence[ConfirmationClaim | Mapping[str, Any]] = (),
    expected_completion_windows: Sequence[
        ExpectedCompletionWindow | Mapping[str, Any]
    ] = (),
    evidence: Sequence[ForecastEvidence | Mapping[str, Any]] | None = None,
    evaluation_eligibility: EvaluationEligibility | Mapping[str, Any] | None = None,
    provider_versions: Mapping[str, str] | None = None,
    model_versions: Mapping[str, str] | None = None,
    prompt_versions: Mapping[str, str] | None = None,
    policy_versions: Mapping[str, str] | None = None,
    forecast_version: int = 1,
    supersedes_forecast_id: str | None = None,
    forecast_id: str | None = None,
) -> ForecastRecord:
    """Adapt decoded stored rows without mutating or re-running their analysis.

    Older rows that do not receive explicit typed claims are retained as
    ``legacy_unscorable``. Narrative target or invalidation text is deliberately
    not parsed into modern claims.
    """

    run = dict(analysis_run)
    resolution = dict(degree_resolution)
    run_id = run.get("id")
    resolution_id = resolution.get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("analysis_run must contain a positive integer id.")
    if not isinstance(resolution_id, int) or resolution_id <= 0:
        raise ValueError("degree_resolution must contain a positive integer id.")
    if resolution.get("run_id") != run_id:
        raise ValueError("Degree resolution does not belong to the supplied analysis run.")
    symbol = _require_text(run.get("symbol"), field_name="analysis_run.symbol")
    response = resolution.get("response")
    if not isinstance(response, Mapping):
        raise ValueError("degree_resolution.response is required.")
    response = dict(response)
    if str(response.get("symbol") or symbol).casefold() != symbol.casefold():
        raise ValueError("Stored run and degree-resolution symbols do not match.")
    analysis_cutoff = _safe_timestamp(
        response.get("data_cutoff"), field_name="degree_resolution.data_cutoff"
    )
    readiness = resolution.get("readiness")
    readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
    validation = resolution.get("validation")
    validation = list(validation) if isinstance(validation, Sequence) else []

    if dataset_cutoffs is None:
        evidence_packet = run.get("evidence")
        evidence_packet = (
            dict(evidence_packet) if isinstance(evidence_packet, Mapping) else {}
        )
        summaries = evidence_packet.get("market_data")
        if not isinstance(summaries, Sequence) or isinstance(summaries, (str, bytes)):
            summaries = ()
        resolved_datasets = tuple(
            dataset_cutoff_from_stored_summary(item, requested_symbol=symbol)
            for item in summaries
            if isinstance(item, Mapping)
        )
    else:
        resolved_datasets = _model_tuple(
            dataset_cutoffs, DatasetCutoff, field_name="dataset_cutoffs"
        )
    if not resolved_datasets:
        raise ValueError(
            "A forecast adapter requires at least one stored decision-time dataset."
        )

    if main_hypothesis is None:
        resolved_main, stored_alternatives = _legacy_hypotheses(response)
    else:
        resolved_main = (
            main_hypothesis
            if isinstance(main_hypothesis, MainHypothesis)
            else MainHypothesis.from_dict(main_hypothesis)
        )
        stored_alternatives = ()
    resolved_alternatives = (
        _model_tuple(
            alternative_hypotheses,
            AlternativeHypothesis,
            field_name="alternative_hypotheses",
        )
        if alternative_hypotheses is not None
        else stored_alternatives
    )
    resolved_targets = _model_tuple(target_claims, TargetClaim, field_name="target_claims")
    resolved_invalidations = _model_tuple(
        invalidation_claims, InvalidationClaim, field_name="invalidation_claims"
    )
    resolved_confirmations = _model_tuple(
        confirmation_claims, ConfirmationClaim, field_name="confirmation_claims"
    )
    resolved_windows = _model_tuple(
        expected_completion_windows,
        ExpectedCompletionWindow,
        field_name="expected_completion_windows",
    )
    has_typed_claims = bool(
        resolved_targets
        or resolved_invalidations
        or resolved_confirmations
        or resolved_windows
    )
    incomplete_candle = any(
        not item.completed_candles_only for item in resolved_datasets
    )
    if evaluation_eligibility is None:
        if has_typed_claims:
            eligibility = EvaluationEligibility(
                status=EvaluationEligibilityStatus.EVALUABLE,
                record_state=(
                    ForecastRecordState.PROVISIONAL
                    if incomplete_candle
                    or str(response.get("resolution_status") or "").casefold()
                    == "provisional"
                    else ForecastRecordState.FINAL
                ),
                reason_codes=(
                    ("incomplete_candle",)
                    if incomplete_candle
                    else (
                        ("source_resolution_provisional",)
                        if str(response.get("resolution_status") or "").casefold()
                        == "provisional"
                        else ()
                    )
                ),
                typed_claims_present=True,
                price_accuracy_evaluable=True,
                timing_accuracy_evaluable=bool(resolved_windows),
            )
        else:
            eligibility = EvaluationEligibility(
                status=EvaluationEligibilityStatus.LEGACY_UNSCORABLE,
                record_state=(
                    ForecastRecordState.PROVISIONAL
                    if incomplete_candle
                    or str(response.get("resolution_status") or "").casefold()
                    == "provisional"
                    else ForecastRecordState.FINAL
                ),
                reason_codes=(
                    "legacy_missing_typed_claims",
                    *(
                        ("incomplete_candle",)
                        if incomplete_candle
                        else ()
                    ),
                    *(
                        ("source_resolution_provisional",)
                        if str(response.get("resolution_status") or "").casefold()
                        == "provisional"
                        else ()
                    ),
                ),
                typed_claims_present=False,
                price_accuracy_evaluable=False,
                timing_accuracy_evaluable=False,
            )
    else:
        eligibility = (
            evaluation_eligibility
            if isinstance(evaluation_eligibility, EvaluationEligibility)
            else EvaluationEligibility.from_dict(evaluation_eligibility)
        )

    resolved_evidence = (
        _legacy_evidence(
            response,
            readiness,
            main_hypothesis_id=resolved_main.hypothesis_id,
        )
        if evidence is None
        else _model_tuple(evidence, ForecastEvidence, field_name="evidence")
    )
    run_hash = stored_record_content_hash(run)
    resolution_hash = stored_record_content_hash(resolution)
    active_wave = _active_wave(response)
    direction = _direction(active_wave.get("direction"))
    target_for_premise = next(
        (
            item
            for item in resolved_targets
            if item.hypothesis_id == resolved_main.hypothesis_id
        ),
        None,
    )
    invalidation_for_premise = next(
        (
            item
            for item in resolved_invalidations
            if item.hypothesis_id == resolved_main.hypothesis_id
            and item.price_level is not None
        ),
        None,
    )
    window_for_premise = next(
        (
            item
            for item in resolved_windows
            if item.hypothesis_id == resolved_main.hypothesis_id
        ),
        None,
    )
    active_position = response.get("active_position")
    active_summary = (
        active_position.get("summary")
        if isinstance(active_position, Mapping)
        else None
    )
    premise = FrozenTechnicalPremise.create(
        resolution_id=str(resolution_id),
        symbol=symbol,
        exchange=str(response.get("exchange") or resolved_datasets[0].exchange),
        analysed_at=_safe_timestamp(
            resolution.get("created_at"), field_name="degree_resolution.created_at"
        ),
        market_data_cutoff=analysis_cutoff,
        technical_structure={
            "degree_resolution": response,
            "deterministic_validation": validation,
            "deterministic_readiness": readiness,
        },
        direction=ScenarioDirection(direction.value),
        current_wave_or_phase=str(
            active_wave.get("wave") or active_summary or "unresolved"
        ),
        target_low=target_for_premise.target_low if target_for_premise else None,
        target_high=target_for_premise.target_high if target_for_premise else None,
        invalidation_level=(
            invalidation_for_premise.price_level
            if invalidation_for_premise is not None
            else None
        ),
        expected_completion_start=(
            window_for_premise.start_utc if window_for_premise else None
        ),
        expected_completion_end=(
            window_for_premise.end_utc if window_for_premise else None
        ),
        technical_confidence=response.get("confidence"),
        technical_readiness=readiness,
        supporting_technical_evidence=tuple(
            item.statement
            for item in resolved_evidence
            if item.status is ForecastEvidenceStatus.SUPPORTING
        ),
        alternative_technical_hypotheses=tuple(
            item.summary for item in resolved_alternatives
        ),
        source_hashes={
            "analysis_run": run_hash,
            "degree_resolution": resolution_hash,
        },
    )

    dataset_values = tuple(resolved_datasets)
    exchanges = {item.exchange for item in dataset_values}
    providers = {item.provider for item in dataset_values}
    feeds = {item.feed_identity for item in dataset_values}
    sessions = {item.session for item in dataset_values}
    timezones = {item.timezone for item in dataset_values}
    adjustments = {canonical_forecast_json(item.adjustment) for item in dataset_values}
    price_bases = {item.price_basis for item in dataset_values}
    run_provider = str(run.get("provider") or "unknown")
    resolution_provider = str(resolution.get("provider") or "unknown")
    run_model = str(run.get("model") or "unavailable")
    resolution_model = str(resolution.get("model") or "unavailable")
    request = run.get("request") if isinstance(run.get("request"), Mapping) else {}
    resolution_request = (
        resolution.get("request")
        if isinstance(resolution.get("request"), Mapping)
        else {}
    )
    created_at = _safe_timestamp(
        resolution.get("created_at"), field_name="degree_resolution.created_at"
    )
    return ForecastRecord.create(
        forecast_id=forecast_id,
        forecast_version=forecast_version,
        supersedes_forecast_id=supersedes_forecast_id,
        source_analysis_run_id=run_id,
        source_degree_resolution_id=resolution_id,
        created_at_utc=created_at,
        analysis_cutoff_utc=analysis_cutoff,
        symbol=symbol,
        exchange=next(iter(exchanges)) if len(exchanges) == 1 else "mixed",
        provider=resolution_provider,
        model=resolution_model,
        feed=next(iter(feeds)) if len(feeds) == 1 else "mixed",
        session=next(iter(sessions)) if len(sessions) == 1 else "mixed",
        timezone=next(iter(timezones)) if len(timezones) == 1 else "mixed",
        adjustment=(
            dataset_values[0].adjustment if len(adjustments) == 1 else {"state": "mixed"}
        ),
        price_basis=next(iter(price_bases)) if len(price_bases) == 1 else "mixed",
        dataset_cutoffs=dataset_values,
        frozen_technical_premise=premise,
        main_hypothesis=resolved_main,
        alternative_hypotheses=resolved_alternatives,
        target_claims=resolved_targets,
        invalidation_claims=resolved_invalidations,
        confirmation_claims=resolved_confirmations,
        expected_completion_windows=resolved_windows,
        evidence=resolved_evidence,
        evaluation_eligibility=eligibility,
        provider_versions=provider_versions
        or {
            "analysis": f"{run_provider}:version_unavailable",
            "degree_resolution": f"{resolution_provider}:version_unavailable",
            "market_data": ",".join(sorted(providers)),
        },
        model_versions=model_versions
        or {
            "analysis": f"{run_model}:version_unavailable",
            "degree_resolution": f"{resolution_model}:version_unavailable",
        },
        prompt_versions=prompt_versions
        or {
            "analysis": _legacy_version("analysis_prompt", request),
            "degree_resolution": _legacy_version(
                "degree_resolution_prompt", resolution_request
            ),
        },
        policy_versions=policy_versions
        or {
            "forecast_ledger": FORECAST_POLICY_VERSION,
            "source_analysis_policy": "legacy_version_unavailable",
            "source_degree_policy": "legacy_version_unavailable",
        },
        original_uncalibrated_confidence=response.get("confidence", {}),
        source_hashes={
            "analysis_run": run_hash,
            "degree_resolution": resolution_hash,
        },
    )


FORECAST_SCHEMA_DEFINITION: Mapping[str, Any] = _freeze_json(
    {
        "schema_version": FORECAST_SCHEMA_VERSION,
        "calculation_version": FORECAST_CALCULATION_VERSION,
        "policy_version": FORECAST_POLICY_VERSION,
        "contracts": (
            "ForecastRecord",
            "MainHypothesis",
            "AlternativeHypothesis",
            "TargetClaim",
            "InvalidationClaim",
            "ConfirmationClaim",
            "ExpectedCompletionWindow",
            "ForecastEvidence",
            "DatasetCutoff",
            "EvaluationEligibility",
        ),
        "policies": {
            "historical_untyped": "legacy_unscorable",
            "candle_default": "completed_candles_only",
            "incomplete_candle": "provisional",
            "target_default": ClaimEvaluationBasis.INTRABAR_TOUCH.value,
            "invalidation_default": ClaimEvaluationBasis.INTRABAR_TOUCH_OR_BREACH.value,
            "confirmation_default": ClaimEvaluationBasis.CANDLE_CLOSE.value,
            "price_and_timing": "evaluated_separately",
            "same_candle_collision": SameCandleCollisionPolicy.INCOMPARABLE_WITHOUT_LOWER_TIMEFRAME_ORDER.value,
            "indicators": "soft_evidence_only",
            "frozen_scope": "technical_only",
        },
        "canonicalization": {
            "json": "UTF-8; ASCII escaping; sorted keys; compact separators; no NaN",
            "hash": "SHA-256 over payload excluding content_hash",
            "timestamps": "ISO-8601 UTC with seconds and +00:00 offset",
        },
    }
)


def forecast_schema_definition_hash() -> str:
    return canonical_sha256(FORECAST_SCHEMA_DEFINITION)


FORECAST_LEDGER_MIGRATION_SQL = (
    """
    CREATE TABLE forecast_schema_versions (
        schema_version TEXT PRIMARY KEY NOT NULL,
        calculation_version TEXT NOT NULL
            CHECK (length(trim(calculation_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        schema_json TEXT NOT NULL
            CHECK (length(trim(schema_json)) > 1),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        created_at_utc TEXT NOT NULL
            CHECK (length(trim(created_at_utc)) > 0)
    )
    """,
    """
    CREATE TABLE forecast_records (
        forecast_id TEXT PRIMARY KEY NOT NULL,
        forecast_version INTEGER NOT NULL
            CHECK (forecast_version >= 1),
        supersedes_forecast_id TEXT
            REFERENCES forecast_records(forecast_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        source_analysis_run_id INTEGER NOT NULL
            REFERENCES analysis_runs(id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        source_degree_resolution_id INTEGER NOT NULL
            REFERENCES degree_resolutions(id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        schema_version TEXT NOT NULL
            REFERENCES forecast_schema_versions(schema_version)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        calculation_version TEXT NOT NULL
            CHECK (length(trim(calculation_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        evaluation_eligibility TEXT NOT NULL
            CHECK (evaluation_eligibility IN ('evaluable', 'legacy_unscorable')),
        record_state TEXT NOT NULL
            CHECK (record_state IN ('final', 'provisional')),
        symbol TEXT NOT NULL
            CHECK (length(trim(symbol)) > 0),
        analysis_cutoff_utc TEXT NOT NULL
            CHECK (length(trim(analysis_cutoff_utc)) > 0),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL
            CHECK (length(trim(record_json)) > 1),
        created_at_utc TEXT NOT NULL
            CHECK (length(trim(created_at_utc)) > 0),
        UNIQUE (
            source_analysis_run_id,
            source_degree_resolution_id,
            forecast_version
        ),
        CHECK (
            supersedes_forecast_id IS NULL
            OR supersedes_forecast_id <> forecast_id
        )
    )
    """,
    "CREATE INDEX forecast_records_source_idx ON forecast_records(source_analysis_run_id, source_degree_resolution_id, forecast_version)",
    "CREATE INDEX forecast_records_symbol_cutoff_idx ON forecast_records(symbol, analysis_cutoff_utc, forecast_version)",
    "CREATE UNIQUE INDEX forecast_records_supersedes_idx ON forecast_records(supersedes_forecast_id) WHERE supersedes_forecast_id IS NOT NULL",
    """
    CREATE TRIGGER forecast_schema_versions_no_update
    BEFORE UPDATE ON forecast_schema_versions
    BEGIN
        SELECT RAISE(ABORT, 'forecast_schema_versions is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_schema_versions_no_delete
    BEFORE DELETE ON forecast_schema_versions
    BEGIN
        SELECT RAISE(ABORT, 'forecast_schema_versions is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_records_no_update
    BEFORE UPDATE ON forecast_records
    BEGIN
        SELECT RAISE(ABORT, 'forecast_records is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_records_no_delete
    BEFORE DELETE ON forecast_records
    BEGIN
        SELECT RAISE(ABORT, 'forecast_records is append-only');
    END
    """,
)

FORECAST_LEDGER_TABLES = frozenset({"forecast_schema_versions", "forecast_records"})
FORECAST_LEDGER_INDEXES = frozenset(
    {
        "forecast_records_source_idx",
        "forecast_records_symbol_cutoff_idx",
        "forecast_records_supersedes_idx",
    }
)
FORECAST_LEDGER_TRIGGERS = frozenset(
    {
        "forecast_schema_versions_no_update",
        "forecast_schema_versions_no_delete",
        "forecast_records_no_update",
        "forecast_records_no_delete",
    }
)


__all__ = [
    "AlternativeHypothesis",
    "ClaimEvaluationBasis",
    "ConfirmationClaim",
    "ConfirmationOperator",
    "DATASET_CUTOFF_SCHEMA_VERSION",
    "DatasetCutoff",
    "DatasetHashScope",
    "EvaluationEligibility",
    "EvaluationEligibilityStatus",
    "EvidenceRuleClass",
    "ExpectedCompletionWindow",
    "FORECAST_CALCULATION_VERSION",
    "FORECAST_LEDGER_INDEXES",
    "FORECAST_LEDGER_MIGRATION_SQL",
    "FORECAST_LEDGER_TABLES",
    "FORECAST_LEDGER_TRIGGERS",
    "FORECAST_POLICY_VERSION",
    "FORECAST_SCHEMA_DEFINITION",
    "FORECAST_SCHEMA_VERSION",
    "ForecastDirection",
    "ForecastEvidence",
    "ForecastEvidenceKind",
    "ForecastEvidenceStatus",
    "ForecastRecord",
    "ForecastRecordState",
    "InvalidationClaim",
    "InvalidationOperator",
    "MainHypothesis",
    "SameCandleCollisionPolicy",
    "TargetClaim",
    "build_forecast_record_from_stored_records",
    "canonical_forecast_json",
    "canonical_sha256",
    "dataset_cutoff_from_stored_summary",
    "forecast_record_content_hash",
    "forecast_schema_definition_hash",
    "stored_record_content_hash",
    "validate_forecast_record",
]
