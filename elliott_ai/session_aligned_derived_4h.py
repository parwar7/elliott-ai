"""Shadow-only deterministic reconstruction of session-aligned 4h evidence.

This module is intentionally isolated from providers, SQLite, the CLI, and the
active Elliott pipeline.  It accepts immutable, completed native intraday
candles plus an immutable NASDAQ session schedule and returns either complete
``derived_4h`` evidence or an explicit ``not_covered``/``inconsistent`` result.

``derived_4h`` is never a synonym for provider-native 4h data.  It is a
separate evidence kind whose lineage includes every underlying intraday row,
the exact session schedule, and the aggregation policy used to construct it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .forecast_records import DatasetCutoff, DatasetHashScope, canonical_sha256
from .lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    InvalidationDirection,
    InvalidationEvaluationBasis,
    NativeOHLCVWindow,
    PivotPriceField,
    PivotType,
    TypedPivot,
    candidate_child_graph_content_hash,
)


DERIVED_4H_SCHEMA_VERSION = "session-aligned-derived-4h-1.0.0"
DERIVED_4H_POLICY_SCHEMA_VERSION = "session-aligned-derived-4h-policy-1.0.0"
DERIVED_4H_SCHEDULE_SCHEMA_VERSION = "nasdaq-session-schedule-1.0.0"
DERIVED_4H_RESULT_SCHEMA_VERSION = "session-aligned-derived-4h-result-1.0.0"
DERIVED_4H_BINDING_SCHEMA_VERSION = "session-aligned-derived-4h-binding-1.0.0"

NASDAQ_EXCHANGE = "NASDAQ"
REGULAR_SESSION = "regular"
NASDAQ_TIMEZONE = "America/New_York"
MAX_4H_SECONDS = 4 * 60 * 60
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_EPSILON = 1e-9


class Derived4hEvidenceKind(StrEnum):
    """Evidence kinds intentionally distinct from provider-native 4h rows."""

    DERIVED_4H = "derived_4h"


class SourceTimestampSemantics(StrEnum):
    """The meaning of a provider's timestamp on an intraday source row."""

    BAR_START = "bar_start"
    BAR_END = "bar_end"


class Derived4hDurationClass(StrEnum):
    FULL_4H = "full_4h"
    SESSION_TERMINAL_SHORTENED = "session_terminal_shortened"


class Derived4hReconstructionStatus(StrEnum):
    AVAILABLE = "available"
    NOT_COVERED = "not_covered"
    INCONSISTENT = "inconsistent"


class Derived4hReasonCode(StrEnum):
    SOURCE_WINDOW_NOT_NATIVE = "source_window_not_native"
    SOURCE_TIMEFRAME_UNSUPPORTED = "source_timeframe_unsupported"
    SOURCE_METADATA_INCOMPATIBLE = "source_metadata_incompatible"
    SOURCE_AFTER_CUTOFF = "source_after_cutoff"
    SOURCE_INCOMPLETE = "source_incomplete"
    SOURCE_HASH_INVALID = "source_hash_invalid"
    SOURCE_TIMESTAMP_SEMANTICS_MISSING = "source_timestamp_semantics_missing"
    SOURCE_INTERVAL_UNKNOWN = "source_interval_unknown"
    SOURCE_INTERVAL_DUPLICATE = "source_interval_duplicate"
    SOURCE_INTERVAL_MISMATCH = "source_interval_mismatch"
    SOURCE_INTERVAL_OVERLAP = "source_interval_overlap"
    SOURCE_INTERVAL_CROSSES_SESSION = "source_interval_crosses_session"
    SOURCE_INTERVAL_MISSING = "source_interval_missing"
    SESSION_SCHEDULE_EMPTY = "session_schedule_empty"
    SESSION_SCHEDULE_INCOMPATIBLE = "session_schedule_incompatible"
    BUCKET_COVERAGE_MISSING = "bucket_coverage_missing"
    POLICY_HASH_MISMATCH = "policy_hash_mismatch"
    TERMINAL_SHORTENED_ACCEPTED = "terminal_shortened_accepted"


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Canonical JSON cannot contain non-finite numbers.")
        return value
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}.")


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
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Immutable JSON cannot contain non-finite numbers.")
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _freeze_json(to_dict())
    raise TypeError(f"Unsupported immutable value: {type(value).__name__}.")


