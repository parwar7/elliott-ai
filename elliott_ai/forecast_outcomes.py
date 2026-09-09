"""Immutable Phase 11B observation sets and deterministic outcome evaluation.

This module consumes one already-frozen Phase 11A ``ForecastRecord`` and
supplied post-cutoff candles.  It does not fetch data, call a model, diagnose a
mistake, modify the technical premise, or update the active analysis pipeline.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TypeVar

from .forecast_records import (
    AlternativeHypothesis,
    ClaimEvaluationBasis,
    ConfirmationClaim,
    ConfirmationOperator,
    DatasetCutoff,
    EvaluationEligibilityStatus,
    ExpectedCompletionWindow,
    ForecastDirection,
    ForecastRecord,
    InvalidationClaim,
    InvalidationOperator,
    MainHypothesis,
    TargetClaim,
    canonical_sha256,
    validate_forecast_record,
)


OBSERVATION_CANDLE_SCHEMA_VERSION = "forecast-observation-candle-1.0.0"
OBSERVATION_COMPLETENESS_SCHEMA_VERSION = (
    "forecast-observation-completeness-1.0.0"
)
OBSERVATION_SET_SCHEMA_VERSION = "forecast-observation-set-1.0.0"
CLAIM_OUTCOME_SCHEMA_VERSION = "forecast-claim-outcome-1.0.0"
HYPOTHESIS_OUTCOME_SCHEMA_VERSION = "forecast-hypothesis-outcome-1.0.0"
OUTCOME_EVALUATION_SCHEMA_VERSION = "forecast-outcome-evaluation-1.0.0"
EVALUATION_POLICY_SCHEMA_VERSION = "forecast-outcome-policy-1.0.0"
OUTCOME_CALCULATION_VERSION = "forecast-outcome-deterministic-1.0.0"
OUTCOME_POLICY_VERSION = "forecast-outcome-policy-1.0.0"


class OutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"
    INSUFFICIENT_DATA = "insufficient_data"
    INCOMPARABLE = "incomparable"


class EventOrdering(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NO_EVENT = "no_event"
    TARGET_ONLY = "target_only"
    INVALIDATION_ONLY = "invalidation_only"
    TARGET_BEFORE_INVALIDATION = "target_before_invalidation"
    INVALIDATION_BEFORE_TARGET = "invalidation_before_target"
    INCOMPARABLE = "incomparable"


class ObservationCompletenessState(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    PARTIAL = "partial"
    CENSORED = "censored"
    NO_DATA = "no_data"


class ObservationScheduleMode(StrEnum):
    EXPLICIT_EXPECTED_OPENS = "explicit_expected_opens"
    CONTINUOUS_INTERVAL = "continuous_interval"
    SCHEDULE_UNAVAILABLE = "schedule_unavailable"


class ClaimType(StrEnum):
    TARGET = "target"
    INVALIDATION = "invalidation"
    CONFIRMATION = "confirmation"
    COMPLETION_WINDOW = "completion_window"


class EventTimePrecision(StrEnum):
    CANDLE_INTERVAL = "candle_interval"
    CANDLE_CLOSE = "candle_close"
    LOWER_TIMEFRAME_CANDLE = "lower_timeframe_candle"
    UNAVAILABLE = "unavailable"


_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
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


def _normalize_optional_utc(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _normalize_utc(value, field_name=field_name)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


def _finite(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be finite.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite.")
    return result


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
class EvaluationPolicy(_JsonContract):
    policy_id: str
    policy_version: str
    expected_interval_seconds: int
    schedule_mode: ObservationScheduleMode
    interval_convention: str = "half_open_[open_time_utc,close_time_utc)"
    post_cutoff_rule: str = "candle_open_time_utc_greater_than_or_equal_to_cutoff"
    target_default_basis: ClaimEvaluationBasis = ClaimEvaluationBasis.INTRABAR_TOUCH
    invalidation_default_basis: ClaimEvaluationBasis = (
        ClaimEvaluationBasis.INTRABAR_TOUCH_OR_BREACH
    )
    confirmation_default_basis: ClaimEvaluationBasis = ClaimEvaluationBasis.CANDLE_CLOSE
    same_candle_collision_rule: str = (
        "incomparable_unless_registered_lower_timeframe_candles_prove_order"
    )
    horizon_rule: str = "never_evaluate_after_predetermined_horizon_end"
    missing_data_rule: str = "never_infer_unobserved_events"
    excursion_rule: str = "require_frozen_reference_price_direction_and_basis"
    schema_version: str = EVALUATION_POLICY_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "policy_version",
            "interval_convention",
            "post_cutoff_rule",
            "same_candle_collision_rule",
            "horizon_rule",
            "missing_data_rule",
            "excursion_rule",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        if (
            not isinstance(self.expected_interval_seconds, int)
            or isinstance(self.expected_interval_seconds, bool)
            or self.expected_interval_seconds <= 0
        ):
            raise ValueError("expected_interval_seconds must be a positive integer.")
        object.__setattr__(
            self,
            "schedule_mode",
            _enum_value(
                self.schedule_mode,
                ObservationScheduleMode,
                field_name="schedule_mode",
            ),
        )
        for name in (
            "target_default_basis",
            "invalidation_default_basis",
            "confirmation_default_basis",
        ):
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name), ClaimEvaluationBasis, field_name=name
                ),
            )
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(
        cls,
        *,
        expected_interval_seconds: int,
        schedule_mode: ObservationScheduleMode = (
            ObservationScheduleMode.EXPLICIT_EXPECTED_OPENS
        ),
        policy_id: str = "phase11b-deterministic-outcome-evaluation",
        policy_version: str = OUTCOME_POLICY_VERSION,
    ) -> "EvaluationPolicy":
        policy = cls(
            policy_id=policy_id,
            policy_version=policy_version,
            expected_interval_seconds=expected_interval_seconds,
            schedule_mode=schedule_mode,
        )
        return replace(policy, content_hash=evaluation_policy_content_hash(policy))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "EvaluationPolicy":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        policy = cls(**raw)
        if verify_hash and policy.content_hash != evaluation_policy_content_hash(policy):
            raise ValueError("EvaluationPolicy content_hash does not match its payload.")
        return policy


def evaluation_policy_content_hash(value: EvaluationPolicy | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, EvaluationPolicy) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ObservationCandle(_JsonContract):
    candle_id: str
    open_time_utc: str
    close_time_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    is_complete: bool = True
    source_sequence: int | None = None
    schema_version: str = OBSERVATION_CANDLE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candle_id", _require_text(self.candle_id, field_name="candle_id"))
        object.__setattr__(
            self,
            "open_time_utc",
            _normalize_utc(self.open_time_utc, field_name="open_time_utc"),
        )
        object.__setattr__(
            self,
            "close_time_utc",
            _normalize_utc(self.close_time_utc, field_name="close_time_utc"),
        )
        if _utc(self.open_time_utc) >= _utc(self.close_time_utc):
            raise ValueError("A candle open must precede its close.")
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, _finite(getattr(self, name), field_name=name))
        if self.low > self.high:
            raise ValueError("Candle low must not exceed high.")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("Candle OHLC values are internally inconsistent.")
        if self.volume is not None:
            volume = _finite(self.volume, field_name="volume")
            if volume < 0:
                raise ValueError("volume must not be negative.")
            object.__setattr__(self, "volume", volume)
        if not isinstance(self.is_complete, bool):
            raise TypeError("is_complete must be boolean.")
        if self.source_sequence is not None and (
            not isinstance(self.source_sequence, int)
            or isinstance(self.source_sequence, bool)
            or self.source_sequence < 0
        ):
            raise ValueError("source_sequence must be a non-negative integer.")
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(cls, **values: Any) -> "ObservationCandle":
        supplied_hash = values.pop("content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("candle_id"):
            seed = {
                "open_time_utc": _normalize_utc(
                    values.get("open_time_utc"), field_name="open_time_utc"
                ),
                "close_time_utc": _normalize_utc(
                    values.get("close_time_utc"), field_name="close_time_utc"
                ),
                "source_sequence": values.get("source_sequence"),
            }
            values["candle_id"] = f"candle_{canonical_sha256(seed)[:32]}"
        candle = cls(**values)
        return replace(candle, content_hash=observation_candle_content_hash(candle))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ObservationCandle":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        candle = cls(**raw)
        if verify_hash and candle.content_hash != observation_candle_content_hash(candle):
            raise ValueError(
                f"ObservationCandle {candle.candle_id} content_hash does not match."
            )
        return candle


def observation_candle_content_hash(
    value: ObservationCandle | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ObservationCandle) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class SessionGap(_JsonContract):
    start_utc: str
    end_utc: str
    reason: str
    schema_version: str = OBSERVATION_COMPLETENESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "start_utc", _normalize_utc(self.start_utc, field_name="start_utc")
        )
        object.__setattr__(
            self, "end_utc", _normalize_utc(self.end_utc, field_name="end_utc")
        )
        if _utc(self.start_utc) >= _utc(self.end_utc):
            raise ValueError("Session-gap start must precede its end.")
        object.__setattr__(self, "reason", _require_text(self.reason, field_name="reason"))
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionGap":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ObservationCompleteness(_JsonContract):
    state: ObservationCompletenessState
    schedule_mode: ObservationScheduleMode
    expected_candle_count: int | None
    observed_candle_count: int
    complete_candle_count: int
    expected_open_times_utc: tuple[str, ...] = ()
    missing_open_times_utc: tuple[str, ...] = ()
    partial_candle_ids: tuple[str, ...] = ()
    unexpected_candle_ids: tuple[str, ...] = ()
    session_gaps: tuple[SessionGap, ...] = ()
    horizon_complete: bool = False
    censored: bool = False
    censoring_reason: str | None = None
    warnings: tuple[str, ...] = ()
    schema_version: str = OBSERVATION_COMPLETENESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state",
            _enum_value(
                self.state, ObservationCompletenessState, field_name="state"
            ),
        )
        object.__setattr__(
            self,
            "schedule_mode",
            _enum_value(
                self.schedule_mode,
                ObservationScheduleMode,
                field_name="schedule_mode",
            ),
        )
        if self.expected_candle_count is not None and (
            not isinstance(self.expected_candle_count, int)
            or isinstance(self.expected_candle_count, bool)
            or self.expected_candle_count < 0
        ):
            raise ValueError("expected_candle_count must be non-negative when supplied.")
        for name in ("observed_candle_count", "complete_candle_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer.")
        if self.complete_candle_count > self.observed_candle_count:
            raise ValueError("complete_candle_count cannot exceed observed_candle_count.")
        expected = tuple(
            _normalize_utc(item, field_name="expected_open_times_utc")
            for item in self.expected_open_times_utc
        )
        missing = tuple(
            _normalize_utc(item, field_name="missing_open_times_utc")
            for item in self.missing_open_times_utc
        )
        if len(set(expected)) != len(expected) or tuple(sorted(expected)) != expected:
            raise ValueError("Expected candle opens must be unique and monotonic.")
        if len(set(missing)) != len(missing) or tuple(sorted(missing)) != missing:
            raise ValueError("Missing candle opens must be unique and monotonic.")
        if not set(missing).issubset(expected):
            raise ValueError("Missing candle opens must belong to the expected schedule.")
        object.__setattr__(self, "expected_open_times_utc", expected)
        object.__setattr__(self, "missing_open_times_utc", missing)
        for name in ("partial_candle_ids", "unexpected_candle_ids", "warnings"):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "session_gaps",
            _model_tuple(self.session_gaps, SessionGap, field_name="session_gaps"),
        )
        for name in ("horizon_complete", "censored"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        if self.censored and not self.censoring_reason:
            raise ValueError("Censored observations require a censoring_reason.")
        if self.censoring_reason is not None:
            object.__setattr__(
                self,
                "censoring_reason",
                _require_text(self.censoring_reason, field_name="censoring_reason"),
            )
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ObservationCompleteness":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ForecastObservationSet(_JsonContract):
    observation_set_id: str
    observation_set_version: int
    supersedes_observation_set_id: str | None
    forecast_id: str
    forecast_content_hash: str
    source_dataset_id: str
    source_dataset_hash: str
    created_at_utc: str
    actual_evaluation_cutoff_utc: str
    horizon_window_ids: tuple[str, ...]
    horizon_start_utc: str
    horizon_end_utc: str
    symbol: str
    exchange: str
    provider: str
    feed: str
    timeframe: str
    session: str
    timezone: str
    adjustment: Any
    price_basis: str
    candles: tuple[ObservationCandle, ...]
    completeness: ObservationCompleteness
    evaluation_policy: EvaluationPolicy
    candle_manifest_hash: str
    policy_hash: str
    source_hashes: Mapping[str, str]
    schema_version: str = OBSERVATION_SET_SCHEMA_VERSION
    calculation_version: str = OUTCOME_CALCULATION_VERSION
    policy_version: str = OUTCOME_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "observation_set_id",
            "forecast_id",
            "source_dataset_id",
            "symbol",
            "exchange",
            "provider",
            "feed",
            "timeframe",
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
        if (
            not isinstance(self.observation_set_version, int)
            or isinstance(self.observation_set_version, bool)
            or self.observation_set_version < 1
        ):
            raise ValueError("observation_set_version must be a positive integer.")
        if self.supersedes_observation_set_id is not None:
            object.__setattr__(
                self,
                "supersedes_observation_set_id",
                _require_text(
                    self.supersedes_observation_set_id,
                    field_name="supersedes_observation_set_id",
                ),
            )
            if self.supersedes_observation_set_id == self.observation_set_id:
                raise ValueError("An observation set cannot supersede itself.")
        for name in (
            "forecast_content_hash",
            "source_dataset_hash",
            "candle_manifest_hash",
            "policy_hash",
        ):
            object.__setattr__(
                self, name, _require_hash(getattr(self, name), field_name=name)
            )
        for name in (
            "created_at_utc",
            "actual_evaluation_cutoff_utc",
            "horizon_start_utc",
            "horizon_end_utc",
        ):
            object.__setattr__(
                self, name, _normalize_utc(getattr(self, name), field_name=name)
            )
        if _utc(self.horizon_start_utc) >= _utc(self.horizon_end_utc):
            raise ValueError("Observation horizon start must precede its end.")
        object.__setattr__(
            self,
            "horizon_window_ids",
            _string_tuple(self.horizon_window_ids, field_name="horizon_window_ids"),
        )
        if len(set(self.horizon_window_ids)) != len(self.horizon_window_ids):
            raise ValueError("horizon_window_ids must not contain duplicates.")
        object.__setattr__(self, "adjustment", _freeze_json(self.adjustment))
        object.__setattr__(
            self,
            "candles",
            _model_tuple(self.candles, ObservationCandle, field_name="candles"),
        )
        if isinstance(self.completeness, Mapping):
            object.__setattr__(
                self,
                "completeness",
                ObservationCompleteness.from_dict(self.completeness),
            )
        elif not isinstance(self.completeness, ObservationCompleteness):
            raise TypeError("completeness has an invalid type.")
        if isinstance(self.evaluation_policy, Mapping):
            object.__setattr__(
                self,
                "evaluation_policy",
                EvaluationPolicy.from_dict(self.evaluation_policy),
            )
        elif not isinstance(self.evaluation_policy, EvaluationPolicy):
            raise TypeError("evaluation_policy has an invalid type.")
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        for key, item in source_hashes.items():
            _require_hash(item, field_name=f"source_hashes[{key!r}]")
        object.__setattr__(self, "source_hashes", source_hashes)
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(cls, **values: Any) -> "ForecastObservationSet":
        supplied_hash = values.pop("content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("observation_set_id"):
            seed = {
                "forecast_id": values.get("forecast_id"),
                "source_dataset_id": values.get("source_dataset_id"),
                "observation_set_version": values.get("observation_set_version"),
                "supersedes_observation_set_id": values.get(
                    "supersedes_observation_set_id"
                ),
                "actual_evaluation_cutoff_utc": _normalize_utc(
                    values.get("actual_evaluation_cutoff_utc"),
                    field_name="actual_evaluation_cutoff_utc",
                ),
                "candle_manifest_hash": values.get("candle_manifest_hash"),
            }
            values["observation_set_id"] = (
                f"forecast_observation_{canonical_sha256(seed)[:32]}"
            )
        result = cls(**values)
        return replace(result, content_hash=forecast_observation_set_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ForecastObservationSet":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != forecast_observation_set_content_hash(result):
            raise ValueError("ForecastObservationSet content_hash does not match its payload.")
        return result


def candle_manifest_hash(candles: Sequence[ObservationCandle]) -> str:
    return canonical_sha256(
        [
            {
                "candle_id": item.candle_id,
                "open_time_utc": item.open_time_utc,
                "close_time_utc": item.close_time_utc,
                "content_hash": item.content_hash,
            }
            for item in candles
        ]
    )


def forecast_observation_set_content_hash(
    value: ForecastObservationSet | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ForecastObservationSet) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ClaimOutcome(_JsonContract):
    claim_id: str
    hypothesis_id: str
    claim_type: ClaimType
    evaluation_basis: ClaimEvaluationBasis | None
    outcome_status: OutcomeStatus
    price_status: OutcomeStatus | None
    timing_status: OutcomeStatus | None
    target_touched: bool | None = None
    invalidation_breached: bool | None = None
    confirmation_met: bool | None = None
    first_target_timestamp_utc: str | None = None
    first_invalidation_timestamp_utc: str | None = None
    first_confirmation_timestamp_utc: str | None = None
    event_time_precision: EventTimePrecision = EventTimePrecision.UNAVAILABLE
    event_order: EventOrdering = EventOrdering.NOT_APPLICABLE
    supporting_candle_ids: tuple[str, ...] = ()
    observed_value: float | None = None
    observation_complete: bool = False
    censored: bool = False
    comparable: bool = True
    unavailable_reason: str | None = None
    warnings: tuple[str, ...] = ()
    schema_version: str = CLAIM_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("claim_id", "hypothesis_id", "schema_version"):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "claim_type",
            _enum_value(self.claim_type, ClaimType, field_name="claim_type"),
        )
        if self.evaluation_basis is not None:
            object.__setattr__(
                self,
                "evaluation_basis",
                _enum_value(
                    self.evaluation_basis,
                    ClaimEvaluationBasis,
                    field_name="evaluation_basis",
                ),
            )
        for name in ("outcome_status", "price_status", "timing_status"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _enum_value(value, OutcomeStatus, field_name=name),
                )
        for name in (
            "target_touched",
            "invalidation_breached",
            "confirmation_met",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean or null.")
        for name in (
            "first_target_timestamp_utc",
            "first_invalidation_timestamp_utc",
            "first_confirmation_timestamp_utc",
        ):
            object.__setattr__(
                self,
                name,
                _normalize_optional_utc(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "event_time_precision",
            _enum_value(
                self.event_time_precision,
                EventTimePrecision,
                field_name="event_time_precision",
            ),
        )
        object.__setattr__(
            self,
            "event_order",
            _enum_value(self.event_order, EventOrdering, field_name="event_order"),
        )
        object.__setattr__(
            self,
            "supporting_candle_ids",
            _string_tuple(
                self.supporting_candle_ids, field_name="supporting_candle_ids"
            ),
        )
        if self.observed_value is not None:
            object.__setattr__(
                self,
                "observed_value",
                _finite(self.observed_value, field_name="observed_value"),
            )
        for name in ("observation_complete", "censored", "comparable"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        if self.unavailable_reason is not None:
            object.__setattr__(
                self,
                "unavailable_reason",
                _require_text(
                    self.unavailable_reason, field_name="unavailable_reason"
                ),
            )
        if not self.comparable and not self.unavailable_reason:
            raise ValueError("An incomparable claim outcome requires an unavailable_reason.")
        object.__setattr__(
            self, "warnings", _string_tuple(self.warnings, field_name="warnings")
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ClaimOutcome":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HypothesisOutcome(_JsonContract):
    hypothesis_id: str
    is_main: bool
    direction: ForecastDirection
    outcome_status: OutcomeStatus
    price_status: OutcomeStatus
    timing_status: OutcomeStatus
    claim_outcomes: tuple[ClaimOutcome, ...]
    event_order: EventOrdering
    first_target_timestamp_utc: str | None = None
    first_invalidation_timestamp_utc: str | None = None
    completion_timestamp_utc: str | None = None
    event_time_precision: EventTimePrecision = EventTimePrecision.UNAVAILABLE
    reference_price: float | None = None
    reference_timestamp_utc: str | None = None
    mfe: float | None = None
    mae: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    excursion_available: bool = False
    excursion_unavailable_reason: str | None = None
    supporting_candle_ids: tuple[str, ...] = ()
    lower_timeframe_resolution_observation_set_id: str | None = None
    observation_complete: bool = False
    censored: bool = False
    comparable: bool = True
    warnings: tuple[str, ...] = ()
    schema_version: str = HYPOTHESIS_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hypothesis_id",
            _require_text(self.hypothesis_id, field_name="hypothesis_id"),
        )
        if not isinstance(self.is_main, bool):
            raise TypeError("is_main must be boolean.")
        object.__setattr__(
            self,
            "direction",
            _enum_value(self.direction, ForecastDirection, field_name="direction"),
        )
        for name in ("outcome_status", "price_status", "timing_status"):
            object.__setattr__(
                self,
                name,
                _enum_value(getattr(self, name), OutcomeStatus, field_name=name),
            )
        object.__setattr__(
            self,
            "claim_outcomes",
            _model_tuple(
                self.claim_outcomes, ClaimOutcome, field_name="claim_outcomes"
            ),
        )
        object.__setattr__(
            self,
            "event_order",
            _enum_value(self.event_order, EventOrdering, field_name="event_order"),
        )
        object.__setattr__(
            self,
            "event_time_precision",
            _enum_value(
                self.event_time_precision,
                EventTimePrecision,
                field_name="event_time_precision",
            ),
        )
        for name in (
            "first_target_timestamp_utc",
            "first_invalidation_timestamp_utc",
            "completion_timestamp_utc",
            "reference_timestamp_utc",
        ):
            object.__setattr__(
                self,
                name,
                _normalize_optional_utc(getattr(self, name), field_name=name),
            )
        for name in ("reference_price", "mfe", "mae", "mfe_pct", "mae_pct"):
            value = getattr(self, name)
            if value is not None:
                finite = _finite(value, field_name=name)
                if name in ("mfe", "mae", "mfe_pct", "mae_pct") and finite < 0:
                    raise ValueError(f"{name} must not be negative.")
                object.__setattr__(self, name, finite)
        if not isinstance(self.excursion_available, bool):
            raise TypeError("excursion_available must be boolean.")
        if self.excursion_available:
            if self.reference_price is None or self.mfe is None or self.mae is None:
                raise ValueError(
                    "Available excursions require reference_price, mfe, and mae."
                )
        elif not self.excursion_unavailable_reason:
            raise ValueError(
                "Unavailable excursions require excursion_unavailable_reason."
            )
        if self.excursion_unavailable_reason is not None:
            object.__setattr__(
                self,
                "excursion_unavailable_reason",
                _require_text(
                    self.excursion_unavailable_reason,
                    field_name="excursion_unavailable_reason",
                ),
            )
        object.__setattr__(
            self,
            "supporting_candle_ids",
            _string_tuple(
                self.supporting_candle_ids, field_name="supporting_candle_ids"
            ),
        )
        if self.lower_timeframe_resolution_observation_set_id is not None:
            object.__setattr__(
                self,
                "lower_timeframe_resolution_observation_set_id",
                _require_text(
                    self.lower_timeframe_resolution_observation_set_id,
                    field_name="lower_timeframe_resolution_observation_set_id",
                ),
            )
        for name in ("observation_complete", "censored", "comparable"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        object.__setattr__(
            self, "warnings", _string_tuple(self.warnings, field_name="warnings")
        )
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HypothesisOutcome":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ForecastOutcomeEvaluation(_JsonContract):
    evaluation_id: str
    evaluation_version: int
    supersedes_evaluation_id: str | None
    forecast_id: str
    forecast_content_hash: str
    observation_set_id: str
    observation_set_content_hash: str
    lower_timeframe_observation_set_ids: tuple[str, ...]
    lower_timeframe_observation_set_hashes: Mapping[str, str]
    created_at_utc: str
    evaluated_through_utc: str
    evaluation_policy_id: str
    evaluation_policy_hash: str
    main_hypothesis_outcome: HypothesisOutcome
    alternative_hypothesis_outcomes: tuple[HypothesisOutcome, ...]
    outcome_status: OutcomeStatus
    warnings: tuple[str, ...]
    source_hashes: Mapping[str, str]
    schema_version: str = OUTCOME_EVALUATION_SCHEMA_VERSION
    calculation_version: str = OUTCOME_CALCULATION_VERSION
    policy_version: str = OUTCOME_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "evaluation_id",
            "forecast_id",
            "observation_set_id",
            "evaluation_policy_id",
            "schema_version",
            "calculation_version",
            "policy_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        if (
            not isinstance(self.evaluation_version, int)
            or isinstance(self.evaluation_version, bool)
            or self.evaluation_version < 1
        ):
            raise ValueError("evaluation_version must be a positive integer.")
        if self.supersedes_evaluation_id is not None:
            object.__setattr__(
                self,
                "supersedes_evaluation_id",
                _require_text(
                    self.supersedes_evaluation_id,
                    field_name="supersedes_evaluation_id",
                ),
            )
            if self.supersedes_evaluation_id == self.evaluation_id:
                raise ValueError("An outcome evaluation cannot supersede itself.")
        for name in (
            "forecast_content_hash",
            "observation_set_content_hash",
            "evaluation_policy_hash",
        ):
            object.__setattr__(
                self, name, _require_hash(getattr(self, name), field_name=name)
            )
        for name in ("created_at_utc", "evaluated_through_utc"):
            object.__setattr__(
                self, name, _normalize_utc(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "lower_timeframe_observation_set_ids",
            _string_tuple(
                self.lower_timeframe_observation_set_ids,
                field_name="lower_timeframe_observation_set_ids",
            ),
        )
        if len(set(self.lower_timeframe_observation_set_ids)) != len(
            self.lower_timeframe_observation_set_ids
        ):
            raise ValueError("Lower-timeframe observation-set IDs must be unique.")
        lower_hashes = _string_mapping(
            self.lower_timeframe_observation_set_hashes,
            field_name="lower_timeframe_observation_set_hashes",
        )
        if set(lower_hashes) != set(self.lower_timeframe_observation_set_ids):
            raise ValueError(
                "Lower-timeframe hashes must match lower-timeframe observation IDs."
            )
        for key, item in lower_hashes.items():
            _require_hash(item, field_name=f"lower_timeframe_hashes[{key!r}]")
        object.__setattr__(
            self, "lower_timeframe_observation_set_hashes", lower_hashes
        )
        if isinstance(self.main_hypothesis_outcome, Mapping):
            object.__setattr__(
                self,
                "main_hypothesis_outcome",
                HypothesisOutcome.from_dict(self.main_hypothesis_outcome),
            )
        elif not isinstance(self.main_hypothesis_outcome, HypothesisOutcome):
            raise TypeError("main_hypothesis_outcome has an invalid type.")
        object.__setattr__(
            self,
            "alternative_hypothesis_outcomes",
            _model_tuple(
                self.alternative_hypothesis_outcomes,
                HypothesisOutcome,
                field_name="alternative_hypothesis_outcomes",
            ),
        )
        object.__setattr__(
            self,
            "outcome_status",
            _enum_value(
                self.outcome_status, OutcomeStatus, field_name="outcome_status"
            ),
        )
        object.__setattr__(
            self, "warnings", _string_tuple(self.warnings, field_name="warnings")
        )
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        for key, item in source_hashes.items():
            _require_hash(item, field_name=f"source_hashes[{key!r}]")
        object.__setattr__(self, "source_hashes", source_hashes)
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(cls, **values: Any) -> "ForecastOutcomeEvaluation":
        supplied_hash = values.pop("content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("evaluation_id"):
            seed = {
                "forecast_id": values.get("forecast_id"),
                "observation_set_id": values.get("observation_set_id"),
                "evaluation_version": values.get("evaluation_version"),
                "supersedes_evaluation_id": values.get("supersedes_evaluation_id"),
                "lower_timeframe_observation_set_ids": values.get(
                    "lower_timeframe_observation_set_ids", ()
                ),
                "evaluation_policy_hash": values.get("evaluation_policy_hash"),
            }
            values["evaluation_id"] = (
                f"forecast_evaluation_{canonical_sha256(seed)[:32]}"
            )
        result = cls(**values)
        return replace(result, content_hash=forecast_outcome_evaluation_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ForecastOutcomeEvaluation":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != forecast_outcome_evaluation_content_hash(result):
            raise ValueError(
                "ForecastOutcomeEvaluation content_hash does not match its payload."
            )
        return result


def forecast_outcome_evaluation_content_hash(
    value: ForecastOutcomeEvaluation | Mapping[str, Any],
) -> str:
    payload = (
        value.to_dict()
        if isinstance(value, ForecastOutcomeEvaluation)
        else dict(value)
    )
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _dataset_for_observation(
    forecast: ForecastRecord, source_dataset_id: str
) -> DatasetCutoff:
    for dataset in forecast.dataset_cutoffs:
        if dataset.dataset_id == source_dataset_id:
            return dataset
    raise ValueError(
        f"Source dataset {source_dataset_id} is not part of forecast {forecast.forecast_id}."
    )


def _normalize_candles(value: Sequence[Any]) -> tuple[ObservationCandle, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("candles must be a sequence.")
    result: list[ObservationCandle] = []
    for item in value:
        if isinstance(item, ObservationCandle):
            if item.content_hash != observation_candle_content_hash(item):
                raise ValueError(
                    f"ObservationCandle {item.candle_id} content_hash does not match."
                )
            result.append(item)
        elif isinstance(item, Mapping):
            raw = dict(item)
            if raw.get("content_hash"):
                result.append(ObservationCandle.from_dict(raw))
            else:
                result.append(ObservationCandle.create(**raw))
        else:
            raise TypeError("Candle items must be ObservationCandle objects or mappings.")
    return tuple(result)


def _continuous_expected_opens(
    *, start: datetime, end: datetime, interval_seconds: int
) -> tuple[str, ...]:
    interval = timedelta(seconds=interval_seconds)
    current = start
    result: list[str] = []
    while current + interval <= end:
        result.append(current.astimezone(timezone.utc).isoformat(timespec="seconds"))
        current += interval
    return tuple(result)


def _derive_completeness(
    *,
    candles: tuple[ObservationCandle, ...],
    policy: EvaluationPolicy,
    expected_open_times_utc: tuple[str, ...],
    session_gaps: tuple[SessionGap, ...],
    actual_evaluation_cutoff_utc: str,
    horizon_end_utc: str,
) -> ObservationCompleteness:
    complete = tuple(item for item in candles if item.is_complete)
    partial = tuple(item.candle_id for item in candles if not item.is_complete)
    observed_complete_opens = {item.open_time_utc for item in complete}
    expected = set(expected_open_times_utc)
    effective_end = min(
        _utc(actual_evaluation_cutoff_utc), _utc(horizon_end_utc)
    )
    interval = timedelta(seconds=policy.expected_interval_seconds)
    due_expected = {
        item
        for item in expected_open_times_utc
        if _utc(item) + interval <= effective_end
    }
    missing = tuple(sorted(due_expected - observed_complete_opens))
    unexpected = tuple(
        item.candle_id
        for item in complete
        if expected and item.open_time_utc not in expected
    )
    censored = _utc(actual_evaluation_cutoff_utc) < _utc(horizon_end_utc)
    schedule_unavailable = (
        policy.schedule_mode is ObservationScheduleMode.SCHEDULE_UNAVAILABLE
    )
    warnings: list[str] = []
    if schedule_unavailable:
        warnings.append(
            "Expected market-session opens were unavailable; gap completeness cannot be proven."
        )
    if missing:
        warnings.append("One or more expected completed candles are missing.")
    if partial:
        warnings.append("One or more supplied candles are partial and are not scored.")
    if unexpected:
        warnings.append("One or more completed candles are outside the expected schedule.")
    if not candles:
        state = ObservationCompletenessState.NO_DATA
    elif partial:
        state = ObservationCompletenessState.PARTIAL
    elif schedule_unavailable or missing or unexpected:
        state = ObservationCompletenessState.INCOMPLETE
    elif censored:
        state = ObservationCompletenessState.CENSORED
    else:
        state = ObservationCompletenessState.COMPLETE
    horizon_complete = (
        not censored
        and state is ObservationCompletenessState.COMPLETE
        and not missing
        and not partial
        and not unexpected
    )
    return ObservationCompleteness(
        state=state,
        schedule_mode=policy.schedule_mode,
        expected_candle_count=(
            None if schedule_unavailable else len(expected_open_times_utc)
        ),
        observed_candle_count=len(candles),
        complete_candle_count=len(complete),
        expected_open_times_utc=expected_open_times_utc,
        missing_open_times_utc=missing,
        partial_candle_ids=partial,
        unexpected_candle_ids=unexpected,
        session_gaps=session_gaps,
        horizon_complete=horizon_complete,
        censored=censored,
        censoring_reason=(
            "Actual evaluation cutoff precedes the predetermined horizon end."
            if censored
            else None
        ),
        warnings=tuple(warnings),
    )


def build_forecast_observation_set(
    forecast: ForecastRecord,
    candles: Sequence[ObservationCandle | Mapping[str, Any]],
    *,
    source_dataset_id: str,
    actual_evaluation_cutoff_utc: str,
    evaluation_policy: EvaluationPolicy | Mapping[str, Any],
    expected_open_times_utc: Sequence[str] | None = None,
    session_gaps: Sequence[SessionGap | Mapping[str, Any]] = (),
    horizon_window_ids: Sequence[str] | None = None,
    observation_set_id: str | None = None,
    observation_set_version: int = 1,
    supersedes_observation_set_id: str | None = None,
    created_at_utc: str | None = None,
    symbol: str | None = None,
    exchange: str | None = None,
    provider: str | None = None,
    feed: str | None = None,
    timeframe: str | None = None,
    session: str | None = None,
    timezone_name: str | None = None,
    adjustment: Any = None,
    price_basis: str | None = None,
    source_hashes: Mapping[str, str] | None = None,
) -> ForecastObservationSet:
    """Build an immutable observation snapshot from caller-supplied candles."""

    if not isinstance(forecast, ForecastRecord):
        raise TypeError("forecast must be a ForecastRecord.")
    forecast_errors = validate_forecast_record(forecast)
    if forecast_errors:
        raise ValueError("Invalid ForecastRecord: " + " ".join(forecast_errors))
    if forecast.evaluation_eligibility.status is EvaluationEligibilityStatus.LEGACY_UNSCORABLE:
        raise ValueError("legacy_unscorable forecasts have no predetermined typed horizon.")
    dataset = _dataset_for_observation(forecast, source_dataset_id)
    if isinstance(evaluation_policy, Mapping):
        policy = EvaluationPolicy.from_dict(evaluation_policy)
    elif isinstance(evaluation_policy, EvaluationPolicy):
        policy = evaluation_policy
        if policy.content_hash != evaluation_policy_content_hash(policy):
            raise ValueError("EvaluationPolicy content_hash does not match its payload.")
    else:
        raise TypeError("evaluation_policy has an invalid type.")
    normalized_candles = _normalize_candles(candles)
    normalized_gaps = _model_tuple(session_gaps, SessionGap, field_name="session_gaps")
    cutoff = _normalize_utc(
        actual_evaluation_cutoff_utc, field_name="actual_evaluation_cutoff_utc"
    )
    selected_window_ids = tuple(
        horizon_window_ids
        if horizon_window_ids is not None
        else (item.window_id for item in forecast.expected_completion_windows)
    )
    selected_window_ids = _string_tuple(
        selected_window_ids, field_name="horizon_window_ids"
    )
    windows_by_id = {
        item.window_id: item for item in forecast.expected_completion_windows
    }
    unknown_windows = sorted(set(selected_window_ids) - set(windows_by_id))
    if unknown_windows:
        raise ValueError(
            "Unknown predetermined completion windows: "
            + ", ".join(unknown_windows)
            + "."
        )
    if not selected_window_ids:
        raise ValueError("At least one predetermined completion window is required.")
    horizon_start = forecast.analysis_cutoff_utc
    horizon_end = max(windows_by_id[item].end_utc for item in selected_window_ids)
    if _utc(cutoff) < _utc(horizon_start):
        raise ValueError("Actual evaluation cutoff cannot precede the forecast cutoff.")
    effective_end = min(_utc(cutoff), _utc(horizon_end))
    if policy.schedule_mode is ObservationScheduleMode.EXPLICIT_EXPECTED_OPENS:
        if expected_open_times_utc is None:
            raise ValueError(
                "explicit_expected_opens policy requires expected_open_times_utc."
            )
        expected_opens = tuple(
            _normalize_utc(item, field_name="expected_open_times_utc")
            for item in expected_open_times_utc
        )
    elif policy.schedule_mode is ObservationScheduleMode.CONTINUOUS_INTERVAL:
        if expected_open_times_utc is not None:
            raise ValueError(
                "continuous_interval policy generates its own expected schedule."
            )
        expected_opens = _continuous_expected_opens(
            start=_utc(horizon_start),
            end=_utc(horizon_end),
            interval_seconds=policy.expected_interval_seconds,
        )
    else:
        if expected_open_times_utc not in (None, (), []):
            raise ValueError(
                "schedule_unavailable policy cannot claim an expected schedule."
            )
        expected_opens = ()
    completeness = _derive_completeness(
        candles=normalized_candles,
        policy=policy,
        expected_open_times_utc=expected_opens,
        session_gaps=normalized_gaps,
        actual_evaluation_cutoff_utc=cutoff,
        horizon_end_utc=horizon_end,
    )
    manifest_hash = candle_manifest_hash(normalized_candles)
    required_source_hashes = {
        "forecast_record": forecast.content_hash,
        "decision_time_dataset": dataset.dataset_hash,
        "candle_manifest": manifest_hash,
        "evaluation_policy": policy.content_hash,
    }
    supplied_source_hashes = dict(source_hashes or {})
    for key, item in required_source_hashes.items():
        if key in supplied_source_hashes and supplied_source_hashes[key] != item:
            raise ValueError(f"source_hashes[{key!r}] conflicts with its source.")
        supplied_source_hashes[key] = item
    record = ForecastObservationSet.create(
        observation_set_id=observation_set_id or "",
        observation_set_version=observation_set_version,
        supersedes_observation_set_id=supersedes_observation_set_id,
        forecast_id=forecast.forecast_id,
        forecast_content_hash=forecast.content_hash,
        source_dataset_id=dataset.dataset_id,
        source_dataset_hash=dataset.dataset_hash,
        created_at_utc=created_at_utc or cutoff,
        actual_evaluation_cutoff_utc=cutoff,
        horizon_window_ids=selected_window_ids,
        horizon_start_utc=horizon_start,
        horizon_end_utc=horizon_end,
        symbol=symbol or forecast.symbol,
        exchange=exchange or dataset.exchange,
        provider=provider or dataset.provider,
        feed=feed or dataset.feed_identity,
        timeframe=timeframe or dataset.timeframe,
        session=session or dataset.session,
        timezone=timezone_name or dataset.timezone,
        adjustment=dataset.adjustment if adjustment is None else adjustment,
        price_basis=price_basis or dataset.price_basis,
        candles=normalized_candles,
        completeness=completeness,
        evaluation_policy=policy,
        candle_manifest_hash=manifest_hash,
        policy_hash=policy.content_hash,
        source_hashes=supplied_source_hashes,
    )
    errors = validate_forecast_observation_set(record, forecast)
    if errors:
        raise ValueError("Invalid ForecastObservationSet: " + " ".join(errors))
    if effective_end < _utc(horizon_start) and normalized_candles:
        raise ValueError("No candles are permitted before the post-cutoff horizon begins.")
    return record


def validate_forecast_observation_set(
    value: ForecastObservationSet | Mapping[str, Any],
    forecast: ForecastRecord,
) -> tuple[str, ...]:
    """Validate an observation set against its immutable Phase 11A forecast."""

    if not isinstance(forecast, ForecastRecord):
        return ("forecast must be a ForecastRecord.",)
    forecast_errors = validate_forecast_record(forecast)
    if forecast_errors:
        return tuple(f"Forecast: {item}" for item in forecast_errors)
    if isinstance(value, ForecastObservationSet):
        record = value
    else:
        try:
            record = ForecastObservationSet.from_dict(value, verify_hash=False)
        except (TypeError, ValueError) as exc:
            return (str(exc),)
    errors: list[str] = []
    if record.schema_version != OBSERVATION_SET_SCHEMA_VERSION:
        errors.append("Unsupported observation-set schema_version.")
    if record.calculation_version != OUTCOME_CALCULATION_VERSION:
        errors.append("Unsupported observation-set calculation_version.")
    if record.policy_version != OUTCOME_POLICY_VERSION:
        errors.append("Unsupported observation-set policy_version.")
    if record.supersedes_observation_set_id is None and record.observation_set_version != 1:
        errors.append("An observation set without a predecessor must have version 1.")
    if record.supersedes_observation_set_id is not None and record.observation_set_version == 1:
        errors.append("A superseding observation set must have version greater than 1.")
    if not record.content_hash:
        errors.append("Observation-set content_hash is required.")
    elif record.content_hash != forecast_observation_set_content_hash(record):
        errors.append("Observation-set content_hash does not match its canonical payload.")
    if record.forecast_id != forecast.forecast_id:
        errors.append("Observation set references a different forecast ID.")
    if record.forecast_content_hash != forecast.content_hash:
        errors.append("Observation set references a different forecast hash.")
    try:
        dataset = _dataset_for_observation(forecast, record.source_dataset_id)
    except ValueError as exc:
        errors.append(str(exc))
        dataset = None
    if dataset is not None:
        expected_metadata = {
            "symbol": forecast.symbol,
            "exchange": dataset.exchange,
            "provider": dataset.provider,
            "feed": dataset.feed_identity,
            "timeframe": dataset.timeframe,
            "session": dataset.session,
            "timezone": dataset.timezone,
            "price_basis": dataset.price_basis,
        }
        for name, expected in expected_metadata.items():
            if str(getattr(record, name)).casefold() != str(expected).casefold():
                errors.append(f"Observation {name} is incompatible with its forecast dataset.")
        if canonical_sha256(record.adjustment) != canonical_sha256(dataset.adjustment):
            errors.append("Observation adjustment is incompatible with its forecast dataset.")
        if record.source_dataset_hash != dataset.dataset_hash:
            errors.append("Observation source dataset hash does not match.")
    if record.horizon_start_utc != forecast.analysis_cutoff_utc:
        errors.append("Observation horizon must start at the forecast cutoff.")
    windows_by_id = {
        item.window_id: item for item in forecast.expected_completion_windows
    }
    unknown_windows = sorted(set(record.horizon_window_ids) - set(windows_by_id))
    if unknown_windows:
        errors.append("Observation set references unknown completion windows.")
    elif record.horizon_window_ids:
        expected_end = max(windows_by_id[item].end_utc for item in record.horizon_window_ids)
        if record.horizon_end_utc != expected_end:
            errors.append("Observation horizon end differs from the frozen windows.")
    else:
        errors.append("Observation set requires at least one completion window.")
    if _utc(record.actual_evaluation_cutoff_utc) < _utc(forecast.analysis_cutoff_utc):
        errors.append("Actual evaluation cutoff precedes the forecast cutoff.")
    if record.policy_hash != record.evaluation_policy.content_hash:
        errors.append("Observation policy hash does not match its embedded policy.")
    if record.policy_hash != evaluation_policy_content_hash(record.evaluation_policy):
        errors.append("Embedded evaluation policy hash does not match its payload.")
    if record.candle_manifest_hash != candle_manifest_hash(record.candles):
        errors.append("Candle manifest hash does not match the ordered candle manifest.")
    required_hashes = {
        "forecast_record": forecast.content_hash,
        "candle_manifest": record.candle_manifest_hash,
        "evaluation_policy": record.policy_hash,
    }
    if dataset is not None:
        required_hashes["decision_time_dataset"] = dataset.dataset_hash
    for key, expected in required_hashes.items():
        if record.source_hashes.get(key) != expected:
            errors.append(f"Observation source_hashes[{key!r}] does not match.")
    candle_ids: set[str] = set()
    previous: ObservationCandle | None = None
    effective_end = min(
        _utc(record.actual_evaluation_cutoff_utc), _utc(record.horizon_end_utc)
    )
    horizon_end = _utc(record.horizon_end_utc)
    interval_seconds = record.evaluation_policy.expected_interval_seconds
    for candle in record.candles:
        if candle.candle_id in candle_ids:
            errors.append(f"Duplicate candle ID {candle.candle_id}.")
        candle_ids.add(candle.candle_id)
        if candle.content_hash != observation_candle_content_hash(candle):
            errors.append(f"Candle {candle.candle_id} hash does not match.")
        if (_utc(candle.close_time_utc) - _utc(candle.open_time_utc)).total_seconds() != interval_seconds:
            errors.append(
                f"Candle {candle.candle_id} interval differs from the evaluation policy."
            )
        if _utc(candle.open_time_utc) < _utc(forecast.analysis_cutoff_utc):
            errors.append(
                f"Candle {candle.candle_id} overlaps decision-time data."
            )
        if _utc(candle.open_time_utc) >= horizon_end or _utc(candle.close_time_utc) > horizon_end:
            errors.append(f"Candle {candle.candle_id} extends beyond the frozen horizon.")
        if candle.is_complete and _utc(candle.close_time_utc) > effective_end:
            errors.append(
                f"Completed candle {candle.candle_id} extends beyond the evaluation cutoff."
            )
        if not candle.is_complete and _utc(candle.open_time_utc) >= effective_end:
            errors.append(
                f"Partial candle {candle.candle_id} begins after the evaluation cutoff."
            )
        if previous is not None:
            if _utc(candle.open_time_utc) <= _utc(previous.open_time_utc):
                errors.append("Candle open timestamps are duplicate or out of order.")
            if _utc(candle.open_time_utc) < _utc(previous.close_time_utc):
                errors.append("Candle intervals overlap.")
        previous = candle
    expected_opens = record.completeness.expected_open_times_utc
    interval = timedelta(seconds=interval_seconds)
    for item in expected_opens:
        if _utc(item) < _utc(forecast.analysis_cutoff_utc):
            errors.append("Expected schedule contains a pre-cutoff candle.")
        if _utc(item) + interval > horizon_end:
            errors.append("Expected schedule extends beyond the frozen horizon.")
    derived = _derive_completeness(
        candles=record.candles,
        policy=record.evaluation_policy,
        expected_open_times_utc=expected_opens,
        session_gaps=record.completeness.session_gaps,
        actual_evaluation_cutoff_utc=record.actual_evaluation_cutoff_utc,
        horizon_end_utc=record.horizon_end_utc,
    )
    if derived.to_dict() != record.completeness.to_dict():
        errors.append("Observation completeness does not match the candle manifest.")
    return tuple(errors)


def _claim_windows(
    hypothesis: MainHypothesis | AlternativeHypothesis,
    forecast: ForecastRecord,
) -> tuple[ExpectedCompletionWindow, ...]:
    by_id = {item.window_id: item for item in forecast.expected_completion_windows}
    return tuple(by_id[item] for item in hypothesis.completion_window_ids if item in by_id)


def _timing_status(
    event_timestamp_utc: str | None,
    windows: Sequence[ExpectedCompletionWindow],
    completeness: ObservationCompleteness,
) -> OutcomeStatus:
    if not windows:
        return OutcomeStatus.INSUFFICIENT_DATA
    if event_timestamp_utc is not None:
        event_time = _utc(event_timestamp_utc)
        if any(
            _utc(item.start_utc) <= event_time <= _utc(item.end_utc)
            for item in windows
        ):
            return OutcomeStatus.SUCCEEDED
        return OutcomeStatus.FAILED
    if completeness.state in {
        ObservationCompletenessState.NO_DATA,
        ObservationCompletenessState.INCOMPLETE,
        ObservationCompletenessState.PARTIAL,
    }:
        return OutcomeStatus.INSUFFICIENT_DATA
    if completeness.censored:
        return OutcomeStatus.UNRESOLVED
    return OutcomeStatus.FAILED


def _unmet_status(completeness: ObservationCompleteness) -> OutcomeStatus:
    if completeness.state in {
        ObservationCompletenessState.NO_DATA,
        ObservationCompletenessState.INCOMPLETE,
        ObservationCompletenessState.PARTIAL,
    }:
        return OutcomeStatus.INSUFFICIENT_DATA
    if completeness.censored:
        return OutcomeStatus.UNRESOLVED
    return OutcomeStatus.FAILED


def _complete_scoring_candles(
    observation_set: ForecastObservationSet,
) -> tuple[ObservationCandle, ...]:
    limit = min(
        _utc(observation_set.actual_evaluation_cutoff_utc),
        _utc(observation_set.horizon_end_utc),
    )
    return tuple(
        item
        for item in observation_set.candles
        if item.is_complete and _utc(item.close_time_utc) <= limit
    )


def _target_hit(
    claim: TargetClaim, candle: ObservationCandle
) -> tuple[bool, float]:
    if claim.evaluation_basis is ClaimEvaluationBasis.CANDLE_CLOSE:
        if claim.direction is ForecastDirection.UP:
            return candle.close >= claim.target_low, candle.close
        if claim.direction is ForecastDirection.DOWN:
            return candle.close <= claim.target_high, candle.close
        return claim.target_low <= candle.close <= claim.target_high, candle.close
    if claim.direction is ForecastDirection.UP:
        return candle.high >= claim.target_low, candle.high
    if claim.direction is ForecastDirection.DOWN:
        return candle.low <= claim.target_high, candle.low
    return candle.low <= claim.target_high and candle.high >= claim.target_low, candle.close


def _operator_hit(
    *,
    operator: InvalidationOperator | ConfirmationOperator,
    basis: ClaimEvaluationBasis,
    level: float,
    candle: ObservationCandle,
) -> tuple[bool, float]:
    if basis is ClaimEvaluationBasis.CANDLE_CLOSE:
        observed = candle.close
        if operator in {
            InvalidationOperator.AT_OR_ABOVE,
            ConfirmationOperator.AT_OR_ABOVE,
        }:
            return observed >= level, observed
        if operator in {
            InvalidationOperator.AT_OR_BELOW,
            ConfirmationOperator.AT_OR_BELOW,
        }:
            return observed <= level, observed
        if operator in {InvalidationOperator.ABOVE, ConfirmationOperator.ABOVE}:
            return observed > level, observed
        return observed < level, observed
    if operator in {
        InvalidationOperator.AT_OR_ABOVE,
        ConfirmationOperator.AT_OR_ABOVE,
    }:
        return candle.high >= level, candle.high
    if operator in {
        InvalidationOperator.AT_OR_BELOW,
        ConfirmationOperator.AT_OR_BELOW,
    }:
        return candle.low <= level, candle.low
    if operator in {InvalidationOperator.ABOVE, ConfirmationOperator.ABOVE}:
        return candle.high > level, candle.high
    return candle.low < level, candle.low


def _precision(basis: ClaimEvaluationBasis) -> EventTimePrecision:
    if basis is ClaimEvaluationBasis.CANDLE_CLOSE:
        return EventTimePrecision.CANDLE_CLOSE
    return EventTimePrecision.CANDLE_INTERVAL


def _evaluate_target_claim(
    claim: TargetClaim,
    *,
    candles: Sequence[ObservationCandle],
    windows: Sequence[ExpectedCompletionWindow],
    completeness: ObservationCompleteness,
) -> ClaimOutcome:
    if claim.direction is ForecastDirection.UNKNOWN:
        return ClaimOutcome(
            claim_id=claim.claim_id,
            hypothesis_id=claim.hypothesis_id,
            claim_type=ClaimType.TARGET,
            evaluation_basis=claim.evaluation_basis,
            outcome_status=OutcomeStatus.INCOMPARABLE,
            price_status=OutcomeStatus.INCOMPARABLE,
            timing_status=OutcomeStatus.INCOMPARABLE,
            comparable=False,
            censored=completeness.censored,
            observation_complete=completeness.horizon_complete,
            unavailable_reason="Target direction is unknown.",
        )
    for candle in candles:
        hit, observed = _target_hit(claim, candle)
        if hit:
            timestamp = candle.close_time_utc
            return ClaimOutcome(
                claim_id=claim.claim_id,
                hypothesis_id=claim.hypothesis_id,
                claim_type=ClaimType.TARGET,
                evaluation_basis=claim.evaluation_basis,
                outcome_status=OutcomeStatus.SUCCEEDED,
                price_status=OutcomeStatus.SUCCEEDED,
                timing_status=_timing_status(timestamp, windows, completeness),
                target_touched=True,
                first_target_timestamp_utc=timestamp,
                event_time_precision=_precision(claim.evaluation_basis),
                event_order=EventOrdering.TARGET_ONLY,
                supporting_candle_ids=(candle.candle_id,),
                observed_value=observed,
                observation_complete=completeness.horizon_complete,
                censored=completeness.censored,
            )
    status = _unmet_status(completeness)
    return ClaimOutcome(
        claim_id=claim.claim_id,
        hypothesis_id=claim.hypothesis_id,
        claim_type=ClaimType.TARGET,
        evaluation_basis=claim.evaluation_basis,
        outcome_status=status,
        price_status=status,
        timing_status=_timing_status(None, windows, completeness),
        target_touched=False,
        event_order=EventOrdering.NO_EVENT,
        observation_complete=completeness.horizon_complete,
        censored=completeness.censored,
        unavailable_reason=(
            "The target was not deterministically evaluable over complete horizon data."
            if status in {OutcomeStatus.INSUFFICIENT_DATA, OutcomeStatus.UNRESOLVED}
            else None
        ),
    )


def _evaluate_invalidation_claim(
    claim: InvalidationClaim,
    *,
    candles: Sequence[ObservationCandle],
    completeness: ObservationCompleteness,
) -> ClaimOutcome:
    if claim.operator is InvalidationOperator.STRUCTURAL_CONDITION:
        return ClaimOutcome(
            claim_id=claim.claim_id,
            hypothesis_id=claim.hypothesis_id,
            claim_type=ClaimType.INVALIDATION,
            evaluation_basis=claim.evaluation_basis,
            outcome_status=OutcomeStatus.UNRESOLVED,
            price_status=OutcomeStatus.UNRESOLVED,
            timing_status=None,
            invalidation_breached=None,
            event_order=EventOrdering.NOT_APPLICABLE,
            observation_complete=completeness.horizon_complete,
            censored=completeness.censored,
            comparable=False,
            unavailable_reason=(
                "Structural invalidation requires a later human structural review."
            ),
        )
    assert claim.price_level is not None
    for candle in candles:
        hit, observed = _operator_hit(
            operator=claim.operator,
            basis=claim.evaluation_basis,
            level=claim.price_level,
            candle=candle,
        )
        if hit:
            return ClaimOutcome(
                claim_id=claim.claim_id,
                hypothesis_id=claim.hypothesis_id,
                claim_type=ClaimType.INVALIDATION,
                evaluation_basis=claim.evaluation_basis,
                outcome_status=OutcomeStatus.FAILED,
                price_status=OutcomeStatus.FAILED,
                timing_status=None,
                invalidation_breached=True,
                first_invalidation_timestamp_utc=candle.close_time_utc,
                event_time_precision=_precision(claim.evaluation_basis),
                event_order=EventOrdering.INVALIDATION_ONLY,
                supporting_candle_ids=(candle.candle_id,),
                observed_value=observed,
                observation_complete=completeness.horizon_complete,
                censored=completeness.censored,
            )
    if completeness.state in {
        ObservationCompletenessState.NO_DATA,
        ObservationCompletenessState.INCOMPLETE,
        ObservationCompletenessState.PARTIAL,
    }:
        status = OutcomeStatus.INSUFFICIENT_DATA
    elif completeness.censored:
        status = OutcomeStatus.UNRESOLVED
    else:
        status = OutcomeStatus.SUCCEEDED
    return ClaimOutcome(
        claim_id=claim.claim_id,
        hypothesis_id=claim.hypothesis_id,
        claim_type=ClaimType.INVALIDATION,
        evaluation_basis=claim.evaluation_basis,
        outcome_status=status,
        price_status=status,
        timing_status=None,
        invalidation_breached=False,
        event_order=EventOrdering.NO_EVENT,
        observation_complete=completeness.horizon_complete,
        censored=completeness.censored,
        unavailable_reason=(
            "A breach cannot be excluded because observation coverage is incomplete."
            if status in {OutcomeStatus.INSUFFICIENT_DATA, OutcomeStatus.UNRESOLVED}
            else None
        ),
    )


def _evaluate_confirmation_claim(
    claim: ConfirmationClaim,
    *,
    candles: Sequence[ObservationCandle],
    windows: Sequence[ExpectedCompletionWindow],
    completeness: ObservationCompleteness,
) -> ClaimOutcome:
    if claim.operator is ConfirmationOperator.STRUCTURAL_CONDITION:
        return ClaimOutcome(
            claim_id=claim.claim_id,
            hypothesis_id=claim.hypothesis_id,
            claim_type=ClaimType.CONFIRMATION,
            evaluation_basis=claim.evaluation_basis,
            outcome_status=OutcomeStatus.UNRESOLVED,
            price_status=OutcomeStatus.UNRESOLVED,
            timing_status=OutcomeStatus.UNRESOLVED,
            confirmation_met=None,
            event_order=EventOrdering.NOT_APPLICABLE,
            observation_complete=completeness.horizon_complete,
            censored=completeness.censored,
            comparable=False,
            unavailable_reason=(
                "Structural confirmation requires a later human structural review."
            ),
        )
    assert claim.price_level is not None
    for candle in candles:
        hit, observed = _operator_hit(
            operator=claim.operator,
            basis=claim.evaluation_basis,
            level=claim.price_level,
            candle=candle,
        )
        if hit:
            timestamp = candle.close_time_utc
            return ClaimOutcome(
                claim_id=claim.claim_id,
                hypothesis_id=claim.hypothesis_id,
                claim_type=ClaimType.CONFIRMATION,
                evaluation_basis=claim.evaluation_basis,
                outcome_status=OutcomeStatus.SUCCEEDED,
                price_status=OutcomeStatus.SUCCEEDED,
                timing_status=_timing_status(timestamp, windows, completeness),
                confirmation_met=True,
                first_confirmation_timestamp_utc=timestamp,
                event_time_precision=_precision(claim.evaluation_basis),
                event_order=EventOrdering.NOT_APPLICABLE,
                supporting_candle_ids=(candle.candle_id,),
                observed_value=observed,
                observation_complete=completeness.horizon_complete,
                censored=completeness.censored,
            )
    status = _unmet_status(completeness)
    return ClaimOutcome(
        claim_id=claim.claim_id,
        hypothesis_id=claim.hypothesis_id,
        claim_type=ClaimType.CONFIRMATION,
        evaluation_basis=claim.evaluation_basis,
        outcome_status=status,
        price_status=status,
        timing_status=_timing_status(None, windows, completeness),
        confirmation_met=False,
        event_order=EventOrdering.NO_EVENT,
        observation_complete=completeness.horizon_complete,
        censored=completeness.censored,
        unavailable_reason=(
            "Confirmation cannot be excluded because observation coverage is incomplete."
            if status in {OutcomeStatus.INSUFFICIENT_DATA, OutcomeStatus.UNRESOLVED}
            else None
        ),
    )


def _first_outcome(
    outcomes: Sequence[ClaimOutcome], timestamp_field: str
) -> ClaimOutcome | None:
    available = [item for item in outcomes if getattr(item, timestamp_field) is not None]
    if not available:
        return None
    return min(available, key=lambda item: _utc(getattr(item, timestamp_field)))


def _event_candle(
    outcome: ClaimOutcome, candle_by_id: Mapping[str, ObservationCandle]
) -> ObservationCandle | None:
    if not outcome.supporting_candle_ids:
        return None
    return candle_by_id.get(outcome.supporting_candle_ids[0])


def _primary_event_order(
    target: ClaimOutcome | None,
    invalidation: ClaimOutcome | None,
    candle_by_id: Mapping[str, ObservationCandle],
) -> EventOrdering:
    if target is None and invalidation is None:
        return EventOrdering.NO_EVENT
    if target is not None and invalidation is None:
        return EventOrdering.TARGET_ONLY
    if target is None:
        return EventOrdering.INVALIDATION_ONLY
    assert invalidation is not None
    target_candle = _event_candle(target, candle_by_id)
    invalidation_candle = _event_candle(invalidation, candle_by_id)
    if target_candle is None or invalidation_candle is None:
        return EventOrdering.INCOMPARABLE
    if target_candle.candle_id == invalidation_candle.candle_id:
        return EventOrdering.INCOMPARABLE
    if _utc(target_candle.open_time_utc) < _utc(invalidation_candle.open_time_utc):
        return EventOrdering.TARGET_BEFORE_INVALIDATION
    return EventOrdering.INVALIDATION_BEFORE_TARGET


def _replace_with_lower_event(
    outcome: ClaimOutcome,
    lower_outcome: ClaimOutcome,
) -> ClaimOutcome:
    if outcome.claim_type is ClaimType.TARGET:
        return replace(
            outcome,
            first_target_timestamp_utc=lower_outcome.first_target_timestamp_utc,
            event_time_precision=EventTimePrecision.LOWER_TIMEFRAME_CANDLE,
            supporting_candle_ids=lower_outcome.supporting_candle_ids,
            observed_value=lower_outcome.observed_value,
        )
    if outcome.claim_type is ClaimType.INVALIDATION:
        return replace(
            outcome,
            first_invalidation_timestamp_utc=(
                lower_outcome.first_invalidation_timestamp_utc
            ),
            event_time_precision=EventTimePrecision.LOWER_TIMEFRAME_CANDLE,
            supporting_candle_ids=lower_outcome.supporting_candle_ids,
            observed_value=lower_outcome.observed_value,
        )
    return outcome


def _resolve_collision_with_lower_timeframe(
    *,
    parent_candle: ObservationCandle,
    target_claims: Sequence[TargetClaim],
    invalidation_claims: Sequence[InvalidationClaim],
    target_outcomes: Sequence[ClaimOutcome],
    invalidation_outcomes: Sequence[ClaimOutcome],
    lower_sets: Sequence[ForecastObservationSet],
    windows: Sequence[ExpectedCompletionWindow],
) -> tuple[
    EventOrdering,
    str | None,
    tuple[ClaimOutcome, ...],
    tuple[ClaimOutcome, ...],
]:
    for lower in sorted(
        lower_sets,
        key=lambda item: (
            item.evaluation_policy.expected_interval_seconds,
            item.observation_set_id,
        ),
    ):
        lower_candles = tuple(
            item
            for item in _complete_scoring_candles(lower)
            if _utc(item.open_time_utc) >= _utc(parent_candle.open_time_utc)
            and _utc(item.close_time_utc) <= _utc(parent_candle.close_time_utc)
        )
        if not lower_candles:
            continue
        lower_targets = tuple(
            _evaluate_target_claim(
                claim,
                candles=lower_candles,
                windows=windows,
                completeness=lower.completeness,
            )
            for claim in target_claims
        )
        lower_invalidations = tuple(
            _evaluate_invalidation_claim(
                claim,
                candles=lower_candles,
                completeness=lower.completeness,
            )
            for claim in invalidation_claims
        )
        first_target = _first_outcome(lower_targets, "first_target_timestamp_utc")
        first_invalidation = _first_outcome(
            lower_invalidations, "first_invalidation_timestamp_utc"
        )
        lower_by_id = {item.candle_id: item for item in lower_candles}
        order = _primary_event_order(first_target, first_invalidation, lower_by_id)
        if order not in {
            EventOrdering.TARGET_BEFORE_INVALIDATION,
            EventOrdering.INVALIDATION_BEFORE_TARGET,
        }:
            continue
        target_by_id = {item.claim_id: item for item in lower_targets}
        invalidation_by_id = {item.claim_id: item for item in lower_invalidations}
        updated_targets = tuple(
            _replace_with_lower_event(item, target_by_id[item.claim_id])
            if target_by_id[item.claim_id].target_touched
            else item
            for item in target_outcomes
        )
        updated_invalidations = tuple(
            _replace_with_lower_event(item, invalidation_by_id[item.claim_id])
            if invalidation_by_id[item.claim_id].invalidation_breached
            else item
            for item in invalidation_outcomes
        )
        return order, lower.observation_set_id, updated_targets, updated_invalidations
    return (
        EventOrdering.INCOMPARABLE,
        None,
        tuple(target_outcomes),
        tuple(invalidation_outcomes),
    )


def _completion_window_outcome(
    window: ExpectedCompletionWindow,
    *,
    completion_timestamp_utc: str | None,
    completeness: ObservationCompleteness,
    supporting_candle_ids: tuple[str, ...],
) -> ClaimOutcome:
    status = _timing_status(completion_timestamp_utc, (window,), completeness)
    return ClaimOutcome(
        claim_id=window.window_id,
        hypothesis_id=window.hypothesis_id,
        claim_type=ClaimType.COMPLETION_WINDOW,
        evaluation_basis=None,
        outcome_status=status,
        price_status=None,
        timing_status=status,
        event_time_precision=(
            EventTimePrecision.CANDLE_INTERVAL
            if completion_timestamp_utc is not None
            else EventTimePrecision.UNAVAILABLE
        ),
        event_order=EventOrdering.NOT_APPLICABLE,
        supporting_candle_ids=supporting_candle_ids,
        observation_complete=completeness.horizon_complete,
        censored=completeness.censored,
        unavailable_reason=(
            "Completion timing cannot be finalized with the available horizon."
            if status in {OutcomeStatus.INSUFFICIENT_DATA, OutcomeStatus.UNRESOLVED}
            else None
        ),
    )


def _aggregate_timing(
    window_outcomes: Sequence[ClaimOutcome],
) -> OutcomeStatus:
    if not window_outcomes:
        return OutcomeStatus.INSUFFICIENT_DATA
    statuses = {item.timing_status for item in window_outcomes}
    if OutcomeStatus.SUCCEEDED in statuses:
        return OutcomeStatus.SUCCEEDED
    if statuses == {OutcomeStatus.FAILED}:
        return OutcomeStatus.FAILED
    if OutcomeStatus.INCOMPARABLE in statuses:
        return OutcomeStatus.INCOMPARABLE
    if OutcomeStatus.INSUFFICIENT_DATA in statuses:
        return OutcomeStatus.INSUFFICIENT_DATA
    return OutcomeStatus.UNRESOLVED


def _aggregate_price(
    *,
    target_outcomes: Sequence[ClaimOutcome],
    invalidation_outcomes: Sequence[ClaimOutcome],
    confirmation_outcomes: Sequence[ClaimOutcome],
    event_order: EventOrdering,
    completeness: ObservationCompleteness,
) -> OutcomeStatus:
    if event_order is EventOrdering.INCOMPARABLE:
        return OutcomeStatus.INCOMPARABLE
    if completeness.state in {
        ObservationCompletenessState.NO_DATA,
        ObservationCompletenessState.INCOMPLETE,
        ObservationCompletenessState.PARTIAL,
    }:
        return OutcomeStatus.INSUFFICIENT_DATA
    if event_order in {
        EventOrdering.INVALIDATION_ONLY,
        EventOrdering.INVALIDATION_BEFORE_TARGET,
    }:
        return OutcomeStatus.FAILED
    if not target_outcomes:
        return OutcomeStatus.INSUFFICIENT_DATA
    target_count = sum(item.target_touched is True for item in target_outcomes)
    all_targets = target_count == len(target_outcomes)
    any_target = target_count > 0
    comparable_confirmations = [
        item for item in confirmation_outcomes if item.comparable
    ]
    unresolved_structural = any(not item.comparable for item in confirmation_outcomes)
    all_confirmations = all(
        item.confirmation_met is True for item in comparable_confirmations
    )
    if completeness.censored:
        if any_target:
            return OutcomeStatus.PARTIAL
        return OutcomeStatus.UNRESOLVED
    if unresolved_structural:
        return OutcomeStatus.UNRESOLVED
    if all_targets and all_confirmations:
        if event_order is EventOrdering.TARGET_BEFORE_INVALIDATION:
            return OutcomeStatus.PARTIAL
        return OutcomeStatus.SUCCEEDED
    if any_target:
        return OutcomeStatus.PARTIAL
    return OutcomeStatus.FAILED


def _aggregate_hypothesis_status(
    price_status: OutcomeStatus, timing_status: OutcomeStatus
) -> OutcomeStatus:
    if OutcomeStatus.INCOMPARABLE in {price_status, timing_status}:
        return OutcomeStatus.INCOMPARABLE
    if OutcomeStatus.INSUFFICIENT_DATA in {price_status, timing_status}:
        return OutcomeStatus.INSUFFICIENT_DATA
    if OutcomeStatus.UNRESOLVED in {price_status, timing_status}:
        return OutcomeStatus.UNRESOLVED
    if price_status is OutcomeStatus.FAILED:
        return OutcomeStatus.FAILED
    if (
        price_status is OutcomeStatus.SUCCEEDED
        and timing_status is OutcomeStatus.SUCCEEDED
    ):
        return OutcomeStatus.SUCCEEDED
    return OutcomeStatus.PARTIAL


def _reference_from_hypothesis(
    hypothesis: MainHypothesis | AlternativeHypothesis,
    *,
    forecast: ForecastRecord,
    observation_set: ForecastObservationSet,
) -> tuple[float | None, str | None, str | None]:
    count = hypothesis.count
    if not isinstance(count, Mapping):
        return None, None, "The frozen hypothesis has no typed evaluation reference."
    raw = count.get("evaluation_reference")
    if isinstance(raw, Mapping):
        reference = dict(raw)
    elif "reference_price" in count:
        reference = {
            "price": count.get("reference_price"),
            "timestamp_utc": count.get("reference_timestamp_utc"),
            "price_basis": count.get("reference_price_basis"),
        }
    else:
        return None, None, "The frozen hypothesis has no typed evaluation reference."
    try:
        price = _finite(reference.get("price"), field_name="reference.price")
        timestamp = _normalize_utc(
            reference.get("timestamp_utc"), field_name="reference.timestamp_utc"
        )
    except (TypeError, ValueError) as exc:
        return None, None, f"The frozen evaluation reference is invalid: {exc}"
    if price <= 0:
        return None, None, "The frozen evaluation reference price must be positive."
    if _utc(timestamp) > _utc(forecast.analysis_cutoff_utc):
        return None, None, "The evaluation reference was not available at the cutoff."
    basis = reference.get("price_basis")
    if not isinstance(basis, str) or basis.casefold() != observation_set.price_basis.casefold():
        return None, None, "The evaluation reference price basis is incompatible."
    if hypothesis.direction not in {ForecastDirection.UP, ForecastDirection.DOWN}:
        return None, None, "MFE and MAE require an up or down hypothesis direction."
    return price, timestamp, None


def _excursions(
    hypothesis: MainHypothesis | AlternativeHypothesis,
    *,
    forecast: ForecastRecord,
    observation_set: ForecastObservationSet,
    candles: Sequence[ObservationCandle],
) -> tuple[
    float | None,
    str | None,
    float | None,
    float | None,
    float | None,
    float | None,
    bool,
    str | None,
    tuple[str, ...],
]:
    reference_price, reference_timestamp, reason = _reference_from_hypothesis(
        hypothesis, forecast=forecast, observation_set=observation_set
    )
    if reason is not None:
        return None, None, None, None, None, None, False, reason, ()
    if observation_set.completeness.state in {
        ObservationCompletenessState.NO_DATA,
        ObservationCompletenessState.INCOMPLETE,
        ObservationCompletenessState.PARTIAL,
    }:
        return (
            reference_price,
            reference_timestamp,
            None,
            None,
            None,
            None,
            False,
            "MFE and MAE are unavailable because the observation sequence has gaps or partial data.",
            (),
        )
    if not candles:
        return (
            reference_price,
            reference_timestamp,
            None,
            None,
            None,
            None,
            False,
            "MFE and MAE require at least one complete post-cutoff candle.",
            (),
        )
    assert reference_price is not None
    if hypothesis.direction is ForecastDirection.UP:
        favorable = max(candles, key=lambda item: item.high)
        adverse = min(candles, key=lambda item: item.low)
        mfe = max(favorable.high - reference_price, 0.0)
        mae = max(reference_price - adverse.low, 0.0)
    else:
        favorable = min(candles, key=lambda item: item.low)
        adverse = max(candles, key=lambda item: item.high)
        mfe = max(reference_price - favorable.low, 0.0)
        mae = max(adverse.high - reference_price, 0.0)
    return (
        reference_price,
        reference_timestamp,
        mfe,
        mae,
        (mfe / reference_price) * 100.0,
        (mae / reference_price) * 100.0,
        True,
        None,
        tuple(dict.fromkeys((favorable.candle_id, adverse.candle_id))),
    )


def _evaluate_hypothesis(
    hypothesis: MainHypothesis | AlternativeHypothesis,
    *,
    is_main: bool,
    forecast: ForecastRecord,
    observation_set: ForecastObservationSet,
    lower_sets: Sequence[ForecastObservationSet],
) -> HypothesisOutcome:
    target_by_id = {item.claim_id: item for item in forecast.target_claims}
    invalidation_by_id = {item.claim_id: item for item in forecast.invalidation_claims}
    confirmation_by_id = {item.claim_id: item for item in forecast.confirmation_claims}
    windows = _claim_windows(hypothesis, forecast)
    candles = _complete_scoring_candles(observation_set)
    target_claims = tuple(
        target_by_id[item]
        for item in hypothesis.target_claim_ids
        if item in target_by_id
    )
    invalidation_claims = tuple(
        invalidation_by_id[item]
        for item in hypothesis.invalidation_claim_ids
        if item in invalidation_by_id
    )
    confirmation_claims = tuple(
        confirmation_by_id[item]
        for item in hypothesis.confirmation_claim_ids
        if item in confirmation_by_id
    )
    target_outcomes = tuple(
        _evaluate_target_claim(
            item,
            candles=candles,
            windows=windows,
            completeness=observation_set.completeness,
        )
        for item in target_claims
    )
    invalidation_outcomes = tuple(
        _evaluate_invalidation_claim(
            item,
            candles=candles,
            completeness=observation_set.completeness,
        )
        for item in invalidation_claims
    )
    confirmation_outcomes = tuple(
        _evaluate_confirmation_claim(
            item,
            candles=candles,
            windows=windows,
            completeness=observation_set.completeness,
        )
        for item in confirmation_claims
    )
    candle_by_id = {item.candle_id: item for item in candles}
    first_target = _first_outcome(target_outcomes, "first_target_timestamp_utc")
    first_invalidation = _first_outcome(
        invalidation_outcomes, "first_invalidation_timestamp_utc"
    )
    event_order = _primary_event_order(
        first_target, first_invalidation, candle_by_id
    )
    lower_resolution_id: str | None = None
    if event_order is EventOrdering.INCOMPARABLE and first_target and first_invalidation:
        target_candle = _event_candle(first_target, candle_by_id)
        invalidation_candle = _event_candle(first_invalidation, candle_by_id)
        if target_candle is not None and invalidation_candle is not None and (
            target_candle.candle_id == invalidation_candle.candle_id
        ):
            (
                event_order,
                lower_resolution_id,
                target_outcomes,
                invalidation_outcomes,
            ) = _resolve_collision_with_lower_timeframe(
                parent_candle=target_candle,
                target_claims=target_claims,
                invalidation_claims=invalidation_claims,
                target_outcomes=target_outcomes,
                invalidation_outcomes=invalidation_outcomes,
                lower_sets=lower_sets,
                windows=windows,
            )
            first_target = _first_outcome(
                target_outcomes, "first_target_timestamp_utc"
            )
            first_invalidation = _first_outcome(
                invalidation_outcomes, "first_invalidation_timestamp_utc"
            )
    touched_targets = [
        item for item in target_outcomes if item.first_target_timestamp_utc is not None
    ]
    completion_timestamp = (
        max(item.first_target_timestamp_utc for item in touched_targets)
        if target_outcomes and len(touched_targets) == len(target_outcomes)
        else None
    )
    completion_candle_ids = tuple(
        dict.fromkeys(
            candle_id
            for item in touched_targets
            for candle_id in item.supporting_candle_ids
        )
    )
    window_outcomes = tuple(
        _completion_window_outcome(
            item,
            completion_timestamp_utc=completion_timestamp,
            completeness=observation_set.completeness,
            supporting_candle_ids=completion_candle_ids,
        )
        for item in windows
    )
    price_status = _aggregate_price(
        target_outcomes=target_outcomes,
        invalidation_outcomes=invalidation_outcomes,
        confirmation_outcomes=confirmation_outcomes,
        event_order=event_order,
        completeness=observation_set.completeness,
    )
    timing_status = _aggregate_timing(window_outcomes)
    outcome_status = _aggregate_hypothesis_status(price_status, timing_status)
    (
        reference_price,
        reference_timestamp,
        mfe,
        mae,
        mfe_pct,
        mae_pct,
        excursion_available,
        excursion_reason,
        excursion_candle_ids,
    ) = _excursions(
        hypothesis,
        forecast=forecast,
        observation_set=observation_set,
        candles=candles,
    )
    all_outcomes = (
        target_outcomes
        + invalidation_outcomes
        + confirmation_outcomes
        + window_outcomes
    )
    warnings = list(observation_set.completeness.warnings)
    if event_order is EventOrdering.INCOMPARABLE:
        warnings.append(
            "Target and invalidation order is incomparable at the available resolution."
        )
    if lower_resolution_id is not None:
        warnings.append(
            "A registered lower-timeframe observation set resolved the event order."
        )
    supporting_ids = tuple(
        dict.fromkeys(
            candle_id
            for item in all_outcomes
            for candle_id in item.supporting_candle_ids
        )
    )
    supporting_ids = tuple(dict.fromkeys(supporting_ids + excursion_candle_ids))
    precision = EventTimePrecision.UNAVAILABLE
    if lower_resolution_id is not None:
        precision = EventTimePrecision.LOWER_TIMEFRAME_CANDLE
    elif first_target is not None:
        precision = first_target.event_time_precision
    elif first_invalidation is not None:
        precision = first_invalidation.event_time_precision
    return HypothesisOutcome(
        hypothesis_id=hypothesis.hypothesis_id,
        is_main=is_main,
        direction=hypothesis.direction,
        outcome_status=outcome_status,
        price_status=price_status,
        timing_status=timing_status,
        claim_outcomes=all_outcomes,
        event_order=event_order,
        first_target_timestamp_utc=(
            first_target.first_target_timestamp_utc if first_target else None
        ),
        first_invalidation_timestamp_utc=(
            first_invalidation.first_invalidation_timestamp_utc
            if first_invalidation
            else None
        ),
        completion_timestamp_utc=completion_timestamp,
        event_time_precision=precision,
        reference_price=reference_price,
        reference_timestamp_utc=reference_timestamp,
        mfe=mfe,
        mae=mae,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        excursion_available=excursion_available,
        excursion_unavailable_reason=excursion_reason,
        supporting_candle_ids=supporting_ids,
        lower_timeframe_resolution_observation_set_id=lower_resolution_id,
        observation_complete=observation_set.completeness.horizon_complete,
        censored=observation_set.completeness.censored,
        comparable=event_order is not EventOrdering.INCOMPARABLE,
        warnings=tuple(warnings),
    )


def evaluate_forecast_observation_set(
    forecast: ForecastRecord,
    observation_set: ForecastObservationSet,
    *,
    lower_timeframe_observation_sets: Sequence[ForecastObservationSet] = (),
    evaluation_id: str | None = None,
    evaluation_version: int = 1,
    supersedes_evaluation_id: str | None = None,
    created_at_utc: str | None = None,
) -> ForecastOutcomeEvaluation:
    """Evaluate one frozen forecast without model calls or external context."""

    errors = validate_forecast_observation_set(observation_set, forecast)
    if errors:
        raise ValueError("Invalid ForecastObservationSet: " + " ".join(errors))
    if forecast.evaluation_eligibility.status is EvaluationEligibilityStatus.LEGACY_UNSCORABLE:
        raise ValueError("legacy_unscorable forecasts cannot be deterministically evaluated.")
    lower_sets = tuple(lower_timeframe_observation_sets)
    for lower in lower_sets:
        lower_errors = validate_forecast_observation_set(lower, forecast)
        if lower_errors:
            raise ValueError(
                f"Invalid lower-timeframe observation set {lower.observation_set_id}: "
                + " ".join(lower_errors)
            )
        if (
            lower.evaluation_policy.expected_interval_seconds
            >= observation_set.evaluation_policy.expected_interval_seconds
        ):
            raise ValueError(
                "A collision-resolution observation set must use a lower timeframe."
            )
        for name in (
            "symbol",
            "exchange",
            "provider",
            "feed",
            "session",
            "timezone",
            "price_basis",
        ):
            if str(getattr(lower, name)).casefold() != str(
                getattr(observation_set, name)
            ).casefold():
                raise ValueError(
                    f"Lower-timeframe observation {name} is incompatible."
                )
        if canonical_sha256(lower.adjustment) != canonical_sha256(
            observation_set.adjustment
        ):
            raise ValueError("Lower-timeframe observation adjustment is incompatible.")
    main_outcome = _evaluate_hypothesis(
        forecast.main_hypothesis,
        is_main=True,
        forecast=forecast,
        observation_set=observation_set,
        lower_sets=lower_sets,
    )
    alternative_outcomes = tuple(
        _evaluate_hypothesis(
            item,
            is_main=False,
            forecast=forecast,
            observation_set=observation_set,
            lower_sets=lower_sets,
        )
        for item in forecast.alternative_hypotheses
    )
    evaluated_through = min(
        _utc(observation_set.actual_evaluation_cutoff_utc),
        _utc(observation_set.horizon_end_utc),
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    lower_ids = tuple(item.observation_set_id for item in lower_sets)
    lower_hashes = {
        item.observation_set_id: item.content_hash for item in lower_sets
    }
    source_hashes = {
        "forecast_record": forecast.content_hash,
        "observation_set": observation_set.content_hash,
        "evaluation_policy": observation_set.policy_hash,
    }
    for item in lower_sets:
        source_hashes[f"lower_observation:{item.observation_set_id}"] = item.content_hash
    result = ForecastOutcomeEvaluation.create(
        evaluation_id=evaluation_id or "",
        evaluation_version=evaluation_version,
        supersedes_evaluation_id=supersedes_evaluation_id,
        forecast_id=forecast.forecast_id,
        forecast_content_hash=forecast.content_hash,
        observation_set_id=observation_set.observation_set_id,
        observation_set_content_hash=observation_set.content_hash,
        lower_timeframe_observation_set_ids=lower_ids,
        lower_timeframe_observation_set_hashes=lower_hashes,
        created_at_utc=created_at_utc or evaluated_through,
        evaluated_through_utc=evaluated_through,
        evaluation_policy_id=observation_set.evaluation_policy.policy_id,
        evaluation_policy_hash=observation_set.policy_hash,
        main_hypothesis_outcome=main_outcome,
        alternative_hypothesis_outcomes=alternative_outcomes,
        outcome_status=main_outcome.outcome_status,
        warnings=observation_set.completeness.warnings,
        source_hashes=source_hashes,
    )
    result_errors = validate_forecast_outcome_evaluation(
        result,
        forecast=forecast,
        observation_set=observation_set,
        lower_timeframe_observation_sets=lower_sets,
    )
    if result_errors:
        raise ValueError(
            "Invalid ForecastOutcomeEvaluation: " + " ".join(result_errors)
        )
    return result


def validate_forecast_outcome_evaluation(
    value: ForecastOutcomeEvaluation | Mapping[str, Any],
    *,
    forecast: ForecastRecord,
    observation_set: ForecastObservationSet,
    lower_timeframe_observation_sets: Sequence[ForecastObservationSet] = (),
) -> tuple[str, ...]:
    if isinstance(value, ForecastOutcomeEvaluation):
        evaluation = value
    else:
        try:
            evaluation = ForecastOutcomeEvaluation.from_dict(
                value, verify_hash=False
            )
        except (TypeError, ValueError) as exc:
            return (str(exc),)
    errors: list[str] = []
    if evaluation.schema_version != OUTCOME_EVALUATION_SCHEMA_VERSION:
        errors.append("Unsupported outcome-evaluation schema_version.")
    if evaluation.calculation_version != OUTCOME_CALCULATION_VERSION:
        errors.append("Unsupported outcome-evaluation calculation_version.")
    if evaluation.policy_version != OUTCOME_POLICY_VERSION:
        errors.append("Unsupported outcome-evaluation policy_version.")
    if evaluation.supersedes_evaluation_id is None and evaluation.evaluation_version != 1:
        errors.append("An evaluation without a predecessor must have version 1.")
    if evaluation.supersedes_evaluation_id is not None and evaluation.evaluation_version == 1:
        errors.append("A superseding evaluation must have version greater than 1.")
    if not evaluation.content_hash:
        errors.append("Outcome-evaluation content_hash is required.")
    elif evaluation.content_hash != forecast_outcome_evaluation_content_hash(evaluation):
        errors.append("Outcome-evaluation content_hash does not match its payload.")
    if evaluation.forecast_id != forecast.forecast_id:
        errors.append("Outcome evaluation references a different forecast ID.")
    if evaluation.forecast_content_hash != forecast.content_hash:
        errors.append("Outcome evaluation references a different forecast hash.")
    if evaluation.observation_set_id != observation_set.observation_set_id:
        errors.append("Outcome evaluation references a different observation-set ID.")
    if evaluation.observation_set_content_hash != observation_set.content_hash:
        errors.append("Outcome evaluation references a different observation-set hash.")
    if evaluation.evaluation_policy_id != observation_set.evaluation_policy.policy_id:
        errors.append("Outcome evaluation references a different policy ID.")
    if evaluation.evaluation_policy_hash != observation_set.policy_hash:
        errors.append("Outcome evaluation references a different policy hash.")
    expected_through = min(
        _utc(observation_set.actual_evaluation_cutoff_utc),
        _utc(observation_set.horizon_end_utc),
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    if evaluation.evaluated_through_utc != expected_through:
        errors.append("Outcome evaluation exceeds or understates its frozen horizon.")
    if evaluation.main_hypothesis_outcome.hypothesis_id != forecast.main_hypothesis.hypothesis_id:
        errors.append("Main hypothesis outcome references a different hypothesis.")
    if not evaluation.main_hypothesis_outcome.is_main:
        errors.append("Main hypothesis outcome is not marked as main.")
    expected_alternatives = tuple(
        item.hypothesis_id for item in forecast.alternative_hypotheses
    )
    actual_alternatives = tuple(
        item.hypothesis_id for item in evaluation.alternative_hypothesis_outcomes
    )
    if actual_alternatives != expected_alternatives:
        errors.append("Alternative outcomes do not preserve forecast order and identity.")
    if any(item.is_main for item in evaluation.alternative_hypothesis_outcomes):
        errors.append("An alternative hypothesis outcome is incorrectly marked main.")
    if evaluation.outcome_status is not evaluation.main_hypothesis_outcome.outcome_status:
        errors.append("Forecast-level outcome must mirror only the main hypothesis.")
    lower_by_id = {
        item.observation_set_id: item
        for item in lower_timeframe_observation_sets
    }
    if tuple(lower_by_id) != evaluation.lower_timeframe_observation_set_ids:
        errors.append("Lower-timeframe observation IDs differ from evaluator inputs.")
    for item_id, item_hash in evaluation.lower_timeframe_observation_set_hashes.items():
        if item_id not in lower_by_id or lower_by_id[item_id].content_hash != item_hash:
            errors.append("A lower-timeframe observation hash does not match.")
    required_hashes = {
        "forecast_record": forecast.content_hash,
        "observation_set": observation_set.content_hash,
        "evaluation_policy": observation_set.policy_hash,
    }
    for item in lower_timeframe_observation_sets:
        required_hashes[f"lower_observation:{item.observation_set_id}"] = item.content_hash
    for key, expected in required_hashes.items():
        if evaluation.source_hashes.get(key) != expected:
            errors.append(f"Outcome source_hashes[{key!r}] does not match.")
    return tuple(errors)


def replay_forecast_observation_set(
    forecast: ForecastRecord,
    serialized_observation_set: Mapping[str, Any],
) -> ForecastObservationSet:
    """Load and verify an immutable observation set without refetching data."""

    result = ForecastObservationSet.from_dict(serialized_observation_set)
    errors = validate_forecast_observation_set(result, forecast)
    if errors:
        raise ValueError("Invalid ForecastObservationSet replay: " + " ".join(errors))
    return result


def replay_forecast_outcome_evaluation(
    forecast: ForecastRecord,
    observation_set: ForecastObservationSet,
    *,
    lower_timeframe_observation_sets: Sequence[ForecastObservationSet] = (),
    evaluation_id: str | None = None,
    evaluation_version: int = 1,
    supersedes_evaluation_id: str | None = None,
    created_at_utc: str | None = None,
) -> ForecastOutcomeEvaluation:
    """Deterministically rerun evaluation from immutable supplied inputs."""

    return evaluate_forecast_observation_set(
        forecast,
        observation_set,
        lower_timeframe_observation_sets=lower_timeframe_observation_sets,
        evaluation_id=evaluation_id,
        evaluation_version=evaluation_version,
        supersedes_evaluation_id=supersedes_evaluation_id,
        created_at_utc=created_at_utc,
    )


# Concise aliases for callers that describe the operation rather than the input.
evaluate_forecast_outcome = evaluate_forecast_observation_set
replay_forecast_outcome = replay_forecast_outcome_evaluation


FORECAST_OUTCOME_MIGRATION_SQL = (
    """
    CREATE TABLE forecast_observation_sets (
        observation_set_id TEXT PRIMARY KEY
            CHECK (length(trim(observation_set_id)) > 0),
        observation_set_version INTEGER NOT NULL
            CHECK (observation_set_version >= 1),
        supersedes_observation_set_id TEXT
            REFERENCES forecast_observation_sets(observation_set_id)
            ON DELETE RESTRICT,
        forecast_id TEXT NOT NULL
            REFERENCES forecast_records(forecast_id)
            ON DELETE RESTRICT,
        source_dataset_id TEXT NOT NULL
            CHECK (length(trim(source_dataset_id)) > 0),
        schema_version TEXT NOT NULL
            CHECK (length(trim(schema_version)) > 0),
        calculation_version TEXT NOT NULL
            CHECK (length(trim(calculation_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        symbol TEXT NOT NULL
            CHECK (length(trim(symbol)) > 0),
        timeframe TEXT NOT NULL
            CHECK (length(trim(timeframe)) > 0),
        actual_evaluation_cutoff_utc TEXT NOT NULL
            CHECK (length(trim(actual_evaluation_cutoff_utc)) > 0),
        horizon_end_utc TEXT NOT NULL
            CHECK (length(trim(horizon_end_utc)) > 0),
        candle_manifest_hash TEXT NOT NULL
            CHECK (
                length(candle_manifest_hash) = 64
                AND candle_manifest_hash NOT GLOB '*[^0-9a-f]*'
            ),
        policy_hash TEXT NOT NULL
            CHECK (
                length(policy_hash) = 64
                AND policy_hash NOT GLOB '*[^0-9a-f]*'
            ),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL
            CHECK (length(trim(record_json)) > 1),
        created_at_utc TEXT NOT NULL
            CHECK (length(trim(created_at_utc)) > 0),
        UNIQUE (forecast_id, source_dataset_id, observation_set_version),
        CHECK (
            supersedes_observation_set_id IS NULL
            OR supersedes_observation_set_id <> observation_set_id
        )
    )
    """,
    """
    CREATE TABLE forecast_outcome_evaluations (
        evaluation_id TEXT PRIMARY KEY
            CHECK (length(trim(evaluation_id)) > 0),
        evaluation_version INTEGER NOT NULL
            CHECK (evaluation_version >= 1),
        supersedes_evaluation_id TEXT
            REFERENCES forecast_outcome_evaluations(evaluation_id)
            ON DELETE RESTRICT,
        forecast_id TEXT NOT NULL
            REFERENCES forecast_records(forecast_id)
            ON DELETE RESTRICT,
        observation_set_id TEXT NOT NULL
            REFERENCES forecast_observation_sets(observation_set_id)
            ON DELETE RESTRICT,
        schema_version TEXT NOT NULL
            CHECK (length(trim(schema_version)) > 0),
        calculation_version TEXT NOT NULL
            CHECK (length(trim(calculation_version)) > 0),
        policy_version TEXT NOT NULL
            CHECK (length(trim(policy_version)) > 0),
        outcome_status TEXT NOT NULL
            CHECK (
                outcome_status IN (
                    'succeeded', 'failed', 'partial', 'unresolved',
                    'insufficient_data', 'incomparable'
                )
            ),
        evaluated_through_utc TEXT NOT NULL
            CHECK (length(trim(evaluated_through_utc)) > 0),
        evaluation_policy_hash TEXT NOT NULL
            CHECK (
                length(evaluation_policy_hash) = 64
                AND evaluation_policy_hash NOT GLOB '*[^0-9a-f]*'
            ),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL
            CHECK (length(trim(record_json)) > 1),
        created_at_utc TEXT NOT NULL
            CHECK (length(trim(created_at_utc)) > 0),
        UNIQUE (forecast_id, observation_set_id, evaluation_version),
        CHECK (
            supersedes_evaluation_id IS NULL
            OR supersedes_evaluation_id <> evaluation_id
        )
    )
    """,
    "CREATE INDEX forecast_observation_sets_forecast_idx ON forecast_observation_sets(forecast_id, actual_evaluation_cutoff_utc, observation_set_version)",
    "CREATE UNIQUE INDEX forecast_observation_sets_supersedes_idx ON forecast_observation_sets(supersedes_observation_set_id) WHERE supersedes_observation_set_id IS NOT NULL",
    "CREATE INDEX forecast_outcome_evaluations_forecast_idx ON forecast_outcome_evaluations(forecast_id, evaluated_through_utc, evaluation_version)",
    "CREATE INDEX forecast_outcome_evaluations_observation_idx ON forecast_outcome_evaluations(observation_set_id, evaluation_version)",
    "CREATE UNIQUE INDEX forecast_outcome_evaluations_supersedes_idx ON forecast_outcome_evaluations(supersedes_evaluation_id) WHERE supersedes_evaluation_id IS NOT NULL",
    """
    CREATE TRIGGER forecast_observation_sets_no_update
    BEFORE UPDATE ON forecast_observation_sets
    BEGIN
        SELECT RAISE(ABORT, 'forecast_observation_sets is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_observation_sets_no_delete
    BEFORE DELETE ON forecast_observation_sets
    BEGIN
        SELECT RAISE(ABORT, 'forecast_observation_sets is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_outcome_evaluations_no_update
    BEFORE UPDATE ON forecast_outcome_evaluations
    BEGIN
        SELECT RAISE(ABORT, 'forecast_outcome_evaluations is append-only');
    END
    """,
    """
    CREATE TRIGGER forecast_outcome_evaluations_no_delete
    BEFORE DELETE ON forecast_outcome_evaluations
    BEGIN
        SELECT RAISE(ABORT, 'forecast_outcome_evaluations is append-only');
    END
    """,
)

FORECAST_OUTCOME_TABLES = frozenset(
    {"forecast_observation_sets", "forecast_outcome_evaluations"}
)
FORECAST_OUTCOME_INDEXES = frozenset(
    {
        "forecast_observation_sets_forecast_idx",
        "forecast_observation_sets_supersedes_idx",
        "forecast_outcome_evaluations_forecast_idx",
        "forecast_outcome_evaluations_observation_idx",
        "forecast_outcome_evaluations_supersedes_idx",
    }
)
FORECAST_OUTCOME_TRIGGERS = frozenset(
    {
        "forecast_observation_sets_no_update",
        "forecast_observation_sets_no_delete",
        "forecast_outcome_evaluations_no_update",
        "forecast_outcome_evaluations_no_delete",
    }
)


__all__ = [
    "CLAIM_OUTCOME_SCHEMA_VERSION",
    "ClaimOutcome",
    "ClaimType",
    "EVALUATION_POLICY_SCHEMA_VERSION",
    "EvaluationPolicy",
    "EventOrdering",
    "EventTimePrecision",
    "FORECAST_OUTCOME_INDEXES",
    "FORECAST_OUTCOME_MIGRATION_SQL",
    "FORECAST_OUTCOME_TABLES",
    "FORECAST_OUTCOME_TRIGGERS",
    "ForecastObservationSet",
    "ForecastOutcomeEvaluation",
    "HYPOTHESIS_OUTCOME_SCHEMA_VERSION",
    "HypothesisOutcome",
    "OBSERVATION_CANDLE_SCHEMA_VERSION",
    "OBSERVATION_COMPLETENESS_SCHEMA_VERSION",
    "OBSERVATION_SET_SCHEMA_VERSION",
    "OUTCOME_CALCULATION_VERSION",
    "OUTCOME_EVALUATION_SCHEMA_VERSION",
    "OUTCOME_POLICY_VERSION",
    "ObservationCandle",
    "ObservationCompleteness",
    "ObservationCompletenessState",
    "ObservationScheduleMode",
    "OutcomeStatus",
    "SessionGap",
    "build_forecast_observation_set",
    "candle_manifest_hash",
    "evaluate_forecast_observation_set",
    "evaluate_forecast_outcome",
    "evaluation_policy_content_hash",
    "forecast_observation_set_content_hash",
    "forecast_outcome_evaluation_content_hash",
    "observation_candle_content_hash",
    "replay_forecast_observation_set",
    "replay_forecast_outcome",
    "replay_forecast_outcome_evaluation",
    "validate_forecast_observation_set",
    "validate_forecast_outcome_evaluation",
]
