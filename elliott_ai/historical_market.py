"""Typed, deterministic historical market-move case contracts.

This module is intentionally disconnected from the active Elliott and Market
Scenario pipelines. It performs no research, provider access, model calls,
retrieval, prediction, persistence, reporting, or trade analysis.

Historical facts are wrapped with explicit temporal classifications. Evidence
known only after a move may be retained for retrospective study, but it cannot
silently enter pre-move or during-move exposure features.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, time, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .market_scenario import (
    EXPOSURE_CATEGORY_COMPATIBILITY,
    QUALITATIVE_CONFIDENCE_VALUES,
    EvidenceItem,
    EvidenceStatus,
    ExposureCategory,
    ExposureItem,
    ExposureType,
    MaterialEvent,
    ScenarioDirection,
    ValidationIssue,
    ValidationResult,
)


HISTORICAL_CASE_SCHEMA_VERSION = "historical-market-case-1.0.0"
HISTORICAL_CAUSAL_SCHEMA_VERSION = "historical-causal-analysis-1.1.0"
HISTORICAL_CAUSAL_LEGACY_SCHEMA_VERSIONS = frozenset(
    {"historical-causal-analysis-1.0.0"}
)
HISTORICAL_SIMILARITY_SCHEMA_VERSION = "historical-similarity-features-1.0.0"
HISTORICAL_CASE_VALIDATION_VERSION = "historical-market-case-validation-1.0.0"
HISTORICAL_CANONICALIZATION_VERSION = "canonical-json-sha256-1.0.0"
HISTORICAL_HASH_ALGORITHM = "sha256"

_NUMERIC_REL_TOLERANCE = 1e-7
_NUMERIC_ABS_TOLERANCE = 1e-7


class HistoricalMoveType(StrEnum):
    UPWARD_IMPULSE = "upward_impulse"
    DOWNWARD_IMPULSE = "downward_impulse"
    CRASH = "crash"
    CORRECTION = "correction"
    CAPITULATION = "capitulation"
    SHORT_SQUEEZE = "short_squeeze"
    MELT_UP = "melt_up"
    GAP_REPRICING = "gap_repricing"
    POST_EARNINGS_REPRICING = "post_earnings_repricing"
    POST_EVENT_REPRICING = "post_event_repricing"
    VALUATION_RERATING = "valuation_rerating"
    VALUATION_DERATING = "valuation_derating"
    FAILED_BREAKOUT = "failed_breakout"
    FAILED_BREAKDOWN = "failed_breakdown"
    REVERSAL = "reversal"
    RECOVERY = "recovery"
    PROLONGED_DECLINE = "prolonged_decline"
    PROLONGED_ADVANCE = "prolonged_advance"


class HistoricalCaseStatus(StrEnum):
    DRAFT = "draft"
    EXTRACTED = "extracted"
    REVIEWED = "reviewed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class HistoricalTemporalClassification(StrEnum):
    KNOWN_BEFORE_MOVE = "known_before_move"
    PUBLISHED_DURING_MOVE = "published_during_move"
    KNOWN_ONLY_AFTER_MOVE = "known_only_after_move"
    RETROSPECTIVE_INTERPRETATION = "retrospective_interpretation"
    UNAVAILABLE = "unavailable"


class HistoricalEventRole(StrEnum):
    CONTEXT = "context"
    INITIATING_CATALYST = "initiating_catalyst"
    DURING_MOVE_CATALYST = "during_move_catalyst"
    RETROSPECTIVE_CONTEXT = "retrospective_context"
    UNAVAILABLE = "unavailable"


class HistoricalAnalystType(StrEnum):
    HUMAN = "human"
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    HYBRID = "hybrid"


class TransmissionMechanism(StrEnum):
    EARNINGS_REVISION = "earnings_revision"
    GUIDANCE_REVISION = "guidance_revision"
    MULTIPLE_COMPRESSION = "multiple_compression"
    MULTIPLE_EXPANSION = "multiple_expansion"
    LIQUIDITY_WITHDRAWAL = "liquidity_withdrawal"
    LIQUIDITY_EXPANSION = "liquidity_expansion"
    FORCED_DELEVERAGING = "forced_deleveraging"
    SHORT_COVERING = "short_covering"
    OPTIONS_GAMMA = "options_gamma"
    MOMENTUM_UNWIND = "momentum_unwind"
    CROWDED_POSITIONING_UNWIND = "crowded_positioning_unwind"
    UNCERTAINTY_PREMIUM = "uncertainty_premium"
    RISK_PREMIUM_EXPANSION = "risk_premium_expansion"
    RISK_PREMIUM_COMPRESSION = "risk_premium_compression"
    FINANCING_FEAR = "financing_fear"
    DILUTION_EXPECTATION = "dilution_expectation"
    EXECUTION_REPRICING = "execution_repricing"
    REGULATORY_REPRICING = "regulatory_repricing"
    DEMAND_REPRICING = "demand_repricing"
    SECTOR_ROTATION = "sector_rotation"
    MACRO_SHOCK = "macro_shock"
    SELL_THE_NEWS = "sell_the_news"
    BUY_THE_RUMOR = "buy_the_rumor"
    CAPITULATION = "capitulation"
    REFLEXIVE_PRICE_FEEDBACK = "reflexive_price_feedback"


class MarketRegimeState(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"
    RESTRICTIVE = "restrictive"
    ACCOMMODATIVE = "accommodative"
    ACCELERATING = "accelerating"
    DECELERATING = "decelerating"
    EXPANDING = "expanding"
    CONTRACTING = "contracting"
    EASING = "easing"
    TIGHTENING = "tightening"
    STRESSED = "stressed"
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    ADVANCING = "advancing"
    DECLINING = "declining"
    OUTPERFORMING = "outperforming"
    UNDERPERFORMING = "underperforming"
    CROWDED = "crowded"
    UNCROWDED = "uncrowded"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class MagnitudeBucket(StrEnum):
    UNDER_10_PERCENT = "under_10_percent"
    FROM_10_TO_20_PERCENT = "10_to_20_percent"
    FROM_20_TO_40_PERCENT = "20_to_40_percent"
    FROM_40_TO_60_PERCENT = "40_to_60_percent"
    OVER_60_PERCENT = "over_60_percent"
    UNAVAILABLE = "unavailable"


class DurationBucket(StrEnum):
    INTRADAY = "intraday"
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"
    YEARS = "years"
    UNAVAILABLE = "unavailable"


class RelativePerformanceBucket(StrEnum):
    STRONG_OUTPERFORMANCE = "strong_outperformance"
    OUTPERFORMANCE = "outperformance"
    IN_LINE = "in_line"
    UNDERPERFORMANCE = "underperformance"
    STRONG_UNDERPERFORMANCE = "strong_underperformance"
    UNAVAILABLE = "unavailable"


def _normalize_timestamp(value: str | datetime, *, field_name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    else:
        raise TypeError(f"{field_name} must be a timestamp string or datetime.")
    aware = (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalize_optional_timestamp(
    value: str | datetime | None,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _normalize_timestamp(value, field_name=field_name)


def _temporal_point(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(
                date.fromisoformat(value),
                time.min,
                timezone.utc,
            )
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    aware = (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
    return aware.astimezone(timezone.utc)


def _event_temporal_point(event: MaterialEvent) -> datetime | None:
    value = event.scheduled_at
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return datetime(
            int(value[:4]),
            int(value[5:7]),
            1,
            tzinfo=timezone.utc,
        )
    quarter = re.fullmatch(r"(\d{4})-Q([1-4])", value)
    if quarter:
        return datetime(
            int(quarter.group(1)),
            (int(quarter.group(2)) - 1) * 3 + 1,
            1,
            tzinfo=timezone.utc,
        )
    return _temporal_point(value)


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


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
    model_type: type[Any],
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


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON contract mappings must use string keys.")
        return MappingProxyType(
            {
                key: _freeze_json(item)
                for key, item in sorted(value.items())
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, set):
        raise TypeError("JSON contract values cannot contain sets.")
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"JSON contract values cannot contain {type(value).__name__} instances."
    )


def _json_compatible(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_compatible(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, datetime):
        return _normalize_timestamp(value, field_name="datetime")
    if isinstance(value, date):
        return value.isoformat()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_compatible(to_dict())
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _contract_content_hash(value: Any) -> str:
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("Content-hash input must provide to_dict().")
    payload = dict(to_dict())
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
            f"{model_name} contains unknown fields: {', '.join(unknown)}."
        )


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _close(first: float, second: float) -> bool:
    return math.isclose(
        float(first),
        float(second),
        rel_tol=_NUMERIC_REL_TOLERANCE,
        abs_tol=_NUMERIC_ABS_TOLERANCE,
    )


def _duplicates(values: Sequence[str]) -> set[str]:
    return {value for value in values if values.count(value) > 1}


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_compatible(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class HistoricalMarketMove(_JsonContract):
    case_id: str
    symbol: str
    exchange: str
    move_type: HistoricalMoveType
    direction: ScenarioDirection
    start_at: str
    end_at: str
    start_price: float
    end_price: float
    percentage_move: float
    logarithmic_move: float
    duration_days: float
    peak_to_trough_or_trough_to_peak: float
    market_data_cutoff: str
    source_hashes: Mapping[str, str]
    case_status: HistoricalCaseStatus
    content_hash: str = ""
    intraday_extreme: float | None = None
    recovery_start_at: str | None = None
    recovery_end_at: str | None = None
    recovery_percentage: float | None = None
    volatility_before: str | None = None
    volatility_during: str | None = None
    volatility_after: str | None = None
    volume_regime_before: str | None = None
    volume_regime_during: str | None = None
    volume_regime_after: str | None = None
    benchmark_symbol: str | None = None
    benchmark_move: float | None = None
    sector_symbol: str | None = None
    sector_move: float | None = None
    technical_structure: Any | None = None
    current_wave_or_phase: str | None = None
    technical_confidence: Any | None = None
    notes: tuple[str, ...] = ()
    schema_version: str = HISTORICAL_CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "move_type",
            _enum_value(
                self.move_type,
                HistoricalMoveType,
                field_name="move_type",
            ),
        )
        object.__setattr__(
            self,
            "direction",
            _enum_value(
                self.direction,
                ScenarioDirection,
                field_name="direction",
            ),
        )
        object.__setattr__(
            self,
            "case_status",
            _enum_value(
                self.case_status,
                HistoricalCaseStatus,
                field_name="case_status",
            ),
        )
        for field_name in ("start_at", "end_at", "market_data_cutoff"):
            object.__setattr__(
                self,
                field_name,
                _normalize_timestamp(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        for field_name in ("recovery_start_at", "recovery_end_at"):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_timestamp(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if not isinstance(self.source_hashes, Mapping):
            raise TypeError("source_hashes must be a mapping.")
        source_hashes = dict(self.source_hashes)
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in source_hashes.items()
        ):
            raise TypeError(
                "source_hashes must contain only string keys and values."
            )
        object.__setattr__(
            self,
            "source_hashes",
            MappingProxyType(dict(sorted(source_hashes.items()))),
        )
        if self.technical_structure is not None:
            object.__setattr__(
                self,
                "technical_structure",
                _freeze_json(self.technical_structure),
            )
        if self.technical_confidence is not None:
            object.__setattr__(
                self,
                "technical_confidence",
                _freeze_json(self.technical_confidence),
            )
        object.__setattr__(
            self,
            "notes",
            _string_tuple(self.notes, field_name="notes"),
        )

    @classmethod
    def create(cls, **values: Any) -> "HistoricalMarketMove":
        supplied_hash = values.pop("content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates content_hash; do not supply it.")
        item = cls(**values, content_hash="")
        return replace(item, content_hash=historical_market_move_content_hash(item))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoricalMarketMove":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceItem(_JsonContract):
    evidence: EvidenceItem
    temporal_classification: HistoricalTemporalClassification
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidenceItem):
            raise TypeError("evidence must be an EvidenceItem.")
        object.__setattr__(
            self,
            "temporal_classification",
            _enum_value(
                self.temporal_classification,
                HistoricalTemporalClassification,
                field_name="temporal_classification",
            ),
        )
        object.__setattr__(
            self,
            "notes",
            _string_tuple(self.notes, field_name="notes"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoricalEvidenceItem":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["evidence"] = EvidenceItem.from_dict(raw["evidence"])
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalMaterialEvent(_JsonContract):
    event: MaterialEvent
    role: HistoricalEventRole
    lag_reasoning: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event, MaterialEvent):
            raise TypeError("event must be a MaterialEvent.")
        object.__setattr__(
            self,
            "role",
            _enum_value(
                self.role,
                HistoricalEventRole,
                field_name="role",
            ),
        )
        object.__setattr__(
            self,
            "notes",
            _string_tuple(self.notes, field_name="notes"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoricalMaterialEvent":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["event"] = MaterialEvent.from_dict(raw["event"])
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MarketRegime(_JsonContract):
    interest_rate_regime: MarketRegimeState
    inflation_regime: MarketRegimeState
    liquidity_regime: MarketRegimeState
    credit_regime: MarketRegimeState
    equity_risk_appetite: MarketRegimeState
    volatility_regime: MarketRegimeState
    small_cap_regime: MarketRegimeState
    growth_stock_regime: MarketRegimeState
    sector_regime: MarketRegimeState
    benchmark_trend: MarketRegimeState
    regime_start_at: str | None
    regime_end_at: str | None
    supporting_evidence_ids: tuple[str, ...]
    confidence: str
    content_hash: str = ""
    schema_version: str = HISTORICAL_CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
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
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(
                    getattr(self, field_name),
                    MarketRegimeState,
                    field_name=field_name,
                ),
            )
        for field_name in ("regime_start_at", "regime_end_at"):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_timestamp(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "supporting_evidence_ids",
            _string_tuple(
                self.supporting_evidence_ids,
                field_name="supporting_evidence_ids",
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "MarketRegime":
        supplied_hash = values.pop("content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates content_hash; do not supply it.")
        item = cls(**values, content_hash="")
        return replace(item, content_hash=market_regime_content_hash(item))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MarketRegime":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalEvidencePacket(_JsonContract):
    historical_move: HistoricalMarketMove
    evidence: tuple[HistoricalEvidenceItem, ...]
    exposures_before_move: tuple[ExposureItem, ...]
    exposures_during_move: tuple[ExposureItem, ...]
    material_events: tuple[HistoricalMaterialEvent, ...]
    market_regime: MarketRegime
    unresolved_unknowns: tuple[str, ...]
    extraction_metadata: Mapping[str, Any]
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""
    schema_version: str = HISTORICAL_CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.historical_move, HistoricalMarketMove):
            raise TypeError(
                "historical_move must be a HistoricalMarketMove."
            )
        object.__setattr__(
            self,
            "evidence",
            _model_tuple(
                self.evidence,
                HistoricalEvidenceItem,
                field_name="evidence",
            ),
        )
        for field_name in ("exposures_before_move", "exposures_during_move"):
            object.__setattr__(
                self,
                field_name,
                _model_tuple(
                    getattr(self, field_name),
                    ExposureItem,
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "material_events",
            _model_tuple(
                self.material_events,
                HistoricalMaterialEvent,
                field_name="material_events",
            ),
        )
        if not isinstance(self.market_regime, MarketRegime):
            raise TypeError("market_regime must be a MarketRegime.")
        object.__setattr__(
            self,
            "unresolved_unknowns",
            _string_tuple(
                self.unresolved_unknowns,
                field_name="unresolved_unknowns",
            ),
        )
        if not isinstance(self.extraction_metadata, Mapping):
            raise TypeError("extraction_metadata must be a mapping.")
        object.__setattr__(
            self,
            "extraction_metadata",
            _freeze_json(self.extraction_metadata),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be a ValidationResult.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoricalEvidencePacket":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["historical_move"] = HistoricalMarketMove.from_dict(
            raw["historical_move"]
        )
        raw["evidence"] = tuple(
            HistoricalEvidenceItem.from_dict(item)
            for item in raw.get("evidence", ())
        )
        for field_name in ("exposures_before_move", "exposures_during_move"):
            raw[field_name] = tuple(
                ExposureItem.from_dict(item)
                for item in raw.get(field_name, ())
            )
        raw["material_events"] = tuple(
            HistoricalMaterialEvent.from_dict(item)
            for item in raw.get("material_events", ())
        )
        raw["market_regime"] = MarketRegime.from_dict(raw["market_regime"])
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalCausalAnalysis(_JsonContract):
    case_id: str
    initiating_conditions: tuple[str, ...]
    primary_drivers: tuple[str, ...]
    secondary_drivers: tuple[str, ...]
    amplifiers: tuple[str, ...]
    dampeners: tuple[str, ...]
    triggering_events: tuple[str, ...]
    transmission_mechanisms: tuple[TransmissionMechanism, ...]
    exposure_interactions: tuple[str, ...]
    market_regime_contribution: str
    company_specific_contribution: str
    sector_contribution: str
    macro_contribution: str
    positioning_contribution: str
    no_single_catalyst: bool
    alternative_explanations: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    confidence: str
    analyst_type: HistoricalAnalystType
    model_name: str
    prompt_version: str
    generated_at: str
    applicable_cutoff: str
    supporting_evidence_ids: tuple[str, ...] = ()
    initiating_condition_evidence_ids: tuple[str, ...] = ()
    retrospective_explanations: tuple[str, ...] = ()
    retrospective_evidence_ids: tuple[str, ...] = ()
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""
    schema_version: str = HISTORICAL_CAUSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "initiating_conditions",
            "primary_drivers",
            "secondary_drivers",
            "amplifiers",
            "dampeners",
            "triggering_events",
            "exposure_interactions",
            "alternative_explanations",
            "contradicting_evidence_ids",
            "missing_evidence",
            "supporting_evidence_ids",
            "initiating_condition_evidence_ids",
            "retrospective_explanations",
            "retrospective_evidence_ids",
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
            "transmission_mechanisms",
            _enum_tuple(
                self.transmission_mechanisms,
                TransmissionMechanism,
                field_name="transmission_mechanisms",
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
            raise TypeError("validation_result must be a ValidationResult.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoricalCausalAnalysis":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoricalSimilarityFeatures(_JsonContract):
    case_id: str
    direction: ScenarioDirection
    magnitude_bucket: MagnitudeBucket
    duration_bucket: DurationBucket
    volatility_regime: MarketRegimeState
    volume_regime: MarketRegimeState
    valuation_state: MarketRegimeState
    liquidity_state: MarketRegimeState
    positioning_state: MarketRegimeState
    company_exposure_types: tuple[ExposureType, ...]
    sector_exposure_types: tuple[ExposureType, ...]
    macro_exposure_types: tuple[ExposureType, ...]
    event_types: tuple[str, ...]
    transmission_mechanisms: tuple[TransmissionMechanism, ...]
    technical_structure: str
    benchmark_relative_performance: RelativePerformanceBucket
    sector_relative_performance: RelativePerformanceBucket
    content_hash: str = ""
    schema_version: str = HISTORICAL_SIMILARITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "direction",
            _enum_value(
                self.direction,
                ScenarioDirection,
                field_name="direction",
            ),
        )
        object.__setattr__(
            self,
            "magnitude_bucket",
            _enum_value(
                self.magnitude_bucket,
                MagnitudeBucket,
                field_name="magnitude_bucket",
            ),
        )
        object.__setattr__(
            self,
            "duration_bucket",
            _enum_value(
                self.duration_bucket,
                DurationBucket,
                field_name="duration_bucket",
            ),
        )
        for field_name in (
            "volatility_regime",
            "volume_regime",
            "valuation_state",
            "liquidity_state",
            "positioning_state",
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(
                    getattr(self, field_name),
                    MarketRegimeState,
                    field_name=field_name,
                ),
            )
        for field_name in (
            "company_exposure_types",
            "sector_exposure_types",
            "macro_exposure_types",
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
            "event_types",
            _string_tuple(self.event_types, field_name="event_types"),
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
            "benchmark_relative_performance",
            "sector_relative_performance",
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(
                    getattr(self, field_name),
                    RelativePerformanceBucket,
                    field_name=field_name,
                ),
            )

    @classmethod
    def create(cls, **values: Any) -> "HistoricalSimilarityFeatures":
        supplied_hash = values.pop("content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates content_hash; do not supply it.")
        item = cls(**values, content_hash="")
        return replace(
            item,
            content_hash=historical_similarity_features_content_hash(item),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "HistoricalSimilarityFeatures":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


def historical_market_move_content_hash(value: HistoricalMarketMove) -> str:
    if not isinstance(value, HistoricalMarketMove):
        raise TypeError("value must be a HistoricalMarketMove.")
    return _contract_content_hash(value)


def market_regime_content_hash(value: MarketRegime) -> str:
    if not isinstance(value, MarketRegime):
        raise TypeError("value must be a MarketRegime.")
    return _contract_content_hash(value)


def historical_evidence_packet_content_hash(
    value: HistoricalEvidencePacket,
) -> str:
    if not isinstance(value, HistoricalEvidencePacket):
        raise TypeError("value must be a HistoricalEvidencePacket.")
    return _contract_content_hash(value)


def historical_causal_analysis_content_hash(
    value: HistoricalCausalAnalysis,
) -> str:
    if not isinstance(value, HistoricalCausalAnalysis):
        raise TypeError("value must be a HistoricalCausalAnalysis.")
    return _contract_content_hash(value)


def historical_similarity_features_content_hash(
    value: HistoricalSimilarityFeatures,
) -> str:
    if not isinstance(value, HistoricalSimilarityFeatures):
        raise TypeError("value must be a HistoricalSimilarityFeatures.")
    return _contract_content_hash(value)


_VALIDATION_RULE_ORDER = (
    "schema_version",
    "content_hash",
    "invalid_move_dates",
    "invalid_prices",
    "percentage_move_inconsistency",
    "logarithmic_move_inconsistency",
    "invalid_duration",
    "direction_mismatch",
    "invalid_optional_numeric",
    "invalid_recovery_dates",
    "market_data_cutoff",
    "missing_provenance",
    "historical_move_valid",
    "duplicate_ids",
    "temporal_classification",
    "historical_cutoff_violation",
    "future_leakage",
    "retrospective_pre_move",
    "dangling_evidence_references",
    "dangling_event_references",
    "exposure_reference_overlap",
    "category_exposure_compatibility",
    "event_lag_reasoning",
    "market_regime_valid",
    "invalid_regime_dates",
    "case_reference",
    "missing_causal_drivers",
    "no_transmission_mechanism",
    "causal_evidence_linkage",
    "causal_temporal_labeling",
    "triggering_event_temporality",
    "unsupported_causal_certainty",
    "causal_contradicting_evidence",
    "invalid_confidence",
    "similarity_feature_compatibility",
    "similarity_feature_duplicates",
    "similarity_feature_values",
)


class _ValidationCollector:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []
        self.failed: set[str] = set()
        self.seen: set[str] = set()

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
            rule
            for rule in _VALIDATION_RULE_ORDER
            if rule in self.seen
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
            validation_version=HISTORICAL_CASE_VALIDATION_VERSION,
        )


def validate_historical_market_move(
    move: HistoricalMarketMove,
) -> ValidationResult:
    if not isinstance(move, HistoricalMarketMove):
        raise TypeError("move must be a HistoricalMarketMove.")
    result = _ValidationCollector()
    result.require(
        "schema_version",
        move.schema_version == HISTORICAL_CASE_SCHEMA_VERSION,
        "schema_version",
        "Historical move schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(move.content_hash)
        and move.content_hash == historical_market_move_content_hash(move),
        "content_hash",
        "Historical move content hash is missing or does not match.",
    )

    start = _temporal_point(move.start_at)
    end = _temporal_point(move.end_at)
    dates_valid = start is not None and end is not None and start < end
    result.require(
        "invalid_move_dates",
        dates_valid,
        "start_at",
        "start_at must be earlier than end_at.",
    )

    prices_valid = (
        _finite_number(move.start_price)
        and _finite_number(move.end_price)
        and float(move.start_price) > 0
        and float(move.end_price) > 0
    )
    result.require(
        "invalid_prices",
        prices_valid,
        "start_price",
        "Move prices must be finite and strictly positive.",
    )

    expected_percentage: float | None = None
    expected_logarithmic: float | None = None
    if prices_valid:
        expected_percentage = (
            float(move.end_price) / float(move.start_price) - 1.0
        ) * 100.0
        expected_logarithmic = math.log(
            float(move.end_price) / float(move.start_price)
        )
    result.require(
        "percentage_move_inconsistency",
        expected_percentage is not None
        and _finite_number(move.percentage_move)
        and _close(float(move.percentage_move), expected_percentage),
        "percentage_move",
        "percentage_move is inconsistent with start_price and end_price.",
    )
    result.require(
        "logarithmic_move_inconsistency",
        expected_logarithmic is not None
        and _finite_number(move.logarithmic_move)
        and _close(float(move.logarithmic_move), expected_logarithmic),
        "logarithmic_move",
        "logarithmic_move is inconsistent with start_price and end_price.",
    )

    expected_duration = (
        (end - start).total_seconds() / 86400.0
        if dates_valid and start is not None and end is not None
        else None
    )
    result.require(
        "invalid_duration",
        expected_duration is not None
        and _finite_number(move.duration_days)
        and float(move.duration_days) > 0
        and _close(float(move.duration_days), expected_duration),
        "duration_days",
        "duration_days must equal elapsed calendar days between move endpoints.",
    )

    expected_direction: ScenarioDirection | None = None
    if expected_percentage is not None:
        if expected_percentage > _NUMERIC_ABS_TOLERANCE:
            expected_direction = ScenarioDirection.UP
        elif expected_percentage < -_NUMERIC_ABS_TOLERANCE:
            expected_direction = ScenarioDirection.DOWN
        else:
            expected_direction = ScenarioDirection.SIDEWAYS
    result.require(
        "direction_mismatch",
        expected_direction is not None and move.direction is expected_direction,
        "direction",
        "direction is inconsistent with the signed price move.",
    )

    optional_numbers = (
        move.intraday_extreme,
        move.recovery_percentage,
        move.benchmark_move,
        move.sector_move,
    )
    result.require(
        "invalid_optional_numeric",
        all(value is None or _finite_number(value) for value in optional_numbers)
        and _finite_number(move.peak_to_trough_or_trough_to_peak)
        and float(move.peak_to_trough_or_trough_to_peak) >= 0,
        "peak_to_trough_or_trough_to_peak",
        "Optional measurements and move magnitude must be finite.",
    )

    recovery_start = _temporal_point(move.recovery_start_at)
    recovery_end = _temporal_point(move.recovery_end_at)
    recovery_pair_valid = (
        (recovery_start is None and recovery_end is None)
        or (
            recovery_start is not None
            and recovery_end is not None
            and recovery_start < recovery_end
            and end is not None
            and recovery_start >= end
        )
    )
    result.require(
        "invalid_recovery_dates",
        recovery_pair_valid,
        "recovery_start_at",
        "Recovery timestamps must be supplied together, ordered, and post-move.",
    )

    cutoff = _temporal_point(move.market_data_cutoff)
    result.require(
        "market_data_cutoff",
        end is not None and cutoff is not None and cutoff >= end,
        "market_data_cutoff",
        "market_data_cutoff cannot precede the historical move endpoint.",
    )
    provenance_valid = (
        bool(move.case_id.strip())
        and bool(move.symbol.strip())
        and bool(move.exchange.strip())
        and bool(move.source_hashes)
        and all(
            key.strip() and value.strip()
            for key, value in move.source_hashes.items()
        )
    )
    result.require(
        "missing_provenance",
        provenance_valid,
        "source_hashes",
        "Historical move identity and source hashes are required.",
    )
    return result.result()


def validate_market_regime(
    regime: MarketRegime,
    *,
    evidence_ids: set[str] | None = None,
) -> ValidationResult:
    if not isinstance(regime, MarketRegime):
        raise TypeError("regime must be a MarketRegime.")
    result = _ValidationCollector()
    result.require(
        "schema_version",
        regime.schema_version == HISTORICAL_CASE_SCHEMA_VERSION,
        "schema_version",
        "Market regime schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(regime.content_hash)
        and regime.content_hash == market_regime_content_hash(regime),
        "content_hash",
        "Market regime content hash is missing or does not match.",
    )
    start = _temporal_point(regime.regime_start_at)
    end = _temporal_point(regime.regime_end_at)
    result.require(
        "invalid_regime_dates",
        (start is None and end is None)
        or (start is not None and end is not None and start < end),
        "regime_start_at",
        "Regime dates must be absent together or ordered.",
    )
    result.require(
        "invalid_confidence",
        regime.confidence in QUALITATIVE_CONFIDENCE_VALUES,
        "confidence",
        "Regime confidence must use the qualitative vocabulary.",
    )
    duplicate_refs = _duplicates(regime.supporting_evidence_ids)
    result.require(
        "duplicate_ids",
        not duplicate_refs,
        "supporting_evidence_ids",
        "Market regime evidence references must be unique.",
    )
    if evidence_ids is not None:
        dangling = set(regime.supporting_evidence_ids) - evidence_ids
        result.require(
            "dangling_evidence_references",
            not dangling,
            "supporting_evidence_ids",
            (
                "Dangling market-regime evidence references: "
                + ", ".join(sorted(dangling))
                + "."
                if dangling
                else ""
            ),
        )
    return result.result()


def _evidence_references(exposure: ExposureItem) -> tuple[str, ...]:
    return (
        exposure.supporting_evidence_ids
        + exposure.contradicting_evidence_ids
    )


def validate_historical_evidence_packet(
    packet: HistoricalEvidencePacket,
) -> ValidationResult:
    if not isinstance(packet, HistoricalEvidencePacket):
        raise TypeError("packet must be a HistoricalEvidencePacket.")
    result = _ValidationCollector()
    move = packet.historical_move
    result.require(
        "schema_version",
        packet.schema_version == HISTORICAL_CASE_SCHEMA_VERSION,
        "schema_version",
        "Historical evidence-packet schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(packet.content_hash)
        and packet.content_hash
        == historical_evidence_packet_content_hash(packet),
        "content_hash",
        "Historical evidence-packet content hash is missing or mismatched.",
    )
    move_validation = validate_historical_market_move(move)
    result.require(
        "historical_move_valid",
        move_validation.is_valid,
        "historical_move",
        "The nested historical move failed deterministic validation.",
    )

    evidence_records = {
        item.evidence.evidence_id: item for item in packet.evidence
    }
    evidence_id_list = [
        item.evidence.evidence_id for item in packet.evidence
    ]
    exposure_ids = [
        item.exposure_id
        for item in (
            packet.exposures_before_move + packet.exposures_during_move
        )
    ]
    event_ids = [item.event.event_id for item in packet.material_events]
    duplicate_ids = (
        _duplicates(evidence_id_list)
        | _duplicates(exposure_ids)
        | _duplicates(event_ids)
    )
    result.require(
        "duplicate_ids",
        not duplicate_ids,
        "evidence",
        (
            "Historical packet IDs must be unique: "
            + ", ".join(sorted(duplicate_ids))
            + "."
            if duplicate_ids
            else ""
        ),
    )

    start = _temporal_point(move.start_at)
    end = _temporal_point(move.end_at)
    move_cutoff = _temporal_point(move.market_data_cutoff)
    for index, record in enumerate(packet.evidence):
        evidence = record.evidence
        publication = _temporal_point(evidence.publication_date)
        applicable = _temporal_point(evidence.applicable_cutoff)
        classification = record.temporal_classification
        unavailable = (
            classification is HistoricalTemporalClassification.UNAVAILABLE
        )
        provenance_valid = unavailable or (
            bool(evidence.source.strip())
            and bool(evidence.source_type.strip())
            and bool(evidence.provider.strip())
            and publication is not None
        )
        result.require(
            "missing_provenance",
            provenance_valid,
            f"evidence[{index}]",
            "Available historical evidence requires source provenance and a publication date.",
        )
        cutoff_valid = (
            unavailable
            or (
                publication is not None
                and applicable is not None
                and publication <= applicable
                and move_cutoff is not None
                and applicable <= move_cutoff
            )
        )
        result.require(
            "historical_cutoff_violation",
            cutoff_valid,
            f"evidence[{index}].applicable_cutoff",
            "Evidence publication or applicable cutoff exceeds the permitted historical cutoff.",
        )

        classification_valid = False
        if classification is HistoricalTemporalClassification.KNOWN_BEFORE_MOVE:
            classification_valid = (
                publication is not None
                and start is not None
                and publication <= start
                and applicable is not None
                and applicable <= start
            )
        elif (
            classification
            is HistoricalTemporalClassification.PUBLISHED_DURING_MOVE
        ):
            classification_valid = (
                publication is not None
                and start is not None
                and end is not None
                and start <= publication <= end
            )
        elif (
            classification
            is HistoricalTemporalClassification.KNOWN_ONLY_AFTER_MOVE
        ):
            classification_valid = (
                publication is not None
                and end is not None
                and publication > end
            )
        elif (
            classification
            is HistoricalTemporalClassification.RETROSPECTIVE_INTERPRETATION
        ):
            classification_valid = evidence.status in {
                EvidenceStatus.INTERPRETATION,
                EvidenceStatus.HYPOTHETICAL,
                EvidenceStatus.UNKNOWN,
                EvidenceStatus.STALE,
            }
        elif classification is HistoricalTemporalClassification.UNAVAILABLE:
            classification_valid = evidence.status in {
                EvidenceStatus.UNAVAILABLE,
                EvidenceStatus.UNKNOWN,
            }
        result.require(
            "temporal_classification",
            classification_valid,
            f"evidence[{index}].temporal_classification",
            "Evidence timing is inconsistent with its temporal classification.",
        )
        retrospective_as_pre_move = (
            classification
            is HistoricalTemporalClassification.KNOWN_BEFORE_MOVE
            and "retrospective" in evidence.source_type.casefold()
        ) or (
            classification
            is HistoricalTemporalClassification.RETROSPECTIVE_INTERPRETATION
            and evidence.status is EvidenceStatus.CONFIRMED
        )
        result.require(
            "retrospective_pre_move",
            not retrospective_as_pre_move,
            f"evidence[{index}]",
            "Retrospective interpretation cannot be represented as confirmed pre-move knowledge.",
        )

    evidence_ids = set(evidence_records)
    allowed_by_lane = {
        "exposures_before_move": {
            HistoricalTemporalClassification.KNOWN_BEFORE_MOVE
        },
        "exposures_during_move": {
            HistoricalTemporalClassification.KNOWN_BEFORE_MOVE,
            HistoricalTemporalClassification.PUBLISHED_DURING_MOVE,
        },
    }
    for collection_name in (
        "exposures_before_move",
        "exposures_during_move",
    ):
        exposures = getattr(packet, collection_name)
        for index, exposure in enumerate(exposures):
            compatible = (
                exposure.exposure_type
                in EXPOSURE_CATEGORY_COMPATIBILITY[exposure.category]
            )
            result.require(
                "category_exposure_compatibility",
                compatible,
                f"{collection_name}[{index}]",
                "Exposure category and mechanism are incompatible.",
            )
            overlap = set(exposure.supporting_evidence_ids) & set(
                exposure.contradicting_evidence_ids
            )
            result.require(
                "exposure_reference_overlap",
                not overlap,
                f"{collection_name}[{index}]",
                "Evidence cannot support and contradict the same exposure.",
            )
            references = _evidence_references(exposure)
            dangling = set(references) - evidence_ids
            result.require(
                "dangling_evidence_references",
                not dangling,
                f"{collection_name}[{index}]",
                (
                    "Dangling exposure evidence references: "
                    + ", ".join(sorted(dangling))
                    + "."
                    if dangling
                    else ""
                ),
            )
            leaked = {
                reference
                for reference in references
                if reference in evidence_records
                and evidence_records[
                    reference
                ].temporal_classification
                not in allowed_by_lane[collection_name]
            }
            result.require(
                "future_leakage",
                not leaked,
                f"{collection_name}[{index}]",
                (
                    "Evidence unavailable in this historical feature lane: "
                    + ", ".join(sorted(leaked))
                    + "."
                    if leaked
                    else ""
                ),
            )

    event_id_set = set(event_ids)
    for index, record in enumerate(packet.evidence):
        related_event = record.evidence.related_event_id
        result.require(
            "dangling_event_references",
            related_event is None or related_event in event_id_set,
            f"evidence[{index}].related_event_id",
            f"Dangling historical event reference: {related_event!r}.",
        )

    for index, historical_event in enumerate(packet.material_events):
        event = historical_event.event
        event_publication = _temporal_point(event.publication_date)
        event_cutoff = _temporal_point(event.applicable_cutoff)
        event_provenance = (
            historical_event.role is HistoricalEventRole.UNAVAILABLE
            or (
                bool(event.source.strip())
                and bool(event.source_type.strip())
                and event_publication is not None
            )
        )
        result.require(
            "missing_provenance",
            event_provenance,
            f"material_events[{index}]",
            "Available historical events require source provenance.",
        )
        event_cutoff_valid = (
            historical_event.role is HistoricalEventRole.UNAVAILABLE
            or (
                event_publication is not None
                and event_cutoff is not None
                and event_publication <= event_cutoff
                and move_cutoff is not None
                and event_cutoff <= move_cutoff
            )
        )
        result.require(
            "historical_cutoff_violation",
            event_cutoff_valid,
            f"material_events[{index}].event.applicable_cutoff",
            "Event evidence exceeds the historical case cutoff.",
        )
        dangling = set(event.related_evidence_ids) - evidence_ids
        result.require(
            "dangling_evidence_references",
            not dangling,
            f"material_events[{index}].event.related_evidence_ids",
            (
                "Dangling event evidence references: "
                + ", ".join(sorted(dangling))
                + "."
                if dangling
                else ""
            ),
        )
        scheduled = _event_temporal_point(event)
        lag_valid = not (
            historical_event.role is HistoricalEventRole.INITIATING_CATALYST
            and scheduled is not None
            and end is not None
            and scheduled > end
            and not historical_event.lag_reasoning.strip()
        )
        result.require(
            "event_lag_reasoning",
            lag_valid,
            f"material_events[{index}].lag_reasoning",
            "A post-move event cannot be an initiating catalyst without explicit lag reasoning.",
        )

    regime_validation = validate_market_regime(
        packet.market_regime,
        evidence_ids=evidence_ids,
    )
    regime_references = set(packet.market_regime.supporting_evidence_ids)
    regime_dangling = regime_references - evidence_ids
    result.require(
        "dangling_evidence_references",
        not regime_dangling,
        "market_regime.supporting_evidence_ids",
        (
            "Dangling market-regime evidence references: "
            + ", ".join(sorted(regime_dangling))
            + "."
            if regime_dangling
            else ""
        ),
    )
    regime_leakage = {
        reference
        for reference in regime_references
        if reference in evidence_records
        and evidence_records[reference].temporal_classification
        not in {
            HistoricalTemporalClassification.KNOWN_BEFORE_MOVE,
            HistoricalTemporalClassification.PUBLISHED_DURING_MOVE,
        }
    }
    result.require(
        "future_leakage",
        not regime_leakage,
        "market_regime.supporting_evidence_ids",
        (
            "Market-regime evidence was not available before or during the "
            "historical move: "
            + ", ".join(sorted(regime_leakage))
            + "."
            if regime_leakage
            else ""
        ),
    )
    result.require(
        "market_regime_valid",
        regime_validation.is_valid,
        "market_regime",
        "The nested market regime failed deterministic validation.",
    )
    result.require(
        "missing_provenance",
        bool(packet.extraction_metadata),
        "extraction_metadata",
        "Historical extraction metadata cannot be empty.",
    )
    return result.result()


_CAUSAL_CERTAINTY_PATTERNS = (
    re.compile(r"\b(?:definitely|certainly|undeniably)\s+caused\b", re.I),
    re.compile(r"\bcertainly\s+happened\s+because\b", re.I),
    re.compile(r"\b(?:sole|only)\s+(?:cause|reason)\b", re.I),
    re.compile(r"\bguaranteed\b", re.I),
    re.compile(r"\bprove(?:s|d)?\s+that\b", re.I),
    re.compile(r"\b(?:the\s+)?chart\s+predicted\b", re.I),
    re.compile(r"\bmust\s+have\s+caused\b", re.I),
)


def _causal_text(analysis: HistoricalCausalAnalysis) -> str:
    values = (
        analysis.initiating_conditions
        + analysis.primary_drivers
        + analysis.secondary_drivers
        + analysis.amplifiers
        + analysis.dampeners
        + analysis.exposure_interactions
        + analysis.alternative_explanations
        + analysis.missing_evidence
        + analysis.retrospective_explanations
        + (
            analysis.market_regime_contribution,
            analysis.company_specific_contribution,
            analysis.sector_contribution,
            analysis.macro_contribution,
            analysis.positioning_contribution,
        )
    )
    return " ".join(values)


def validate_historical_causal_analysis(
    analysis: HistoricalCausalAnalysis,
    *,
    packet: HistoricalEvidencePacket | None = None,
) -> ValidationResult:
    if not isinstance(analysis, HistoricalCausalAnalysis):
        raise TypeError("analysis must be a HistoricalCausalAnalysis.")
    if packet is not None and not isinstance(packet, HistoricalEvidencePacket):
        raise TypeError("packet must be a HistoricalEvidencePacket or null.")
    result = _ValidationCollector()
    result.require(
        "schema_version",
        analysis.schema_version
        in {
            HISTORICAL_CAUSAL_SCHEMA_VERSION,
            *HISTORICAL_CAUSAL_LEGACY_SCHEMA_VERSIONS,
        },
        "schema_version",
        "Historical causal-analysis schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(analysis.content_hash)
        and analysis.content_hash
        == historical_causal_analysis_content_hash(analysis),
        "content_hash",
        "Historical causal-analysis content hash is missing or mismatched.",
    )
    if packet is not None:
        result.require(
            "case_reference",
            analysis.case_id == packet.historical_move.case_id,
            "case_id",
            "Causal analysis case_id does not match the historical packet.",
        )
    else:
        result.require(
            "case_reference",
            bool(analysis.case_id.strip()),
            "case_id",
            "Causal analysis case_id is required.",
        )

    driver_count = len(analysis.primary_drivers) + len(
        analysis.secondary_drivers
    )
    drivers_valid = bool(analysis.primary_drivers) and (
        not analysis.no_single_catalyst or driver_count >= 2
    )
    result.require(
        "missing_causal_drivers",
        drivers_valid,
        "primary_drivers",
        "At least one primary driver is required; multi-factor cases require two drivers.",
    )
    result.require(
        "no_transmission_mechanism",
        bool(analysis.transmission_mechanisms),
        "transmission_mechanisms",
        "At least one controlled transmission mechanism is required.",
    )
    certainty_matches = [
        pattern.pattern
        for pattern in _CAUSAL_CERTAINTY_PATTERNS
        if pattern.search(_causal_text(analysis))
    ]
    result.require(
        "unsupported_causal_certainty",
        not certainty_matches,
        "primary_drivers",
        "Causal analysis contains unsupported certainty language.",
    )
    result.require(
        "causal_contradicting_evidence",
        bool(analysis.contradicting_evidence_ids),
        "contradicting_evidence_ids",
        "Historical causal analysis must retain contradicting evidence.",
    )
    result.require(
        "invalid_confidence",
        analysis.confidence in QUALITATIVE_CONFIDENCE_VALUES,
        "confidence",
        "Causal confidence must use the qualitative vocabulary.",
    )
    metadata_valid = (
        bool(analysis.model_name.strip())
        and bool(analysis.prompt_version.strip())
    )
    result.require(
        "missing_provenance",
        metadata_valid,
        "model_name",
        "Analyst/model identity and prompt version are required.",
    )
    generated = _temporal_point(analysis.generated_at)
    applicable = _temporal_point(analysis.applicable_cutoff)
    cutoff_valid = (
        generated is not None
        and applicable is not None
        and applicable <= generated
    )
    if packet is not None:
        move_cutoff = _temporal_point(
            packet.historical_move.market_data_cutoff
        )
        cutoff_valid = (
            cutoff_valid
            and move_cutoff is not None
            and applicable is not None
            and applicable <= move_cutoff
        )
    result.require(
        "historical_cutoff_violation",
        cutoff_valid,
        "applicable_cutoff",
        "Causal analysis exceeds its historical evidence cutoff.",
    )

    if packet is not None:
        evidence_ids = {
            item.evidence.evidence_id for item in packet.evidence
        }
        evidence_by_id = {
            item.evidence.evidence_id: item for item in packet.evidence
        }
        event_ids = {
            item.event.event_id for item in packet.material_events
        }
        event_by_id = {
            item.event.event_id: item for item in packet.material_events
        }
        dangling_evidence = (
            set(
                analysis.contradicting_evidence_ids
                + analysis.supporting_evidence_ids
                + analysis.initiating_condition_evidence_ids
                + analysis.retrospective_evidence_ids
            )
            - evidence_ids
        )
        dangling_events = set(analysis.triggering_events) - event_ids
        result.require(
            "dangling_evidence_references",
            not dangling_evidence,
            "contradicting_evidence_ids",
            (
                "Dangling causal evidence references: "
                + ", ".join(sorted(dangling_evidence))
                + "."
                if dangling_evidence
                else ""
            ),
        )
        if analysis.schema_version == HISTORICAL_CAUSAL_SCHEMA_VERSION:
            initiating_ids = set(analysis.initiating_condition_evidence_ids)
            retrospective_ids = set(analysis.retrospective_evidence_ids)
            supporting_ids = set(analysis.supporting_evidence_ids)
            initiation_linkage_valid = (
                (not analysis.initiating_conditions or bool(initiating_ids))
                and initiating_ids <= supporting_ids
            )
            retrospective_linkage_valid = (
                (
                    not analysis.retrospective_explanations
                    or bool(retrospective_ids)
                )
                and retrospective_ids <= supporting_ids
            )
            result.require(
                "causal_evidence_linkage",
                bool(supporting_ids)
                and initiation_linkage_valid
                and retrospective_linkage_valid,
                "supporting_evidence_ids",
                "Causal claims require supporting evidence; initiating and retrospective claims require explicit lane-specific linkage.",
            )
            initiating_leakage = {
                evidence_id
                for evidence_id in initiating_ids
                if evidence_id in evidence_by_id
                and evidence_by_id[
                    evidence_id
                ].temporal_classification
                is not HistoricalTemporalClassification.KNOWN_BEFORE_MOVE
            }
            retrospective_misclassification = {
                evidence_id
                for evidence_id in retrospective_ids
                if evidence_id in evidence_by_id
                and evidence_by_id[
                    evidence_id
                ].temporal_classification
                not in {
                    HistoricalTemporalClassification.KNOWN_ONLY_AFTER_MOVE,
                    HistoricalTemporalClassification.RETROSPECTIVE_INTERPRETATION,
                }
            }
            post_move_support_without_label = {
                evidence_id
                for evidence_id in supporting_ids
                if evidence_id in evidence_by_id
                and evidence_by_id[
                    evidence_id
                ].temporal_classification
                in {
                    HistoricalTemporalClassification.KNOWN_ONLY_AFTER_MOVE,
                    HistoricalTemporalClassification.RETROSPECTIVE_INTERPRETATION,
                }
                and evidence_id not in retrospective_ids
            }
            unavailable_factual_references = {
                evidence_id
                for evidence_id in supporting_ids
                | set(analysis.contradicting_evidence_ids)
                if evidence_id in evidence_by_id
                and evidence_by_id[
                    evidence_id
                ].temporal_classification
                is HistoricalTemporalClassification.UNAVAILABLE
            }
            result.require(
                "future_leakage",
                not initiating_leakage,
                "initiating_condition_evidence_ids",
                (
                    "Initiating conditions use evidence unavailable before the move: "
                    + ", ".join(sorted(initiating_leakage))
                    + "."
                    if initiating_leakage
                    else ""
                ),
            )
            result.require(
                "retrospective_pre_move",
                not retrospective_misclassification,
                "retrospective_evidence_ids",
                (
                    "Retrospective explanations use evidence from an earlier temporal lane: "
                    + ", ".join(sorted(retrospective_misclassification))
                    + "."
                    if retrospective_misclassification
                    else ""
                ),
            )
            result.require(
                "causal_temporal_labeling",
                not post_move_support_without_label
                and not unavailable_factual_references,
                "supporting_evidence_ids",
                (
                    "Post-move support is not labeled retrospective: "
                    + ", ".join(sorted(post_move_support_without_label))
                    + ". "
                    if post_move_support_without_label
                    else ""
                )
                + (
                    "Unavailable evidence is used as factual support or contradiction: "
                    + ", ".join(sorted(unavailable_factual_references))
                    + "."
                    if unavailable_factual_references
                    else ""
                ),
            )
        else:
            result.warning(
                "legacy_causal_evidence_linkage",
                "schema_version",
                "Legacy causal payload is readable but lacks Phase 4 typed evidence-linkage fields.",
            )
        invalid_triggering_events = {
            event_id
            for event_id in analysis.triggering_events
            if event_id in event_by_id
            and (
                event_by_id[event_id].role
                in {
                    HistoricalEventRole.RETROSPECTIVE_CONTEXT,
                    HistoricalEventRole.UNAVAILABLE,
                }
                or (
                    _event_temporal_point(event_by_id[event_id].event)
                    is not None
                    and _temporal_point(packet.historical_move.end_at)
                    is not None
                    and _event_temporal_point(event_by_id[event_id].event)
                    > _temporal_point(packet.historical_move.end_at)
                )
            )
        }
        result.require(
            "triggering_event_temporality",
            not invalid_triggering_events,
            "triggering_events",
            (
                "Triggering events use retrospective or unavailable event roles: "
                + ", ".join(sorted(invalid_triggering_events))
                + "."
                if invalid_triggering_events
                else ""
            ),
        )
        result.require(
            "dangling_event_references",
            not dangling_events,
            "triggering_events",
            (
                "Dangling causal event references: "
                + ", ".join(sorted(dangling_events))
                + "."
                if dangling_events
                else ""
            ),
        )
    return result.result()


def validate_historical_similarity_features(
    features: HistoricalSimilarityFeatures,
) -> ValidationResult:
    if not isinstance(features, HistoricalSimilarityFeatures):
        raise TypeError("features must be HistoricalSimilarityFeatures.")
    result = _ValidationCollector()
    result.require(
        "schema_version",
        features.schema_version == HISTORICAL_SIMILARITY_SCHEMA_VERSION,
        "schema_version",
        "Historical similarity schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(features.content_hash)
        and features.content_hash
        == historical_similarity_features_content_hash(features),
        "content_hash",
        "Historical similarity-feature content hash is missing or mismatched.",
    )
    compatibility = (
        all(
            item
            in EXPOSURE_CATEGORY_COMPATIBILITY[ExposureCategory.COMPANY]
            for item in features.company_exposure_types
        )
        and all(
            item
            in EXPOSURE_CATEGORY_COMPATIBILITY[
                ExposureCategory.SECTOR_AND_COMPETITORS
            ]
            for item in features.sector_exposure_types
        )
        and all(
            item
            in EXPOSURE_CATEGORY_COMPATIBILITY[
                ExposureCategory.MACRO_AND_CROSS_ASSET
            ]
            for item in features.macro_exposure_types
        )
    )
    result.require(
        "similarity_feature_compatibility",
        compatibility,
        "company_exposure_types",
        "Similarity exposure mechanisms must match their declared category.",
    )
    duplicates = (
        len(features.company_exposure_types)
        != len(set(features.company_exposure_types))
        or len(features.sector_exposure_types)
        != len(set(features.sector_exposure_types))
        or len(features.macro_exposure_types)
        != len(set(features.macro_exposure_types))
        or len(features.event_types) != len(set(features.event_types))
        or len(features.transmission_mechanisms)
        != len(set(features.transmission_mechanisms))
    )
    result.require(
        "similarity_feature_duplicates",
        not duplicates,
        "company_exposure_types",
        "Normalized similarity features must not contain duplicates.",
    )
    normalized_event_types = all(
        bool(value)
        and value == re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.casefold())).strip("_")
        for value in features.event_types
    )
    values_valid = (
        bool(features.case_id.strip())
        and bool(features.technical_structure.strip())
        and normalized_event_types
    )
    result.require(
        "similarity_feature_values",
        values_valid,
        "technical_structure",
        "Similarity identifiers and categorical strings must be normalized.",
    )
    return result.result()


def finalize_historical_evidence_packet(
    packet: HistoricalEvidencePacket,
) -> HistoricalEvidencePacket:
    if not isinstance(packet, HistoricalEvidencePacket):
        raise TypeError("packet must be a HistoricalEvidencePacket.")
    seed = replace(
        packet,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(
        seed,
        content_hash=historical_evidence_packet_content_hash(seed),
    )
    validation = validate_historical_evidence_packet(seed)
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=historical_evidence_packet_content_hash(finalized),
    )
    repeated = validate_historical_evidence_packet(finalized)
    if repeated != validation:
        raise RuntimeError(
            "Historical evidence-packet validation did not stabilize."
        )
    return finalized


def finalize_historical_causal_analysis(
    analysis: HistoricalCausalAnalysis,
    *,
    packet: HistoricalEvidencePacket | None = None,
) -> HistoricalCausalAnalysis:
    if not isinstance(analysis, HistoricalCausalAnalysis):
        raise TypeError("analysis must be a HistoricalCausalAnalysis.")
    seed = replace(
        analysis,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(
        seed,
        content_hash=historical_causal_analysis_content_hash(seed),
    )
    validation = validate_historical_causal_analysis(seed, packet=packet)
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=historical_causal_analysis_content_hash(finalized),
    )
    repeated = validate_historical_causal_analysis(
        finalized,
        packet=packet,
    )
    if repeated != validation:
        raise RuntimeError(
            "Historical causal-analysis validation did not stabilize."
        )
    return finalized


def validate_historical_case(
    packet: HistoricalEvidencePacket,
    *,
    causal_analysis: HistoricalCausalAnalysis | None = None,
    similarity_features: HistoricalSimilarityFeatures | None = None,
) -> Mapping[str, ValidationResult]:
    """Validate related historical contracts without mutating any input."""
    results: dict[str, ValidationResult] = {
        "evidence_packet": validate_historical_evidence_packet(packet)
    }
    if causal_analysis is not None:
        results["causal_analysis"] = validate_historical_causal_analysis(
            causal_analysis,
            packet=packet,
        )
    if similarity_features is not None:
        results["similarity_features"] = (
            validate_historical_similarity_features(similarity_features)
        )
        case_matches = similarity_features.case_id == packet.historical_move.case_id
        if not case_matches:
            current = results["similarity_features"]
            issue = ValidationIssue(
                code="case_reference",
                severity="error",
                path="case_id",
                message=(
                    "Similarity features case_id does not match the historical "
                    "packet."
                ),
            )
            results["similarity_features"] = replace(
                current,
                is_valid=False,
                errors=current.errors + (issue,),
                failed_rules=tuple(
                    dict.fromkeys(current.failed_rules + ("case_reference",))
                ),
            )
    return MappingProxyType(results)


@runtime_checkable
class HistoricalCaseExtractor(Protocol):
    """Provider-neutral interface for a future historical extractor.

    Implementations will receive an immutable dated move and supplied context,
    normalize and temporally classify evidence, map evidence to specific
    exposures, assemble a draft packet, and validate/hash it. This phase
    intentionally provides no implementation.
    """

    def extract(
        self,
        historical_move: HistoricalMarketMove,
        supplied_context: Mapping[str, Any],
    ) -> HistoricalEvidencePacket: ...


__all__ = [
    "DurationBucket",
    "HISTORICAL_CANONICALIZATION_VERSION",
    "HISTORICAL_CASE_SCHEMA_VERSION",
    "HISTORICAL_CASE_VALIDATION_VERSION",
    "HISTORICAL_CAUSAL_SCHEMA_VERSION",
    "HISTORICAL_CAUSAL_LEGACY_SCHEMA_VERSIONS",
    "HISTORICAL_HASH_ALGORITHM",
    "HISTORICAL_SIMILARITY_SCHEMA_VERSION",
    "HistoricalAnalystType",
    "HistoricalCaseExtractor",
    "HistoricalCaseStatus",
    "HistoricalCausalAnalysis",
    "HistoricalEvidenceItem",
    "HistoricalEvidencePacket",
    "HistoricalEventRole",
    "HistoricalMarketMove",
    "HistoricalMaterialEvent",
    "HistoricalMoveType",
    "HistoricalSimilarityFeatures",
    "HistoricalTemporalClassification",
    "MagnitudeBucket",
    "MarketRegime",
    "MarketRegimeState",
    "RelativePerformanceBucket",
    "TransmissionMechanism",
    "finalize_historical_causal_analysis",
    "finalize_historical_evidence_packet",
    "historical_causal_analysis_content_hash",
    "historical_evidence_packet_content_hash",
    "historical_market_move_content_hash",
    "historical_similarity_features_content_hash",
    "market_regime_content_hash",
    "validate_historical_case",
    "validate_historical_causal_analysis",
    "validate_historical_evidence_packet",
    "validate_historical_market_move",
    "validate_historical_similarity_features",
    "validate_market_regime",
]