def _normalize_utc(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit UTC offset.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


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
        raise ValueError(f"{field_name} must be finite and numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite and numeric.")
    return result


def _enum(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _string_tuple(value: Sequence[str], *, field_name: str, sort_unique: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(_require_text(item, field_name=field_name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} cannot contain duplicates.")
    return tuple(sorted(result)) if sort_unique else result


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, model_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{model_name} contains unknown fields: {', '.join(unknown)}.")


def _coerce(value: Any, model_type: type[Any], *, field_name: str) -> Any:
    if isinstance(value, model_type):
        return value
    if isinstance(value, Mapping):
        parser = getattr(model_type, "from_dict", None)
        if callable(parser):
            return parser(value)
    raise TypeError(f"{field_name} must contain {model_type.__name__} values.")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


def _hash_payload(value: Any) -> str:
    payload = value.to_dict() if hasattr(value, "to_dict") else _json_value(value)
    if isinstance(payload, Mapping):
        payload = dict(payload)
        payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class NasdaqTradingSession(_JsonContract):
    """One immutable regular-session schedule entry.

    The schedule is supplied by the caller.  This contract validates its local
    NASDAQ boundaries but never invents holiday or early-close dates.
    """

    session_id: str
    trading_date: str
    session_open_utc: str
    session_close_utc: str
    is_early_close: bool
    schema_version: str = DERIVED_4H_SCHEDULE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _require_text(self.session_id, field_name="session_id"))
        try:
            parsed_date = date.fromisoformat(_require_text(self.trading_date, field_name="trading_date"))
        except ValueError as exc:
            raise ValueError("trading_date must use YYYY-MM-DD.") from exc
        object.__setattr__(self, "trading_date", parsed_date.isoformat())
        object.__setattr__(self, "session_open_utc", _normalize_utc(self.session_open_utc, field_name="session_open_utc"))
        object.__setattr__(self, "session_close_utc", _normalize_utc(self.session_close_utc, field_name="session_close_utc"))
        if _utc(self.session_close_utc) <= _utc(self.session_open_utc):
            raise ValueError("session_close_utc must be after session_open_utc.")
        if not isinstance(self.is_early_close, bool):
            raise TypeError("is_early_close must be boolean.")
        local_zone = ZoneInfo(NASDAQ_TIMEZONE)
        local_open = _utc(self.session_open_utc).astimezone(local_zone)
        local_close = _utc(self.session_close_utc).astimezone(local_zone)
        if local_open.date() != parsed_date or local_close.date() != parsed_date:
            raise ValueError("NASDAQ session boundaries must resolve to the declared local trading date.")
        if local_open.timetz().replace(tzinfo=None) != time(9, 30):
            raise ValueError("NASDAQ regular sessions must open at 09:30 America/New_York.")
        if local_close.timetz().replace(tzinfo=None) > time(16, 0):
            raise ValueError("NASDAQ regular-session close cannot be after 16:00 America/New_York.")
        actual_early_close = local_close.timetz().replace(tzinfo=None) != time(16, 0)
        if actual_early_close != self.is_early_close:
            raise ValueError("is_early_close must match the declared local session close.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != nasdaq_trading_session_content_hash(self):
            raise ValueError("NasdaqTradingSession content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "NasdaqTradingSession":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=nasdaq_trading_session_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "NasdaqTradingSession":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != nasdaq_trading_session_content_hash(result):
            raise ValueError("NasdaqTradingSession content_hash does not match its payload.")
        return result


def nasdaq_trading_session_content_hash(value: NasdaqTradingSession | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class NasdaqSessionSchedule(_JsonContract):
    """Versioned immutable NASDAQ regular-session schedule.

    Holidays are represented by their absence from ``sessions``.  The caller
    must supply the calendar source and version; the reconstruction engine never
    infers an exchange calendar from an apparent timestamp gap.
    """

    schedule_id: str
    calendar_source: str
    calendar_version: str
    exchange: str
    session: str
    timezone: str
    sessions: tuple[NasdaqTradingSession, ...]
    schema_version: str = DERIVED_4H_SCHEDULE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("schedule_id", "calendar_source", "calendar_version", "exchange", "session", "timezone", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        if self.exchange != NASDAQ_EXCHANGE:
            raise ValueError("Derived 4h schedules must identify NASDAQ exactly.")
        if self.session != REGULAR_SESSION:
            raise ValueError("Derived 4h schedules must use the regular session exactly.")
        if self.timezone != NASDAQ_TIMEZONE:
            raise ValueError("Derived 4h schedules must use America/New_York exactly.")
        if isinstance(self.sessions, (str, bytes)) or not isinstance(self.sessions, Sequence):
            raise TypeError("sessions must be a sequence.")
        sessions = tuple(_coerce(item, NasdaqTradingSession, field_name="sessions") for item in self.sessions)
        if not sessions:
            raise ValueError("NasdaqSessionSchedule requires at least one supplied session.")
        if len({item.session_id for item in sessions}) != len(sessions):
            raise ValueError("NasdaqSessionSchedule cannot contain duplicate session IDs.")
        if len({item.trading_date for item in sessions}) != len(sessions):
            raise ValueError("NasdaqSessionSchedule cannot contain duplicate trading dates.")
        ordered = tuple(sorted(sessions, key=lambda item: item.session_open_utc))
        if ordered != sessions:
            raise ValueError("NasdaqSessionSchedule sessions must be strictly ordered by session open.")
        for previous, current in zip(sessions, sessions[1:]):
            if _utc(previous.session_close_utc) >= _utc(current.session_open_utc):
                raise ValueError("NasdaqSessionSchedule sessions cannot overlap.")
        object.__setattr__(self, "sessions", sessions)
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != nasdaq_session_schedule_content_hash(self):
            raise ValueError("NasdaqSessionSchedule content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "NasdaqSessionSchedule":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=nasdaq_session_schedule_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "NasdaqSessionSchedule":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != nasdaq_session_schedule_content_hash(result):
            raise ValueError("NasdaqSessionSchedule content_hash does not match its payload.")
        return result


def nasdaq_session_schedule_content_hash(value: NasdaqSessionSchedule | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class IntradaySourceInterval(_JsonContract):
    """Exact immutable interval mapping for one original source candle."""

    candle_id: str
    source_row_hash: str
    interval_start_utc: str
    interval_end_utc: str
    timestamp_semantics: SourceTimestampSemantics
    schema_version: str = DERIVED_4H_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candle_id", _require_text(self.candle_id, field_name="candle_id"))
        object.__setattr__(self, "source_row_hash", _require_hash(self.source_row_hash, field_name="source_row_hash"))
        object.__setattr__(self, "interval_start_utc", _normalize_utc(self.interval_start_utc, field_name="interval_start_utc"))
        object.__setattr__(self, "interval_end_utc", _normalize_utc(self.interval_end_utc, field_name="interval_end_utc"))
        if _utc(self.interval_end_utc) <= _utc(self.interval_start_utc):
            raise ValueError("Intraday source interval must end after it starts.")
        object.__setattr__(self, "timestamp_semantics", _enum(self.timestamp_semantics, SourceTimestampSemantics, field_name="timestamp_semantics"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != intraday_source_interval_content_hash(self):
            raise ValueError("IntradaySourceInterval content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "IntradaySourceInterval":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=intraday_source_interval_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "IntradaySourceInterval":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != intraday_source_interval_content_hash(result):
            raise ValueError("IntradaySourceInterval content_hash does not match its payload.")
        return result


def intraday_source_interval_content_hash(value: IntradaySourceInterval | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class SessionAligned4hAggregationPolicy(_JsonContract):
    """The immutable policy that defines session-aligned 4h buckets."""

    policy_id: str
    policy_version: str
    exchange: str
    session: str
    timezone: str
    maximum_bucket_seconds: int
    allow_terminal_shortened_buckets: bool
    allowed_source_timeframes: tuple[str, ...]
    schema_version: str = DERIVED_4H_POLICY_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("policy_id", "policy_version", "exchange", "session", "timezone", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        if self.exchange != NASDAQ_EXCHANGE or self.session != REGULAR_SESSION or self.timezone != NASDAQ_TIMEZONE:
            raise ValueError("Session-aligned derived 4h policy must use NASDAQ regular America/New_York data.")
        if not isinstance(self.maximum_bucket_seconds, int) or isinstance(self.maximum_bucket_seconds, bool):
            raise TypeError("maximum_bucket_seconds must be an integer.")
        if self.maximum_bucket_seconds != MAX_4H_SECONDS:
            raise ValueError("maximum_bucket_seconds must equal four hours exactly.")
        if not isinstance(self.allow_terminal_shortened_buckets, bool):
            raise TypeError("allow_terminal_shortened_buckets must be boolean.")
        if isinstance(self.allowed_source_timeframes, (str, bytes)) or not isinstance(self.allowed_source_timeframes, Sequence):
            raise TypeError("allowed_source_timeframes must be a sequence.")
        allowed = _string_tuple(self.allowed_source_timeframes, field_name="allowed_source_timeframes", sort_unique=True)
        if not allowed or "4h" in allowed:
            raise ValueError("Derived 4h policy requires non-4h native intraday source timeframes.")
        object.__setattr__(self, "allowed_source_timeframes", allowed)
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != session_aligned_4h_policy_content_hash(self):
            raise ValueError("SessionAligned4hAggregationPolicy content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "SessionAligned4hAggregationPolicy":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=session_aligned_4h_policy_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "SessionAligned4hAggregationPolicy":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != session_aligned_4h_policy_content_hash(result):
            raise ValueError("SessionAligned4hAggregationPolicy content_hash does not match its payload.")
        return result


def session_aligned_4h_policy_content_hash(value: SessionAligned4hAggregationPolicy | Mapping[str, Any]) -> str:
    return _hash_payload(value)


def nasdaq_session_aligned_4h_policy() -> SessionAligned4hAggregationPolicy:
    """Return the first approved policy, including terminal shortened buckets."""

    return SessionAligned4hAggregationPolicy.create(
        policy_id="nasdaq-session-aligned-max-4h-v1",
        policy_version="1.0.0",
        exchange=NASDAQ_EXCHANGE,
        session=REGULAR_SESSION,
        timezone=NASDAQ_TIMEZONE,
        maximum_bucket_seconds=MAX_4H_SECONDS,
        allow_terminal_shortened_buckets=True,
        allowed_source_timeframes=("1m", "5m", "15m", "30m", "1h"),
    )


@dataclass(frozen=True, slots=True)
class Derived4hBar(_JsonContract):
    """One complete session-aligned bucket built from native intraday rows."""

    bar_id: str
    session_id: str
    timestamp_utc: str
    bucket_start_utc: str
    bucket_end_utc: str
    duration_seconds: int
    duration_class: Derived4hDurationClass
    is_terminal_bucket: bool
    open: float
    high: float
    low: float
    close: float
    volume: float
    source_candle_ids: tuple[str, ...]
    source_row_hashes: tuple[str, ...]
    source_window_hash: str
    evidence_kind: Derived4hEvidenceKind = Derived4hEvidenceKind.DERIVED_4H
    schema_version: str = DERIVED_4H_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bar_id", _require_text(self.bar_id, field_name="bar_id"))
        object.__setattr__(self, "session_id", _require_text(self.session_id, field_name="session_id"))
        object.__setattr__(self, "timestamp_utc", _normalize_utc(self.timestamp_utc, field_name="timestamp_utc"))
        object.__setattr__(self, "bucket_start_utc", _normalize_utc(self.bucket_start_utc, field_name="bucket_start_utc"))
        object.__setattr__(self, "bucket_end_utc", _normalize_utc(self.bucket_end_utc, field_name="bucket_end_utc"))
        if self.timestamp_utc != self.bucket_start_utc:
            raise ValueError("Derived4hBar timestamp_utc must equal bucket_start_utc.")
        duration = int(self.duration_seconds)
        if isinstance(self.duration_seconds, bool) or duration <= 0 or duration > MAX_4H_SECONDS:
            raise ValueError("Derived4hBar duration_seconds must be greater than zero and no more than four hours.")
        if int((_utc(self.bucket_end_utc) - _utc(self.bucket_start_utc)).total_seconds()) != duration:
            raise ValueError("Derived4hBar duration_seconds must match its UTC boundaries.")
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "duration_class", _enum(self.duration_class, Derived4hDurationClass, field_name="duration_class"))
        if not isinstance(self.is_terminal_bucket, bool):
            raise TypeError("is_terminal_bucket must be boolean.")
        if self.duration_class is Derived4hDurationClass.FULL_4H and duration != MAX_4H_SECONDS:
            raise ValueError("full_4h evidence must have an exact four-hour duration.")
        if self.duration_class is Derived4hDurationClass.SESSION_TERMINAL_SHORTENED:
            if not self.is_terminal_bucket or duration >= MAX_4H_SECONDS:
                raise ValueError("A terminal shortened bucket must be terminal and shorter than four hours.")
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, _finite(getattr(self, name), field_name=name))
        if self.volume < 0:
            raise ValueError("Derived4hBar volume must be non-negative.")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("Derived4hBar OHLC values are inconsistent.")
        source_ids = _string_tuple(self.source_candle_ids, field_name="source_candle_ids")
        source_hashes = tuple(_require_hash(item, field_name="source_row_hashes") for item in self.source_row_hashes)
        if not source_ids or len(source_ids) != len(source_hashes):
            raise ValueError("Derived4hBar source candle IDs and hashes must be non-empty and aligned.")
        if len(set(source_hashes)) != len(source_hashes):
            raise ValueError("Derived4hBar cannot reference the same source row twice.")
        object.__setattr__(self, "source_candle_ids", source_ids)
        object.__setattr__(self, "source_row_hashes", source_hashes)
        object.__setattr__(self, "source_window_hash", _require_hash(self.source_window_hash, field_name="source_window_hash"))
        object.__setattr__(self, "evidence_kind", _enum(self.evidence_kind, Derived4hEvidenceKind, field_name="evidence_kind"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_bar_content_hash(self):
            raise ValueError("Derived4hBar content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hBar":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("bar_id"):
            values["bar_id"] = f"derived_4h_{canonical_sha256({'start': values.get('bucket_start_utc'), 'end': values.get('bucket_end_utc'), 'sources': values.get('source_row_hashes')})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_bar_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hBar":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_bar_content_hash(result):
            raise ValueError("Derived4hBar content_hash does not match its payload.")
        return result


def derived_4h_bar_content_hash(value: Derived4hBar | Mapping[str, Any]) -> str:
    return _hash_payload(value)


def derived_4h_rows_hash(bars: Sequence[Derived4hBar]) -> str:
    return canonical_sha256(
        [
            {
                "bar_id": item.bar_id,
                "content_hash": item.content_hash,
            }
            for item in bars
        ]
    )


def _derived_dataset_cutoff(
    source_window: NativeOHLCVWindow,
    policy: SessionAligned4hAggregationPolicy,
    rows_hash: str,
    *,
    bar_count: int,
) -> DatasetCutoff:
    source = source_window.dataset_cutoff
    return DatasetCutoff(
        dataset_id=f"derived_4h_{canonical_sha256({'source': source.dataset_id, 'policy': policy.content_hash, 'rows': rows_hash})[:32]}",
        source_reference=f"{source.source_reference}#derived_4h/{policy.policy_id}/{policy.policy_version}",
        timeframe="4h",
        cutoff_utc=source.cutoff_utc,
        dataset_hash=rows_hash,
        hash_scope=DatasetHashScope.STORED_DATASET_SUMMARY,
        provider=source.provider,
        feed_identity=source.feed_identity,
        requested_symbol=source.requested_symbol,
        resolved_symbol=source.resolved_symbol,
        exchange=source.exchange,
        session=source.session,
        timezone=source.timezone,
        adjustment=source.adjustment,
        price_basis=source.price_basis,
        completed_candles_only=True,
        source_document_hash=source.source_document_hash,
        captured_at_utc=source.captured_at_utc,
        bar_count=bar_count,
    )


@dataclass(frozen=True, slots=True)
class Derived4hWindow(_JsonContract):
    """A complete immutable `derived_4h` window and all its source lineage."""

    window_id: str
    source_window: NativeOHLCVWindow
    source_intervals: tuple[IntradaySourceInterval, ...]
    schedule: NasdaqSessionSchedule
    policy: SessionAligned4hAggregationPolicy
    dataset_cutoff: DatasetCutoff
    bars: tuple[Derived4hBar, ...]
    evidence_kind: Derived4hEvidenceKind = Derived4hEvidenceKind.DERIVED_4H
    source_rows_hash: str = ""
    derived_rows_hash: str = ""
    schema_version: str = DERIVED_4H_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_id", _require_text(self.window_id, field_name="window_id"))
        source = _coerce(self.source_window, NativeOHLCVWindow, field_name="source_window")
        policy = _coerce(self.policy, SessionAligned4hAggregationPolicy, field_name="policy")
        if not source.is_native:
            raise ValueError("Derived4hWindow source_window must contain provider-native rows.")
        if source.timeframe not in policy.allowed_source_timeframes:
            raise ValueError("Derived4hWindow source timeframe is not allowed by its aggregation policy.")
        object.__setattr__(self, "source_window", source)
        if isinstance(self.source_intervals, (str, bytes)) or not isinstance(self.source_intervals, Sequence):
            raise TypeError("source_intervals must be a sequence.")
        intervals = tuple(_coerce(item, IntradaySourceInterval, field_name="source_intervals") for item in self.source_intervals)
        object.__setattr__(self, "source_intervals", intervals)
        schedule = _coerce(self.schedule, NasdaqSessionSchedule, field_name="schedule")
        object.__setattr__(self, "schedule", schedule)
        object.__setattr__(self, "policy", policy)
        dataset = self.dataset_cutoff if isinstance(self.dataset_cutoff, DatasetCutoff) else DatasetCutoff.from_dict(self.dataset_cutoff)
        if dataset.timeframe != "4h" or not dataset.completed_candles_only:
            raise ValueError("Derived4hWindow requires a completed-candle 4h DatasetCutoff.")
        for name in ("provider", "feed_identity", "requested_symbol", "resolved_symbol", "exchange", "session", "timezone", "adjustment", "price_basis"):
            if _json_value(getattr(dataset, name)) != _json_value(getattr(source.dataset_cutoff, name)):
                raise ValueError("Derived4hWindow DatasetCutoff must preserve source market-data identity.")
        if dataset.cutoff_utc != source.dataset_cutoff.cutoff_utc:
            raise ValueError("Derived4hWindow DatasetCutoff must preserve the source cutoff.")
        if dataset.exchange != policy.exchange or dataset.session != policy.session or dataset.timezone != policy.timezone:
            raise ValueError("Derived4hWindow DatasetCutoff does not match its aggregation policy.")
        object.__setattr__(self, "dataset_cutoff", dataset)
        if isinstance(self.bars, (str, bytes)) or not isinstance(self.bars, Sequence):
            raise TypeError("bars must be a sequence.")
        bars = tuple(_coerce(item, Derived4hBar, field_name="bars") for item in self.bars)
        if not bars:
            raise ValueError("Derived4hWindow requires at least one complete derived bar.")
        starts = tuple(_utc(item.bucket_start_utc) for item in bars)
        if starts != tuple(sorted(starts)) or len(set(starts)) != len(starts):
            raise ValueError("Derived4hWindow bars must be strictly ordered by bucket start.")
        for previous, current in zip(bars, bars[1:]):
            if _utc(previous.bucket_end_utc) > _utc(current.bucket_start_utc):
                raise ValueError("Derived4hWindow bars cannot overlap.")
        session_by_id = {item.session_id: item for item in schedule.sessions}
        source_by_id = {item.candle_id: item for item in source.candles}
        source_by_hash = {item.source_row_hash: item for item in source.candles}
        used_source_hashes: list[str] = []
        for bar in bars:
            session = session_by_id.get(bar.session_id)
            if session is None:
                raise ValueError("Derived4hBar references an unknown schedule session.")
            if _utc(bar.bucket_start_utc) < _utc(session.session_open_utc) or _utc(bar.bucket_end_utc) > _utc(session.session_close_utc):
                raise ValueError("Derived4hBar cannot cross its official NASDAQ session boundary.")
            if bar.source_window_hash != source.native_rows_hash:
                raise ValueError("Derived4hBar source_window_hash must match the immutable source rows.")
            for candle_id, row_hash in zip(bar.source_candle_ids, bar.source_row_hashes):
                candle = source_by_id.get(candle_id)
                if candle is None or candle.source_row_hash != row_hash or source_by_hash.get(row_hash) != candle:
                    raise ValueError("Derived4hBar source lineage does not match the source window.")
                used_source_hashes.append(row_hash)
        if len(used_source_hashes) != len(set(used_source_hashes)):
            raise ValueError("Derived4hWindow cannot reuse a source candle across derived bars.")
        object.__setattr__(self, "bars", bars)
        object.__setattr__(self, "evidence_kind", _enum(self.evidence_kind, Derived4hEvidenceKind, field_name="evidence_kind"))
        source_rows_hash = source.native_rows_hash
        if self.source_rows_hash and _require_hash(self.source_rows_hash, field_name="source_rows_hash") != source_rows_hash:
            raise ValueError("Derived4hWindow source_rows_hash does not match source rows.")
        object.__setattr__(self, "source_rows_hash", source_rows_hash)
        rows_hash = derived_4h_rows_hash(bars)
        if self.derived_rows_hash and _require_hash(self.derived_rows_hash, field_name="derived_rows_hash") != rows_hash:
            raise ValueError("Derived4hWindow derived_rows_hash does not match derived bars.")
        if dataset.dataset_hash != rows_hash:
            raise ValueError("Derived4hWindow DatasetCutoff hash must equal derived_rows_hash.")
        object.__setattr__(self, "derived_rows_hash", rows_hash)
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_window_content_hash(self):
            raise ValueError("Derived4hWindow content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hWindow":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        values.setdefault("source_rows_hash", "")
        values.setdefault("derived_rows_hash", "")
        result = cls(**values)
        return replace(result, content_hash=derived_4h_window_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hWindow":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_window_content_hash(result):
            raise ValueError("Derived4hWindow content_hash does not match its payload.")
        return result


def derived_4h_window_content_hash(value: Derived4hWindow | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hCoverageAttestation(_JsonContract):
    """Coverage evidence for a specific candidate parent interval."""

    attestation_id: str
    derived_window_hash: str
    derived_rows_hash: str
    policy_hash: str
    schedule_hash: str
    interval_start_utc: str
    interval_end_utc: str
    coverage_complete: bool
    missing_interval_ids: tuple[str, ...]
    covered_bucket_ids: tuple[str, ...]
    coverage_method: str
    schema_version: str = DERIVED_4H_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("attestation_id", "coverage_method", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        for name in ("derived_window_hash", "derived_rows_hash", "policy_hash", "schedule_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "interval_start_utc", _normalize_utc(self.interval_start_utc, field_name="interval_start_utc"))
        object.__setattr__(self, "interval_end_utc", _normalize_utc(self.interval_end_utc, field_name="interval_end_utc"))
        if _utc(self.interval_end_utc) < _utc(self.interval_start_utc):
            raise ValueError("Coverage interval end cannot be before its start.")
        if not isinstance(self.coverage_complete, bool):
            raise TypeError("coverage_complete must be boolean.")
        missing = _string_tuple(self.missing_interval_ids, field_name="missing_interval_ids", sort_unique=True)
        buckets = _string_tuple(self.covered_bucket_ids, field_name="covered_bucket_ids", sort_unique=True)
        if self.coverage_complete and missing:
            raise ValueError("Complete derived 4h coverage cannot list missing intervals.")
        object.__setattr__(self, "missing_interval_ids", missing)
        object.__setattr__(self, "covered_bucket_ids", buckets)
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_coverage_attestation_content_hash(self):
            raise ValueError("Derived4hCoverageAttestation content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hCoverageAttestation":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("attestation_id"):
            values["attestation_id"] = f"derived_4h_coverage_{canonical_sha256({'window': values.get('derived_window_hash'), 'start': values.get('interval_start_utc'), 'end': values.get('interval_end_utc')})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_coverage_attestation_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hCoverageAttestation":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_coverage_attestation_content_hash(result):
            raise ValueError("Derived4hCoverageAttestation content_hash does not match its payload.")
        return result


def derived_4h_coverage_attestation_content_hash(value: Derived4hCoverageAttestation | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hReconstructionRequest(_JsonContract):
    """All immutable inputs to a deterministic reconstruction attempt."""

    request_id: str
    source_window: NativeOHLCVWindow
    source_intervals: tuple[IntradaySourceInterval, ...]
    schedule: NasdaqSessionSchedule
    policy: SessionAligned4hAggregationPolicy
    analysis_cutoff_utc: str
    schema_version: str = DERIVED_4H_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        object.__setattr__(self, "source_window", _coerce(self.source_window, NativeOHLCVWindow, field_name="source_window"))
        if isinstance(self.source_intervals, (str, bytes)) or not isinstance(self.source_intervals, Sequence):
            raise TypeError("source_intervals must be a sequence.")
        object.__setattr__(self, "source_intervals", tuple(_coerce(item, IntradaySourceInterval, field_name="source_intervals") for item in self.source_intervals))
        object.__setattr__(self, "schedule", _coerce(self.schedule, NasdaqSessionSchedule, field_name="schedule"))
        object.__setattr__(self, "policy", _coerce(self.policy, SessionAligned4hAggregationPolicy, field_name="policy"))
        object.__setattr__(self, "analysis_cutoff_utc", _normalize_utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_reconstruction_request_content_hash(self):
            raise ValueError("Derived4hReconstructionRequest content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hReconstructionRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("request_id"):
            values["request_id"] = f"derived_4h_request_{canonical_sha256({'source': _json_value(values.get('source_window')), 'schedule': _json_value(values.get('schedule')), 'policy': _json_value(values.get('policy')), 'cutoff': values.get('analysis_cutoff_utc')})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_reconstruction_request_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hReconstructionRequest":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_reconstruction_request_content_hash(result):
            raise ValueError("Derived4hReconstructionRequest content_hash does not match its payload.")
        return result


def derived_4h_reconstruction_request_content_hash(value: Derived4hReconstructionRequest | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hReconstructionResult(_JsonContract):
    """The only reconstruction outcomes: available, not covered, inconsistent."""

    result_id: str
    status: Derived4hReconstructionStatus
    request_hash: str
    source_window_hash: str
    policy_hash: str
    schedule_hash: str
    window: Derived4hWindow | None
    missing_interval_ids: tuple[str, ...]
    reason_codes: tuple[Derived4hReasonCode, ...]
    warnings: tuple[str, ...]
    schema_version: str = DERIVED_4H_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _require_text(self.result_id, field_name="result_id"))
        object.__setattr__(self, "status", _enum(self.status, Derived4hReconstructionStatus, field_name="status"))
        for name in ("request_hash", "source_window_hash", "policy_hash", "schedule_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        if self.window is not None:
            object.__setattr__(self, "window", _coerce(self.window, Derived4hWindow, field_name="window"))
        missing = _string_tuple(self.missing_interval_ids, field_name="missing_interval_ids", sort_unique=True)
        if isinstance(self.reason_codes, (str, bytes)) or not isinstance(self.reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        reasons = tuple(_enum(item, Derived4hReasonCode, field_name="reason_codes") for item in self.reason_codes)
        if len(reasons) != len(set(reasons)):
            raise ValueError("reason_codes cannot contain duplicates.")
        warnings = _string_tuple(self.warnings, field_name="warnings", sort_unique=True)
        if self.status is Derived4hReconstructionStatus.AVAILABLE:
            if self.window is None or missing:
                raise ValueError("Available derived 4h evidence requires a complete window and no missing intervals.")
        elif self.window is not None:
            raise ValueError("Only an available derived 4h result may include a reconstructed window.")
        object.__setattr__(self, "missing_interval_ids", missing)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_reconstruction_result_content_hash(self):
            raise ValueError("Derived4hReconstructionResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hReconstructionResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("result_id"):
            values["result_id"] = f"derived_4h_result_{canonical_sha256({'request': values.get('request_hash'), 'status': _json_value(values.get('status'))})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_reconstruction_result_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hReconstructionResult":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_reconstruction_result_content_hash(result):
            raise ValueError("Derived4hReconstructionResult content_hash does not match its payload.")
        return result


def derived_4h_reconstruction_result_content_hash(value: Derived4hReconstructionResult | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hCandidateGraphBinding(_JsonContract):
    """Immutable binding of a candidate graph to an exact aggregation policy."""

    binding_id: str
    graph_id: str
    graph_content_hash: str
    policy_id: str
    policy_version: str
    policy_hash: str
    evidence_kind: Derived4hEvidenceKind = Derived4hEvidenceKind.DERIVED_4H
    schema_version: str = DERIVED_4H_BINDING_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("binding_id", "graph_id", "policy_id", "policy_version", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "graph_content_hash", _require_hash(self.graph_content_hash, field_name="graph_content_hash"))
        object.__setattr__(self, "policy_hash", _require_hash(self.policy_hash, field_name="policy_hash"))
        object.__setattr__(self, "evidence_kind", _enum(self.evidence_kind, Derived4hEvidenceKind, field_name="evidence_kind"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_candidate_graph_binding_content_hash(self):
            raise ValueError("Derived4hCandidateGraphBinding content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hCandidateGraphBinding":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("binding_id"):
            values["binding_id"] = f"derived_4h_graph_binding_{canonical_sha256({'graph': values.get('graph_content_hash'), 'policy': values.get('policy_hash')})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_candidate_graph_binding_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hCandidateGraphBinding":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_candidate_graph_binding_content_hash(result):
            raise ValueError("Derived4hCandidateGraphBinding content_hash does not match its payload.")
        return result


def derived_4h_candidate_graph_binding_content_hash(value: Derived4hCandidateGraphBinding | Mapping[str, Any]) -> str:
    return _hash_payload(value)


def bind_candidate_graph_to_derived_4h(
    graph: CandidateChildGraph,
    policy: SessionAligned4hAggregationPolicy,
) -> Derived4hCandidateGraphBinding:
    """Bind, but never mutate, a candidate graph to the supplied policy."""

    if not isinstance(graph, CandidateChildGraph):
        raise TypeError("graph must be a CandidateChildGraph.")
    if not isinstance(policy, SessionAligned4hAggregationPolicy):
        raise TypeError("policy must be a SessionAligned4hAggregationPolicy.")
    if graph.target_timeframe != "4h":
        raise ValueError("Only a candidate graph targeting 4h can bind derived_4h evidence.")
    if graph.content_hash != candidate_child_graph_content_hash(graph):
        raise ValueError("Candidate graph content hash is invalid.")
    if policy.content_hash != session_aligned_4h_policy_content_hash(policy):
        raise ValueError("Aggregation policy content hash is invalid.")
    return Derived4hCandidateGraphBinding.create(
        graph_id=graph.graph_id,
        graph_content_hash=graph.content_hash,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_hash=policy.content_hash,
    )


def _session_for_interval(
    schedule: NasdaqSessionSchedule,
    start: datetime,
    end: datetime,
) -> NasdaqTradingSession | None:
    for session in schedule.sessions:
        if _utc(session.session_open_utc) <= start and end <= _utc(session.session_close_utc):
            return session
    return None


def _session_buckets(
    session: NasdaqTradingSession,
    policy: SessionAligned4hAggregationPolicy,
) -> tuple[tuple[datetime, datetime, Derived4hDurationClass, bool], ...]:
    start = _utc(session.session_open_utc)
    close = _utc(session.session_close_utc)
    result: list[tuple[datetime, datetime, Derived4hDurationClass, bool]] = []
    current = start
    while current < close:
        end = min(current + timedelta(seconds=policy.maximum_bucket_seconds), close)
        duration = int((end - current).total_seconds())
        terminal = end == close
        if duration < policy.maximum_bucket_seconds:
            if not terminal or not policy.allow_terminal_shortened_buckets:
                raise ValueError("The policy does not permit this shortened session bucket.")
            duration_class = Derived4hDurationClass.SESSION_TERMINAL_SHORTENED
        else:
            duration_class = Derived4hDurationClass.FULL_4H
        result.append((current, end, duration_class, terminal))
        current = end
    return tuple(result)


def _source_metadata_reasons(
    request: Derived4hReconstructionRequest,
) -> tuple[Derived4hReasonCode, ...]:
    source = request.source_window
    policy = request.policy
    reasons: list[Derived4hReasonCode] = []
    if not source.is_native:
        reasons.append(Derived4hReasonCode.SOURCE_WINDOW_NOT_NATIVE)
    if source.timeframe not in policy.allowed_source_timeframes:
        reasons.append(Derived4hReasonCode.SOURCE_TIMEFRAME_UNSUPPORTED)
    dataset = source.dataset_cutoff
    if dataset.exchange != policy.exchange or dataset.session != policy.session or dataset.timezone != policy.timezone:
        reasons.append(Derived4hReasonCode.SOURCE_METADATA_INCOMPATIBLE)
    cutoff = _utc(request.analysis_cutoff_utc)
    if _utc(dataset.cutoff_utc) > cutoff or any(_utc(item.timestamp_utc) > cutoff for item in source.candles):
        reasons.append(Derived4hReasonCode.SOURCE_AFTER_CUTOFF)
    if not dataset.completed_candles_only or any(not item.completed for item in source.candles):
        reasons.append(Derived4hReasonCode.SOURCE_INCOMPLETE)
    expected_rows = canonical_sha256(
        [{"candle_id": item.candle_id, "source_row_hash": item.source_row_hash} for item in source.candles]
    )
    if source.native_rows_hash != expected_rows:
        reasons.append(Derived4hReasonCode.SOURCE_HASH_INVALID)
    if not request.schedule.sessions:
        reasons.append(Derived4hReasonCode.SESSION_SCHEDULE_EMPTY)
    if request.schedule.exchange != policy.exchange or request.schedule.session != policy.session or request.schedule.timezone != policy.timezone:
        reasons.append(Derived4hReasonCode.SESSION_SCHEDULE_INCOMPATIBLE)
    return tuple(dict.fromkeys(reasons))


def _result_for_request(
    request: Derived4hReconstructionRequest,
    *,
    status: Derived4hReconstructionStatus,
    window: Derived4hWindow | None = None,
    missing_interval_ids: Sequence[str] = (),
    reason_codes: Sequence[Derived4hReasonCode] = (),
    warnings: Sequence[str] = (),
) -> Derived4hReconstructionResult:
    return Derived4hReconstructionResult.create(
        result_id="",
        status=status,
        request_hash=request.content_hash,
        source_window_hash=request.source_window.content_hash,
        policy_hash=request.policy.content_hash,
        schedule_hash=request.schedule.content_hash,
        window=window,
        missing_interval_ids=tuple(missing_interval_ids),
        reason_codes=tuple(reason_codes),
        warnings=tuple(warnings),
    )


def reconstruct_session_aligned_4h(
    request: Derived4hReconstructionRequest,
) -> Derived4hReconstructionResult:
    """Construct complete session-aligned buckets without filling any gaps.

    The function is pure: it does not fetch, persist, mutate, or infer missing
    source evidence.  Missing interval metadata or rows becomes ``not_covered``;
    malformed or incompatible inputs become ``inconsistent``.
    """

    if not isinstance(request, Derived4hReconstructionRequest):
        raise TypeError("request must be a Derived4hReconstructionRequest.")
    reasons = list(_source_metadata_reasons(request))
    if reasons:
        return _result_for_request(
            request,
            status=Derived4hReconstructionStatus.INCONSISTENT,
            reason_codes=reasons,
        )

    source = request.source_window
    candles_by_id = {item.candle_id: item for item in source.candles}
    rows_by_hash = {item.source_row_hash: item for item in source.candles}
    intervals_by_id: dict[str, IntradaySourceInterval] = {}
    interval_ranges: list[tuple[datetime, datetime, IntradaySourceInterval]] = []
    missing_ids: list[str] = []

    for interval in request.source_intervals:
        candle = candles_by_id.get(interval.candle_id)
        if candle is None:
            reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_UNKNOWN)
            continue
        if interval.candle_id in intervals_by_id:
            reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_DUPLICATE)
            continue
        if candle.source_row_hash != interval.source_row_hash or rows_by_hash.get(interval.source_row_hash) != candle:
            reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_MISMATCH)
            continue
        expected_timestamp = interval.interval_start_utc if interval.timestamp_semantics is SourceTimestampSemantics.BAR_START else interval.interval_end_utc
        if candle.timestamp_utc != expected_timestamp:
            reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_MISMATCH)
            continue
        start = _utc(interval.interval_start_utc)
        end = _utc(interval.interval_end_utc)
        if _session_for_interval(request.schedule, start, end) is None:
            reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_CROSSES_SESSION)
            continue
        if end > _utc(request.analysis_cutoff_utc):
            reasons.append(Derived4hReasonCode.SOURCE_AFTER_CUTOFF)
            continue
        intervals_by_id[interval.candle_id] = interval
        interval_ranges.append((start, end, interval))

    for candle in source.candles:
        if candle.candle_id not in intervals_by_id:
            missing_ids.append(f"source_interval:{candle.candle_id}")

    for (_, previous_end, _), (current_start, _, _) in zip(
        sorted(interval_ranges, key=lambda item: (item[0], item[1], item[2].candle_id)),
        sorted(interval_ranges, key=lambda item: (item[0], item[1], item[2].candle_id))[1:],
    ):
        if current_start < previous_end:
            reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_OVERLAP)
            break

    if reasons:
        return _result_for_request(
            request,
            status=Derived4hReconstructionStatus.INCONSISTENT,
            missing_interval_ids=missing_ids,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )
    if missing_ids:
        return _result_for_request(
            request,
            status=Derived4hReconstructionStatus.NOT_COVERED,
            missing_interval_ids=missing_ids,
            reason_codes=(Derived4hReasonCode.SOURCE_TIMESTAMP_SEMANTICS_MISSING,),
        )

    bars: list[Derived4hBar] = []
    coverage_missing: list[str] = []
    terminal_shortened = False
    sorted_intervals = sorted(intervals_by_id.values(), key=lambda item: (item.interval_start_utc, item.interval_end_utc, item.candle_id))
    for session in request.schedule.sessions:
        for bucket_index, (bucket_start, bucket_end, duration_class, terminal) in enumerate(_session_buckets(session, request.policy)):
            members = [
                item
                for item in sorted_intervals
                if _utc(item.interval_start_utc) >= bucket_start and _utc(item.interval_end_utc) <= bucket_end
            ]
            cursor = bucket_start
            bucket_missing: list[str] = []
            for item in members:
                item_start = _utc(item.interval_start_utc)
                item_end = _utc(item.interval_end_utc)
                if item_start != cursor:
                    bucket_missing.append(
                        f"{session.session_id}:{bucket_index}:gap:{cursor.isoformat(timespec='seconds')}:{item_start.isoformat(timespec='seconds')}"
                    )
                    cursor = item_start
                if item_end <= item_start or item_end > bucket_end:
                    reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_MISMATCH)
                    break
                cursor = item_end
            if reasons:
                break
            if not members:
                bucket_missing.append(f"{session.session_id}:{bucket_index}:missing_bucket")
            elif cursor != bucket_end:
                bucket_missing.append(
                    f"{session.session_id}:{bucket_index}:gap:{cursor.isoformat(timespec='seconds')}:{bucket_end.isoformat(timespec='seconds')}"
                )
            if bucket_missing:
                coverage_missing.extend(bucket_missing)
                continue
            candles = [candles_by_id[item.candle_id] for item in members]
            duration = int((bucket_end - bucket_start).total_seconds())
            bars.append(
                Derived4hBar.create(
                    bar_id=f"derived_4h:{session.session_id}:{bucket_index}",
                    session_id=session.session_id,
                    timestamp_utc=bucket_start.isoformat(timespec="seconds"),
                    bucket_start_utc=bucket_start.isoformat(timespec="seconds"),
                    bucket_end_utc=bucket_end.isoformat(timespec="seconds"),
                    duration_seconds=duration,
                    duration_class=duration_class,
                    is_terminal_bucket=terminal,
                    open=candles[0].open,
                    high=max(item.high for item in candles),
                    low=min(item.low for item in candles),
                    close=candles[-1].close,
                    volume=sum(item.volume for item in candles),
                    source_candle_ids=tuple(item.candle_id for item in candles),
                    source_row_hashes=tuple(item.source_row_hash for item in candles),
                    source_window_hash=source.native_rows_hash,
                )
            )
            terminal_shortened = terminal_shortened or duration_class is Derived4hDurationClass.SESSION_TERMINAL_SHORTENED
        if reasons:
            break

    # A source row in a partially covered bucket is still valid source
    # evidence; the missing neighbour makes the bucket not covered, not
    # cross-session or malformed. Only an otherwise valid interval that cannot
    # fit any policy bucket is inconsistent.
    unbucketed = []
    for interval in intervals_by_id.values():
        interval_start = _utc(interval.interval_start_utc)
        interval_end = _utc(interval.interval_end_utc)
        session = _session_for_interval(request.schedule, interval_start, interval_end)
        if session is None:
            unbucketed.append(interval.candle_id)
            continue
        if not any(
            interval_start >= bucket_start and interval_end <= bucket_end
            for bucket_start, bucket_end, _, _ in _session_buckets(session, request.policy)
        ):
            unbucketed.append(interval.candle_id)
    if unbucketed:
        reasons.append(Derived4hReasonCode.SOURCE_INTERVAL_MISMATCH)
    if reasons:
        return _result_for_request(
            request,
            status=Derived4hReconstructionStatus.INCONSISTENT,
            missing_interval_ids=coverage_missing,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )
    if coverage_missing:
        return _result_for_request(
            request,
            status=Derived4hReconstructionStatus.NOT_COVERED,
            missing_interval_ids=coverage_missing,
            reason_codes=(Derived4hReasonCode.BUCKET_COVERAGE_MISSING,),
        )

    rows_hash = derived_4h_rows_hash(bars)
    dataset = _derived_dataset_cutoff(source, request.policy, rows_hash, bar_count=len(bars))
    window = Derived4hWindow.create(
        window_id=f"derived_4h_window_{canonical_sha256({'request': request.content_hash, 'rows': rows_hash})[:24]}",
        source_window=source,
        source_intervals=tuple(sorted_intervals),
        schedule=request.schedule,
        policy=request.policy,
        dataset_cutoff=dataset,
        bars=tuple(bars),
    )
    warnings: tuple[str, ...] = (
        "One or more complete session-terminal shortened buckets were accepted under the explicit aggregation policy.",
    ) if terminal_shortened else ()
    success_reasons: tuple[Derived4hReasonCode, ...] = (
        (Derived4hReasonCode.TERMINAL_SHORTENED_ACCEPTED,) if terminal_shortened else ()
    )
    return _result_for_request(
        request,
        status=Derived4hReconstructionStatus.AVAILABLE,
        window=window,
        reason_codes=success_reasons,
        warnings=warnings,
    )


def build_derived_4h_pivot_catalog(
    window: Derived4hWindow,
    *,
    pivot_window: int = 1,
) -> tuple[TypedPivot, ...]:
    """Build a deterministic catalog from derived bars without calling it native."""

    if not isinstance(window, Derived4hWindow):
        raise TypeError("window must be a Derived4hWindow.")
    if not isinstance(pivot_window, int) or pivot_window < 1:
        raise ValueError("pivot_window must be a positive integer.")
    pivots: list[TypedPivot] = []
    for index, bar in enumerate(window.bars):
        left = window.bars[max(0, index - pivot_window):index]
        right = window.bars[index + 1:index + pivot_window + 1]
        neighbors = (*left, *right)
        boundary = index in {0, len(window.bars) - 1}
        if boundary or all(bar.high >= item.high for item in neighbors):
            pivots.append(
                TypedPivot.create(
                    pivot_id=f"{window.window_id}:high:{index}",
                    timestamp_utc=bar.timestamp_utc,
                    price=bar.high,
                    price_field=PivotPriceField.HIGH,
                    pivot_type=PivotType.HIGH,
                    timeframe="4h",
                    source_window_hash=window.derived_rows_hash,
                    source_bar_hash=bar.content_hash,
                )
            )
        if boundary or all(bar.low <= item.low for item in neighbors):
            pivots.append(
                TypedPivot.create(
                    pivot_id=f"{window.window_id}:low:{index}",
                    timestamp_utc=bar.timestamp_utc,
                    price=bar.low,
                    price_field=PivotPriceField.LOW,
                    pivot_type=PivotType.LOW,
                    timeframe="4h",
                    source_window_hash=window.derived_rows_hash,
                    source_bar_hash=bar.content_hash,
                )
            )
    if not pivots:
        raise ValueError("No derived 4h pivots were available.")
    return tuple(sorted(pivots, key=lambda item: (item.timestamp_utc, item.pivot_id)))


def derived_4h_pivot_matches(pivot: TypedPivot, window: Derived4hWindow) -> bool:
    if pivot.timeframe != "4h" or pivot.source_window_hash != window.derived_rows_hash:
        return False
    bar = next((item for item in window.bars if item.content_hash == pivot.source_bar_hash), None)
    if bar is None or bar.timestamp_utc != pivot.timestamp_utc:
        return False
    return abs(float(getattr(bar, pivot.price_field.value)) - pivot.price) <= _EPSILON


def derived_4h_invalidation_breached(
    invalidation: Any,
    start_pivot: TypedPivot,
    window: Derived4hWindow,
) -> bool:
    """Evaluate typed Elliott invalidations on complete derived bars only."""

    start = _utc(start_pivot.timestamp_utc)
    for bar in window.bars:
        if _utc(bar.timestamp_utc) <= start:
            continue
        if invalidation.evaluation_basis is InvalidationEvaluationBasis.CANDLE_CLOSE:
            observed = bar.close
        elif invalidation.direction is InvalidationDirection.BELOW:
            observed = bar.low
        else:
            observed = bar.high
        if invalidation.direction is InvalidationDirection.BELOW and observed <= invalidation.threshold_price:
            return True
        if invalidation.direction is InvalidationDirection.ABOVE and observed >= invalidation.threshold_price:
            return True
    return False


def build_derived_4h_coverage_attestation(
    window: Derived4hWindow,
    *,
    interval_start_utc: str,
    interval_end_utc: str,
) -> Derived4hCoverageAttestation:
    """Create an explicit coverage fact for a parent interval from valid bars."""

    if not isinstance(window, Derived4hWindow):
        raise TypeError("window must be a Derived4hWindow.")
    start = _utc(_normalize_utc(interval_start_utc, field_name="interval_start_utc"))
    end = _utc(_normalize_utc(interval_end_utc, field_name="interval_end_utc"))
    first = next((item for item in window.bars if _utc(item.bucket_start_utc) <= start <= _utc(item.bucket_end_utc)), None)
    last = next((item for item in reversed(window.bars) if _utc(item.bucket_start_utc) <= end <= _utc(item.bucket_end_utc)), None)
    missing: list[str] = []
    covered: list[str] = []
    if first is None:
        missing.append(f"coverage_start:{start.isoformat(timespec='seconds')}")
    if last is None:
        missing.append(f"coverage_end:{end.isoformat(timespec='seconds')}")
    if first is not None and last is not None:
        start_index = window.bars.index(first)
        end_index = window.bars.index(last)
        if end_index < start_index:
            missing.append("coverage_order_invalid")
        else:
            covered.extend(item.bar_id for item in window.bars[start_index:end_index + 1])
    return Derived4hCoverageAttestation.create(
        attestation_id="",
        derived_window_hash=window.content_hash,
        derived_rows_hash=window.derived_rows_hash,
        policy_hash=window.policy.content_hash,
        schedule_hash=window.schedule.content_hash,
        interval_start_utc=start.isoformat(timespec="seconds"),
        interval_end_utc=end.isoformat(timespec="seconds"),
        coverage_complete=not missing,
        missing_interval_ids=tuple(missing),
        covered_bucket_ids=tuple(covered),
        coverage_method="deterministic_session_aligned_native_intraday_tiling",
    )


__all__ = [
    "DERIVED_4H_BINDING_SCHEMA_VERSION",
    "DERIVED_4H_POLICY_SCHEMA_VERSION",
    "DERIVED_4H_RESULT_SCHEMA_VERSION",
    "DERIVED_4H_SCHEMA_VERSION",
    "DERIVED_4H_SCHEDULE_SCHEMA_VERSION",
    "Derived4hBar",
    "Derived4hCandidateGraphBinding",
    "Derived4hCoverageAttestation",
    "Derived4hDurationClass",
    "Derived4hEvidenceKind",
    "Derived4hReasonCode",
    "Derived4hReconstructionRequest",
    "Derived4hReconstructionResult",
    "Derived4hReconstructionStatus",
    "Derived4hWindow",
    "IntradaySourceInterval",
    "MAX_4H_SECONDS",
    "NASDAQ_EXCHANGE",
    "NASDAQ_TIMEZONE",
    "NasdaqSessionSchedule",
    "NasdaqTradingSession",
    "REGULAR_SESSION",
    "SessionAligned4hAggregationPolicy",
    "SourceTimestampSemantics",
    "bind_candidate_graph_to_derived_4h",
    "build_derived_4h_coverage_attestation",
    "build_derived_4h_pivot_catalog",
    "derived_4h_bar_content_hash",
    "derived_4h_candidate_graph_binding_content_hash",
    "derived_4h_coverage_attestation_content_hash",
    "derived_4h_invalidation_breached",
    "derived_4h_pivot_matches",
    "derived_4h_reconstruction_request_content_hash",
    "derived_4h_reconstruction_result_content_hash",
    "derived_4h_rows_hash",
    "derived_4h_window_content_hash",
    "nasdaq_session_aligned_4h_policy",
    "nasdaq_session_schedule_content_hash",
    "nasdaq_trading_session_content_hash",
    "reconstruct_session_aligned_4h",
    "session_aligned_4h_policy_content_hash",
]
