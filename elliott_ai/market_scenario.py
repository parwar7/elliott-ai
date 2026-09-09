"""Typed, deterministic contracts for the post-technical Market Scenario Engine.

This module is intentionally side-effect free. It does not collect research,
call a model, mutate Elliott analysis, persist records, or render reports.

Schema 2.0.0 intentionally breaks the provisional 1.1 exposure shape. The five
broad research lanes are now ExposureCategory values, while ExposureType holds
specific economic or operational mechanisms. Old broad values are never
silently accepted as mechanisms.
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
from typing import Any, Mapping, Sequence


MARKET_SCENARIO_SCHEMA_VERSION = "market-scenario-report-2.0.0"
MARKET_SCENARIO_VALIDATION_VERSION = "market-scenario-validation-1.0.0"
CANONICALIZATION_VERSION = "canonical-json-sha256-1.0.0"
HASH_ALGORITHM = "sha256"

QUALITATIVE_CONFIDENCE_VALUES = frozenset(
    {"high", "moderate", "low", "insufficient_evidence", "unavailable"}
)
EXPOSURE_MATERIALITY_VALUES = frozenset(
    {"material", "not_material", "unknown", "unavailable"}
)
EXPOSURE_DIRECTION_VALUES = frozenset(
    {"positive", "negative", "mixed", "neutral", "unknown", "incomparable"}
)


class EvidenceStatus(StrEnum):
    CONFIRMED = "confirmed"
    SCHEDULED = "scheduled"
    INTERPRETATION = "interpretation"
    HYPOTHETICAL = "hypothetical"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class EvidenceImplication(StrEnum):
    SUPPORTIVE = "supportive"
    CONTRADICTORY = "contradictory"
    NEUTRAL = "neutral"
    INCOMPARABLE = "incomparable"


class ScenarioDirection(StrEnum):
    """The direction vocabulary already used by degree-resolution output."""

    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"
    UNKNOWN = "unknown"


class ExposureCategory(StrEnum):
    """Broad research coverage lanes from the Market Scenario specification."""

    COMPANY = "company"
    SECTOR_AND_COMPETITORS = "sector_and_competitors"
    MACRO_AND_CROSS_ASSET = "macro_and_cross_asset"
    VALUATION_AND_POSITIONING = "valuation_and_positioning"
    EVENT_CALENDAR = "event_calendar"


class ExposureType(StrEnum):
    """Specific reusable economic and operational exposure mechanisms."""

    REVENUE_GROWTH_RISK = "revenue_growth_risk"
    REVENUE_VISIBILITY = "revenue_visibility"
    EARNINGS_RISK = "earnings_risk"
    GUIDANCE_RISK = "guidance_risk"
    MARGIN_PRESSURE = "margin_pressure"
    CASH_BURN_RISK = "cash_burn_risk"
    LIQUIDITY_RISK = "liquidity_risk"
    FINANCING_RISK = "financing_risk"
    DILUTION_RISK = "dilution_risk"
    DEBT_RISK = "debt_risk"
    EXECUTION_RISK = "execution_risk"
    OPERATIONAL_RISK = "operational_risk"
    DEVELOPMENT_DELAY_RISK = "development_delay_risk"
    SUPPLY_CHAIN_RISK = "supply_chain_risk"
    CUSTOMER_CONCENTRATION_RISK = "customer_concentration_risk"
    CONTRACT_RISK = "contract_risk"
    REGULATORY_RISK = "regulatory_risk"
    LITIGATION_RISK = "litigation_risk"
    MANAGEMENT_RISK = "management_risk"
    ACQUISITION_INTEGRATION_RISK = "acquisition_integration_risk"

    SECTOR_DEMAND_RISK = "sector_demand_risk"
    SECTOR_VALUATION_RISK = "sector_valuation_risk"
    COMPETITIVE_PRESSURE = "competitive_pressure"
    PRICING_PRESSURE = "pricing_pressure"
    MARKET_SHARE_RISK = "market_share_risk"
    COMPETITOR_EXECUTION_RISK = "competitor_execution_risk"
    SECTOR_REGULATORY_RISK = "sector_regulatory_risk"
    GOVERNMENT_SPENDING_EXPOSURE = "government_spending_exposure"
    SECTOR_SUPPLY_CHAIN_RISK = "sector_supply_chain_risk"
    SECTOR_SENTIMENT_RISK = "sector_sentiment_risk"

    INTEREST_RATE_SENSITIVITY = "interest_rate_sensitivity"
    INFLATION_SENSITIVITY = "inflation_sensitivity"
    RECESSION_SENSITIVITY = "recession_sensitivity"
    LIQUIDITY_CONDITIONS = "liquidity_conditions"
    CREDIT_CONDITIONS = "credit_conditions"
    EQUITY_RISK_APPETITE = "equity_risk_appetite"
    SMALL_CAP_SENSITIVITY = "small_cap_sensitivity"
    GROWTH_STOCK_SENSITIVITY = "growth_stock_sensitivity"
    CURRENCY_EXPOSURE = "currency_exposure"
    COMMODITY_EXPOSURE = "commodity_exposure"
    GEOPOLITICAL_EXPOSURE = "geopolitical_exposure"

    VALUATION_COMPRESSION = "valuation_compression"
    VALUATION_EXPANSION = "valuation_expansion"
    GROWTH_EXPECTATION_RISK = "growth_expectation_risk"
    ANALYST_REVISION_RISK = "analyst_revision_risk"
    INSTITUTIONAL_POSITIONING = "institutional_positioning"
    INSIDER_POSITIONING = "insider_positioning"
    SHORT_INTEREST_RISK = "short_interest_risk"
    OPTIONS_POSITIONING = "options_positioning"
    CROWDED_TRADE_RISK = "crowded_trade_risk"
    OWNERSHIP_CONCENTRATION_RISK = "ownership_concentration_risk"
    MOMENTUM_UNWIND_RISK = "momentum_unwind_risk"

    EARNINGS_EVENT_RISK = "earnings_event_risk"
    PRODUCT_MILESTONE_RISK = "product_milestone_risk"
    LAUNCH_EVENT_RISK = "launch_event_risk"
    REGULATORY_EVENT_RISK = "regulatory_event_risk"
    CONTRACT_AWARD_RISK = "contract_award_risk"
    FINANCING_EVENT_RISK = "financing_event_risk"
    CENTRAL_BANK_EVENT_RISK = "central_bank_event_risk"
    ECONOMIC_RELEASE_RISK = "economic_release_risk"
    INDEX_EVENT_RISK = "index_event_risk"
    SELL_THE_NEWS_RISK = "sell_the_news_risk"
    PRE_EVENT_UNCERTAINTY = "pre_event_uncertainty"


class ExposureCoverageState(StrEnum):
    EXPOSURES_IDENTIFIED = "exposures_identified"
    RESEARCHED_NO_MATERIAL_EXPOSURE = "researched_no_material_exposure"
    UNAVAILABLE = "unavailable"
    UNDECLARED = "undeclared"


EXPOSURE_CATEGORY_COMPATIBILITY: Mapping[
    ExposureCategory, frozenset[ExposureType]
] = MappingProxyType(
    {
        ExposureCategory.COMPANY: frozenset(
            {
                ExposureType.REVENUE_GROWTH_RISK,
                ExposureType.REVENUE_VISIBILITY,
                ExposureType.EARNINGS_RISK,
                ExposureType.GUIDANCE_RISK,
                ExposureType.MARGIN_PRESSURE,
                ExposureType.CASH_BURN_RISK,
                ExposureType.LIQUIDITY_RISK,
                ExposureType.FINANCING_RISK,
                ExposureType.DILUTION_RISK,
                ExposureType.DEBT_RISK,
                ExposureType.EXECUTION_RISK,
                ExposureType.OPERATIONAL_RISK,
                ExposureType.DEVELOPMENT_DELAY_RISK,
                ExposureType.SUPPLY_CHAIN_RISK,
                ExposureType.CUSTOMER_CONCENTRATION_RISK,
                ExposureType.CONTRACT_RISK,
                ExposureType.REGULATORY_RISK,
                ExposureType.LITIGATION_RISK,
                ExposureType.MANAGEMENT_RISK,
                ExposureType.ACQUISITION_INTEGRATION_RISK,
            }
        ),
        ExposureCategory.SECTOR_AND_COMPETITORS: frozenset(
            {
                ExposureType.SECTOR_DEMAND_RISK,
                ExposureType.SECTOR_VALUATION_RISK,
                ExposureType.COMPETITIVE_PRESSURE,
                ExposureType.PRICING_PRESSURE,
                ExposureType.MARKET_SHARE_RISK,
                ExposureType.COMPETITOR_EXECUTION_RISK,
                ExposureType.SECTOR_REGULATORY_RISK,
                ExposureType.GOVERNMENT_SPENDING_EXPOSURE,
                ExposureType.SECTOR_SUPPLY_CHAIN_RISK,
                ExposureType.SECTOR_SENTIMENT_RISK,
            }
        ),
        ExposureCategory.MACRO_AND_CROSS_ASSET: frozenset(
            {
                ExposureType.INTEREST_RATE_SENSITIVITY,
                ExposureType.INFLATION_SENSITIVITY,
                ExposureType.RECESSION_SENSITIVITY,
                ExposureType.LIQUIDITY_CONDITIONS,
                ExposureType.CREDIT_CONDITIONS,
                ExposureType.EQUITY_RISK_APPETITE,
                ExposureType.SMALL_CAP_SENSITIVITY,
                ExposureType.GROWTH_STOCK_SENSITIVITY,
                ExposureType.CURRENCY_EXPOSURE,
                ExposureType.COMMODITY_EXPOSURE,
                ExposureType.GEOPOLITICAL_EXPOSURE,
            }
        ),
        ExposureCategory.VALUATION_AND_POSITIONING: frozenset(
            {
                ExposureType.VALUATION_COMPRESSION,
                ExposureType.VALUATION_EXPANSION,
                ExposureType.GROWTH_EXPECTATION_RISK,
                ExposureType.ANALYST_REVISION_RISK,
                ExposureType.INSTITUTIONAL_POSITIONING,
                ExposureType.INSIDER_POSITIONING,
                ExposureType.SHORT_INTEREST_RISK,
                ExposureType.OPTIONS_POSITIONING,
                ExposureType.CROWDED_TRADE_RISK,
                ExposureType.OWNERSHIP_CONCENTRATION_RISK,
                ExposureType.MOMENTUM_UNWIND_RISK,
            }
        ),
        ExposureCategory.EVENT_CALENDAR: frozenset(
            {
                ExposureType.EARNINGS_EVENT_RISK,
                ExposureType.PRODUCT_MILESTONE_RISK,
                ExposureType.LAUNCH_EVENT_RISK,
                ExposureType.REGULATORY_EVENT_RISK,
                ExposureType.CONTRACT_AWARD_RISK,
                ExposureType.FINANCING_EVENT_RISK,
                ExposureType.CENTRAL_BANK_EVENT_RISK,
                ExposureType.ECONOMIC_RELEASE_RISK,
                ExposureType.INDEX_EVENT_RISK,
                ExposureType.SELL_THE_NEWS_RISK,
                ExposureType.PRE_EVENT_UNCERTAINTY,
            }
        ),
    }
)
EXPOSURE_TYPE_CATEGORY: Mapping[ExposureType, ExposureCategory] = MappingProxyType(
    {
        exposure_type: category
        for category, exposure_types in EXPOSURE_CATEGORY_COMPATIBILITY.items()
        for exposure_type in exposure_types
    }
)


class AdversarialRecommendation(StrEnum):
    KEEP = "keep"
    REVISE = "revise"
    DOWNGRADE = "downgrade"
    REJECT = "reject"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EventDatePrecision(StrEnum):
    EXACT = "exact"
    DATE_ONLY = "date_only"
    MONTH = "month"
    QUARTER = "quarter"
    WINDOW = "window"
    UNKNOWN = "unknown"


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


def _json_compatible(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, _JsonContract):
        return value.to_dict()
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
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON contract mappings must use string keys.")
        frozen = {
            key: _freeze_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
        return MappingProxyType(frozen)
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


def _normalize_timestamp(value: str | datetime, *, field_name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    else:
        raise TypeError(f"{field_name} must be a timestamp string or datetime.")
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalize_optional_temporal(
    value: str | date | datetime | None, *, field_name: str
) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _normalize_timestamp(value, field_name=field_name)
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be an ISO date, timestamp, or null.")
    raw = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO date or timestamp.") from exc
        return raw
    return _normalize_timestamp(raw, field_name=field_name)


def _normalize_scheduled_at(
    value: str | date | datetime | None,
    *,
    precision: EventDatePrecision,
) -> str | None:
    if value is None:
        return None
    if precision is EventDatePrecision.MONTH and isinstance(value, str):
        raw = value.strip()
        match = re.fullmatch(r"(\d{4})-(\d{2})", raw)
        if match and 1 <= int(match.group(2)) <= 12:
            return raw
        raise ValueError("scheduled_at must be YYYY-MM for month precision.")
    if precision is EventDatePrecision.QUARTER and isinstance(value, str):
        raw = value.strip().upper()
        if re.fullmatch(r"\d{4}-Q[1-4]", raw):
            return raw
        raise ValueError("scheduled_at must be YYYY-Q1 through YYYY-Q4.")
    if precision is EventDatePrecision.WINDOW and isinstance(value, str):
        raw = re.sub(r"\s+", " ", value.strip())
        if raw:
            return raw
        raise ValueError("scheduled_at window cannot be empty.")
    return _normalize_optional_temporal(value, field_name="scheduled_at")


def _temporal_point(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(date.fromisoformat(value), time.min, timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc)


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _string_tuple(value: Sequence[str] | None, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(value)
    if not all(isinstance(item, str) for item in result):
        raise TypeError(f"{field_name} must contain only strings.")
    return result


def _model_tuple(
    value: Sequence[Any] | None, model_type: type[Any], *, field_name: str
) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    result = tuple(value)
    if not all(isinstance(item, model_type) for item in result):
        raise TypeError(f"{field_name} must contain only {model_type.__name__} values.")
    return result


def _mapping(value: Mapping[str, Any], *, model_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{model_name}.from_dict requires a mapping.")
    return dict(value)


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], *, model_name: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{model_name} contains unknown fields: {', '.join(unknown)}.")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_compatible(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class ValidationIssue(_JsonContract):
    code: str
    severity: str
    path: str
    message: str

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning"}:
            raise ValueError("ValidationIssue.severity must be error or warning.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ValidationIssue":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ValidationResult(_JsonContract):
    is_valid: bool
    score: float
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    failed_rules: tuple[str, ...] = ()
    passed_rules: tuple[str, ...] = ()
    frozen_hash_before: str = ""
    frozen_hash_after: str = ""
    validation_version: str = MARKET_SCENARIO_VALIDATION_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "errors",
            _model_tuple(self.errors, ValidationIssue, field_name="errors"),
        )
        object.__setattr__(
            self,
            "warnings",
            _model_tuple(self.warnings, ValidationIssue, field_name="warnings"),
        )
        object.__setattr__(
            self,
            "failed_rules",
            _string_tuple(self.failed_rules, field_name="failed_rules"),
        )
        object.__setattr__(
            self,
            "passed_rules",
            _string_tuple(self.passed_rules, field_name="passed_rules"),
        )

    @classmethod
    def unvalidated(cls) -> "ValidationResult":
        return cls(
            is_valid=False,
            score=0.0,
            failed_rules=("not_validated",),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ValidationResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        raw["errors"] = tuple(
            ValidationIssue.from_dict(item) for item in raw.get("errors", ())
        )
        raw["warnings"] = tuple(
            ValidationIssue.from_dict(item) for item in raw.get("warnings", ())
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class FrozenTechnicalPremise(_JsonContract):
    resolution_id: str
    symbol: str
    analysed_at: str
    market_data_cutoff: str
    technical_structure: Any
    direction: ScenarioDirection
    technical_confidence: Any
    technical_readiness: Any
    source_hashes: Mapping[str, str]
    exchange: str | None = None
    current_wave_or_phase: str | None = None
    target_low: float | None = None
    target_high: float | None = None
    invalidation_level: float | None = None
    expected_completion_start: str | None = None
    expected_completion_end: str | None = None
    supporting_technical_evidence: tuple[str, ...] = ()
    alternative_technical_hypotheses: tuple[str, ...] = ()
    frozen_content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "analysed_at",
            _normalize_timestamp(self.analysed_at, field_name="analysed_at"),
        )
        object.__setattr__(
            self,
            "market_data_cutoff",
            _normalize_timestamp(
                self.market_data_cutoff, field_name="market_data_cutoff"
            ),
        )
        object.__setattr__(
            self,
            "direction",
            _enum_value(
                self.direction, ScenarioDirection, field_name="direction"
            ),
        )
        object.__setattr__(
            self, "technical_structure", _freeze_json(self.technical_structure)
        )
        object.__setattr__(
            self, "technical_confidence", _freeze_json(self.technical_confidence)
        )
        object.__setattr__(
            self, "technical_readiness", _freeze_json(self.technical_readiness)
        )
        if not isinstance(self.source_hashes, Mapping):
            raise TypeError("source_hashes must be a mapping of names to hashes.")
        source_hashes = dict(self.source_hashes)
        if not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in source_hashes.items()
        ):
            raise TypeError("source_hashes must contain only string keys and values.")
        object.__setattr__(
            self,
            "source_hashes",
            MappingProxyType(dict(sorted(source_hashes.items()))),
        )
        object.__setattr__(
            self,
            "expected_completion_start",
            _normalize_optional_temporal(
                self.expected_completion_start,
                field_name="expected_completion_start",
            ),
        )
        object.__setattr__(
            self,
            "expected_completion_end",
            _normalize_optional_temporal(
                self.expected_completion_end,
                field_name="expected_completion_end",
            ),
        )
        object.__setattr__(
            self,
            "supporting_technical_evidence",
            _string_tuple(
                self.supporting_technical_evidence,
                field_name="supporting_technical_evidence",
            ),
        )
        object.__setattr__(
            self,
            "alternative_technical_hypotheses",
            _string_tuple(
                self.alternative_technical_hypotheses,
                field_name="alternative_technical_hypotheses",
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "FrozenTechnicalPremise":
        supplied_hash = values.pop("frozen_content_hash", "")
        if supplied_hash:
            raise ValueError("create() calculates frozen_content_hash; do not supply it.")
        premise = cls(**values, frozen_content_hash="")
        return replace(
            premise,
            frozen_content_hash=frozen_technical_premise_content_hash(premise),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FrozenTechnicalPremise":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class EvidenceItem(_JsonContract):
    evidence_id: str
    category: str
    claim: str
    source: str
    source_type: str
    provider: str
    publication_date: str | None
    retrieval_timestamp: str
    applicable_cutoff: str
    status: EvidenceStatus
    implication: EvidenceImplication
    confidence: str
    relevance_to_technical_scenario: str
    related_event_id: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "publication_date",
            _normalize_optional_temporal(
                self.publication_date, field_name="publication_date"
            ),
        )
        object.__setattr__(
            self,
            "retrieval_timestamp",
            _normalize_timestamp(
                self.retrieval_timestamp, field_name="retrieval_timestamp"
            ),
        )
        object.__setattr__(
            self,
            "applicable_cutoff",
            _normalize_timestamp(
                self.applicable_cutoff, field_name="applicable_cutoff"
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(self.status, EvidenceStatus, field_name="status"),
        )
        object.__setattr__(
            self,
            "implication",
            _enum_value(
                self.implication, EvidenceImplication, field_name="implication"
            ),
        )
        object.__setattr__(
            self, "warnings", _string_tuple(self.warnings, field_name="warnings")
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceItem":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ExposureItem(_JsonContract):
    exposure_id: str
    category: ExposureCategory
    exposure_type: ExposureType
    summary: str
    materiality: str
    direction: str
    possible_magnitude: str | None
    time_horizon: str | None
    status: EvidenceStatus
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    related_technical_outcomes: tuple[str, ...]
    confidence: str = "unavailable"
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "category",
            _enum_value(self.category, ExposureCategory, field_name="category"),
        )
        object.__setattr__(
            self,
            "exposure_type",
            _enum_value(
                self.exposure_type, ExposureType, field_name="exposure_type"
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(self.status, EvidenceStatus, field_name="status"),
        )
        for name in (
            "supporting_evidence_ids",
            "contradicting_evidence_ids",
            "related_technical_outcomes",
            "assumptions",
            "limitations",
        ):
            object.__setattr__(
                self, name, _string_tuple(getattr(self, name), field_name=name)
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExposureItem":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ExposureCategoryCoverage(_JsonContract):
    category: ExposureCategory
    state: ExposureCoverageState
    exposure_ids: tuple[str, ...] = ()
    unmapped_evidence_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "category",
            _enum_value(self.category, ExposureCategory, field_name="category"),
        )
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, ExposureCoverageState, field_name="state"),
        )
        for name in ("exposure_ids", "unmapped_evidence_ids", "notes"):
            object.__setattr__(
                self, name, _string_tuple(getattr(self, name), field_name=name)
            )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "ExposureCategoryCoverage":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ExposureMappingWarning(_JsonContract):
    code: str
    evidence_id: str
    message: str
    category: ExposureCategory | None = None
    requested_exposure_type: str | None = None

    def __post_init__(self) -> None:
        if self.category is not None:
            object.__setattr__(
                self,
                "category",
                _enum_value(
                    self.category, ExposureCategory, field_name="category"
                ),
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExposureMappingWarning":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MaterialEvent(_JsonContract):
    event_id: str
    event_type: str
    title: str
    scheduled_at: str | None
    date_precision: EventDatePrecision
    status: EvidenceStatus
    source: str
    source_type: str
    publication_date: str | None
    retrieval_timestamp: str
    applicable_cutoff: str
    related_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        precision = _enum_value(
            self.date_precision,
            EventDatePrecision,
            field_name="date_precision",
        )
        object.__setattr__(self, "date_precision", precision)
        object.__setattr__(
            self,
            "scheduled_at",
            _normalize_scheduled_at(
                self.scheduled_at,
                precision=precision,
            ),
        )
        object.__setattr__(
            self,
            "publication_date",
            _normalize_optional_temporal(
                self.publication_date, field_name="publication_date"
            ),
        )
        object.__setattr__(
            self,
            "retrieval_timestamp",
            _normalize_timestamp(
                self.retrieval_timestamp, field_name="retrieval_timestamp"
            ),
        )
        object.__setattr__(
            self,
            "applicable_cutoff",
            _normalize_timestamp(
                self.applicable_cutoff, field_name="applicable_cutoff"
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(self.status, EvidenceStatus, field_name="status"),
        )
        object.__setattr__(
            self,
            "related_evidence_ids",
            _string_tuple(
                self.related_evidence_ids, field_name="related_evidence_ids"
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MaterialEvent":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ScenarioNarrative(_JsonContract):
    scenario_id: str
    name: str
    summary: str
    drivers: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    assumptions: tuple[str, ...]
    causal_chain: tuple[str, ...]
    magnitude_explanation: str
    time_horizon: str
    confirmation_signals: tuple[str, ...]
    weakening_signals: tuple[str, ...]
    invalidation_signals: tuple[str, ...]
    is_no_company_specific_news_scenario: bool
    technical_consistency: str
    fundamental_plausibility: str
    confidence: str

    def __post_init__(self) -> None:
        for name in (
            "drivers",
            "evidence_ids",
            "contradicting_evidence_ids",
            "assumptions",
            "causal_chain",
            "confirmation_signals",
            "weakening_signals",
            "invalidation_signals",
        ):
            object.__setattr__(
                self, name, _string_tuple(getattr(self, name), field_name=name)
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScenarioNarrative":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class TimelineBranch(_JsonContract):
    summary: str
    mechanisms: tuple[str, ...]
    related_event_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    assumptions: tuple[str, ...]
    confidence: str

    def __post_init__(self) -> None:
        for name in (
            "mechanisms",
            "related_event_ids",
            "supporting_evidence_ids",
            "contradicting_evidence_ids",
            "assumptions",
        ):
            object.__setattr__(
                self, name, _string_tuple(getattr(self, name), field_name=name)
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TimelineBranch":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class TimelineBranches(_JsonContract):
    before_events: TimelineBranch | None
    around_events: TimelineBranch | None
    after_events: TimelineBranch | None

    def __post_init__(self) -> None:
        for name in ("before_events", "around_events", "after_events"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, TimelineBranch):
                raise TypeError(f"{name} must be TimelineBranch or null.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TimelineBranches":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(
            before_events=(
                TimelineBranch.from_dict(raw["before_events"])
                if raw.get("before_events") is not None
                else None
            ),
            around_events=(
                TimelineBranch.from_dict(raw["around_events"])
                if raw.get("around_events") is not None
                else None
            ),
            after_events=(
                TimelineBranch.from_dict(raw["after_events"])
                if raw.get("after_events") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class AdversarialReview(_JsonContract):
    strongest_counterargument: str
    missing_evidence: tuple[str, ...]
    opposing_evidence_ids: tuple[str, ...]
    unsupported_assumptions: tuple[str, ...]
    simpler_explanations: tuple[str, ...]
    timing_conflicts: tuple[str, ...]
    technical_invalidation_signals: tuple[str, ...]
    fundamental_contradiction_signals: tuple[str, ...]
    recommendation: AdversarialRecommendation
    revised_primary_scenario_id: str | None
    confidence: str

    def __post_init__(self) -> None:
        for name in (
            "missing_evidence",
            "opposing_evidence_ids",
            "unsupported_assumptions",
            "simpler_explanations",
            "timing_conflicts",
            "technical_invalidation_signals",
            "fundamental_contradiction_signals",
        ):
            object.__setattr__(
                self, name, _string_tuple(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "recommendation",
            _enum_value(
                self.recommendation,
                AdversarialRecommendation,
                field_name="recommendation",
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdversarialReview":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MarketScenarioReport(_JsonContract):
    schema_version: str
    report_id: str
    generated_at: str
    research_cutoff: str
    evidence_availability_statement: str
    frozen_technical_premise: FrozenTechnicalPremise
    evidence: tuple[EvidenceItem, ...]
    exposures: tuple[ExposureItem, ...]
    material_events: tuple[MaterialEvent, ...]
    narratives: tuple[ScenarioNarrative, ...]
    primary_scenario_id: str
    timeline_branches: TimelineBranches
    no_company_specific_news_scenario_id: str
    adversarial_review: AdversarialReview
    unresolved_unknowns: tuple[str, ...]
    final_synthesis: str
    confidence: str
    explicit_limitations: tuple[str, ...]
    exposure_coverage: tuple[ExposureCategoryCoverage, ...] = ()
    exposure_mapping_warnings: tuple[ExposureMappingWarning, ...] = ()
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _normalize_timestamp(self.generated_at, field_name="generated_at"),
        )
        object.__setattr__(
            self,
            "research_cutoff",
            _normalize_timestamp(
                self.research_cutoff, field_name="research_cutoff"
            ),
        )
        if not isinstance(self.frozen_technical_premise, FrozenTechnicalPremise):
            raise TypeError(
                "frozen_technical_premise must be FrozenTechnicalPremise."
            )
        object.__setattr__(
            self,
            "evidence",
            _model_tuple(self.evidence, EvidenceItem, field_name="evidence"),
        )
        object.__setattr__(
            self,
            "exposures",
            _model_tuple(self.exposures, ExposureItem, field_name="exposures"),
        )
        object.__setattr__(
            self,
            "exposure_coverage",
            _model_tuple(
                self.exposure_coverage,
                ExposureCategoryCoverage,
                field_name="exposure_coverage",
            ),
        )
        object.__setattr__(
            self,
            "exposure_mapping_warnings",
            _model_tuple(
                self.exposure_mapping_warnings,
                ExposureMappingWarning,
                field_name="exposure_mapping_warnings",
            ),
        )
        object.__setattr__(
            self,
            "material_events",
            _model_tuple(
                self.material_events, MaterialEvent, field_name="material_events"
            ),
        )
        object.__setattr__(
            self,
            "narratives",
            _model_tuple(
                self.narratives, ScenarioNarrative, field_name="narratives"
            ),
        )
        if not isinstance(self.timeline_branches, TimelineBranches):
            raise TypeError("timeline_branches must be TimelineBranches.")
        if not isinstance(self.adversarial_review, AdversarialReview):
            raise TypeError("adversarial_review must be AdversarialReview.")
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")
        object.__setattr__(
            self,
            "unresolved_unknowns",
            _string_tuple(
                self.unresolved_unknowns, field_name="unresolved_unknowns"
            ),
        )
        object.__setattr__(
            self,
            "explicit_limitations",
            _string_tuple(
                self.explicit_limitations, field_name="explicit_limitations"
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MarketScenarioReport":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        raw["frozen_technical_premise"] = FrozenTechnicalPremise.from_dict(
            raw["frozen_technical_premise"]
        )
        raw["evidence"] = tuple(
            EvidenceItem.from_dict(item) for item in raw.get("evidence", ())
        )
        raw["exposures"] = tuple(
            ExposureItem.from_dict(item) for item in raw.get("exposures", ())
        )
        raw["exposure_coverage"] = tuple(
            ExposureCategoryCoverage.from_dict(item)
            for item in raw.get("exposure_coverage", ())
        )
        raw["exposure_mapping_warnings"] = tuple(
            ExposureMappingWarning.from_dict(item)
            for item in raw.get("exposure_mapping_warnings", ())
        )
        raw["material_events"] = tuple(
            MaterialEvent.from_dict(item)
            for item in raw.get("material_events", ())
        )
        raw["narratives"] = tuple(
            ScenarioNarrative.from_dict(item)
            for item in raw.get("narratives", ())
        )
        raw["timeline_branches"] = TimelineBranches.from_dict(
            raw["timeline_branches"]
        )
        raw["adversarial_review"] = AdversarialReview.from_dict(
            raw["adversarial_review"]
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get("validation_result", ValidationResult.unvalidated().to_dict())
        )
        return cls(**raw)


def frozen_technical_premise_content_hash(
    premise: FrozenTechnicalPremise | Mapping[str, Any],
) -> str:
    payload = (
        premise.to_dict()
        if isinstance(premise, FrozenTechnicalPremise)
        else _json_compatible(premise)
    )
    if not isinstance(payload, dict):
        raise TypeError("Frozen premise hash input must be a mapping.")
    payload = dict(payload)
    payload.pop("frozen_content_hash", None)
    return _hash_payload(payload)


def market_scenario_report_content_hash(
    report: MarketScenarioReport | Mapping[str, Any],
) -> str:
    payload = (
        report.to_dict()
        if isinstance(report, MarketScenarioReport)
        else _json_compatible(report)
    )
    if not isinstance(payload, dict):
        raise TypeError("Market scenario hash input must be a mapping.")
    payload = dict(payload)
    payload.pop("content_hash", None)
    return _hash_payload(payload)


_RULE_ORDER = (
    "report_content_hash",
    "frozen_premise_required_fields",
    "frozen_premise_hash",
    "narrative_minimum",
    "narrative_maximum",
    "primary_scenario_required",
    "primary_scenario_reference",
    "primary_independent_drivers",
    "primary_contradictory_evidence",
    "no_company_news_required",
    "no_company_news_reference",
    "no_company_news_flag",
    "timeline_before_events",
    "timeline_around_events",
    "timeline_after_events",
    "strongest_counterargument",
    "technical_invalidation_signals",
    "fundamental_contradiction_signals",
    "factual_evidence_provenance",
    "hypothetical_not_confirmed",
    "stale_evidence_warning",
    "historical_cutoff",
    "unique_ids",
    "exposure_coverage_required",
    "unique_exposure_coverage",
    "exposure_coverage_state",
    "unique_exposure_keys",
    "category_exposure_compatibility",
    "exposure_values",
    "exposure_references",
    "exposure_reference_overlap",
    "mapping_warning_references",
    "unsupported_forced_mappings",
    "evidence_references",
    "event_references",
    "scenario_references",
    "qualitative_confidence",
    "target_range",
    "prohibited_certainty_language",
    "technical_event_prediction",
)

_PROHIBITED_CERTAINTY_PHRASES = (
    "this will happen because",
    "the chart proves",
    "the company must announce",
    "the elliott count predicts this event",
    "the target guarantees",
)

_TECHNICAL_EVENT_PREDICTION = re.compile(
    r"\b(?:the\s+)?(?:chart|technical\s+analysis|technical\s+count|"
    r"elliott(?:\s+wave)?\s+count)\b.{0,60}\b"
    r"(?:predicts?|guarantees?|proves?)\b.{0,60}\b"
    r"(?:event|news|announcement|earnings|guidance|merger|bankruptcy)\b",
    re.IGNORECASE,
)


class _ValidationCollector:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []
        self.failed: set[str] = set()

    def require(self, rule: str, condition: bool, path: str, message: str) -> None:
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

    def error(self, rule: str, path: str, message: str) -> None:
        self.require(rule, False, path, message)

    def warning(self, code: str, path: str, message: str) -> None:
        self.warnings.append(
            ValidationIssue(
                code=code,
                severity="warning",
                path=path,
                message=message,
            )
        )

    def result(
        self, *, frozen_hash_before: str, frozen_hash_after: str
    ) -> ValidationResult:
        failed_rules = tuple(rule for rule in _RULE_ORDER if rule in self.failed)
        passed_rules = tuple(rule for rule in _RULE_ORDER if rule not in self.failed)
        score = round(100.0 * len(passed_rules) / len(_RULE_ORDER), 2)
        return ValidationResult(
            is_valid=not self.errors,
            score=score,
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            failed_rules=failed_rules,
            passed_rules=passed_rules,
            frozen_hash_before=frozen_hash_before,
            frozen_hash_after=frozen_hash_after,
        )


def _duplicates(values: Sequence[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _all_evidence_references(report: MarketScenarioReport) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []

    def add(path: str, values: Sequence[str]) -> None:
        references.extend((path, value) for value in values)

    for index, event in enumerate(report.material_events):
        add(f"material_events[{index}].related_evidence_ids", event.related_evidence_ids)
    for index, narrative in enumerate(report.narratives):
        add(f"narratives[{index}].evidence_ids", narrative.evidence_ids)
        add(
            f"narratives[{index}].contradicting_evidence_ids",
            narrative.contradicting_evidence_ids,
        )
    for branch_name in ("before_events", "around_events", "after_events"):
        branch = getattr(report.timeline_branches, branch_name)
        if branch is not None:
            add(
                f"timeline_branches.{branch_name}.supporting_evidence_ids",
                branch.supporting_evidence_ids,
            )
            add(
                f"timeline_branches.{branch_name}.contradicting_evidence_ids",
                branch.contradicting_evidence_ids,
            )
    add(
        "adversarial_review.opposing_evidence_ids",
        report.adversarial_review.opposing_evidence_ids,
    )
    return references


def _all_event_references(report: MarketScenarioReport) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    for index, evidence in enumerate(report.evidence):
        if evidence.related_event_id:
            references.append(
                (f"evidence[{index}].related_event_id", evidence.related_event_id)
            )
    for branch_name in ("before_events", "around_events", "after_events"):
        branch = getattr(report.timeline_branches, branch_name)
        if branch is not None:
            references.extend(
                (
                    f"timeline_branches.{branch_name}.related_event_ids",
                    event_id,
                )
                for event_id in branch.related_event_ids
            )
    return references


def _confidence_fields(report: MarketScenarioReport) -> list[tuple[str, str]]:
    values = [("confidence", report.confidence)]
    values.extend(
        (
            (f"evidence[{index}].confidence", item.confidence)
            for index, item in enumerate(report.evidence)
        )
    )
    values.extend(
        (
            (f"exposures[{index}].confidence", item.confidence)
            for index, item in enumerate(report.exposures)
        )
    )
    values.extend(
        (
            (f"narratives[{index}].confidence", item.confidence)
            for index, item in enumerate(report.narratives)
        )
    )
    for branch_name in ("before_events", "around_events", "after_events"):
        branch = getattr(report.timeline_branches, branch_name)
        if branch is not None:
            values.append(
                (f"timeline_branches.{branch_name}.confidence", branch.confidence)
            )
    values.append(("adversarial_review.confidence", report.adversarial_review.confidence))
    return values


def validate_market_scenario_report(
    report: MarketScenarioReport,
) -> ValidationResult:
    """Collect deterministic contract issues without mutating the report."""
    if not isinstance(report, MarketScenarioReport):
        raise TypeError("report must be a MarketScenarioReport.")

    result = _ValidationCollector()
    premise = report.frozen_technical_premise
    calculated_premise_hash = frozen_technical_premise_content_hash(premise)
    calculated_report_hash = market_scenario_report_content_hash(report)

    result.require(
        "report_content_hash",
        bool(report.content_hash) and report.content_hash == calculated_report_hash,
        "content_hash",
        "Report content_hash is missing or does not match its canonical payload.",
    )
    required_premise = (
        bool(premise.resolution_id.strip())
        and bool(premise.symbol.strip())
        and premise.technical_structure is not None
        and bool(premise.source_hashes)
        and all(key.strip() and value.strip() for key, value in premise.source_hashes.items())
    )
    result.require(
        "frozen_premise_required_fields",
        required_premise,
        "frozen_technical_premise",
        "The frozen technical premise is missing required identity, structure, or source-hash data.",
    )
    result.require(
        "frozen_premise_hash",
        bool(premise.frozen_content_hash)
        and premise.frozen_content_hash == calculated_premise_hash,
        "frozen_technical_premise.frozen_content_hash",
        "Frozen technical premise hash does not match its immutable payload.",
    )

    narrative_count = len(report.narratives)
    result.require(
        "narrative_minimum",
        narrative_count >= 3,
        "narratives",
        "A complete report requires at least three competing narratives.",
    )
    result.require(
        "narrative_maximum",
        narrative_count <= 5,
        "narratives",
        "A complete report allows no more than five competing narratives.",
    )

    scenario_by_id = {item.scenario_id: item for item in report.narratives}
    primary = scenario_by_id.get(report.primary_scenario_id)
    result.require(
        "primary_scenario_required",
        bool(report.primary_scenario_id.strip()),
        "primary_scenario_id",
        "A primary scenario ID is required.",
    )
    result.require(
        "primary_scenario_reference",
        primary is not None,
        "primary_scenario_id",
        "The primary scenario ID does not reference an included narrative.",
    )
    normalized_drivers = (
        {
            re.sub(r"\s+", " ", driver.strip()).casefold()
            for driver in primary.drivers
            if driver.strip()
        }
        if primary is not None
        else set()
    )
    result.require(
        "primary_independent_drivers",
        primary is not None and len(normalized_drivers) >= 2,
        "primary_scenario_id",
        "The primary narrative requires at least two distinct independent drivers.",
    )
    result.require(
        "primary_contradictory_evidence",
        primary is not None and bool(primary.contradicting_evidence_ids),
        "primary_scenario_id",
        "The primary narrative must retain contradictory evidence.",
    )

    no_news_id = report.no_company_specific_news_scenario_id
    no_news = scenario_by_id.get(no_news_id)
    result.require(
        "no_company_news_required",
        bool(no_news_id.strip()),
        "no_company_specific_news_scenario_id",
        "A no-company-specific-news scenario ID is required.",
    )
    result.require(
        "no_company_news_reference",
        no_news is not None,
        "no_company_specific_news_scenario_id",
        "The no-company-specific-news scenario ID does not reference an included narrative.",
    )
    result.require(
        "no_company_news_flag",
        no_news is not None and no_news.is_no_company_specific_news_scenario,
        "no_company_specific_news_scenario_id",
        "The referenced narrative is not marked as the no-company-specific-news scenario.",
    )

    for branch_name, rule in (
        ("before_events", "timeline_before_events"),
        ("around_events", "timeline_around_events"),
        ("after_events", "timeline_after_events"),
    ):
        result.require(
            rule,
            getattr(report.timeline_branches, branch_name) is not None,
            f"timeline_branches.{branch_name}",
            f"The {branch_name} timeline branch is required.",
        )

    review = report.adversarial_review
    result.require(
        "strongest_counterargument",
        bool(review.strongest_counterargument.strip()),
        "adversarial_review.strongest_counterargument",
        "The adversarial review requires its strongest counterargument.",
    )
    result.require(
        "technical_invalidation_signals",
        bool(review.technical_invalidation_signals),
        "adversarial_review.technical_invalidation_signals",
        "Technical invalidation signals are required.",
    )
    result.require(
        "fundamental_contradiction_signals",
        bool(review.fundamental_contradiction_signals),
        "adversarial_review.fundamental_contradiction_signals",
        "Fundamental contradiction signals are required.",
    )

    factual_statuses = {EvidenceStatus.CONFIRMED, EvidenceStatus.SCHEDULED}
    for index, evidence in enumerate(report.evidence):
        if evidence.status in factual_statuses:
            provenance = (
                evidence.source,
                evidence.source_type,
                evidence.provider,
                evidence.publication_date,
                evidence.retrieval_timestamp,
                evidence.applicable_cutoff,
            )
            result.require(
                "factual_evidence_provenance",
                all(item is not None and str(item).strip() for item in provenance),
                f"evidence[{index}]",
                "Confirmed or scheduled evidence requires complete provenance.",
            )
        hypothetical_markers = {
            "hypothetical",
            "assumption",
            "model_hypothesis",
            "scenario_hypothesis",
        }
        presented_hypothesis_as_fact = (
            evidence.status is EvidenceStatus.CONFIRMED
            and (
                evidence.source_type.strip().casefold() in hypothetical_markers
                or evidence.category.strip().casefold() in hypothetical_markers
                or evidence.claim.strip().casefold().startswith("[hypothetical]")
            )
        )
        result.require(
            "hypothetical_not_confirmed",
            not presented_hypothesis_as_fact,
            f"evidence[{index}].status",
            "Hypothetical evidence cannot be presented as confirmed fact.",
        )
    for index, event in enumerate(report.material_events):
        if event.status in factual_statuses:
            provenance = (
                event.source,
                event.source_type,
                event.publication_date,
                event.retrieval_timestamp,
                event.applicable_cutoff,
            )
            result.require(
                "factual_evidence_provenance",
                all(item is not None and str(item).strip() for item in provenance),
                f"material_events[{index}]",
                "Confirmed or scheduled events require complete provenance.",
            )

    evidence_references = _all_evidence_references(report)
    global_warning_text = " ".join(report.explicit_limitations).casefold()
    for index, evidence in enumerate(report.evidence):
        if evidence.status is not EvidenceStatus.STALE:
            continue
        result.warning(
            "stale_evidence_retained",
            f"evidence[{index}]",
            "Stale evidence is retained only as explicitly qualified context.",
        )
        item_warning = " ".join(evidence.warnings).casefold()
        explicitly_warned = (
            "stale" in item_warning
            or (
                "stale" in global_warning_text
                and (
                    evidence.evidence_id.casefold() in global_warning_text
                    or "stale evidence" in global_warning_text
                )
            )
        )
        result.require(
            "stale_evidence_warning",
            explicitly_warned,
            f"evidence[{index}]",
            "Stale evidence requires an explicit stale-data warning.",
        )

    research_cutoff = _temporal_point(report.research_cutoff)
    for collection_name, collection in (
        ("evidence", report.evidence),
        ("material_events", report.material_events),
    ):
        for index, item in enumerate(collection):
            publication = _temporal_point(item.publication_date)
            applicable_cutoff = _temporal_point(item.applicable_cutoff)
            cutoff_ok = (
                publication is None
                or applicable_cutoff is None
                or publication <= applicable_cutoff
            )
            if applicable_cutoff is not None and research_cutoff is not None:
                cutoff_ok = cutoff_ok and applicable_cutoff <= research_cutoff
            result.require(
                "historical_cutoff",
                cutoff_ok,
                f"{collection_name}[{index}].publication_date",
                "Evidence was not available at the applicable historical cutoff.",
            )

    id_groups = (
        ("evidence", [item.evidence_id for item in report.evidence]),
        ("exposures", [item.exposure_id for item in report.exposures]),
        ("material_events", [item.event_id for item in report.material_events]),
        ("narratives", [item.scenario_id for item in report.narratives]),
    )
    for path, identifiers in id_groups:
        duplicates = _duplicates(identifiers)
        result.require(
            "unique_ids",
            not duplicates,
            path,
            (
                f"Duplicate IDs are not allowed: {', '.join(sorted(duplicates))}."
                if duplicates
                else ""
            ),
        )

    evidence_ids = {item.evidence_id for item in report.evidence}
    coverage_categories = [item.category for item in report.exposure_coverage]
    missing_coverage = set(ExposureCategory) - set(coverage_categories)
    result.require(
        "exposure_coverage_required",
        not missing_coverage,
        "exposure_coverage",
        (
            "Research coverage is missing required categories: "
            + ", ".join(
                item.value
                for item in ExposureCategory
                if item in missing_coverage
            )
            + "."
            if missing_coverage
            else ""
        ),
    )
    duplicate_coverage = {
        item
        for item in coverage_categories
        if coverage_categories.count(item) > 1
    }
    result.require(
        "unique_exposure_coverage",
        not duplicate_coverage,
        "exposure_coverage",
        (
            "Each research category requires exactly one coverage declaration: "
            + ", ".join(
                item.value
                for item in ExposureCategory
                if item in duplicate_coverage
            )
            + "."
            if duplicate_coverage
            else ""
        ),
    )

    exposure_keys = [
        (item.category, item.exposure_type) for item in report.exposures
    ]
    duplicate_exposure_keys = {
        item for item in exposure_keys if exposure_keys.count(item) > 1
    }
    result.require(
        "unique_exposure_keys",
        not duplicate_exposure_keys,
        "exposures",
        (
            "Duplicate category and exposure-type pairs must be merged: "
            + ", ".join(
                f"{category.value}/{exposure_type.value}"
                for category, exposure_type in sorted(
                    duplicate_exposure_keys,
                    key=lambda item: (item[0].value, item[1].value),
                )
            )
            + "."
            if duplicate_exposure_keys
            else ""
        ),
    )

    for index, exposure in enumerate(report.exposures):
        result.require(
            "category_exposure_compatibility",
            exposure.exposure_type
            in EXPOSURE_CATEGORY_COMPATIBILITY[exposure.category],
            f"exposures[{index}]",
            (
                f"Exposure type {exposure.exposure_type.value!r} is incompatible "
                f"with category {exposure.category.value!r}."
            ),
        )
        result.require(
            "exposure_values",
            exposure.materiality in EXPOSURE_MATERIALITY_VALUES
            and exposure.direction in EXPOSURE_DIRECTION_VALUES,
            f"exposures[{index}]",
            "Exposure materiality or direction uses an unsupported value.",
        )
        for field_name, references in (
            ("supporting_evidence_ids", exposure.supporting_evidence_ids),
            ("contradicting_evidence_ids", exposure.contradicting_evidence_ids),
        ):
            for reference in references:
                result.require(
                    "exposure_references",
                    reference in evidence_ids,
                    f"exposures[{index}].{field_name}",
                    f"Dangling exposure evidence reference: {reference!r}.",
                )
        overlap = set(exposure.supporting_evidence_ids) & set(
            exposure.contradicting_evidence_ids
        )
        result.require(
            "exposure_reference_overlap",
            not overlap,
            f"exposures[{index}]",
            (
                "Evidence cannot support and contradict the same exposure: "
                + ", ".join(sorted(overlap))
                + "."
                if overlap
                else ""
            ),
        )

    exposure_ids_by_category = {
        category: {
            item.exposure_id
            for item in report.exposures
            if item.category is category
        }
        for category in ExposureCategory
    }
    for index, coverage in enumerate(report.exposure_coverage):
        actual_ids = exposure_ids_by_category[coverage.category]
        declared_ids = set(coverage.exposure_ids)
        declared_ids_are_exact = (
            len(declared_ids) == len(coverage.exposure_ids)
            and declared_ids == actual_ids
        )
        if actual_ids:
            valid_state = (
                coverage.state is ExposureCoverageState.EXPOSURES_IDENTIFIED
                and declared_ids_are_exact
            )
        else:
            valid_state = (
                coverage.state
                in {
                    ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
                    ExposureCoverageState.UNAVAILABLE,
                }
                and not coverage.exposure_ids
            )
        result.require(
            "exposure_coverage_state",
            valid_state,
            f"exposure_coverage[{index}]",
            (
                "Coverage state and exposure IDs must exactly match the "
                "category's specific exposure mechanisms."
            ),
        )
        for reference in coverage.unmapped_evidence_ids:
            result.require(
                "exposure_references",
                reference in evidence_ids,
                f"exposure_coverage[{index}].unmapped_evidence_ids",
                f"Dangling unmapped evidence reference: {reference!r}.",
            )

    for index, warning in enumerate(report.exposure_mapping_warnings):
        result.require(
            "mapping_warning_references",
            warning.evidence_id in evidence_ids,
            f"exposure_mapping_warnings[{index}].evidence_id",
            f"Dangling mapping-warning evidence reference: {warning.evidence_id!r}.",
        )
        result.require(
            "unsupported_forced_mappings",
            warning.code != "unsupported_forced_mapping",
            f"exposure_mapping_warnings[{index}]",
            "An explicitly requested exposure mapping violated the compatibility map.",
        )
        result.warning(
            "exposure_mapping_warning",
            f"exposure_mapping_warnings[{index}]",
            warning.message,
        )

    for path, reference in evidence_references:
        result.require(
            "evidence_references",
            reference in evidence_ids,
            path,
            f"Dangling evidence reference: {reference!r}.",
        )

    event_ids = {item.event_id for item in report.material_events}
    for path, reference in _all_event_references(report):
        result.require(
            "event_references",
            reference in event_ids,
            path,
            f"Dangling event reference: {reference!r}.",
        )

    revised_id = review.revised_primary_scenario_id
    result.require(
        "scenario_references",
        revised_id is None or revised_id in scenario_by_id,
        "adversarial_review.revised_primary_scenario_id",
        f"Dangling scenario reference: {revised_id!r}.",
    )

    for path, confidence in _confidence_fields(report):
        result.require(
            "qualitative_confidence",
            confidence in QUALITATIVE_CONFIDENCE_VALUES,
            path,
            "Confidence must use the qualitative Market Scenario vocabulary.",
        )

    low = premise.target_low
    high = premise.target_high
    valid_target = True
    for value in (low, high):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            valid_target = False
    if low is not None and high is not None and valid_target:
        valid_target = float(low) <= float(high)
    result.require(
        "target_range",
        valid_target,
        "frozen_technical_premise.target_low",
        "Technical target_low and target_high must be finite and ordered.",
    )

    synthesis = re.sub(r"\s+", " ", report.final_synthesis.strip()).casefold()
    certainty_matches = [
        phrase for phrase in _PROHIBITED_CERTAINTY_PHRASES if phrase in synthesis
    ]
    result.require(
        "prohibited_certainty_language",
        not certainty_matches,
        "final_synthesis",
        (
            "Final synthesis contains prohibited certainty language: "
            + ", ".join(certainty_matches)
            if certainty_matches
            else ""
        ),
    )
    result.require(
        "technical_event_prediction",
        _TECHNICAL_EVENT_PREDICTION.search(report.final_synthesis) is None,
        "final_synthesis",
        "Technical analysis must not be described as predicting a future event.",
    )

    return result.result(
        frozen_hash_before=premise.frozen_content_hash,
        frozen_hash_after=calculated_premise_hash,
    )


def finalize_market_scenario_report(
    report: MarketScenarioReport,
) -> MarketScenarioReport:
    """Attach validation and a stable report hash without mutating the input."""
    if not isinstance(report, MarketScenarioReport):
        raise TypeError("report must be a MarketScenarioReport.")

    seed = replace(report, content_hash="")
    seed = replace(seed, content_hash=market_scenario_report_content_hash(seed))
    validation = validate_market_scenario_report(seed)
    finalized = replace(seed, validation_result=validation, content_hash="")
    finalized = replace(
        finalized,
        content_hash=market_scenario_report_content_hash(finalized),
    )
    repeated = validate_market_scenario_report(finalized)
    if repeated != validation:
        raise RuntimeError("Market Scenario validation did not stabilize deterministically.")
    return finalized


__all__ = [
    "AdversarialRecommendation",
    "AdversarialReview",
    "CANONICALIZATION_VERSION",
    "EvidenceImplication",
    "EvidenceItem",
    "EvidenceStatus",
    "EventDatePrecision",
    "EXPOSURE_CATEGORY_COMPATIBILITY",
    "EXPOSURE_TYPE_CATEGORY",
    "ExposureCategory",
    "ExposureCategoryCoverage",
    "ExposureCoverageState",
    "ExposureItem",
    "ExposureMappingWarning",
    "ExposureType",
    "FrozenTechnicalPremise",
    "HASH_ALGORITHM",
    "MARKET_SCENARIO_SCHEMA_VERSION",
    "MARKET_SCENARIO_VALIDATION_VERSION",
    "MarketScenarioReport",
    "MaterialEvent",
    "QUALITATIVE_CONFIDENCE_VALUES",
    "ScenarioDirection",
    "ScenarioNarrative",
    "TimelineBranch",
    "TimelineBranches",
    "ValidationIssue",
    "ValidationResult",
    "finalize_market_scenario_report",
    "frozen_technical_premise_content_hash",
    "market_scenario_report_content_hash",
    "validate_market_scenario_report",
]
