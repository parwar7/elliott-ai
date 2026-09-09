"""Offline company knowledge and event-state modeling.

Phase 10 converts explicitly supplied structured company data into immutable
company snapshots and non-predictive event candidates. It performs no
research, provider calls, Elliott analysis, scenario generation, persistence,
or trading logic.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .current_scenarios import CurrentScenarioSet
from .current_state import CurrentMarketState
from .historical_market import DurationBucket, TransmissionMechanism
from .market_scenario import (
    EvidenceImplication,
    EvidenceItem,
    EvidenceStatus,
    EventDatePrecision,
    ExposureItem,
    ExposureType,
    FrozenTechnicalPremise,
    MaterialEvent,
    ScenarioDirection,
    ValidationIssue,
    ValidationResult,
)


COMPANY_KNOWLEDGE_INPUT_SCHEMA_VERSION = "company-knowledge-input-1.0.0"
COMPANY_KNOWN_FACT_SCHEMA_VERSION = "company-known-fact-1.0.0"
COMPANY_PROFILE_SCHEMA_VERSION = "company-profile-1.0.0"
COMPANY_FINANCIAL_STATE_SCHEMA_VERSION = "company-financial-state-1.0.0"
COMPANY_OPERATIONAL_STATE_SCHEMA_VERSION = "company-operational-state-1.0.0"
COMPANY_CAPITAL_STRUCTURE_SCHEMA_VERSION = (
    "company-capital-structure-1.0.0"
)
COMPANY_OWNERSHIP_POSITIONING_SCHEMA_VERSION = (
    "company-ownership-positioning-1.0.0"
)
SCHEDULED_COMPANY_EVENT_SCHEMA_VERSION = "scheduled-company-event-1.0.0"
HISTORICAL_COMPANY_EVENT_PROFILE_SCHEMA_VERSION = (
    "historical-company-event-profile-1.0.0"
)
COMPANY_DEPENDENCY_SCHEMA_VERSION = "company-dependency-1.0.0"
COMPANY_STRUCTURAL_RISK_SCHEMA_VERSION = "company-structural-risk-1.0.0"
COMPANY_STRUCTURAL_OPPORTUNITY_SCHEMA_VERSION = (
    "company-structural-opportunity-1.0.0"
)
COMPANY_EVENT_ARCHETYPE_SCHEMA_VERSION = "company-event-archetype-1.0.0"
COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION = (
    "company-event-archetype-library-1.0.0"
)
COMPANY_EVENT_CANDIDATE_SCHEMA_VERSION = "company-event-candidate-1.0.0"
COMPANY_KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION = (
    "company-knowledge-snapshot-1.0.0"
)
COMPANY_SCENARIO_CONTEXT_SCHEMA_VERSION = "company-scenario-context-1.0.0"
COMPANY_KNOWLEDGE_REPLAY_SCHEMA_VERSION = "company-knowledge-replay-1.0.0"
COMPANY_KNOWLEDGE_REPLAY_RESULT_SCHEMA_VERSION = (
    "company-knowledge-replay-result-1.0.0"
)
COMPANY_KNOWLEDGE_BUILD_RESULT_SCHEMA_VERSION = (
    "company-knowledge-build-result-1.0.0"
)
COMPANY_MARKET_INPUT_BRIDGE_SCHEMA_VERSION = (
    "company-market-input-bridge-1.0.0"
)
COMPANY_KNOWLEDGE_VALIDATION_VERSION = "company-knowledge-validation-1.0.0"
COMPANY_EVENT_CANDIDATE_POLICY_VERSION = (
    "company-event-candidate-policy-1.0.0"
)
COMPANY_SCENARIO_USAGE_VALIDATION_VERSION = (
    "company-scenario-usage-validation-1.0.0"
)


class CompanyStage(StrEnum):
    PRE_REVENUE = "pre_revenue"
    EARLY_REVENUE = "early_revenue"
    GROWTH = "growth"
    SCALING = "scaling"
    MATURE = "mature"
    RESTRUCTURING = "restructuring"
    DISTRESSED = "distressed"
    UNKNOWN = "unknown"


class CompanyStateValue(StrEnum):
    STRONG = "strong"
    ADEQUATE = "adequate"
    CONSTRAINED = "constrained"
    WEAK = "weak"
    DETERIORATING = "deteriorating"
    IMPROVING = "improving"
    VOLATILE = "volatile"
    STABLE = "stable"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    PROFITABLE = "profitable"
    UNPROFITABLE = "unprofitable"
    PRESENT = "present"
    ABSENT = "absent"
    RISING = "rising"
    FALLING = "falling"
    NONE = "none"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class CompanyClassification(StrEnum):
    LIMITED = "limited"
    DEVELOPING = "developing"
    ESTABLISHED = "established"
    LONG_RUNNING = "long_running"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    UNKNOWN = "unknown"


class CompanyEventType(StrEnum):
    EARNINGS_RELEASE = "earnings_release"
    EARNINGS_CALL = "earnings_call"
    INVESTOR_DAY = "investor_day"
    SHAREHOLDER_MEETING = "shareholder_meeting"
    PRODUCT_LAUNCH = "product_launch"
    SERVICE_LAUNCH = "service_launch"
    SPACE_LAUNCH = "space_launch"
    CLINICAL_READOUT = "clinical_readout"
    REGULATORY_DECISION = "regulatory_decision"
    CONTRACT_MILESTONE = "contract_milestone"
    DEBT_MATURITY = "debt_maturity"
    REFINANCING_WINDOW = "refinancing_window"
    LOCKUP_EXPIRATION = "lockup_expiration"
    INSIDER_TRADING_WINDOW = "insider_trading_window"
    LITIGATION_HEARING = "litigation_hearing"
    MANAGEMENT_TRANSITION = "management_transition"
    PRODUCTION_MILESTONE = "production_milestone"
    CUSTOMER_DELIVERY = "customer_delivery"
    INDEX_REBALANCE = "index_rebalance"
    OPTION_EXPIRATION = "option_expiration"
    OTHER = "other"


class CompanyEventStatus(StrEnum):
    ANNOUNCED = "announced"
    SCHEDULED = "scheduled"
    DELAYED = "delayed"
    RESCHEDULED = "rescheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class EventRecurrenceClass(StrEnum):
    ONE_TIME = "one_time"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    PERIODIC = "periodic"
    IRREGULAR = "irregular"
    RECURRING = "recurring"
    UNKNOWN = "unknown"


class CompanyDataQuality(StrEnum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    SPARSE = "sparse"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class CompanyDependencyType(StrEnum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    GOVERNMENT = "government"
    REGULATOR = "regulator"
    PRODUCT = "product"
    SERVICE = "service"
    LAUNCH_PROVIDER = "launch_provider"
    MANUFACTURER = "manufacturer"
    COMMODITY = "commodity"
    CURRENCY = "currency"
    GEOGRAPHY = "geography"
    INTEREST_RATE = "interest_rate"
    CAPITAL_MARKET = "capital_market"
    KEY_PERSON = "key_person"
    TECHNOLOGY = "technology"
    INFRASTRUCTURE = "infrastructure"
    PARTNER = "partner"
    CONTRACT = "contract"
    SINGLE_ASSET = "single_asset"


class ConcentrationClass(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    SINGLE_SOURCE = "single_source"
    UNKNOWN = "unknown"


class SubstitutabilityClass(StrEnum):
    EASY = "easy"
    MODERATE = "moderate"
    DIFFICULT = "difficult"
    NOT_SUBSTITUTABLE = "not_substitutable"
    UNKNOWN = "unknown"


class ReplacementTimeClass(StrEnum):
    IMMEDIATE = "immediate"
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"
    UNKNOWN = "unknown"


class ImpactClass(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class StructuralConditionStatus(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    CONDITIONAL = "conditional"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"


class CompanyStructuralRiskType(StrEnum):
    LIQUIDITY = "liquidity"
    FINANCING = "financing"
    DILUTION = "dilution"
    REFINANCING = "refinancing"
    CUSTOMER_CONCENTRATION = "customer_concentration"
    SUPPLIER_CONCENTRATION = "supplier_concentration"
    EXECUTION = "execution"
    PRODUCTION = "production"
    PRODUCT_DELAY = "product_delay"
    LAUNCH_DELAY = "launch_delay"
    REGULATORY = "regulatory"
    LITIGATION = "litigation"
    COMPETITION = "competition"
    MARGIN_PRESSURE = "margin_pressure"
    DEMAND_WEAKNESS = "demand_weakness"
    VALUATION_COMPRESSION = "valuation_compression"
    MANAGEMENT = "management"
    KEY_PERSON = "key_person"
    TECHNOLOGY_FAILURE = "technology_failure"
    CONTRACT_LOSS = "contract_loss"
    BACKLOG_CONVERSION = "backlog_conversion"
    GEOGRAPHIC = "geographic"
    CURRENCY = "currency"
    COMMODITY = "commodity"
    INTEREST_RATE = "interest_rate"
    DEPENDENCY_FAILURE = "dependency_failure"


class CompanyStructuralOpportunityType(StrEnum):
    CONTRACT_AWARD = "contract_award"
    PRODUCT_SUCCESS = "product_success"
    LAUNCH_SUCCESS = "launch_success"
    REGULATORY_APPROVAL = "regulatory_approval"
    CUSTOMER_EXPANSION = "customer_expansion"
    MARGIN_IMPROVEMENT = "margin_improvement"
    DEMAND_ACCELERATION = "demand_acceleration"
    FINANCING_IMPROVEMENT = "financing_improvement"
    STRATEGIC_PARTNERSHIP = "strategic_partnership"
    ACQUISITION = "acquisition"
    BACKLOG_CONVERSION = "backlog_conversion"
    MARKET_EXPANSION = "market_expansion"
    COST_REDUCTION = "cost_reduction"
    TECHNOLOGY_VALIDATION = "technology_validation"
    CAPACITY_EXPANSION = "capacity_expansion"


class CompanyEventArchetypeType(StrEnum):
    EARNINGS_MISS = "earnings_miss"
    EARNINGS_BEAT = "earnings_beat"
    GUIDANCE_REDUCTION = "guidance_reduction"
    GUIDANCE_INCREASE = "guidance_increase"
    LAUNCH_DELAY = "launch_delay"
    LAUNCH_SUCCESS = "launch_success"
    PRODUCT_DELAY = "product_delay"
    CONTRACT_AWARD = "contract_award"
    CONTRACT_CANCELLATION = "contract_cancellation"
    CUSTOMER_LOSS = "customer_loss"
    CUSTOMER_EXPANSION = "customer_expansion"
    CAPITAL_RAISE = "capital_raise"
    CONVERTIBLE_FINANCING = "convertible_financing"
    DEBT_REFINANCING = "debt_refinancing"
    COVENANT_PRESSURE = "covenant_pressure"
    REGULATORY_APPROVAL = "regulatory_approval"
    REGULATORY_REJECTION = "regulatory_rejection"
    LITIGATION_ESCALATION = "litigation_escalation"
    MANAGEMENT_DEPARTURE = "management_departure"
    STRATEGIC_PARTNERSHIP = "strategic_partnership"
    ACQUISITION_PROPOSAL = "acquisition_proposal"
    OPERATIONAL_OUTAGE = "operational_outage"
    PRODUCTION_SHORTFALL = "production_shortfall"
    MARGIN_COMPRESSION = "margin_compression"
    MARGIN_EXPANSION = "margin_expansion"
    BACKLOG_CONVERSION_FAILURE = "backlog_conversion_failure"
    SHORT_SQUEEZE = "short_squeeze"
    VALUATION_COMPRESSION = "valuation_compression"
    SECTOR_SYMPATHY_MOVE = "sector_sympathy_move"


class CompanyEventClassification(StrEnum):
    KNOWN_SCHEDULED_EVENT = "known_scheduled_event"
    STRUCTURAL_EVENT_RISK = "structural_event_risk"
    STRUCTURAL_OPPORTUNITY = "structural_opportunity"
    HYPOTHETICAL_FUTURE_EVENT = "hypothetical_future_event"


class CandidateEligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    DUPLICATE = "duplicate"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class CompanyKnowledgeBuildStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


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
            f"{field_name} must be a timestamp string or datetime."
        )
    aware = (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalize_optional_temporal(
    value: str | date | datetime | None,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _normalize_timestamp(value, field_name=field_name)
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be temporal text or null.")
    raw = value.strip()
    if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", raw):
        return raw
    return _normalize_timestamp(raw, field_name=field_name)


def _temporal_point(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{4}", raw):
            parsed = datetime(int(raw), 1, 1)
        elif re.fullmatch(r"\d{4}-\d{2}", raw):
            parsed = datetime.fromisoformat(raw + "-01")
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
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


def _string_tuple(
    value: Sequence[str] | None,
    *,
    field_name: str,
    sort_values: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(value)
    if not all(isinstance(item, str) for item in result):
        raise TypeError(f"{field_name} must contain only strings.")
    return tuple(sorted(result)) if sort_values else result


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


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON mappings must use string keys.")
        return MappingProxyType(
            {
                key: _freeze_json(item)
                for key, item in sorted(value.items())
            }
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numeric values must be finite.")
        return value
    raise TypeError(
        f"JSON contracts cannot contain {type(value).__name__} values."
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    return value


def _hash_payload(value: Any) -> str:
    payload = json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _contract_hash(value: Any) -> str:
    payload = dict(value.to_dict())
    payload.pop("content_hash", None)
    return _hash_payload(payload)


def _mapping(
    value: Mapping[str, Any],
    *,
    model_name: str,
) -> dict[str, Any]:
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


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


def _simple_from_dict(cls: type, value: Mapping[str, Any]) -> Any:
    raw = _mapping(value, model_name=cls.__name__)
    _reject_unknown(
        raw,
        {item.name for item in fields(cls)},
        model_name=cls.__name__,
    )
    return cls(**raw)


def company_known_fact_id(
    input_id: str,
    section: str,
    field_name: str,
) -> str:
    return _normalized_token(
        f"company_fact_{input_id}_{section}_{field_name}"
    )


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeInput(_JsonContract):
    input_id: str
    symbol: str
    company_name: str
    exchange: str
    jurisdiction: str
    sector: str
    industry: str
    business_model: str
    applicable_cutoff: str
    company_profile_inputs: Mapping[str, Any]
    financial_state_inputs: Mapping[str, Any]
    operational_state_inputs: Mapping[str, Any]
    capital_structure_inputs: Mapping[str, Any]
    ownership_and_positioning_inputs: Mapping[str, Any]
    scheduled_event_inputs: tuple[Mapping[str, Any], ...]
    dependency_inputs: tuple[Mapping[str, Any], ...]
    historical_company_event_inputs: tuple[Mapping[str, Any], ...]
    known_risk_inputs: tuple[Mapping[str, Any], ...]
    known_opportunity_inputs: tuple[Mapping[str, Any], ...]
    source_metadata: Mapping[str, Any]
    created_at: str
    content_hash: str = ""
    schema_version: str = COMPANY_KNOWLEDGE_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        for name in ("applicable_cutoff", "created_at"):
            object.__setattr__(
                self,
                name,
                _normalize_timestamp(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        for name in (
            "company_profile_inputs",
            "financial_state_inputs",
            "operational_state_inputs",
            "capital_structure_inputs",
            "ownership_and_positioning_inputs",
            "source_metadata",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping.")
            object.__setattr__(self, name, _freeze_json(value))
        for name in (
            "scheduled_event_inputs",
            "dependency_inputs",
            "historical_company_event_inputs",
            "known_risk_inputs",
            "known_opportunity_inputs",
        ):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(
                value,
                Sequence,
            ):
                raise TypeError(f"{name} must be a sequence of mappings.")
            if not all(isinstance(item, Mapping) for item in value):
                raise TypeError(f"{name} must contain only mappings.")
            object.__setattr__(
                self,
                name,
                tuple(_freeze_json(item) for item in value),
            )

    @classmethod
    def create(cls, **values: Any) -> "CompanyKnowledgeInput":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash.")
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyKnowledgeInput":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyKnownFact(_JsonContract):
    fact_id: str
    symbol: str
    section: str
    field_name: str
    value: Any
    data_as_of: str | None
    applicable_cutoff: str
    provenance: Mapping[str, Any]
    content_hash: str = ""
    schema_version: str = COMPANY_KNOWN_FACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "value", _freeze_json(self.value))
        object.__setattr__(
            self,
            "data_as_of",
            _normalize_optional_temporal(
                self.data_as_of,
                field_name="data_as_of",
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
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping.")
        object.__setattr__(self, "provenance", _freeze_json(self.provenance))

    @classmethod
    def create(cls, **values: Any) -> "CompanyKnownFact":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyKnownFact":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyProfile(_JsonContract):
    company_id: str
    symbol: str
    legal_name: str
    common_name: str
    exchange: str
    jurisdiction: str
    sector: str
    industry: str
    business_model: str
    revenue_model: str
    primary_products: tuple[str, ...]
    primary_services: tuple[str, ...]
    geographic_exposure: tuple[str, ...]
    customer_types: tuple[str, ...]
    company_stage: CompanyStage
    operating_history_class: CompanyClassification
    cyclicality_class: CompanyClassification
    capital_intensity_class: CompanyClassification
    regulatory_intensity_class: CompanyClassification
    content_hash: str = ""
    schema_version: str = COMPANY_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        for name in (
            "primary_products",
            "primary_services",
            "geographic_exposure",
            "customer_types",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        object.__setattr__(
            self,
            "company_stage",
            _enum_value(
                self.company_stage,
                CompanyStage,
                field_name="company_stage",
            ),
        )
        for name in (
            "operating_history_class",
            "cyclicality_class",
            "capital_intensity_class",
            "regulatory_intensity_class",
        ):
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name),
                    CompanyClassification,
                    field_name=name,
                ),
            )

    @classmethod
    def create(cls, **values: Any) -> "CompanyProfile":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompanyProfile":
        return _simple_from_dict(cls, value)


_FINANCIAL_STATE_FIELDS = (
    "revenue_state",
    "revenue_growth_state",
    "profitability_state",
    "gross_margin_state",
    "operating_margin_state",
    "free_cash_flow_state",
    "cash_balance_state",
    "cash_runway_state",
    "debt_state",
    "debt_maturity_state",
    "interest_burden_state",
    "liquidity_state",
    "working_capital_state",
    "capital_expenditure_state",
    "financing_dependency_state",
    "dilution_dependency_state",
    "guidance_state",
    "estimate_dispersion_state",
    "valuation_state",
)


@dataclass(frozen=True, slots=True)
class CompanyFinancialState(_JsonContract):
    state_id: str
    revenue_state: CompanyStateValue
    revenue_growth_state: CompanyStateValue
    profitability_state: CompanyStateValue
    gross_margin_state: CompanyStateValue
    operating_margin_state: CompanyStateValue
    free_cash_flow_state: CompanyStateValue
    cash_balance_state: CompanyStateValue
    cash_runway_state: CompanyStateValue
    debt_state: CompanyStateValue
    debt_maturity_state: CompanyStateValue
    interest_burden_state: CompanyStateValue
    liquidity_state: CompanyStateValue
    working_capital_state: CompanyStateValue
    capital_expenditure_state: CompanyStateValue
    financing_dependency_state: CompanyStateValue
    dilution_dependency_state: CompanyStateValue
    guidance_state: CompanyStateValue
    estimate_dispersion_state: CompanyStateValue
    valuation_state: CompanyStateValue
    quantitative_values: Mapping[str, Any]
    data_as_of: str | None
    applicable_cutoff: str
    missing_fields: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_FINANCIAL_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in _FINANCIAL_STATE_FIELDS:
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name),
                    CompanyStateValue,
                    field_name=name,
                ),
            )
        if not isinstance(self.quantitative_values, Mapping):
            raise TypeError("quantitative_values must be a mapping.")
        object.__setattr__(
            self,
            "quantitative_values",
            _freeze_json(self.quantitative_values),
        )
        object.__setattr__(
            self,
            "data_as_of",
            _normalize_optional_temporal(
                self.data_as_of,
                field_name="data_as_of",
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
            "missing_fields",
            _string_tuple(
                self.missing_fields,
                field_name="missing_fields",
                sort_values=True,
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "CompanyFinancialState":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyFinancialState":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class SpaceOperationalExtension(_JsonContract):
    launch_cadence_state: CompanyStateValue
    launch_reliability_state: CompanyStateValue
    mission_backlog_state: CompanyStateValue
    launch_site_dependency_state: CompanyStateValue
    payload_concentration_state: CompanyStateValue
    government_contract_exposure_state: CompanyStateValue

    def __post_init__(self) -> None:
        for item in fields(self):
            object.__setattr__(
                self,
                item.name,
                _enum_value(
                    getattr(self, item.name),
                    CompanyStateValue,
                    field_name=item.name,
                ),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "SpaceOperationalExtension":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class BiotechOperationalExtension(_JsonContract):
    trial_phase: str
    trial_readout_schedule: tuple[str, ...]
    cash_runway_state: CompanyStateValue
    regulatory_milestone_state: CompanyStateValue
    product_concentration_state: CompanyStateValue

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "trial_readout_schedule",
            _string_tuple(
                self.trial_readout_schedule,
                field_name="trial_readout_schedule",
            ),
        )
        for name in (
            "cash_runway_state",
            "regulatory_milestone_state",
            "product_concentration_state",
        ):
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name),
                    CompanyStateValue,
                    field_name=name,
                ),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "BiotechOperationalExtension":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class SoftwareOperationalExtension(_JsonContract):
    subscriber_growth_state: CompanyStateValue
    retention_state: CompanyStateValue
    customer_acquisition_efficiency_state: CompanyStateValue
    cloud_infrastructure_exposure_state: CompanyStateValue
    enterprise_contract_concentration_state: CompanyStateValue

    def __post_init__(self) -> None:
        for item in fields(self):
            object.__setattr__(
                self,
                item.name,
                _enum_value(
                    getattr(self, item.name),
                    CompanyStateValue,
                    field_name=item.name,
                ),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "SoftwareOperationalExtension":
        return _simple_from_dict(cls, value)


_OPERATIONAL_STATE_FIELDS = (
    "production_state",
    "service_delivery_state",
    "execution_state",
    "product_development_state",
    "launch_state",
    "manufacturing_state",
    "supply_chain_state",
    "backlog_state",
    "customer_demand_state",
    "customer_concentration_state",
    "contract_dependency_state",
    "regulatory_approval_state",
    "litigation_state",
    "management_execution_state",
    "hiring_state",
    "geographic_dependency_state",
    "infrastructure_dependency_state",
)


@dataclass(frozen=True, slots=True)
class CompanyOperationalState(_JsonContract):
    state_id: str
    production_state: CompanyStateValue
    service_delivery_state: CompanyStateValue
    execution_state: CompanyStateValue
    product_development_state: CompanyStateValue
    launch_state: CompanyStateValue
    manufacturing_state: CompanyStateValue
    supply_chain_state: CompanyStateValue
    backlog_state: CompanyStateValue
    customer_demand_state: CompanyStateValue
    customer_concentration_state: CompanyStateValue
    contract_dependency_state: CompanyStateValue
    regulatory_approval_state: CompanyStateValue
    litigation_state: CompanyStateValue
    management_execution_state: CompanyStateValue
    hiring_state: CompanyStateValue
    geographic_dependency_state: CompanyStateValue
    infrastructure_dependency_state: CompanyStateValue
    operational_bottlenecks: tuple[str, ...]
    space_extension: SpaceOperationalExtension | None
    biotech_extension: BiotechOperationalExtension | None
    software_extension: SoftwareOperationalExtension | None
    data_as_of: str | None
    missing_fields: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_OPERATIONAL_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in _OPERATIONAL_STATE_FIELDS:
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name),
                    CompanyStateValue,
                    field_name=name,
                ),
            )
        object.__setattr__(
            self,
            "operational_bottlenecks",
            _string_tuple(
                self.operational_bottlenecks,
                field_name="operational_bottlenecks",
            ),
        )
        for name, model_type in (
            ("space_extension", SpaceOperationalExtension),
            ("biotech_extension", BiotechOperationalExtension),
            ("software_extension", SoftwareOperationalExtension),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, model_type):
                raise TypeError(f"{name} must be {model_type.__name__} or null.")
        object.__setattr__(
            self,
            "data_as_of",
            _normalize_optional_temporal(
                self.data_as_of,
                field_name="data_as_of",
            ),
        )
        object.__setattr__(
            self,
            "missing_fields",
            _string_tuple(
                self.missing_fields,
                field_name="missing_fields",
                sort_values=True,
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "CompanyOperationalState":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyOperationalState":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        for name, model_type in (
            ("space_extension", SpaceOperationalExtension),
            ("biotech_extension", BiotechOperationalExtension),
            ("software_extension", SoftwareOperationalExtension),
        ):
            if raw.get(name) is not None:
                raw[name] = model_type.from_dict(raw[name])
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CapitalStructureState(_JsonContract):
    state_id: str
    share_count_state: CompanyStateValue
    recent_dilution_state: CompanyStateValue
    authorized_share_capacity: float | None
    equity_raise_history: tuple[str, ...]
    debt_instruments: tuple[str, ...]
    convertible_instruments: tuple[str, ...]
    warrant_overhang: CompanyStateValue
    employee_compensation_dilution: CompanyStateValue
    refinancing_requirements: tuple[str, ...]
    covenant_constraints: tuple[str, ...]
    maturity_schedule: tuple[str, ...]
    capital_access_state: CompanyStateValue
    financing_optionalities: tuple[str, ...]
    data_as_of: str | None
    missing_fields: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_CAPITAL_STRUCTURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "share_count_state",
            "recent_dilution_state",
            "warrant_overhang",
            "employee_compensation_dilution",
            "capital_access_state",
        ):
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name),
                    CompanyStateValue,
                    field_name=name,
                ),
            )
        if self.authorized_share_capacity is not None:
            if (
                isinstance(self.authorized_share_capacity, bool)
                or not isinstance(
                    self.authorized_share_capacity,
                    (int, float),
                )
                or not math.isfinite(float(self.authorized_share_capacity))
                or float(self.authorized_share_capacity) < 0
            ):
                raise ValueError(
                    "authorized_share_capacity must be finite, non-negative, or null."
                )
            object.__setattr__(
                self,
                "authorized_share_capacity",
                float(self.authorized_share_capacity),
            )
        for name in (
            "equity_raise_history",
            "debt_instruments",
            "convertible_instruments",
            "refinancing_requirements",
            "covenant_constraints",
            "maturity_schedule",
            "financing_optionalities",
            "missing_fields",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                    sort_values=name == "missing_fields",
                ),
            )
        object.__setattr__(
            self,
            "data_as_of",
            _normalize_optional_temporal(
                self.data_as_of,
                field_name="data_as_of",
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "CapitalStructureState":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CapitalStructureState":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class OwnershipPositioningState(_JsonContract):
    state_id: str
    insider_ownership_state: CompanyStateValue
    institutional_ownership_state: CompanyStateValue
    concentrated_holder_state: CompanyStateValue
    short_interest_state: CompanyStateValue
    borrow_availability_state: CompanyStateValue
    option_positioning_state: CompanyStateValue
    lockup_state: CompanyStateValue
    insider_transaction_state: CompanyStateValue
    index_membership_state: CompanyStateValue
    passive_flow_exposure: CompanyStateValue
    known_positioning_events: tuple[str, ...]
    data_as_of: str | None
    missing_fields: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_OWNERSHIP_POSITIONING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "insider_ownership_state",
            "institutional_ownership_state",
            "concentrated_holder_state",
            "short_interest_state",
            "borrow_availability_state",
            "option_positioning_state",
            "lockup_state",
            "insider_transaction_state",
            "index_membership_state",
            "passive_flow_exposure",
        ):
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name),
                    CompanyStateValue,
                    field_name=name,
                ),
            )
        object.__setattr__(
            self,
            "known_positioning_events",
            _string_tuple(
                self.known_positioning_events,
                field_name="known_positioning_events",
            ),
        )
        object.__setattr__(
            self,
            "missing_fields",
            _string_tuple(
                self.missing_fields,
                field_name="missing_fields",
                sort_values=True,
            ),
        )
        object.__setattr__(
            self,
            "data_as_of",
            _normalize_optional_temporal(
                self.data_as_of,
                field_name="data_as_of",
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "OwnershipPositioningState":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "OwnershipPositioningState":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class ScheduledCompanyEvent(_JsonContract):
    event_id: str
    event_type: CompanyEventType
    title: str
    symbol: str
    announced_at: str
    scheduled_start: str | None
    scheduled_end: str | None
    date_precision: EventDatePrecision
    status: CompanyEventStatus
    recurrence: EventRecurrenceClass
    source_type: str
    source_reference: str
    known_at_cutoff: bool
    related_product: str | None
    related_contract: str | None
    related_regulator: str | None
    related_customer: str | None
    expected_information_type: tuple[str, ...]
    applicable_cutoff: str
    content_hash: str = ""
    schema_version: str = SCHEDULED_COMPANY_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self,
            "event_type",
            _enum_value(
                self.event_type,
                CompanyEventType,
                field_name="event_type",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                CompanyEventStatus,
                field_name="status",
            ),
        )
        object.__setattr__(
            self,
            "recurrence",
            _enum_value(
                self.recurrence,
                EventRecurrenceClass,
                field_name="recurrence",
            ),
        )
        object.__setattr__(
            self,
            "date_precision",
            _enum_value(
                self.date_precision,
                EventDatePrecision,
                field_name="date_precision",
            ),
        )
        object.__setattr__(
            self,
            "announced_at",
            _normalize_timestamp(
                self.announced_at,
                field_name="announced_at",
            ),
        )
        for name in ("scheduled_start", "scheduled_end"):
            object.__setattr__(
                self,
                name,
                _normalize_optional_temporal(
                    getattr(self, name),
                    field_name=name,
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
            "expected_information_type",
            _string_tuple(
                self.expected_information_type,
                field_name="expected_information_type",
            ),
        )
        if not isinstance(self.known_at_cutoff, bool):
            raise TypeError("known_at_cutoff must be bool.")

    @classmethod
    def create(cls, **values: Any) -> "ScheduledCompanyEvent":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ScheduledCompanyEvent":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class HistoricalCompanyEventProfile(_JsonContract):
    event_profile_id: str
    symbol: str
    event_type: CompanyEventArchetypeType
    observed_event_count: int
    observation_start: str | None
    observation_end: str | None
    recurrence_class: EventRecurrenceClass
    historical_outcome_classes: tuple[str, ...]
    common_preconditions: tuple[str, ...]
    common_transmission_mechanisms: tuple[TransmissionMechanism, ...]
    known_amplifiers: tuple[str, ...]
    known_dampeners: tuple[str, ...]
    data_quality: CompanyDataQuality
    provenance: Mapping[str, Any]
    content_hash: str = ""
    schema_version: str = HISTORICAL_COMPANY_EVENT_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self,
            "event_type",
            _enum_value(
                self.event_type,
                CompanyEventArchetypeType,
                field_name="event_type",
            ),
        )
        object.__setattr__(
            self,
            "recurrence_class",
            _enum_value(
                self.recurrence_class,
                EventRecurrenceClass,
                field_name="recurrence_class",
            ),
        )
        object.__setattr__(
            self,
            "data_quality",
            _enum_value(
                self.data_quality,
                CompanyDataQuality,
                field_name="data_quality",
            ),
        )
        if (
            isinstance(self.observed_event_count, bool)
            or not isinstance(self.observed_event_count, int)
            or self.observed_event_count < 0
        ):
            raise ValueError("observed_event_count must be a non-negative integer.")
        for name in ("observation_start", "observation_end"):
            object.__setattr__(
                self,
                name,
                _normalize_optional_temporal(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        for name in (
            "historical_outcome_classes",
            "common_preconditions",
            "known_amplifiers",
            "known_dampeners",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "common_transmission_mechanisms",
            _enum_tuple(
                self.common_transmission_mechanisms,
                TransmissionMechanism,
                field_name="common_transmission_mechanisms",
            ),
        )
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping.")
        object.__setattr__(self, "provenance", _freeze_json(self.provenance))

    @classmethod
    def create(cls, **values: Any) -> "HistoricalCompanyEventProfile":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "HistoricalCompanyEventProfile":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyDependency(_JsonContract):
    dependency_id: str
    dependency_type: CompanyDependencyType
    subject: str
    dependency_target: str
    concentration_class: ConcentrationClass
    substitutability_class: SubstitutabilityClass
    time_to_replace_class: ReplacementTimeClass
    operational_impact_class: ImpactClass
    financial_impact_class: ImpactClass
    known_mitigants: tuple[str, ...]
    applicable_cutoff: str
    provenance: Mapping[str, Any]
    content_hash: str = ""
    schema_version: str = COMPANY_DEPENDENCY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, enum_type in (
            ("dependency_type", CompanyDependencyType),
            ("concentration_class", ConcentrationClass),
            ("substitutability_class", SubstitutabilityClass),
            ("time_to_replace_class", ReplacementTimeClass),
            ("operational_impact_class", ImpactClass),
            ("financial_impact_class", ImpactClass),
        ):
            object.__setattr__(
                self,
                name,
                _enum_value(
                    getattr(self, name),
                    enum_type,
                    field_name=name,
                ),
            )
        object.__setattr__(
            self,
            "known_mitigants",
            _string_tuple(
                self.known_mitigants,
                field_name="known_mitigants",
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
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping.")
        object.__setattr__(self, "provenance", _freeze_json(self.provenance))

    @classmethod
    def create(cls, **values: Any) -> "CompanyDependency":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyDependency":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyStructuralRisk(_JsonContract):
    item_id: str
    type: CompanyStructuralRiskType
    description: str
    supporting_fact_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    scheduled_event_ids: tuple[str, ...]
    preconditions: tuple[str, ...]
    invalidating_conditions: tuple[str, ...]
    status: StructuralConditionStatus
    provenance: Mapping[str, Any]
    content_hash: str = ""
    schema_version: str = COMPANY_STRUCTURAL_RISK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "type",
            _enum_value(
                self.type,
                CompanyStructuralRiskType,
                field_name="type",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                StructuralConditionStatus,
                field_name="status",
            ),
        )
        for name in (
            "supporting_fact_ids",
            "dependency_ids",
            "scheduled_event_ids",
            "preconditions",
            "invalidating_conditions",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping.")
        object.__setattr__(self, "provenance", _freeze_json(self.provenance))

    @classmethod
    def create(cls, **values: Any) -> "CompanyStructuralRisk":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyStructuralRisk":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyStructuralOpportunity(_JsonContract):
    item_id: str
    type: CompanyStructuralOpportunityType
    description: str
    supporting_fact_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    scheduled_event_ids: tuple[str, ...]
    preconditions: tuple[str, ...]
    invalidating_conditions: tuple[str, ...]
    status: StructuralConditionStatus
    provenance: Mapping[str, Any]
    content_hash: str = ""
    schema_version: str = COMPANY_STRUCTURAL_OPPORTUNITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "type",
            _enum_value(
                self.type,
                CompanyStructuralOpportunityType,
                field_name="type",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                StructuralConditionStatus,
                field_name="status",
            ),
        )
        for name in (
            "supporting_fact_ids",
            "dependency_ids",
            "scheduled_event_ids",
            "preconditions",
            "invalidating_conditions",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping.")
        object.__setattr__(self, "provenance", _freeze_json(self.provenance))

    @classmethod
    def create(cls, **values: Any) -> "CompanyStructuralOpportunity":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyStructuralOpportunity":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyEventArchetype(_JsonContract):
    archetype_id: str
    archetype_type: CompanyEventArchetypeType
    applicable_company_stages: tuple[CompanyStage, ...]
    applicable_industries: tuple[str, ...]
    required_preconditions: tuple[str, ...]
    optional_preconditions: tuple[str, ...]
    contradictory_conditions: tuple[str, ...]
    initiating_event_class: CompanyEventClassification
    transmission_mechanisms: tuple[TransmissionMechanism, ...]
    common_amplifiers: tuple[str, ...]
    common_dampeners: tuple[str, ...]
    expected_time_horizon_classes: tuple[DurationBucket, ...]
    compatible_exposure_types: tuple[ExposureType, ...]
    compatible_technical_directions: tuple[ScenarioDirection, ...]
    falsification_conditions: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_EVENT_ARCHETYPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "archetype_type",
            _enum_value(
                self.archetype_type,
                CompanyEventArchetypeType,
                field_name="archetype_type",
            ),
        )
        object.__setattr__(
            self,
            "applicable_company_stages",
            _enum_tuple(
                self.applicable_company_stages,
                CompanyStage,
                field_name="applicable_company_stages",
            ),
        )
        object.__setattr__(
            self,
            "initiating_event_class",
            _enum_value(
                self.initiating_event_class,
                CompanyEventClassification,
                field_name="initiating_event_class",
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
            "expected_time_horizon_classes",
            _enum_tuple(
                self.expected_time_horizon_classes,
                DurationBucket,
                field_name="expected_time_horizon_classes",
            ),
        )
        object.__setattr__(
            self,
            "compatible_exposure_types",
            _enum_tuple(
                self.compatible_exposure_types,
                ExposureType,
                field_name="compatible_exposure_types",
            ),
        )
        object.__setattr__(
            self,
            "compatible_technical_directions",
            _enum_tuple(
                self.compatible_technical_directions,
                ScenarioDirection,
                field_name="compatible_technical_directions",
            ),
        )
        for name in (
            "applicable_industries",
            "required_preconditions",
            "optional_preconditions",
            "contradictory_conditions",
            "common_amplifiers",
            "common_dampeners",
            "falsification_conditions",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                ),
            )

    @classmethod
    def create(cls, **values: Any) -> "CompanyEventArchetype":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyEventArchetype":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyEventArchetypeLibrary(_JsonContract):
    library_id: str
    library_version: str
    archetypes: tuple[CompanyEventArchetype, ...]
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "archetypes",
            _model_tuple(
                self.archetypes,
                CompanyEventArchetype,
                field_name="archetypes",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        library_id: str,
        library_version: str,
        archetypes: Sequence[CompanyEventArchetype],
    ) -> "CompanyEventArchetypeLibrary":
        ordered = tuple(sorted(archetypes, key=lambda item: item.archetype_id))
        seed = cls(
            library_id=library_id,
            library_version=library_version,
            archetypes=ordered,
        )
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyEventArchetypeLibrary":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["archetypes"] = tuple(
            CompanyEventArchetype.from_dict(item)
            for item in raw.get("archetypes", ())
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CompanyEventCandidate(_JsonContract):
    candidate_id: str
    archetype_id: str
    event_type: CompanyEventArchetypeType
    event_classification: CompanyEventClassification
    hypothesis_label: str
    supporting_company_facts: tuple[str, ...]
    supporting_financial_states: tuple[str, ...]
    supporting_operational_states: tuple[str, ...]
    supporting_dependencies: tuple[str, ...]
    supporting_structural_risks: tuple[str, ...]
    supporting_structural_opportunities: tuple[str, ...]
    related_scheduled_events: tuple[str, ...]
    contradictory_conditions: tuple[str, ...]
    missing_preconditions: tuple[str, ...]
    plausible_transmission_mechanisms: tuple[TransmissionMechanism, ...]
    compatible_time_horizons: tuple[DurationBucket, ...]
    compatible_technical_directions: tuple[ScenarioDirection, ...]
    eligibility_status: CandidateEligibilityStatus
    relevance_score: int
    explainability_codes: tuple[str, ...]
    exclusion_reasons: tuple[str, ...]
    causal_signature: str
    content_hash: str = ""
    schema_version: str = COMPANY_EVENT_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_type",
            _enum_value(
                self.event_type,
                CompanyEventArchetypeType,
                field_name="event_type",
            ),
        )
        object.__setattr__(
            self,
            "event_classification",
            _enum_value(
                self.event_classification,
                CompanyEventClassification,
                field_name="event_classification",
            ),
        )
        object.__setattr__(
            self,
            "eligibility_status",
            _enum_value(
                self.eligibility_status,
                CandidateEligibilityStatus,
                field_name="eligibility_status",
            ),
        )
        object.__setattr__(
            self,
            "plausible_transmission_mechanisms",
            _enum_tuple(
                self.plausible_transmission_mechanisms,
                TransmissionMechanism,
                field_name="plausible_transmission_mechanisms",
            ),
        )
        object.__setattr__(
            self,
            "compatible_time_horizons",
            _enum_tuple(
                self.compatible_time_horizons,
                DurationBucket,
                field_name="compatible_time_horizons",
            ),
        )
        object.__setattr__(
            self,
            "compatible_technical_directions",
            _enum_tuple(
                self.compatible_technical_directions,
                ScenarioDirection,
                field_name="compatible_technical_directions",
            ),
        )
        for name in (
            "supporting_company_facts",
            "supporting_financial_states",
            "supporting_operational_states",
            "supporting_dependencies",
            "supporting_structural_risks",
            "supporting_structural_opportunities",
            "related_scheduled_events",
            "contradictory_conditions",
            "missing_preconditions",
            "explainability_codes",
            "exclusion_reasons",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        if (
            isinstance(self.relevance_score, bool)
            or not isinstance(self.relevance_score, int)
            or not 0 <= self.relevance_score <= 100
        ):
            raise ValueError("relevance_score must be an integer from 0 to 100.")

    @classmethod
    def create(cls, **values: Any) -> "CompanyEventCandidate":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyEventCandidate":
        return _simple_from_dict(cls, value)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeSnapshot(_JsonContract):
    snapshot_id: str
    schema_version: str
    symbol: str
    applicable_cutoff: str
    company_profile: CompanyProfile
    financial_state: CompanyFinancialState
    operational_state: CompanyOperationalState
    capital_structure_state: CapitalStructureState
    ownership_positioning_state: OwnershipPositioningState
    known_facts: tuple[CompanyKnownFact, ...]
    scheduled_events: tuple[ScheduledCompanyEvent, ...]
    historical_event_profiles: tuple[HistoricalCompanyEventProfile, ...]
    dependencies: tuple[CompanyDependency, ...]
    structural_risks: tuple[CompanyStructuralRisk, ...]
    structural_opportunities: tuple[CompanyStructuralOpportunity, ...]
    known_missing_information: tuple[str, ...]
    validation_result: ValidationResult
    source_metadata: Mapping[str, Any]
    source_input_hash: str
    created_at: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
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
            "created_at",
            _normalize_timestamp(
                self.created_at,
                field_name="created_at",
            ),
        )
        for name, model_type in (
            ("company_profile", CompanyProfile),
            ("financial_state", CompanyFinancialState),
            ("operational_state", CompanyOperationalState),
            ("capital_structure_state", CapitalStructureState),
            ("ownership_positioning_state", OwnershipPositioningState),
        ):
            if not isinstance(getattr(self, name), model_type):
                raise TypeError(f"{name} must be {model_type.__name__}.")
        for name, model_type in (
            ("known_facts", CompanyKnownFact),
            ("scheduled_events", ScheduledCompanyEvent),
            (
                "historical_event_profiles",
                HistoricalCompanyEventProfile,
            ),
            ("dependencies", CompanyDependency),
            ("structural_risks", CompanyStructuralRisk),
            (
                "structural_opportunities",
                CompanyStructuralOpportunity,
            ),
        ):
            object.__setattr__(
                self,
                name,
                _model_tuple(
                    getattr(self, name),
                    model_type,
                    field_name=name,
                ),
            )
        object.__setattr__(
            self,
            "known_missing_information",
            _string_tuple(
                self.known_missing_information,
                field_name="known_missing_information",
                sort_values=True,
            ),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")
        if not isinstance(self.source_metadata, Mapping):
            raise TypeError("source_metadata must be a mapping.")
        object.__setattr__(
            self,
            "source_metadata",
            _freeze_json(self.source_metadata),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyKnowledgeSnapshot":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        for name, model_type in (
            ("company_profile", CompanyProfile),
            ("financial_state", CompanyFinancialState),
            ("operational_state", CompanyOperationalState),
            ("capital_structure_state", CapitalStructureState),
            ("ownership_positioning_state", OwnershipPositioningState),
        ):
            raw[name] = model_type.from_dict(raw[name])
        for name, model_type in (
            ("known_facts", CompanyKnownFact),
            ("scheduled_events", ScheduledCompanyEvent),
            (
                "historical_event_profiles",
                HistoricalCompanyEventProfile,
            ),
            ("dependencies", CompanyDependency),
            ("structural_risks", CompanyStructuralRisk),
            (
                "structural_opportunities",
                CompanyStructuralOpportunity,
            ),
        ):
            raw[name] = tuple(
                model_type.from_dict(item)
                for item in raw.get(name, ())
            )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CompanyScenarioContext(_JsonContract):
    context_id: str
    company_knowledge_snapshot_hash: str
    symbol: str
    applicable_cutoff: str
    eligible_event_candidates: tuple[CompanyEventCandidate, ...]
    excluded_event_candidates: tuple[CompanyEventCandidate, ...]
    scheduled_events: tuple[ScheduledCompanyEvent, ...]
    structural_risks: tuple[CompanyStructuralRisk, ...]
    structural_opportunities: tuple[CompanyStructuralOpportunity, ...]
    dependencies: tuple[CompanyDependency, ...]
    missing_company_information: tuple[str, ...]
    warnings: tuple[str, ...]
    candidate_policy_version: str
    archetype_library_version: str
    archetype_library_hash: str
    frozen_technical_premise_hash: str
    current_market_state_hash: str
    relevant_exposure_ids: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_SCENARIO_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self,
            "applicable_cutoff",
            _normalize_timestamp(
                self.applicable_cutoff,
                field_name="applicable_cutoff",
            ),
        )
        for name, model_type in (
            ("eligible_event_candidates", CompanyEventCandidate),
            ("excluded_event_candidates", CompanyEventCandidate),
            ("scheduled_events", ScheduledCompanyEvent),
            ("structural_risks", CompanyStructuralRisk),
            (
                "structural_opportunities",
                CompanyStructuralOpportunity,
            ),
            ("dependencies", CompanyDependency),
        ):
            object.__setattr__(
                self,
                name,
                _model_tuple(
                    getattr(self, name),
                    model_type,
                    field_name=name,
                ),
            )
        for name in (
            "missing_company_information",
            "warnings",
            "relevant_exposure_ids",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                    sort_values=True,
                ),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyScenarioContext":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        for name, model_type in (
            ("eligible_event_candidates", CompanyEventCandidate),
            ("excluded_event_candidates", CompanyEventCandidate),
            ("scheduled_events", ScheduledCompanyEvent),
            ("structural_risks", CompanyStructuralRisk),
            (
                "structural_opportunities",
                CompanyStructuralOpportunity,
            ),
            ("dependencies", CompanyDependency),
        ):
            raw[name] = tuple(
                model_type.from_dict(item)
                for item in raw.get(name, ())
            )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeBuildResult(_JsonContract):
    status: CompanyKnowledgeBuildStatus
    snapshot: CompanyKnowledgeSnapshot | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    source_hash_before: str
    source_hash_after: str
    content_hash: str = ""
    schema_version: str = COMPANY_KNOWLEDGE_BUILD_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                CompanyKnowledgeBuildStatus,
                field_name="status",
            ),
        )
        if self.snapshot is not None and not isinstance(
            self.snapshot,
            CompanyKnowledgeSnapshot,
        ):
            raise TypeError(
                "snapshot must be CompanyKnowledgeSnapshot or null."
            )
        for name in ("errors", "warnings"):
            object.__setattr__(
                self,
                name,
                _string_tuple(
                    getattr(self, name),
                    field_name=name,
                ),
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyKnowledgeBuildResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("snapshot") is not None:
            raw["snapshot"] = CompanyKnowledgeSnapshot.from_dict(
                raw["snapshot"]
            )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CompanyMarketInputBridgeResult(_JsonContract):
    snapshot_hash: str
    evidence: tuple[EvidenceItem, ...]
    material_events: tuple[MaterialEvent, ...]
    invented_exposures: tuple[ExposureItem, ...]
    warnings: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_MARKET_INPUT_BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence",
            _model_tuple(
                self.evidence,
                EvidenceItem,
                field_name="evidence",
            ),
        )
        object.__setattr__(
            self,
            "material_events",
            _model_tuple(
                self.material_events,
                MaterialEvent,
                field_name="material_events",
            ),
        )
        object.__setattr__(
            self,
            "invented_exposures",
            _model_tuple(
                self.invented_exposures,
                ExposureItem,
                field_name="invented_exposures",
            ),
        )
        object.__setattr__(
            self,
            "warnings",
            _string_tuple(self.warnings, field_name="warnings"),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyMarketInputBridgeResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["evidence"] = tuple(
            EvidenceItem.from_dict(item)
            for item in raw.get("evidence", ())
        )
        raw["material_events"] = tuple(
            MaterialEvent.from_dict(item)
            for item in raw.get("material_events", ())
        )
        raw["invented_exposures"] = tuple(
            ExposureItem.from_dict(item)
            for item in raw.get("invented_exposures", ())
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeReplayPacket(_JsonContract):
    replay_id: str
    input: CompanyKnowledgeInput
    frozen_technical_premise: FrozenTechnicalPremise
    current_market_state: CurrentMarketState
    relevant_exposures: tuple[ExposureItem, ...]
    expected_snapshot_hash: str
    expected_candidate_hashes: tuple[str, ...]
    expected_scenario_context_hash: str
    schema_versions: Mapping[str, str]
    archetype_library: CompanyEventArchetypeLibrary
    archetype_library_version: str
    created_at: str
    content_hash: str = ""
    schema_version: str = COMPANY_KNOWLEDGE_REPLAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.input, CompanyKnowledgeInput):
            raise TypeError("input must be CompanyKnowledgeInput.")
        if not isinstance(
            self.frozen_technical_premise,
            FrozenTechnicalPremise,
        ):
            raise TypeError(
                "frozen_technical_premise must be FrozenTechnicalPremise."
            )
        if not isinstance(self.current_market_state, CurrentMarketState):
            raise TypeError(
                "current_market_state must be CurrentMarketState."
            )
        object.__setattr__(
            self,
            "relevant_exposures",
            _model_tuple(
                self.relevant_exposures,
                ExposureItem,
                field_name="relevant_exposures",
            ),
        )
        object.__setattr__(
            self,
            "expected_candidate_hashes",
            _string_tuple(
                self.expected_candidate_hashes,
                field_name="expected_candidate_hashes",
            ),
        )
        if not isinstance(self.schema_versions, Mapping):
            raise TypeError("schema_versions must be a mapping.")
        if not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in self.schema_versions.items()
        ):
            raise TypeError(
                "schema_versions must contain string keys and values."
            )
        object.__setattr__(
            self,
            "schema_versions",
            _freeze_json(self.schema_versions),
        )
        if not isinstance(
            self.archetype_library,
            CompanyEventArchetypeLibrary,
        ):
            raise TypeError(
                "archetype_library must be CompanyEventArchetypeLibrary."
            )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(
                self.created_at,
                field_name="created_at",
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "CompanyKnowledgeReplayPacket":
        seed = cls(**values, content_hash="")
        return replace(seed, content_hash=_contract_hash(seed))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyKnowledgeReplayPacket":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["input"] = CompanyKnowledgeInput.from_dict(raw["input"])
        raw["frozen_technical_premise"] = FrozenTechnicalPremise.from_dict(
            raw["frozen_technical_premise"]
        )
        raw["current_market_state"] = CurrentMarketState.from_dict(
            raw["current_market_state"]
        )
        raw["relevant_exposures"] = tuple(
            ExposureItem.from_dict(item)
            for item in raw.get("relevant_exposures", ())
        )
        raw["archetype_library"] = (
            CompanyEventArchetypeLibrary.from_dict(
                raw["archetype_library"]
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeReplayResult(_JsonContract):
    replay_id: str
    success: bool
    snapshot_hash: str
    candidate_hashes: tuple[str, ...]
    scenario_context_hash: str
    validation_result: ValidationResult
    errors: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_KNOWLEDGE_REPLAY_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_hashes",
            _string_tuple(
                self.candidate_hashes,
                field_name="candidate_hashes",
            ),
        )
        object.__setattr__(
            self,
            "errors",
            _string_tuple(self.errors, field_name="errors"),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyKnowledgeReplayResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


def company_knowledge_input_content_hash(
    value: CompanyKnowledgeInput,
) -> str:
    return _contract_hash(value)


def company_known_fact_content_hash(value: CompanyKnownFact) -> str:
    return _contract_hash(value)


def company_profile_content_hash(value: CompanyProfile) -> str:
    return _contract_hash(value)


def company_financial_state_content_hash(
    value: CompanyFinancialState,
) -> str:
    return _contract_hash(value)


def company_operational_state_content_hash(
    value: CompanyOperationalState,
) -> str:
    return _contract_hash(value)


def company_capital_structure_content_hash(
    value: CapitalStructureState,
) -> str:
    return _contract_hash(value)


def company_ownership_positioning_content_hash(
    value: OwnershipPositioningState,
) -> str:
    return _contract_hash(value)


def scheduled_company_event_content_hash(
    value: ScheduledCompanyEvent,
) -> str:
    return _contract_hash(value)


def historical_company_event_profile_content_hash(
    value: HistoricalCompanyEventProfile,
) -> str:
    return _contract_hash(value)


def company_dependency_content_hash(value: CompanyDependency) -> str:
    return _contract_hash(value)


def company_structural_risk_content_hash(
    value: CompanyStructuralRisk,
) -> str:
    return _contract_hash(value)


def company_structural_opportunity_content_hash(
    value: CompanyStructuralOpportunity,
) -> str:
    return _contract_hash(value)


def company_event_archetype_content_hash(
    value: CompanyEventArchetype,
) -> str:
    return _contract_hash(value)


def company_event_archetype_library_content_hash(
    value: CompanyEventArchetypeLibrary,
) -> str:
    return _contract_hash(value)


def company_event_candidate_content_hash(
    value: CompanyEventCandidate,
) -> str:
    return _contract_hash(value)


def company_knowledge_snapshot_content_hash(
    value: CompanyKnowledgeSnapshot,
) -> str:
    return _contract_hash(value)


def company_scenario_context_content_hash(
    value: CompanyScenarioContext,
) -> str:
    return _contract_hash(value)


def company_knowledge_replay_packet_content_hash(
    value: CompanyKnowledgeReplayPacket,
) -> str:
    return _contract_hash(value)


class _ValidationCollector:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []
        self.seen: set[str] = set()
        self.failed: set[str] = set()

    def require(
        self,
        code: str,
        condition: bool,
        path: str,
        message: str,
    ) -> None:
        self.seen.add(code)
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

    def result(
        self,
        *,
        frozen_hash_before: str = "",
        frozen_hash_after: str = "",
        version: str = COMPANY_KNOWLEDGE_VALIDATION_VERSION,
    ) -> ValidationResult:
        ordered = tuple(sorted(self.seen))
        failed = tuple(item for item in ordered if item in self.failed)
        passed = tuple(item for item in ordered if item not in self.failed)
        score = (
            round(100.0 * len(passed) / len(ordered), 2)
            if ordered
            else 100.0
        )
        return ValidationResult(
            is_valid=not self.errors,
            score=score,
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            failed_rules=failed,
            passed_rules=passed,
            frozen_hash_before=frozen_hash_before,
            frozen_hash_after=frozen_hash_after,
            validation_version=version,
        )


def _duplicates(values: Sequence[str]) -> set[str]:
    return {
        item
        for item, count in Counter(values).items()
        if count > 1
    }


def _valid_hash(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def validate_company_knowledge_input(
    value: CompanyKnowledgeInput,
) -> ValidationResult:
    if not isinstance(value, CompanyKnowledgeInput):
        raise TypeError("value must be CompanyKnowledgeInput.")
    result = _ValidationCollector()
    result.require(
        "input_schema_version",
        value.schema_version == COMPANY_KNOWLEDGE_INPUT_SCHEMA_VERSION,
        "schema_version",
        "Company-knowledge input schema version is unsupported.",
    )
    result.require(
        "input_content_hash",
        _valid_hash(value.content_hash)
        and value.content_hash == company_knowledge_input_content_hash(value),
        "content_hash",
        "Company-knowledge input hash is missing or mismatched.",
    )
    result.require(
        "input_identity",
        bool(value.input_id.strip())
        and value.input_id == _normalized_token(value.input_id)
        and bool(value.symbol)
        and bool(value.company_name.strip())
        and bool(value.exchange.strip()),
        "input_id",
        "Input ID, symbol, company name, and exchange are required.",
    )
    cutoff = _temporal_point(value.applicable_cutoff)
    created = _temporal_point(value.created_at)
    result.require(
        "input_cutoff",
        cutoff is not None and created is not None and cutoff <= created,
        "applicable_cutoff",
        "Applicable cutoff must not follow input creation time.",
    )
    result.require(
        "input_source_metadata",
        bool(value.source_metadata),
        "source_metadata",
        "Explicit source metadata is required.",
    )
    return result.result()


def validate_company_event_archetype_library(
    library: CompanyEventArchetypeLibrary,
) -> ValidationResult:
    if not isinstance(library, CompanyEventArchetypeLibrary):
        raise TypeError(
            "library must be CompanyEventArchetypeLibrary."
        )
    result = _ValidationCollector()
    ids = tuple(item.archetype_id for item in library.archetypes)
    result.require(
        "archetype_library_version",
        library.library_version
        == COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION,
        "library_version",
        "Archetype library version is unsupported.",
    )
    result.require(
        "archetype_library_hash",
        _valid_hash(library.content_hash)
        and library.content_hash
        == company_event_archetype_library_content_hash(library),
        "content_hash",
        "Archetype library hash is missing or mismatched.",
    )
    result.require(
        "archetype_library_identity",
        bool(library.library_id.strip())
        and not _duplicates(ids)
        and ids == tuple(sorted(ids)),
        "archetypes",
        "Archetype IDs must be unique and deterministically ordered.",
    )
    for index, item in enumerate(library.archetypes):
        result.require(
            "archetype_hash",
            _valid_hash(item.content_hash)
            and item.content_hash
            == company_event_archetype_content_hash(item),
            f"archetypes[{index}].content_hash",
            "Archetype hash is missing or mismatched.",
        )
        result.require(
            "archetype_controls",
            bool(item.archetype_id)
            and bool(item.applicable_company_stages)
            and bool(item.applicable_industries)
            and bool(item.transmission_mechanisms)
            and bool(item.expected_time_horizon_classes)
            and bool(item.compatible_technical_directions)
            and bool(item.falsification_conditions),
            f"archetypes[{index}]",
            "Archetype controlled applicability and falsification fields are required.",
        )
    return result.result()


def _hash_ok(item: Any, function: Any) -> bool:
    return _valid_hash(item.content_hash) and item.content_hash == function(item)


def validate_company_knowledge_snapshot(
    snapshot: CompanyKnowledgeSnapshot,
    *,
    source_input: CompanyKnowledgeInput | None = None,
) -> ValidationResult:
    if not isinstance(snapshot, CompanyKnowledgeSnapshot):
        raise TypeError("snapshot must be CompanyKnowledgeSnapshot.")
    result = _ValidationCollector()
    result.require(
        "snapshot_schema_version",
        snapshot.schema_version
        == COMPANY_KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION,
        "schema_version",
        "Company-knowledge snapshot schema version is unsupported.",
    )
    result.require(
        "snapshot_content_hash",
        _valid_hash(snapshot.content_hash)
        and snapshot.content_hash
        == company_knowledge_snapshot_content_hash(snapshot),
        "content_hash",
        "Snapshot content hash is missing or mismatched.",
    )
    result.require(
        "snapshot_identity",
        bool(snapshot.snapshot_id)
        and snapshot.snapshot_id == _normalized_token(snapshot.snapshot_id)
        and bool(snapshot.symbol),
        "snapshot_id",
        "Snapshot identity and symbol are required.",
    )
    if source_input is not None:
        if not isinstance(source_input, CompanyKnowledgeInput):
            raise TypeError(
                "source_input must be CompanyKnowledgeInput or null."
            )
        result.require(
            "snapshot_source_input",
            snapshot.source_input_hash == source_input.content_hash
            and snapshot.symbol == source_input.symbol
            and snapshot.applicable_cutoff
            == source_input.applicable_cutoff,
            "source_input_hash",
            "Snapshot does not preserve its source input identity.",
        )
    result.require(
        "snapshot_symbol_consistency",
        snapshot.company_profile.symbol == snapshot.symbol
        and all(
            item.symbol == snapshot.symbol
            for item in (
                snapshot.known_facts
                + snapshot.scheduled_events
                + snapshot.historical_event_profiles
            )
        ),
        "symbol",
        "Snapshot child records must use the snapshot symbol.",
    )
    cutoff = _temporal_point(snapshot.applicable_cutoff)
    event_cutoff_ok = all(
        _temporal_point(item.announced_at) is not None
        and cutoff is not None
        and _temporal_point(item.announced_at) <= cutoff
        and item.known_at_cutoff
        and item.applicable_cutoff == snapshot.applicable_cutoff
        for item in snapshot.scheduled_events
    )
    result.require(
        "scheduled_event_cutoff",
        event_cutoff_ok,
        "scheduled_events",
        "Scheduled events must have been announced and known by the cutoff.",
    )
    ids_by_group = {
        "fact": tuple(item.fact_id for item in snapshot.known_facts),
        "event": tuple(item.event_id for item in snapshot.scheduled_events),
        "historical": tuple(
            item.event_profile_id
            for item in snapshot.historical_event_profiles
        ),
        "dependency": tuple(
            item.dependency_id for item in snapshot.dependencies
        ),
        "risk": tuple(item.item_id for item in snapshot.structural_risks),
        "opportunity": tuple(
            item.item_id for item in snapshot.structural_opportunities
        ),
    }
    result.require(
        "snapshot_duplicate_ids",
        all(not _duplicates(values) for values in ids_by_group.values()),
        "snapshot",
        "Snapshot child IDs must be unique within each record family.",
    )
    fact_ids = set(ids_by_group["fact"])
    event_ids = set(ids_by_group["event"])
    dependency_ids = set(ids_by_group["dependency"])
    references_ok = all(
        set(item.supporting_fact_ids) <= fact_ids
        and set(item.scheduled_event_ids) <= event_ids
        and set(item.dependency_ids) <= dependency_ids
        for item in (
            snapshot.structural_risks
            + snapshot.structural_opportunities
        )
    )
    result.require(
        "snapshot_references",
        references_ok,
        "structural_risks",
        "Structural condition references must resolve within the snapshot.",
    )
    all_provenance = (
        [snapshot.source_metadata]
        + [item.provenance for item in snapshot.known_facts]
        + [item.provenance for item in snapshot.dependencies]
        + [item.provenance for item in snapshot.structural_risks]
        + [
            item.provenance
            for item in snapshot.structural_opportunities
        ]
        + [
            item.provenance
            for item in snapshot.historical_event_profiles
        ]
    )
    result.require(
        "snapshot_provenance",
        all(bool(item) for item in all_provenance),
        "source_metadata",
        "Every supplied company record requires explicit provenance.",
    )
    hash_checks = (
        (
            snapshot.company_profile,
            company_profile_content_hash,
        ),
        (
            snapshot.financial_state,
            company_financial_state_content_hash,
        ),
        (
            snapshot.operational_state,
            company_operational_state_content_hash,
        ),
        (
            snapshot.capital_structure_state,
            company_capital_structure_content_hash,
        ),
        (
            snapshot.ownership_positioning_state,
            company_ownership_positioning_content_hash,
        ),
    )
    child_hashes_ok = all(
        _hash_ok(item, function)
        for item, function in hash_checks
    )
    child_hashes_ok = child_hashes_ok and all(
        _hash_ok(item, function)
        for records, function in (
            (snapshot.known_facts, company_known_fact_content_hash),
            (
                snapshot.scheduled_events,
                scheduled_company_event_content_hash,
            ),
            (
                snapshot.historical_event_profiles,
                historical_company_event_profile_content_hash,
            ),
            (
                snapshot.dependencies,
                company_dependency_content_hash,
            ),
            (
                snapshot.structural_risks,
                company_structural_risk_content_hash,
            ),
            (
                snapshot.structural_opportunities,
                company_structural_opportunity_content_hash,
            ),
        )
        for item in records
    )
    result.require(
        "snapshot_child_hashes",
        child_hashes_ok,
        "snapshot",
        "One or more snapshot child hashes are invalid.",
    )
    result.warn(
        "snapshot_sparse_information",
        not snapshot.known_missing_information,
        "known_missing_information",
        "Snapshot contains explicitly missing company information.",
    )
    return result.result(
        frozen_hash_before=snapshot.source_input_hash,
        frozen_hash_after=snapshot.source_input_hash,
    )


def validate_company_scenario_context(
    context: CompanyScenarioContext,
    *,
    snapshot: CompanyKnowledgeSnapshot | None = None,
    premise: FrozenTechnicalPremise | None = None,
    state: CurrentMarketState | None = None,
    exposures: Sequence[ExposureItem] = (),
) -> ValidationResult:
    if not isinstance(context, CompanyScenarioContext):
        raise TypeError("context must be CompanyScenarioContext.")
    result = _ValidationCollector()
    result.require(
        "context_schema_version",
        context.schema_version == COMPANY_SCENARIO_CONTEXT_SCHEMA_VERSION,
        "schema_version",
        "Company scenario context schema version is unsupported.",
    )
    result.require(
        "context_content_hash",
        _valid_hash(context.content_hash)
        and context.content_hash
        == company_scenario_context_content_hash(context),
        "content_hash",
        "Company scenario context hash is missing or mismatched.",
    )
    if snapshot is not None:
        result.require(
            "context_snapshot_linkage",
            context.company_knowledge_snapshot_hash
            == snapshot.content_hash
            and context.symbol == snapshot.symbol
            and context.applicable_cutoff == snapshot.applicable_cutoff,
            "company_knowledge_snapshot_hash",
            "Context does not preserve its company snapshot.",
        )
    if premise is not None and state is not None:
        result.require(
            "context_technical_linkage",
            context.frozen_technical_premise_hash
            == premise.frozen_content_hash
            and context.current_market_state_hash == state.content_hash
            and context.symbol == premise.symbol == state.symbol
            and context.applicable_cutoff == state.applicable_cutoff,
            "frozen_technical_premise_hash",
            "Context technical premise or market-state linkage is invalid.",
        )
    exposure_items = tuple(exposures)
    if exposure_items:
        result.require(
            "context_exposure_linkage",
            set(context.relevant_exposure_ids)
            <= {item.exposure_id for item in exposure_items},
            "relevant_exposure_ids",
            "Context contains an unknown exposure reference.",
        )
    candidates = (
        context.eligible_event_candidates
        + context.excluded_event_candidates
    )
    candidate_ids = tuple(item.candidate_id for item in candidates)
    result.require(
        "context_candidate_identity",
        not _duplicates(candidate_ids)
        and all(
            _hash_ok(item, company_event_candidate_content_hash)
            for item in candidates
        ),
        "eligible_event_candidates",
        "Company candidates require unique identities and stable hashes.",
    )
    result.require(
        "context_candidate_partition",
        all(
            item.eligibility_status is CandidateEligibilityStatus.ELIGIBLE
            for item in context.eligible_event_candidates
        )
        and all(
            item.eligibility_status
            is not CandidateEligibilityStatus.ELIGIBLE
            for item in context.excluded_event_candidates
        ),
        "eligible_event_candidates",
        "Candidate eligibility partitions are inconsistent.",
    )
    result.require(
        "context_candidate_policy",
        context.candidate_policy_version
        == COMPANY_EVENT_CANDIDATE_POLICY_VERSION
        and context.archetype_library_version
        == COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION
        and _valid_hash(context.archetype_library_hash),
        "candidate_policy_version",
        "Candidate policy or archetype library provenance is invalid.",
    )
    return result.result()


def _state_value(
    source: Mapping[str, Any],
    name: str,
    *,
    missing: list[str],
    warnings: list[str],
) -> CompanyStateValue:
    raw = source.get(name)
    if raw is None:
        missing.append(name)
        return CompanyStateValue.UNKNOWN
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        distinct = tuple(dict.fromkeys(str(item) for item in raw))
        if len(distinct) != 1:
            missing.append(name)
            warnings.append(f"contradictory_state:{name}")
            return CompanyStateValue.UNKNOWN
        raw = distinct[0]
    try:
        return CompanyStateValue(raw)
    except (TypeError, ValueError):
        missing.append(name)
        warnings.append(f"unsupported_state:{name}")
        return CompanyStateValue.UNKNOWN


def _classification_value(
    source: Mapping[str, Any],
    name: str,
    *,
    warnings: list[str],
) -> CompanyClassification:
    raw = source.get(name, CompanyClassification.UNKNOWN.value)
    try:
        return CompanyClassification(raw)
    except (TypeError, ValueError):
        warnings.append(f"unsupported_classification:{name}")
        return CompanyClassification.UNKNOWN


def _source_provenance(
    item: Mapping[str, Any],
    default: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = item.get("provenance")
    return raw if isinstance(raw, Mapping) and raw else default


def _tuple_value(
    source: Mapping[str, Any],
    name: str,
) -> tuple[str, ...]:
    raw = source.get(name, ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw)
    raise TypeError(f"{name} must be text or a sequence of text.")


def _fact_references(
    item: Mapping[str, Any],
    input_id: str,
) -> tuple[str, ...]:
    direct = _tuple_value(item, "supporting_fact_ids")
    fields_supplied = _tuple_value(item, "supporting_fields")
    generated = tuple(
        company_known_fact_id(
            input_id,
            *(value.split(".", 1) if "." in value else ("company", value)),
        )
        for value in fields_supplied
    )
    return tuple(dict.fromkeys(direct + generated))


def _create_known_facts(
    value: CompanyKnowledgeInput,
) -> tuple[CompanyKnownFact, ...]:
    sources = (
        ("profile", value.company_profile_inputs),
        ("financial", value.financial_state_inputs),
        ("operational", value.operational_state_inputs),
        ("capital", value.capital_structure_inputs),
        ("ownership", value.ownership_and_positioning_inputs),
    )
    facts: list[CompanyKnownFact] = []
    omitted = {
        "data_as_of",
        "quantitative_values",
        "space_extension",
        "biotech_extension",
        "software_extension",
        "provenance",
    }
    for section, source in sources:
        data_as_of = source.get("data_as_of")
        provenance = _source_provenance(source, value.source_metadata)
        for field_name, raw in sorted(source.items()):
            if field_name in omitted or raw is None:
                continue
            if isinstance(raw, str) and raw in {
                CompanyStateValue.UNKNOWN.value,
                CompanyStateValue.UNAVAILABLE.value,
            }:
                continue
            fact = CompanyKnownFact.create(
                fact_id=company_known_fact_id(
                    value.input_id,
                    section,
                    field_name,
                ),
                symbol=value.symbol,
                section=section,
                field_name=field_name,
                value=raw,
                data_as_of=data_as_of,
                applicable_cutoff=value.applicable_cutoff,
                provenance=provenance,
            )
            facts.append(fact)
    return tuple(sorted(facts, key=lambda item: item.fact_id))


def _build_profile(
    value: CompanyKnowledgeInput,
    warnings: list[str],
) -> CompanyProfile:
    source = value.company_profile_inputs
    try:
        stage = CompanyStage(
            source.get("company_stage", CompanyStage.UNKNOWN.value)
        )
    except (TypeError, ValueError):
        stage = CompanyStage.UNKNOWN
        warnings.append("unsupported_company_stage")
    return CompanyProfile.create(
        company_id=_normalized_token(
            str(source.get("company_id") or f"company_{value.symbol}")
        ),
        symbol=value.symbol,
        legal_name=str(
            source.get("legal_name") or value.company_name
        ),
        common_name=str(
            source.get("common_name") or value.company_name
        ),
        exchange=value.exchange,
        jurisdiction=value.jurisdiction,
        sector=value.sector,
        industry=value.industry,
        business_model=value.business_model,
        revenue_model=str(source.get("revenue_model") or "unknown"),
        primary_products=_tuple_value(source, "primary_products"),
        primary_services=_tuple_value(source, "primary_services"),
        geographic_exposure=_tuple_value(
            source,
            "geographic_exposure",
        ),
        customer_types=_tuple_value(source, "customer_types"),
        company_stage=stage,
        operating_history_class=_classification_value(
            source,
            "operating_history_class",
            warnings=warnings,
        ),
        cyclicality_class=_classification_value(
            source,
            "cyclicality_class",
            warnings=warnings,
        ),
        capital_intensity_class=_classification_value(
            source,
            "capital_intensity_class",
            warnings=warnings,
        ),
        regulatory_intensity_class=_classification_value(
            source,
            "regulatory_intensity_class",
            warnings=warnings,
        ),
    )


def _build_financial(
    value: CompanyKnowledgeInput,
    warnings: list[str],
) -> CompanyFinancialState:
    source = value.financial_state_inputs
    missing: list[str] = []
    states = {
        name: _state_value(
            source,
            name,
            missing=missing,
            warnings=warnings,
        )
        for name in _FINANCIAL_STATE_FIELDS
    }
    quantitative = source.get("quantitative_values", {})
    if not isinstance(quantitative, Mapping):
        warnings.append("invalid_financial_quantitative_values")
        quantitative = {}
    return CompanyFinancialState.create(
        state_id=_normalized_token(
            str(
                source.get("state_id")
                or f"financial_{value.input_id}"
            )
        ),
        **states,
        quantitative_values=quantitative,
        data_as_of=source.get("data_as_of"),
        applicable_cutoff=value.applicable_cutoff,
        missing_fields=tuple(missing),
    )


def _extension_state(
    source: Mapping[str, Any],
    fields_required: Sequence[str],
) -> dict[str, CompanyStateValue]:
    result: dict[str, CompanyStateValue] = {}
    for name in fields_required:
        try:
            result[name] = CompanyStateValue(
                source.get(name, CompanyStateValue.UNKNOWN.value)
            )
        except (TypeError, ValueError):
            result[name] = CompanyStateValue.UNKNOWN
    return result


def _build_operational(
    value: CompanyKnowledgeInput,
    warnings: list[str],
) -> CompanyOperationalState:
    source = value.operational_state_inputs
    missing: list[str] = []
    states = {
        name: _state_value(
            source,
            name,
            missing=missing,
            warnings=warnings,
        )
        for name in _OPERATIONAL_STATE_FIELDS
    }
    space = None
    raw_space = source.get("space_extension")
    if isinstance(raw_space, Mapping):
        space = SpaceOperationalExtension(
            **_extension_state(
                raw_space,
                (
                    "launch_cadence_state",
                    "launch_reliability_state",
                    "mission_backlog_state",
                    "launch_site_dependency_state",
                    "payload_concentration_state",
                    "government_contract_exposure_state",
                ),
            )
        )
    biotech = None
    raw_biotech = source.get("biotech_extension")
    if isinstance(raw_biotech, Mapping):
        biotech = BiotechOperationalExtension(
            trial_phase=str(raw_biotech.get("trial_phase") or "unknown"),
            trial_readout_schedule=_tuple_value(
                raw_biotech,
                "trial_readout_schedule",
            ),
            **_extension_state(
                raw_biotech,
                (
                    "cash_runway_state",
                    "regulatory_milestone_state",
                    "product_concentration_state",
                ),
            ),
        )
    software = None
    raw_software = source.get("software_extension")
    if isinstance(raw_software, Mapping):
        software = SoftwareOperationalExtension(
            **_extension_state(
                raw_software,
                (
                    "subscriber_growth_state",
                    "retention_state",
                    "customer_acquisition_efficiency_state",
                    "cloud_infrastructure_exposure_state",
                    "enterprise_contract_concentration_state",
                ),
            )
        )
    return CompanyOperationalState.create(
        state_id=_normalized_token(
            str(
                source.get("state_id")
                or f"operational_{value.input_id}"
            )
        ),
        **states,
        operational_bottlenecks=_tuple_value(
            source,
            "operational_bottlenecks",
        ),
        space_extension=space,
        biotech_extension=biotech,
        software_extension=software,
        data_as_of=source.get("data_as_of"),
        missing_fields=tuple(missing),
    )


def _build_capital(
    value: CompanyKnowledgeInput,
    warnings: list[str],
) -> CapitalStructureState:
    source = value.capital_structure_inputs
    state_names = (
        "share_count_state",
        "recent_dilution_state",
        "warrant_overhang",
        "employee_compensation_dilution",
        "capital_access_state",
    )
    missing: list[str] = []
    states = {
        name: _state_value(
            source,
            name,
            missing=missing,
            warnings=warnings,
        )
        for name in state_names
    }
    capacity = source.get("authorized_share_capacity")
    if capacity is not None and (
        isinstance(capacity, bool)
        or not isinstance(capacity, (int, float))
        or not math.isfinite(float(capacity))
        or float(capacity) < 0
    ):
        warnings.append("invalid_authorized_share_capacity")
        capacity = None
    return CapitalStructureState.create(
        state_id=_normalized_token(
            str(source.get("state_id") or f"capital_{value.input_id}")
        ),
        **states,
        authorized_share_capacity=capacity,
        equity_raise_history=_tuple_value(
            source,
            "equity_raise_history",
        ),
        debt_instruments=_tuple_value(source, "debt_instruments"),
        convertible_instruments=_tuple_value(
            source,
            "convertible_instruments",
        ),
        refinancing_requirements=_tuple_value(
            source,
            "refinancing_requirements",
        ),
        covenant_constraints=_tuple_value(
            source,
            "covenant_constraints",
        ),
        maturity_schedule=_tuple_value(source, "maturity_schedule"),
        financing_optionalities=_tuple_value(
            source,
            "financing_optionalities",
        ),
        data_as_of=source.get("data_as_of"),
        missing_fields=tuple(missing),
    )


def _build_ownership(
    value: CompanyKnowledgeInput,
    warnings: list[str],
) -> OwnershipPositioningState:
    source = value.ownership_and_positioning_inputs
    state_names = (
        "insider_ownership_state",
        "institutional_ownership_state",
        "concentrated_holder_state",
        "short_interest_state",
        "borrow_availability_state",
        "option_positioning_state",
        "lockup_state",
        "insider_transaction_state",
        "index_membership_state",
        "passive_flow_exposure",
    )
    missing: list[str] = []
    states = {
        name: _state_value(
            source,
            name,
            missing=missing,
            warnings=warnings,
        )
        for name in state_names
    }
    return OwnershipPositioningState.create(
        state_id=_normalized_token(
            str(
                source.get("state_id")
                or f"ownership_{value.input_id}"
            )
        ),
        **states,
        known_positioning_events=_tuple_value(
            source,
            "known_positioning_events",
        ),
        data_as_of=source.get("data_as_of"),
        missing_fields=tuple(missing),
    )


def _build_scheduled_events(
    value: CompanyKnowledgeInput,
) -> tuple[ScheduledCompanyEvent, ...]:
    events: list[ScheduledCompanyEvent] = []
    for index, source in enumerate(value.scheduled_event_inputs):
        event_id = _normalized_token(
            str(
                source.get("event_id")
                or f"company_event_{value.input_id}_{index + 1}"
            )
        )
        announced_at = source.get("announced_at")
        if announced_at is None:
            raise ValueError(
                f"scheduled_event_inputs[{index}].announced_at is required."
            )
        events.append(
            ScheduledCompanyEvent.create(
                event_id=event_id,
                event_type=source.get(
                    "event_type",
                    CompanyEventType.OTHER.value,
                ),
                title=str(source.get("title") or event_id),
                symbol=str(source.get("symbol") or value.symbol),
                announced_at=announced_at,
                scheduled_start=source.get("scheduled_start"),
                scheduled_end=source.get("scheduled_end"),
                date_precision=source.get(
                    "date_precision",
                    EventDatePrecision.UNKNOWN.value,
                ),
                status=source.get(
                    "status",
                    CompanyEventStatus.UNCERTAIN.value,
                ),
                recurrence=source.get(
                    "recurrence",
                    EventRecurrenceClass.UNKNOWN.value,
                ),
                source_type=str(source.get("source_type") or "unknown"),
                source_reference=str(
                    source.get("source_reference") or ""
                ),
                known_at_cutoff=source.get("known_at_cutoff", False),
                related_product=source.get("related_product"),
                related_contract=source.get("related_contract"),
                related_regulator=source.get("related_regulator"),
                related_customer=source.get("related_customer"),
                expected_information_type=_tuple_value(
                    source,
                    "expected_information_type",
                ),
                applicable_cutoff=value.applicable_cutoff,
            )
        )
    return tuple(sorted(events, key=lambda item: item.event_id))


def _build_dependencies(
    value: CompanyKnowledgeInput,
) -> tuple[CompanyDependency, ...]:
    dependencies: list[CompanyDependency] = []
    for index, source in enumerate(value.dependency_inputs):
        dependency_id = _normalized_token(
            str(
                source.get("dependency_id")
                or f"dependency_{value.input_id}_{index + 1}"
            )
        )
        dependencies.append(
            CompanyDependency.create(
                dependency_id=dependency_id,
                dependency_type=source.get(
                    "dependency_type",
                    CompanyDependencyType.CONTRACT.value,
                ),
                subject=str(source.get("subject") or value.company_name),
                dependency_target=str(
                    source.get("dependency_target") or "unknown"
                ),
                concentration_class=source.get(
                    "concentration_class",
                    ConcentrationClass.UNKNOWN.value,
                ),
                substitutability_class=source.get(
                    "substitutability_class",
                    SubstitutabilityClass.UNKNOWN.value,
                ),
                time_to_replace_class=source.get(
                    "time_to_replace_class",
                    ReplacementTimeClass.UNKNOWN.value,
                ),
                operational_impact_class=source.get(
                    "operational_impact_class",
                    ImpactClass.UNKNOWN.value,
                ),
                financial_impact_class=source.get(
                    "financial_impact_class",
                    ImpactClass.UNKNOWN.value,
                ),
                known_mitigants=_tuple_value(
                    source,
                    "known_mitigants",
                ),
                applicable_cutoff=value.applicable_cutoff,
                provenance=_source_provenance(
                    source,
                    value.source_metadata,
                ),
            )
        )
    return tuple(
        sorted(dependencies, key=lambda item: item.dependency_id)
    )


def _build_historical_profiles(
    value: CompanyKnowledgeInput,
) -> tuple[HistoricalCompanyEventProfile, ...]:
    profiles: list[HistoricalCompanyEventProfile] = []
    for index, source in enumerate(
        value.historical_company_event_inputs
    ):
        profile_id = _normalized_token(
            str(
                source.get("event_profile_id")
                or f"event_profile_{value.input_id}_{index + 1}"
            )
        )
        profiles.append(
            HistoricalCompanyEventProfile.create(
                event_profile_id=profile_id,
                symbol=str(source.get("symbol") or value.symbol),
                event_type=source.get(
                    "event_type",
                    CompanyEventArchetypeType.EARNINGS_MISS.value,
                ),
                observed_event_count=int(
                    source.get("observed_event_count", 0)
                ),
                observation_start=source.get("observation_start"),
                observation_end=source.get("observation_end"),
                recurrence_class=source.get(
                    "recurrence_class",
                    EventRecurrenceClass.UNKNOWN.value,
                ),
                historical_outcome_classes=_tuple_value(
                    source,
                    "historical_outcome_classes",
                ),
                common_preconditions=_tuple_value(
                    source,
                    "common_preconditions",
                ),
                common_transmission_mechanisms=tuple(
                    source.get("common_transmission_mechanisms", ())
                ),
                known_amplifiers=_tuple_value(
                    source,
                    "known_amplifiers",
                ),
                known_dampeners=_tuple_value(
                    source,
                    "known_dampeners",
                ),
                data_quality=source.get(
                    "data_quality",
                    CompanyDataQuality.UNKNOWN.value,
                ),
                provenance=_source_provenance(
                    source,
                    value.source_metadata,
                ),
            )
        )
    return tuple(
        sorted(profiles, key=lambda item: item.event_profile_id)
    )


def _build_structural_risks(
    value: CompanyKnowledgeInput,
) -> tuple[CompanyStructuralRisk, ...]:
    risks: list[CompanyStructuralRisk] = []
    for index, source in enumerate(value.known_risk_inputs):
        item_id = _normalized_token(
            str(
                source.get("item_id")
                or f"risk_{value.input_id}_{index + 1}"
            )
        )
        risks.append(
            CompanyStructuralRisk.create(
                item_id=item_id,
                type=source.get(
                    "type",
                    CompanyStructuralRiskType.EXECUTION.value,
                ),
                description=str(source.get("description") or item_id),
                supporting_fact_ids=_fact_references(
                    source,
                    value.input_id,
                ),
                dependency_ids=_tuple_value(
                    source,
                    "dependency_ids",
                ),
                scheduled_event_ids=_tuple_value(
                    source,
                    "scheduled_event_ids",
                ),
                preconditions=_tuple_value(source, "preconditions"),
                invalidating_conditions=_tuple_value(
                    source,
                    "invalidating_conditions",
                ),
                status=source.get(
                    "status",
                    StructuralConditionStatus.UNKNOWN.value,
                ),
                provenance=_source_provenance(
                    source,
                    value.source_metadata,
                ),
            )
        )
    return tuple(sorted(risks, key=lambda item: item.item_id))


def _build_structural_opportunities(
    value: CompanyKnowledgeInput,
) -> tuple[CompanyStructuralOpportunity, ...]:
    opportunities: list[CompanyStructuralOpportunity] = []
    for index, source in enumerate(value.known_opportunity_inputs):
        item_id = _normalized_token(
            str(
                source.get("item_id")
                or f"opportunity_{value.input_id}_{index + 1}"
            )
        )
        opportunities.append(
            CompanyStructuralOpportunity.create(
                item_id=item_id,
                type=source.get(
                    "type",
                    CompanyStructuralOpportunityType.PRODUCT_SUCCESS.value,
                ),
                description=str(source.get("description") or item_id),
                supporting_fact_ids=_fact_references(
                    source,
                    value.input_id,
                ),
                dependency_ids=_tuple_value(
                    source,
                    "dependency_ids",
                ),
                scheduled_event_ids=_tuple_value(
                    source,
                    "scheduled_event_ids",
                ),
                preconditions=_tuple_value(source, "preconditions"),
                invalidating_conditions=_tuple_value(
                    source,
                    "invalidating_conditions",
                ),
                status=source.get(
                    "status",
                    StructuralConditionStatus.UNKNOWN.value,
                ),
                provenance=_source_provenance(
                    source,
                    value.source_metadata,
                ),
            )
        )
    return tuple(
        sorted(opportunities, key=lambda item: item.item_id)
    )


def _finalize_snapshot(
    seed: CompanyKnowledgeSnapshot,
    *,
    source_input: CompanyKnowledgeInput,
) -> CompanyKnowledgeSnapshot:
    unhashed = replace(
        seed,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    hashed = replace(
        unhashed,
        content_hash=company_knowledge_snapshot_content_hash(unhashed),
    )
    validation = validate_company_knowledge_snapshot(
        hashed,
        source_input=source_input,
    )
    finalized = replace(
        hashed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=company_knowledge_snapshot_content_hash(finalized),
    )
    repeated = validate_company_knowledge_snapshot(
        finalized,
        source_input=source_input,
    )
    if repeated != validation:
        raise RuntimeError(
            "Company-knowledge snapshot validation did not stabilize."
        )
    return finalized


class CompanyKnowledgeEngine:
    """Build a deterministic snapshot from supplied structured data."""

    def build(
        self,
        value: CompanyKnowledgeInput,
    ) -> CompanyKnowledgeBuildResult:
        if not isinstance(value, CompanyKnowledgeInput):
            raise TypeError("value must be CompanyKnowledgeInput.")
        before = company_knowledge_input_content_hash(value)
        input_validation = validate_company_knowledge_input(value)
        if not input_validation.is_valid:
            seed = CompanyKnowledgeBuildResult(
                status=CompanyKnowledgeBuildStatus.FAILED,
                snapshot=None,
                errors=tuple(
                    item.message for item in input_validation.errors
                ),
                warnings=tuple(
                    item.message for item in input_validation.warnings
                ),
                source_hash_before=before,
                source_hash_after=company_knowledge_input_content_hash(value),
            )
            return replace(seed, content_hash=_contract_hash(seed))

        warnings: list[str] = []
        try:
            profile = _build_profile(value, warnings)
            financial = _build_financial(value, warnings)
            operational = _build_operational(value, warnings)
            capital = _build_capital(value, warnings)
            ownership = _build_ownership(value, warnings)
            facts = _create_known_facts(value)
            events = _build_scheduled_events(value)
            profiles = _build_historical_profiles(value)
            dependencies = _build_dependencies(value)
            risks = _build_structural_risks(value)
            opportunities = _build_structural_opportunities(value)
        except (TypeError, ValueError) as exc:
            after = company_knowledge_input_content_hash(value)
            seed = CompanyKnowledgeBuildResult(
                status=CompanyKnowledgeBuildStatus.FAILED,
                snapshot=None,
                errors=(str(exc),),
                warnings=tuple(sorted(set(warnings))),
                source_hash_before=before,
                source_hash_after=after,
            )
            return replace(seed, content_hash=_contract_hash(seed))
        missing = tuple(
            sorted(
                {
                    *(f"financial.{item}" for item in financial.missing_fields),
                    *(
                        f"operational.{item}"
                        for item in operational.missing_fields
                    ),
                    *(f"capital.{item}" for item in capital.missing_fields),
                    *(
                        f"ownership.{item}"
                        for item in ownership.missing_fields
                    ),
                    *(
                        ("profile.company_stage",)
                        if profile.company_stage is CompanyStage.UNKNOWN
                        else ()
                    ),
                }
            )
        )
        snapshot_seed = CompanyKnowledgeSnapshot(
            snapshot_id=_normalized_token(
                f"company_snapshot_{value.input_id}"
            ),
            schema_version=COMPANY_KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION,
            symbol=value.symbol,
            applicable_cutoff=value.applicable_cutoff,
            company_profile=profile,
            financial_state=financial,
            operational_state=operational,
            capital_structure_state=capital,
            ownership_positioning_state=ownership,
            known_facts=facts,
            scheduled_events=events,
            historical_event_profiles=profiles,
            dependencies=dependencies,
            structural_risks=risks,
            structural_opportunities=opportunities,
            known_missing_information=missing,
            validation_result=ValidationResult.unvalidated(),
            source_metadata=value.source_metadata,
            source_input_hash=value.content_hash,
            created_at=value.created_at,
        )
        snapshot = _finalize_snapshot(
            snapshot_seed,
            source_input=value,
        )
        after = company_knowledge_input_content_hash(value)
        if before != after:
            raise RuntimeError(
                "CompanyKnowledgeEngine mutated its source input."
            )
        errors = tuple(
            item.message for item in snapshot.validation_result.errors
        )
        validation_warnings = tuple(
            item.message for item in snapshot.validation_result.warnings
        )
        all_warnings = tuple(sorted(set(warnings) | set(validation_warnings)))
        status = (
            CompanyKnowledgeBuildStatus.FAILED
            if errors
            else (
                CompanyKnowledgeBuildStatus.COMPLETED_WITH_WARNINGS
                if all_warnings or missing
                else CompanyKnowledgeBuildStatus.COMPLETED
            )
        )
        result_seed = CompanyKnowledgeBuildResult(
            status=status,
            snapshot=snapshot,
            errors=errors,
            warnings=all_warnings,
            source_hash_before=before,
            source_hash_after=after,
        )
        return replace(
            result_seed,
            content_hash=_contract_hash(result_seed),
        )


_ALL_STAGES = tuple(CompanyStage)
_OPERATING_STAGES = tuple(
    item for item in CompanyStage if item is not CompanyStage.UNKNOWN
)
_ALL_DIRECTIONS = (
    ScenarioDirection.UP,
    ScenarioDirection.DOWN,
    ScenarioDirection.SIDEWAYS,
)
_ALL_HORIZONS = (
    DurationBucket.DAYS,
    DurationBucket.WEEKS,
    DurationBucket.MONTHS,
    DurationBucket.YEARS,
)


def _archetype(
    archetype_type: CompanyEventArchetypeType,
    *,
    required: Sequence[str],
    optional: Sequence[str],
    contradictory: Sequence[str],
    mechanisms: Sequence[TransmissionMechanism],
    exposures: Sequence[ExposureType],
    directions: Sequence[ScenarioDirection],
    stages: Sequence[CompanyStage] = _OPERATING_STAGES,
    industries: Sequence[str] = ("*",),
    horizons: Sequence[DurationBucket] = _ALL_HORIZONS,
    classification: CompanyEventClassification = (
        CompanyEventClassification.HYPOTHETICAL_FUTURE_EVENT
    ),
    amplifiers: Sequence[str] = (
        "broader market conditions could amplify repricing",
    ),
    dampeners: Sequence[str] = (
        "opposing company evidence could dampen repricing",
    ),
    falsification: Sequence[str] = (
        "the required company preconditions no longer hold",
    ),
) -> CompanyEventArchetype:
    return CompanyEventArchetype.create(
        archetype_id=f"company_archetype_{archetype_type.value}",
        archetype_type=archetype_type,
        applicable_company_stages=tuple(stages),
        applicable_industries=tuple(industries),
        required_preconditions=tuple(required),
        optional_preconditions=tuple(optional),
        contradictory_conditions=tuple(contradictory),
        initiating_event_class=classification,
        transmission_mechanisms=tuple(mechanisms),
        common_amplifiers=tuple(amplifiers),
        common_dampeners=tuple(dampeners),
        expected_time_horizon_classes=tuple(horizons),
        compatible_exposure_types=tuple(exposures),
        compatible_technical_directions=tuple(directions),
        falsification_conditions=tuple(falsification),
    )


def default_company_event_archetype_library(
) -> CompanyEventArchetypeLibrary:
    """Return the immutable Phase 10 deterministic archetype library."""

    archetypes = (
        _archetype(
            CompanyEventArchetypeType.EARNINGS_MISS,
            required=(
                "any:financial:guidance_state=deteriorating|financial:revenue_growth_state=weak|risk:demand_weakness",
            ),
            optional=(
                "scheduled:earnings_release",
                "financial:estimate_dispersion_state=high",
            ),
            contradictory=(
                "financial:revenue_growth_state=strong",
                "financial:guidance_state=improving",
            ),
            mechanisms=(
                TransmissionMechanism.EARNINGS_REVISION,
                TransmissionMechanism.MULTIPLE_COMPRESSION,
            ),
            exposures=(
                ExposureType.EARNINGS_RISK,
                ExposureType.REVENUE_GROWTH_RISK,
            ),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.EARNINGS_BEAT,
            required=(
                "any:financial:revenue_growth_state=strong|financial:revenue_growth_state=improving|opportunity:demand_acceleration",
            ),
            optional=(
                "scheduled:earnings_release",
                "financial:gross_margin_state=improving",
            ),
            contradictory=(
                "financial:revenue_growth_state=weak",
                "financial:guidance_state=deteriorating",
            ),
            mechanisms=(
                TransmissionMechanism.EARNINGS_REVISION,
                TransmissionMechanism.MULTIPLE_EXPANSION,
            ),
            exposures=(
                ExposureType.EARNINGS_RISK,
                ExposureType.REVENUE_VISIBILITY,
            ),
            directions=(ScenarioDirection.UP,),
        ),
        _archetype(
            CompanyEventArchetypeType.GUIDANCE_REDUCTION,
            required=(
                "any:financial:guidance_state=deteriorating|risk:demand_weakness|risk:execution",
            ),
            optional=("scheduled:earnings_call",),
            contradictory=("financial:guidance_state=improving",),
            mechanisms=(
                TransmissionMechanism.GUIDANCE_REVISION,
                TransmissionMechanism.RISK_PREMIUM_EXPANSION,
            ),
            exposures=(ExposureType.GUIDANCE_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.GUIDANCE_INCREASE,
            required=(
                "any:financial:guidance_state=improving|opportunity:demand_acceleration|opportunity:margin_improvement",
            ),
            optional=("scheduled:earnings_call",),
            contradictory=("financial:guidance_state=deteriorating",),
            mechanisms=(
                TransmissionMechanism.GUIDANCE_REVISION,
                TransmissionMechanism.RISK_PREMIUM_COMPRESSION,
            ),
            exposures=(
                ExposureType.GUIDANCE_RISK,
                ExposureType.REVENUE_VISIBILITY,
            ),
            directions=(ScenarioDirection.UP,),
        ),
        _archetype(
            CompanyEventArchetypeType.LAUNCH_DELAY,
            required=(
                "any:operational:launch_state=constrained|operational:launch_state=weak|risk:launch_delay",
            ),
            optional=(
                "scheduled:space_launch",
                "dependency:launch_provider",
            ),
            contradictory=(
                "operational:launch_state=strong",
                "opportunity:launch_success",
            ),
            mechanisms=(
                TransmissionMechanism.EXECUTION_REPRICING,
                TransmissionMechanism.UNCERTAINTY_PREMIUM,
            ),
            exposures=(
                ExposureType.LAUNCH_EVENT_RISK,
                ExposureType.EXECUTION_RISK,
            ),
            directions=(ScenarioDirection.DOWN,),
            industries=("space", "aerospace",),
        ),
        _archetype(
            CompanyEventArchetypeType.LAUNCH_SUCCESS,
            required=(
                "any:operational:launch_state=strong|opportunity:launch_success|scheduled:space_launch",
            ),
            optional=("operational:backlog_state=strong",),
            contradictory=("risk:launch_delay",),
            mechanisms=(
                TransmissionMechanism.EXECUTION_REPRICING,
                TransmissionMechanism.RISK_PREMIUM_COMPRESSION,
            ),
            exposures=(
                ExposureType.LAUNCH_EVENT_RISK,
                ExposureType.OPERATIONAL_RISK,
            ),
            directions=(ScenarioDirection.UP,),
            industries=("space", "aerospace",),
        ),
        _archetype(
            CompanyEventArchetypeType.PRODUCT_DELAY,
            required=(
                "any:operational:product_development_state=constrained|risk:product_delay",
            ),
            optional=("scheduled:product_launch",),
            contradictory=(
                "operational:product_development_state=strong",
            ),
            mechanisms=(
                TransmissionMechanism.EXECUTION_REPRICING,
                TransmissionMechanism.DEMAND_REPRICING,
            ),
            exposures=(ExposureType.DEVELOPMENT_DELAY_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.CONTRACT_AWARD,
            required=(
                "any:opportunity:contract_award|scheduled:contract_milestone",
            ),
            optional=(
                "operational:backlog_state=improving",
                "dependency:government",
            ),
            contradictory=("risk:contract_loss",),
            mechanisms=(
                TransmissionMechanism.DEMAND_REPRICING,
                TransmissionMechanism.EARNINGS_REVISION,
            ),
            exposures=(
                ExposureType.CONTRACT_AWARD_RISK,
                ExposureType.CONTRACT_RISK,
            ),
            directions=(ScenarioDirection.UP,),
        ),
        _archetype(
            CompanyEventArchetypeType.CONTRACT_CANCELLATION,
            required=("any:risk:contract_loss|dependency:contract",),
            optional=("operational:contract_dependency_state=high",),
            contradictory=("opportunity:contract_award",),
            mechanisms=(
                TransmissionMechanism.DEMAND_REPRICING,
                TransmissionMechanism.RISK_PREMIUM_EXPANSION,
            ),
            exposures=(
                ExposureType.CONTRACT_RISK,
                ExposureType.REVENUE_VISIBILITY,
            ),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.CUSTOMER_LOSS,
            required=(
                "any:risk:customer_concentration|dependency:customer",
            ),
            optional=(
                "operational:customer_concentration_state=high",
            ),
            contradictory=("opportunity:customer_expansion",),
            mechanisms=(
                TransmissionMechanism.DEMAND_REPRICING,
                TransmissionMechanism.EARNINGS_REVISION,
            ),
            exposures=(ExposureType.CUSTOMER_CONCENTRATION_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.CUSTOMER_EXPANSION,
            required=("opportunity:customer_expansion",),
            optional=("operational:customer_demand_state=improving",),
            contradictory=("risk:demand_weakness",),
            mechanisms=(
                TransmissionMechanism.DEMAND_REPRICING,
                TransmissionMechanism.MULTIPLE_EXPANSION,
            ),
            exposures=(ExposureType.REVENUE_GROWTH_RISK,),
            directions=(ScenarioDirection.UP,),
        ),
        _archetype(
            CompanyEventArchetypeType.CAPITAL_RAISE,
            required=(
                "any:financial:liquidity_state=constrained|financial:financing_dependency_state=high|risk:financing|risk:dilution",
            ),
            optional=(
                "capital:recent_dilution_state=present",
                "historical:capital_raise",
            ),
            contradictory=(
                "financial:liquidity_state=strong",
                "capital:capital_access_state=strong",
            ),
            mechanisms=(
                TransmissionMechanism.FINANCING_FEAR,
                TransmissionMechanism.DILUTION_EXPECTATION,
            ),
            exposures=(
                ExposureType.FINANCING_RISK,
                ExposureType.DILUTION_RISK,
            ),
            directions=(ScenarioDirection.DOWN,),
            stages=(
                CompanyStage.PRE_REVENUE,
                CompanyStage.EARLY_REVENUE,
                CompanyStage.GROWTH,
                CompanyStage.SCALING,
                CompanyStage.RESTRUCTURING,
                CompanyStage.DISTRESSED,
            ),
        ),
        _archetype(
            CompanyEventArchetypeType.CONVERTIBLE_FINANCING,
            required=(
                "any:risk:financing|capital:capital_access_state=constrained",
            ),
            optional=("capital:convertible_instruments=present",),
            contradictory=("financial:liquidity_state=strong",),
            mechanisms=(
                TransmissionMechanism.FINANCING_FEAR,
                TransmissionMechanism.DILUTION_EXPECTATION,
            ),
            exposures=(
                ExposureType.FINANCING_RISK,
                ExposureType.DILUTION_RISK,
            ),
            directions=(ScenarioDirection.DOWN, ScenarioDirection.SIDEWAYS),
        ),
        _archetype(
            CompanyEventArchetypeType.DEBT_REFINANCING,
            required=(
                "any:risk:refinancing|scheduled:debt_maturity|scheduled:refinancing_window",
            ),
            optional=(
                "financial:debt_maturity_state=constrained",
                "dependency:capital_market",
            ),
            contradictory=("financial:debt_state=low",),
            mechanisms=(
                TransmissionMechanism.FINANCING_FEAR,
                TransmissionMechanism.RISK_PREMIUM_EXPANSION,
            ),
            exposures=(
                ExposureType.DEBT_RISK,
                ExposureType.FINANCING_EVENT_RISK,
            ),
            directions=(
                ScenarioDirection.DOWN,
                ScenarioDirection.UP,
                ScenarioDirection.SIDEWAYS,
            ),
        ),
        _archetype(
            CompanyEventArchetypeType.COVENANT_PRESSURE,
            required=(
                "any:financial:debt_state=high|risk:refinancing",
            ),
            optional=("financial:interest_burden_state=high",),
            contradictory=("financial:debt_state=low",),
            mechanisms=(
                TransmissionMechanism.FINANCING_FEAR,
                TransmissionMechanism.FORCED_DELEVERAGING,
            ),
            exposures=(ExposureType.DEBT_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.REGULATORY_APPROVAL,
            required=(
                "any:opportunity:regulatory_approval|scheduled:regulatory_decision",
            ),
            optional=("dependency:regulator",),
            contradictory=("risk:regulatory",),
            mechanisms=(
                TransmissionMechanism.REGULATORY_REPRICING,
                TransmissionMechanism.RISK_PREMIUM_COMPRESSION,
            ),
            exposures=(ExposureType.REGULATORY_EVENT_RISK,),
            directions=(ScenarioDirection.UP,),
        ),
        _archetype(
            CompanyEventArchetypeType.REGULATORY_REJECTION,
            required=(
                "any:risk:regulatory|scheduled:regulatory_decision",
            ),
            optional=("dependency:regulator",),
            contradictory=("opportunity:regulatory_approval",),
            mechanisms=(
                TransmissionMechanism.REGULATORY_REPRICING,
                TransmissionMechanism.RISK_PREMIUM_EXPANSION,
            ),
            exposures=(ExposureType.REGULATORY_EVENT_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.LITIGATION_ESCALATION,
            required=(
                "any:risk:litigation|scheduled:litigation_hearing",
            ),
            optional=("operational:litigation_state=high",),
            contradictory=("operational:litigation_state=absent",),
            mechanisms=(
                TransmissionMechanism.UNCERTAINTY_PREMIUM,
                TransmissionMechanism.RISK_PREMIUM_EXPANSION,
            ),
            exposures=(ExposureType.LITIGATION_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.MANAGEMENT_DEPARTURE,
            required=("any:risk:management|dependency:key_person",),
            optional=("scheduled:management_transition",),
            contradictory=("operational:management_execution_state=strong",),
            mechanisms=(
                TransmissionMechanism.UNCERTAINTY_PREMIUM,
                TransmissionMechanism.EXECUTION_REPRICING,
            ),
            exposures=(ExposureType.MANAGEMENT_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.STRATEGIC_PARTNERSHIP,
            required=("opportunity:strategic_partnership",),
            optional=("dependency:partner",),
            contradictory=(),
            mechanisms=(
                TransmissionMechanism.DEMAND_REPRICING,
                TransmissionMechanism.RISK_PREMIUM_COMPRESSION,
            ),
            exposures=(ExposureType.REVENUE_VISIBILITY,),
            directions=(ScenarioDirection.UP,),
        ),
        _archetype(
            CompanyEventArchetypeType.ACQUISITION_PROPOSAL,
            required=("opportunity:acquisition",),
            optional=("ownership:concentrated_holder_state=high",),
            contradictory=(),
            mechanisms=(
                TransmissionMechanism.RISK_PREMIUM_COMPRESSION,
                TransmissionMechanism.REFLEXIVE_PRICE_FEEDBACK,
            ),
            exposures=(ExposureType.ACQUISITION_INTEGRATION_RISK,),
            directions=(ScenarioDirection.UP, ScenarioDirection.SIDEWAYS),
        ),
        _archetype(
            CompanyEventArchetypeType.OPERATIONAL_OUTAGE,
            required=(
                "any:risk:technology_failure|risk:dependency_failure|operational:infrastructure_dependency_state=high",
            ),
            optional=("dependency:infrastructure",),
            contradictory=("operational:service_delivery_state=strong",),
            mechanisms=(
                TransmissionMechanism.EXECUTION_REPRICING,
                TransmissionMechanism.RISK_PREMIUM_EXPANSION,
            ),
            exposures=(ExposureType.OPERATIONAL_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.PRODUCTION_SHORTFALL,
            required=(
                "any:risk:production|operational:production_state=weak|operational:production_state=constrained",
            ),
            optional=("scheduled:production_milestone",),
            contradictory=("operational:production_state=strong",),
            mechanisms=(
                TransmissionMechanism.EXECUTION_REPRICING,
                TransmissionMechanism.EARNINGS_REVISION,
            ),
            exposures=(ExposureType.OPERATIONAL_RISK,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.MARGIN_COMPRESSION,
            required=(
                "any:risk:margin_pressure|financial:gross_margin_state=deteriorating|financial:operating_margin_state=deteriorating",
            ),
            optional=("financial:capital_expenditure_state=high",),
            contradictory=("opportunity:margin_improvement",),
            mechanisms=(
                TransmissionMechanism.EARNINGS_REVISION,
                TransmissionMechanism.MULTIPLE_COMPRESSION,
            ),
            exposures=(ExposureType.MARGIN_PRESSURE,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.MARGIN_EXPANSION,
            required=(
                "any:opportunity:margin_improvement|financial:gross_margin_state=improving|financial:operating_margin_state=improving",
            ),
            optional=("opportunity:cost_reduction",),
            contradictory=("risk:margin_pressure",),
            mechanisms=(
                TransmissionMechanism.EARNINGS_REVISION,
                TransmissionMechanism.MULTIPLE_EXPANSION,
            ),
            exposures=(ExposureType.MARGIN_PRESSURE,),
            directions=(ScenarioDirection.UP,),
        ),
        _archetype(
            CompanyEventArchetypeType.BACKLOG_CONVERSION_FAILURE,
            required=(
                "any:risk:backlog_conversion|operational:backlog_state=weak",
            ),
            optional=("operational:execution_state=weak",),
            contradictory=("opportunity:backlog_conversion",),
            mechanisms=(
                TransmissionMechanism.DEMAND_REPRICING,
                TransmissionMechanism.EXECUTION_REPRICING,
            ),
            exposures=(ExposureType.REVENUE_VISIBILITY,),
            directions=(ScenarioDirection.DOWN,),
        ),
        _archetype(
            CompanyEventArchetypeType.SHORT_SQUEEZE,
            required=("ownership:short_interest_state=high",),
            optional=("ownership:borrow_availability_state=constrained",),
            contradictory=("ownership:short_interest_state=low",),
            mechanisms=(
                TransmissionMechanism.SHORT_COVERING,
                TransmissionMechanism.REFLEXIVE_PRICE_FEEDBACK,
            ),
            exposures=(ExposureType.SHORT_INTEREST_RISK,),
            directions=(ScenarioDirection.UP,),
            classification=(
                CompanyEventClassification.STRUCTURAL_OPPORTUNITY
            ),
        ),
        _archetype(
            CompanyEventArchetypeType.VALUATION_COMPRESSION,
            required=(
                "any:risk:valuation_compression|financial:valuation_state=high",
            ),
            optional=("exposure:valuation_compression",),
            contradictory=("financial:valuation_state=low",),
            mechanisms=(
                TransmissionMechanism.MULTIPLE_COMPRESSION,
                TransmissionMechanism.RISK_PREMIUM_EXPANSION,
            ),
            exposures=(ExposureType.VALUATION_COMPRESSION,),
            directions=(ScenarioDirection.DOWN,),
            classification=(
                CompanyEventClassification.STRUCTURAL_EVENT_RISK
            ),
        ),
        _archetype(
            CompanyEventArchetypeType.SECTOR_SYMPATHY_MOVE,
            required=("exposure:sector_sentiment_risk",),
            optional=("exposure:sector_demand_risk",),
            contradictory=(),
            mechanisms=(TransmissionMechanism.SECTOR_ROTATION,),
            exposures=(
                ExposureType.SECTOR_SENTIMENT_RISK,
                ExposureType.SECTOR_DEMAND_RISK,
            ),
            directions=_ALL_DIRECTIONS,
            classification=(
                CompanyEventClassification.STRUCTURAL_EVENT_RISK
            ),
        ),
    )
    return CompanyEventArchetypeLibrary.create(
        library_id="company_event_archetype_library",
        library_version=COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION,
        archetypes=archetypes,
    )


def _feature_add(
    features: set[str],
    refs: dict[str, set[str]],
    token: str,
    reference: str,
) -> None:
    features.add(token)
    refs.setdefault(token, set()).add(reference)


def _snapshot_features(
    snapshot: CompanyKnowledgeSnapshot,
    exposures: Sequence[ExposureItem],
) -> tuple[set[str], dict[str, set[str]]]:
    features: set[str] = set()
    refs: dict[str, set[str]] = {}
    _feature_add(
        features,
        refs,
        f"stage:{snapshot.company_profile.company_stage.value}",
        snapshot.company_profile.company_id,
    )
    industry = _normalized_token(snapshot.company_profile.industry)
    _feature_add(
        features,
        refs,
        f"industry:{industry}",
        snapshot.company_profile.company_id,
    )
    fact_by_field = {
        (item.section, item.field_name): item.fact_id
        for item in snapshot.known_facts
    }
    for name in _FINANCIAL_STATE_FIELDS:
        state = getattr(snapshot.financial_state, name)
        if state in {
            CompanyStateValue.UNKNOWN,
            CompanyStateValue.UNAVAILABLE,
        }:
            continue
        token = f"financial:{name}={state.value}"
        _feature_add(
            features,
            refs,
            token,
            fact_by_field.get(
                ("financial", name),
                snapshot.financial_state.state_id,
            ),
        )
    for name in _OPERATIONAL_STATE_FIELDS:
        state = getattr(snapshot.operational_state, name)
        if state in {
            CompanyStateValue.UNKNOWN,
            CompanyStateValue.UNAVAILABLE,
        }:
            continue
        token = f"operational:{name}={state.value}"
        _feature_add(
            features,
            refs,
            token,
            fact_by_field.get(
                ("operational", name),
                snapshot.operational_state.state_id,
            ),
        )
    for name in (
        "share_count_state",
        "recent_dilution_state",
        "warrant_overhang",
        "employee_compensation_dilution",
        "capital_access_state",
    ):
        state = getattr(snapshot.capital_structure_state, name)
        if state in {
            CompanyStateValue.UNKNOWN,
            CompanyStateValue.UNAVAILABLE,
        }:
            continue
        token = f"capital:{name}={state.value}"
        _feature_add(
            features,
            refs,
            token,
            fact_by_field.get(
                ("capital", name),
                snapshot.capital_structure_state.state_id,
            ),
        )
    if snapshot.capital_structure_state.convertible_instruments:
        _feature_add(
            features,
            refs,
            "capital:convertible_instruments=present",
            snapshot.capital_structure_state.state_id,
        )
    for name in (
        "insider_ownership_state",
        "institutional_ownership_state",
        "concentrated_holder_state",
        "short_interest_state",
        "borrow_availability_state",
        "option_positioning_state",
        "lockup_state",
        "insider_transaction_state",
        "index_membership_state",
        "passive_flow_exposure",
    ):
        state = getattr(snapshot.ownership_positioning_state, name)
        if state in {
            CompanyStateValue.UNKNOWN,
            CompanyStateValue.UNAVAILABLE,
        }:
            continue
        token = f"ownership:{name}={state.value}"
        _feature_add(
            features,
            refs,
            token,
            fact_by_field.get(
                ("ownership", name),
                snapshot.ownership_positioning_state.state_id,
            ),
        )
    for item in snapshot.structural_risks:
        if item.status in {
            StructuralConditionStatus.ACTIVE,
            StructuralConditionStatus.CONDITIONAL,
        }:
            _feature_add(
                features,
                refs,
                f"risk:{item.type.value}",
                item.item_id,
            )
    for item in snapshot.structural_opportunities:
        if item.status in {
            StructuralConditionStatus.ACTIVE,
            StructuralConditionStatus.CONDITIONAL,
        }:
            _feature_add(
                features,
                refs,
                f"opportunity:{item.type.value}",
                item.item_id,
            )
    for item in snapshot.dependencies:
        _feature_add(
            features,
            refs,
            f"dependency:{item.dependency_type.value}",
            item.dependency_id,
        )
    for item in snapshot.scheduled_events:
        if item.status not in {
            CompanyEventStatus.CANCELLED,
            CompanyEventStatus.COMPLETED,
        }:
            _feature_add(
                features,
                refs,
                f"scheduled:{item.event_type.value}",
                item.event_id,
            )
    for item in snapshot.historical_event_profiles:
        if item.observed_event_count > 0:
            _feature_add(
                features,
                refs,
                f"historical:{item.event_type.value}",
                item.event_profile_id,
            )
    for item in exposures:
        _feature_add(
            features,
            refs,
            f"exposure:{item.exposure_type.value}",
            item.exposure_id,
        )
    return features, refs


def _condition_match(condition: str, features: set[str]) -> bool:
    if condition.startswith("any:"):
        return any(
            item in features
            for item in condition.removeprefix("any:").split("|")
        )
    if condition.startswith("all:"):
        return all(
            item in features
            for item in condition.removeprefix("all:").split("|")
        )
    if condition.startswith("not:"):
        return condition.removeprefix("not:") not in features
    return condition in features


def _condition_refs(
    condition: str,
    features: set[str],
    refs: Mapping[str, set[str]],
) -> set[str]:
    if condition.startswith(("any:", "all:")):
        tokens = condition.split(":", 1)[1].split("|")
    elif condition.startswith("not:"):
        return set()
    else:
        tokens = [condition]
    return {
        reference
        for token in tokens
        if token in features
        for reference in refs.get(token, set())
    }


def _industry_compatible(
    industry: str,
    permitted: Sequence[str],
) -> bool:
    normalized = _normalized_token(industry)
    return "*" in permitted or any(
        _normalized_token(item) in normalized
        or normalized in _normalized_token(item)
        for item in permitted
    )


def _candidate_reference_groups(
    references: set[str],
    snapshot: CompanyKnowledgeSnapshot,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    fact_ids = {item.fact_id for item in snapshot.known_facts}
    dependency_ids = {
        item.dependency_id for item in snapshot.dependencies
    }
    risk_ids = {item.item_id for item in snapshot.structural_risks}
    opportunity_ids = {
        item.item_id for item in snapshot.structural_opportunities
    }
    event_ids = {item.event_id for item in snapshot.scheduled_events}
    return (
        tuple(sorted(references & fact_ids)),
        tuple(sorted(references & dependency_ids)),
        tuple(sorted(references & risk_ids)),
        tuple(sorted(references & opportunity_ids)),
        tuple(sorted(references & event_ids)),
    )


def _candidate_score(
    *,
    required_total: int,
    required_matched: int,
    optional_total: int,
    optional_matched: int,
    exposure_match: bool,
    contextual_match: bool,
    direction_match: bool,
    duration_match: bool,
) -> int:
    required_points = (
        round(45 * required_matched / required_total)
        if required_total
        else 45
    )
    optional_points = (
        round(20 * optional_matched / optional_total)
        if optional_total
        else 0
    )
    return min(
        100,
        required_points
        + optional_points
        + (10 if exposure_match else 0)
        + (10 if contextual_match else 0)
        + (7 if direction_match else 0)
        + (8 if duration_match else 0),
    )


class CompanyEventCandidateBuilder:
    """Match explicit company state to versioned archetypes."""

    def __init__(
        self,
        library: CompanyEventArchetypeLibrary | None = None,
    ) -> None:
        self.library = (
            library
            if library is not None
            else default_company_event_archetype_library()
        )
        validation = validate_company_event_archetype_library(self.library)
        if not validation.is_valid:
            raise ValueError(
                "Company event archetype library is invalid: "
                + "; ".join(item.message for item in validation.errors)
            )

    def build(
        self,
        snapshot: CompanyKnowledgeSnapshot,
        premise: FrozenTechnicalPremise,
        state: CurrentMarketState,
        relevant_exposures: Sequence[ExposureItem] = (),
    ) -> tuple[CompanyEventCandidate, ...]:
        if not isinstance(snapshot, CompanyKnowledgeSnapshot):
            raise TypeError("snapshot must be CompanyKnowledgeSnapshot.")
        if not isinstance(premise, FrozenTechnicalPremise):
            raise TypeError("premise must be FrozenTechnicalPremise.")
        if not isinstance(state, CurrentMarketState):
            raise TypeError("state must be CurrentMarketState.")
        exposures = tuple(relevant_exposures)
        if not all(isinstance(item, ExposureItem) for item in exposures):
            raise TypeError(
                "relevant_exposures must contain ExposureItem values."
            )
        before = (
            snapshot.to_dict(),
            premise.to_dict(),
            state.to_dict(),
            [item.to_dict() for item in exposures],
        )
        features, feature_refs = _snapshot_features(snapshot, exposures)
        exposure_types = {item.exposure_type for item in exposures}
        candidates: list[CompanyEventCandidate] = []
        for archetype in self.library.archetypes:
            required_matches = tuple(
                condition
                for condition in archetype.required_preconditions
                if _condition_match(condition, features)
            )
            missing = tuple(
                condition
                for condition in archetype.required_preconditions
                if condition not in required_matches
            )
            optional_matches = tuple(
                condition
                for condition in archetype.optional_preconditions
                if _condition_match(condition, features)
            )
            contradictions = tuple(
                condition
                for condition in archetype.contradictory_conditions
                if _condition_match(condition, features)
            )
            stage_ok = (
                snapshot.company_profile.company_stage
                in archetype.applicable_company_stages
            )
            industry_ok = _industry_compatible(
                snapshot.company_profile.industry,
                archetype.applicable_industries,
            )
            direction_ok = (
                premise.direction
                in archetype.compatible_technical_directions
            )
            duration_ok = (
                state.expected_duration_bucket
                in archetype.expected_time_horizon_classes
            )
            exposure_ok = bool(
                exposure_types
                & set(archetype.compatible_exposure_types)
            )
            references = {
                reference
                for condition in required_matches + optional_matches
                for reference in _condition_refs(
                    condition,
                    features,
                    feature_refs,
                )
            }
            (
                fact_refs,
                dependency_refs,
                risk_refs,
                opportunity_refs,
                event_refs,
            ) = _candidate_reference_groups(references, snapshot)
            financial_support = tuple(
                sorted(
                    condition
                    for condition in required_matches + optional_matches
                    if "financial:" in condition
                    or "capital:" in condition
                )
            )
            operational_support = tuple(
                sorted(
                    condition
                    for condition in required_matches + optional_matches
                    if "operational:" in condition
                    or "ownership:" in condition
                )
            )
            contextual = bool(
                event_refs
                or risk_refs
                or opportunity_refs
                or dependency_refs
                or any(
                    "historical:" in item
                    for item in optional_matches + required_matches
                )
            )
            exclusion: list[str] = []
            if missing:
                exclusion.append("missing_required_preconditions")
            if contradictions:
                exclusion.append("contradictory_conditions_present")
            if not stage_ok:
                exclusion.append("incompatible_company_stage")
            if not industry_ok:
                exclusion.append("incompatible_industry")
            if not direction_ok:
                exclusion.append("incompatible_technical_direction")
            if not duration_ok:
                exclusion.append("incompatible_technical_duration")
            if not archetype.transmission_mechanisms:
                exclusion.append("no_transmission_mechanism")
            if missing and not (
                contradictions
                or not stage_ok
                or not industry_ok
                or not direction_ok
                or not duration_ok
            ):
                eligibility = (
                    CandidateEligibilityStatus.INSUFFICIENT_INFORMATION
                )
            elif exclusion:
                eligibility = CandidateEligibilityStatus.INELIGIBLE
            else:
                eligibility = CandidateEligibilityStatus.ELIGIBLE
            relevance = _candidate_score(
                required_total=len(archetype.required_preconditions),
                required_matched=len(required_matches),
                optional_total=len(archetype.optional_preconditions),
                optional_matched=len(optional_matches),
                exposure_match=exposure_ok,
                contextual_match=contextual,
                direction_match=direction_ok,
                duration_match=duration_ok,
            )
            causal_signature = _hash_payload(
                {
                    "event_type": archetype.archetype_type.value,
                    "transmission_mechanisms": tuple(
                        item.value
                        for item in archetype.transmission_mechanisms
                    ),
                    "supporting_conditions": tuple(
                        sorted(required_matches + optional_matches)
                    ),
                }
            )
            candidate_id = _normalized_token(
                "company_candidate_"
                + archetype.archetype_id
                + "_"
                + snapshot.content_hash[:12]
            )
            explainability = [
                f"required_matched:{len(required_matches)}/{len(archetype.required_preconditions)}",
                f"optional_matched:{len(optional_matches)}/{len(archetype.optional_preconditions)}",
                f"exposure_match:{str(exposure_ok).lower()}",
                f"direction_match:{str(direction_ok).lower()}",
                f"duration_match:{str(duration_ok).lower()}",
                f"candidate_policy:{COMPANY_EVENT_CANDIDATE_POLICY_VERSION}",
            ]
            candidates.append(
                CompanyEventCandidate.create(
                    candidate_id=candidate_id,
                    archetype_id=archetype.archetype_id,
                    event_type=archetype.archetype_type,
                    event_classification=archetype.initiating_event_class,
                    hypothesis_label=(
                        "A "
                        + archetype.archetype_type.value.replace("_", " ")
                        + " could become relevant only if its explicit "
                        "preconditions remain present."
                    ),
                    supporting_company_facts=fact_refs,
                    supporting_financial_states=financial_support,
                    supporting_operational_states=operational_support,
                    supporting_dependencies=dependency_refs,
                    supporting_structural_risks=risk_refs,
                    supporting_structural_opportunities=opportunity_refs,
                    related_scheduled_events=event_refs,
                    contradictory_conditions=contradictions,
                    missing_preconditions=missing,
                    plausible_transmission_mechanisms=(
                        archetype.transmission_mechanisms
                    ),
                    compatible_time_horizons=(
                        archetype.expected_time_horizon_classes
                    ),
                    compatible_technical_directions=(
                        archetype.compatible_technical_directions
                    ),
                    eligibility_status=eligibility,
                    relevance_score=relevance,
                    explainability_codes=tuple(explainability),
                    exclusion_reasons=tuple(exclusion),
                    causal_signature=causal_signature,
                )
            )
        candidates.sort(
            key=lambda item: (
                0
                if item.eligibility_status
                is CandidateEligibilityStatus.ELIGIBLE
                else 1,
                -item.relevance_score,
                item.candidate_id,
            )
        )
        seen_signatures: set[str] = set()
        deduplicated: list[CompanyEventCandidate] = []
        for item in candidates:
            if (
                item.eligibility_status
                is CandidateEligibilityStatus.ELIGIBLE
                and item.causal_signature in seen_signatures
            ):
                duplicate = replace(
                    item,
                    eligibility_status=CandidateEligibilityStatus.DUPLICATE,
                    exclusion_reasons=tuple(
                        sorted(
                            set(item.exclusion_reasons)
                            | {"duplicate_causal_structure"}
                        )
                    ),
                    content_hash="",
                )
                duplicate = replace(
                    duplicate,
                    content_hash=company_event_candidate_content_hash(
                        duplicate
                    ),
                )
                deduplicated.append(duplicate)
            else:
                deduplicated.append(item)
                if (
                    item.eligibility_status
                    is CandidateEligibilityStatus.ELIGIBLE
                ):
                    seen_signatures.add(item.causal_signature)
        deduplicated.sort(
            key=lambda item: (
                0
                if item.eligibility_status
                is CandidateEligibilityStatus.ELIGIBLE
                else 1,
                -item.relevance_score,
                item.candidate_id,
            )
        )
        after = (
            snapshot.to_dict(),
            premise.to_dict(),
            state.to_dict(),
            [item.to_dict() for item in exposures],
        )
        if before != after:
            raise RuntimeError(
                "CompanyEventCandidateBuilder mutated an input."
            )
        return tuple(deduplicated)


def build_company_scenario_context(
    snapshot: CompanyKnowledgeSnapshot,
    premise: FrozenTechnicalPremise,
    state: CurrentMarketState,
    relevant_exposures: Sequence[ExposureItem] = (),
    *,
    library: CompanyEventArchetypeLibrary | None = None,
) -> CompanyScenarioContext:
    if snapshot.symbol != premise.symbol or snapshot.symbol != state.symbol:
        raise ValueError(
            "Snapshot, technical premise, and current state symbols must match."
        )
    if snapshot.applicable_cutoff != state.applicable_cutoff:
        raise ValueError(
            "Snapshot and current-state cutoffs must match."
        )
    exposures = tuple(relevant_exposures)
    candidates = CompanyEventCandidateBuilder(library).build(
        snapshot,
        premise,
        state,
        exposures,
    )
    active_library = (
        library
        if library is not None
        else default_company_event_archetype_library()
    )
    eligible = tuple(
        item
        for item in candidates
        if item.eligibility_status is CandidateEligibilityStatus.ELIGIBLE
    )
    excluded = tuple(
        item
        for item in candidates
        if item.eligibility_status is not CandidateEligibilityStatus.ELIGIBLE
    )
    warnings: list[str] = []
    if not eligible:
        warnings.append("no_eligible_company_event_candidate")
    if snapshot.known_missing_information:
        warnings.append("company_information_is_incomplete")
    seed = CompanyScenarioContext(
        context_id=_normalized_token(
            "company_context_"
            + snapshot.snapshot_id
            + "_"
            + premise.frozen_content_hash[:10]
        ),
        company_knowledge_snapshot_hash=snapshot.content_hash,
        symbol=snapshot.symbol,
        applicable_cutoff=snapshot.applicable_cutoff,
        eligible_event_candidates=eligible,
        excluded_event_candidates=excluded,
        scheduled_events=snapshot.scheduled_events,
        structural_risks=snapshot.structural_risks,
        structural_opportunities=snapshot.structural_opportunities,
        dependencies=snapshot.dependencies,
        missing_company_information=(
            snapshot.known_missing_information
        ),
        warnings=tuple(warnings),
        candidate_policy_version=COMPANY_EVENT_CANDIDATE_POLICY_VERSION,
        archetype_library_version=active_library.library_version,
        archetype_library_hash=active_library.content_hash,
        frozen_technical_premise_hash=premise.frozen_content_hash,
        current_market_state_hash=state.content_hash,
        relevant_exposure_ids=tuple(
            sorted(item.exposure_id for item in exposures)
        ),
    )
    context = replace(
        seed,
        content_hash=company_scenario_context_content_hash(seed),
    )
    validation = validate_company_scenario_context(
        context,
        snapshot=snapshot,
        premise=premise,
        state=state,
        exposures=exposures,
    )
    if not validation.is_valid:
        raise ValueError(
            "Company scenario context is invalid: "
            + "; ".join(item.message for item in validation.errors)
        )
    return context


def company_snapshot_to_market_inputs(
    snapshot: CompanyKnowledgeSnapshot,
) -> CompanyMarketInputBridgeResult:
    """Convert supplied facts/events without inventing exposures.

    The returned evidence is neutral company context. Exposure mapping remains
    an explicit downstream action and the `invented_exposures` tuple is always
    empty by contract.
    """

    if not isinstance(snapshot, CompanyKnowledgeSnapshot):
        raise TypeError("snapshot must be CompanyKnowledgeSnapshot.")
    if not snapshot.validation_result.is_valid:
        raise ValueError(
            "snapshot must be finalized and valid before bridging."
        )
    source = str(
        snapshot.source_metadata.get("source")
        or snapshot.source_metadata.get("source_id")
        or "supplied_structured_company_data"
    )
    source_type = str(
        snapshot.source_metadata.get("source_type")
        or "offline_structured_input"
    )
    evidence: list[EvidenceItem] = []
    for item in snapshot.known_facts:
        evidence.append(
            EvidenceItem(
                evidence_id=_normalized_token(
                    f"company_evidence_{item.fact_id}"
                ),
                category=f"company_{item.section}",
                claim=(
                    f"Supplied company field {item.section}."
                    f"{item.field_name} is {_json_value(item.value)!r}."
                ),
                source=source,
                source_type=source_type,
                provider="offline_company_knowledge",
                publication_date=item.data_as_of,
                retrieval_timestamp=snapshot.created_at,
                applicable_cutoff=snapshot.applicable_cutoff,
                status=EvidenceStatus.CONFIRMED,
                implication=EvidenceImplication.NEUTRAL,
                confidence="unavailable",
                relevance_to_technical_scenario=(
                    "Structured company fact for downstream interpretation; "
                    "it does not alter the frozen technical premise."
                ),
                warnings=(),
            )
        )
    for item in snapshot.structural_risks:
        evidence.append(
            EvidenceItem(
                evidence_id=_normalized_token(
                    f"company_evidence_{item.item_id}"
                ),
                category="company_structural_risk",
                claim=item.description,
                source=source,
                source_type=source_type,
                provider="offline_company_knowledge",
                publication_date=None,
                retrieval_timestamp=snapshot.created_at,
                applicable_cutoff=snapshot.applicable_cutoff,
                status=EvidenceStatus.INTERPRETATION,
                implication=EvidenceImplication.NEUTRAL,
                confidence="unavailable",
                relevance_to_technical_scenario=(
                    "Structural risk context, not an event prediction."
                ),
                warnings=("structural_condition_not_prediction",),
            )
        )
    for item in snapshot.structural_opportunities:
        evidence.append(
            EvidenceItem(
                evidence_id=_normalized_token(
                    f"company_evidence_{item.item_id}"
                ),
                category="company_structural_opportunity",
                claim=item.description,
                source=source,
                source_type=source_type,
                provider="offline_company_knowledge",
                publication_date=None,
                retrieval_timestamp=snapshot.created_at,
                applicable_cutoff=snapshot.applicable_cutoff,
                status=EvidenceStatus.INTERPRETATION,
                implication=EvidenceImplication.NEUTRAL,
                confidence="unavailable",
                relevance_to_technical_scenario=(
                    "Structural opportunity context, not an event prediction."
                ),
                warnings=("structural_condition_not_prediction",),
            )
        )
    events: list[MaterialEvent] = []
    for item in snapshot.scheduled_events:
        status = (
            EvidenceStatus.CONFIRMED
            if item.status is CompanyEventStatus.COMPLETED
            else (
                EvidenceStatus.UNKNOWN
                if item.status
                in {
                    CompanyEventStatus.CANCELLED,
                    CompanyEventStatus.UNCERTAIN,
                }
                else EvidenceStatus.SCHEDULED
            )
        )
        events.append(
            MaterialEvent(
                event_id=item.event_id,
                event_type=item.event_type.value,
                title=item.title,
                scheduled_at=item.scheduled_start,
                date_precision=item.date_precision,
                status=status,
                source=item.source_reference or source,
                source_type=item.source_type or source_type,
                publication_date=item.announced_at,
                retrieval_timestamp=snapshot.created_at,
                applicable_cutoff=snapshot.applicable_cutoff,
                related_evidence_ids=(),
            )
        )
    seed = CompanyMarketInputBridgeResult(
        snapshot_hash=snapshot.content_hash,
        evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
        material_events=tuple(
            sorted(events, key=lambda item: item.event_id)
        ),
        invented_exposures=(),
        warnings=(
            "No exposures were inferred; exposure mapping remains explicit.",
        ),
    )
    return replace(seed, content_hash=_contract_hash(seed))


_CERTAINTY_EVENT_LANGUAGE = re.compile(
    r"\b(?:will|must|guaranteed|certainly|definitely|is expected to)\b",
    re.IGNORECASE,
)


def _scenario_text(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(
            text
            for item in value.values()
            for text in _scenario_text(item)
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(text for item in value for text in _scenario_text(item))
    return ()


def _scenario_company_tokens(
    scenario: Any,
) -> set[str]:
    values = set(scenario.assumptions)
    for step in scenario.timeline_sequence:
        values.update(step.assumptions)
    return values


def validate_company_scenario_usage(
    scenario_set: CurrentScenarioSet,
    context: CompanyScenarioContext,
) -> ValidationResult:
    """Validate only the optional Phase 10 references in a scenario set."""

    if not isinstance(scenario_set, CurrentScenarioSet):
        raise TypeError("scenario_set must be CurrentScenarioSet.")
    if not isinstance(context, CompanyScenarioContext):
        raise TypeError("context must be CompanyScenarioContext.")
    result = _ValidationCollector()
    eligible = {
        item.candidate_id: item
        for item in context.eligible_event_candidates
    }
    excluded_ids = {
        item.candidate_id for item in context.excluded_event_candidates
    }
    all_company_ids = set(eligible) | excluded_ids
    valid_refs = {
        *(item.item_id for item in context.structural_risks),
        *(item.item_id for item in context.structural_opportunities),
        *(item.dependency_id for item in context.dependencies),
        *(item.event_id for item in context.scheduled_events),
    }
    used_any = False
    for scenario_index, scenario in enumerate(scenario_set.scenarios):
        hypothetical_by_id = {
            item.hypothetical_event_id: item
            for item in scenario.hypothetical_future_events
        }
        used = set(hypothetical_by_id) & all_company_ids
        if not used:
            continue
        used_any = True
        tokens = _scenario_company_tokens(scenario)
        for candidate_id in sorted(used):
            candidate = eligible.get(candidate_id)
            path = (
                f"scenarios[{scenario_index}]."
                f"hypothetical_future_events[{candidate_id}]"
            )
            result.require(
                "company_candidate_reference",
                candidate is not None,
                path,
                "Scenario may use only an eligible company-event candidate.",
            )
            if candidate is None:
                continue
            event = hypothetical_by_id[candidate_id]
            result.require(
                "company_candidate_hypothetical_label",
                event.explicitly_hypothetical
                and event.event_type == candidate.event_type.value
                and bool(
                    re.search(
                        r"\b(?:could|may|might|hypothetical)\b",
                        event.description,
                        re.IGNORECASE,
                    )
                ),
                path,
                "Company candidate must retain its event type and explicit hypothetical labeling.",
            )
            classification_token = (
                f"company_classification:{candidate_id}:"
                f"{candidate.event_classification.value}"
            )
            result.require(
                "company_candidate_classification",
                classification_token in tokens,
                f"{path}.classification",
                "Scenario must preserve the candidate event classification.",
            )
            referenced_company = {
                token.removeprefix("company_ref:")
                for token in tokens
                if token.startswith("company_ref:")
            }
            candidate_refs = {
                *candidate.supporting_company_facts,
                *candidate.supporting_dependencies,
                *candidate.supporting_structural_risks,
                *candidate.supporting_structural_opportunities,
                *candidate.related_scheduled_events,
            }
            result.require(
                "company_candidate_grounding",
                bool(referenced_company & candidate_refs)
                and referenced_company <= (valid_refs | candidate_refs),
                f"{path}.supporting_company_references",
                "Scenario must identify valid supporting company-state references.",
            )
            result.require(
                "company_candidate_mechanisms",
                bool(
                    set(scenario.transmission_mechanisms)
                    & set(candidate.plausible_transmission_mechanisms)
                ),
                f"{path}.transmission_mechanisms",
                "Scenario must preserve at least one candidate transmission mechanism.",
            )
            contradiction_tokens = {
                token.removeprefix("company_contradiction:")
                for token in tokens
                if token.startswith("company_contradiction:")
            }
            missing_tokens = {
                token.removeprefix("company_missing:")
                for token in tokens
                if token.startswith("company_missing:")
            }
            result.require(
                "company_candidate_contradictions",
                set(candidate.contradictory_conditions)
                <= contradiction_tokens,
                f"{path}.contradictory_conditions",
                "Scenario must disclose candidate contradictory conditions.",
            )
            result.require(
                "company_candidate_missing_information",
                set(candidate.missing_preconditions) <= missing_tokens
                and bool(scenario.missing_evidence),
                f"{path}.missing_evidence",
                "Scenario must preserve candidate missing preconditions and missing evidence.",
            )
            result.require(
                "company_candidate_invalidation",
                bool(scenario.invalidation_conditions),
                f"{path}.invalidation_conditions",
                "Scenario must include invalidation conditions.",
            )
            result.require(
                "company_candidate_balance",
                bool(scenario.amplifiers) and bool(scenario.dampeners),
                f"{path}.amplifiers",
                "Scenario must identify amplifiers and dampeners.",
            )
            candidate_text = "\n".join(
                _scenario_text(event.to_dict())
            )
            result.require(
                "company_candidate_no_certainty",
                not _CERTAINTY_EVENT_LANGUAGE.search(candidate_text),
                path,
                "A hypothetical company event cannot be stated as a future fact.",
            )
    result.require(
        "company_candidate_known_ids",
        all_company_ids
        >= {
            item.hypothetical_event_id
            for scenario in scenario_set.scenarios
            for item in scenario.hypothetical_future_events
            if item.hypothetical_event_id.startswith(
                "company_candidate_"
            )
        },
        "scenarios.hypothetical_future_events",
        "Scenario contains an unknown company-event candidate ID.",
    )
    result.warn(
        "company_context_unused",
        used_any or not context.eligible_event_candidates,
        "scenarios",
        "Eligible company context was supplied but no candidate was used.",
    )
    return result.result(version=COMPANY_SCENARIO_USAGE_VALIDATION_VERSION)


def create_company_knowledge_replay_packet(
    *,
    replay_id: str,
    input: CompanyKnowledgeInput,
    frozen_technical_premise: FrozenTechnicalPremise,
    current_market_state: CurrentMarketState,
    relevant_exposures: Sequence[ExposureItem],
    snapshot: CompanyKnowledgeSnapshot,
    context: CompanyScenarioContext,
    archetype_library: CompanyEventArchetypeLibrary,
    created_at: str | datetime,
) -> CompanyKnowledgeReplayPacket:
    candidate_hashes = tuple(
        item.content_hash
        for item in (
            context.eligible_event_candidates
            + context.excluded_event_candidates
        )
    )
    return CompanyKnowledgeReplayPacket.create(
        replay_id=_normalized_token(replay_id),
        input=input,
        frozen_technical_premise=frozen_technical_premise,
        current_market_state=current_market_state,
        relevant_exposures=tuple(relevant_exposures),
        expected_snapshot_hash=snapshot.content_hash,
        expected_candidate_hashes=candidate_hashes,
        expected_scenario_context_hash=context.content_hash,
        schema_versions=company_knowledge_schema_versions(),
        archetype_library=archetype_library,
        archetype_library_version=archetype_library.library_version,
        created_at=created_at,
    )


def replay_company_knowledge(
    packet: CompanyKnowledgeReplayPacket,
) -> CompanyKnowledgeReplayResult:
    if not isinstance(packet, CompanyKnowledgeReplayPacket):
        raise TypeError("packet must be CompanyKnowledgeReplayPacket.")
    collector = _ValidationCollector()
    collector.require(
        "replay_packet_hash",
        packet.content_hash
        == company_knowledge_replay_packet_content_hash(packet),
        "content_hash",
        "Replay packet hash is invalid.",
    )
    collector.require(
        "replay_library",
        packet.archetype_library_version
        == packet.archetype_library.library_version
        and packet.archetype_library.content_hash
        == company_event_archetype_library_content_hash(
            packet.archetype_library
        ),
        "archetype_library",
        "Replay archetype library is invalid.",
    )
    build = CompanyKnowledgeEngine().build(packet.input)
    snapshot = build.snapshot
    if snapshot is None:
        validation = collector.result()
        seed = CompanyKnowledgeReplayResult(
            replay_id=packet.replay_id,
            success=False,
            snapshot_hash="",
            candidate_hashes=(),
            scenario_context_hash="",
            validation_result=validation,
            errors=build.errors,
        )
        return replace(seed, content_hash=_contract_hash(seed))
    context = build_company_scenario_context(
        snapshot,
        packet.frozen_technical_premise,
        packet.current_market_state,
        packet.relevant_exposures,
        library=packet.archetype_library,
    )
    candidate_hashes = tuple(
        item.content_hash
        for item in (
            context.eligible_event_candidates
            + context.excluded_event_candidates
        )
    )
    collector.require(
        "replay_snapshot_hash",
        snapshot.content_hash == packet.expected_snapshot_hash,
        "expected_snapshot_hash",
        "Replay snapshot hash differs from the expected hash.",
    )
    collector.require(
        "replay_candidate_hashes",
        candidate_hashes == packet.expected_candidate_hashes,
        "expected_candidate_hashes",
        "Replay candidate hashes differ from the expected hashes.",
    )
    collector.require(
        "replay_context_hash",
        context.content_hash == packet.expected_scenario_context_hash,
        "expected_scenario_context_hash",
        "Replay scenario-context hash differs from the expected hash.",
    )
    validation = collector.result()
    seed = CompanyKnowledgeReplayResult(
        replay_id=packet.replay_id,
        success=validation.is_valid,
        snapshot_hash=snapshot.content_hash,
        candidate_hashes=candidate_hashes,
        scenario_context_hash=context.content_hash,
        validation_result=validation,
        errors=tuple(item.message for item in validation.errors),
    )
    return replace(seed, content_hash=_contract_hash(seed))


def company_knowledge_schema_versions() -> Mapping[str, str]:
    return MappingProxyType(
        {
            "company_capital_structure": (
                COMPANY_CAPITAL_STRUCTURE_SCHEMA_VERSION
            ),
            "company_dependency": COMPANY_DEPENDENCY_SCHEMA_VERSION,
            "company_event_archetype": (
                COMPANY_EVENT_ARCHETYPE_SCHEMA_VERSION
            ),
            "company_event_candidate": (
                COMPANY_EVENT_CANDIDATE_SCHEMA_VERSION
            ),
            "company_financial_state": (
                COMPANY_FINANCIAL_STATE_SCHEMA_VERSION
            ),
            "company_knowledge_input": (
                COMPANY_KNOWLEDGE_INPUT_SCHEMA_VERSION
            ),
            "company_knowledge_replay": (
                COMPANY_KNOWLEDGE_REPLAY_SCHEMA_VERSION
            ),
            "company_knowledge_snapshot": (
                COMPANY_KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION
            ),
            "company_operational_state": (
                COMPANY_OPERATIONAL_STATE_SCHEMA_VERSION
            ),
            "company_ownership_positioning": (
                COMPANY_OWNERSHIP_POSITIONING_SCHEMA_VERSION
            ),
            "company_profile": COMPANY_PROFILE_SCHEMA_VERSION,
            "company_scenario_context": (
                COMPANY_SCENARIO_CONTEXT_SCHEMA_VERSION
            ),
            "company_structural_opportunity": (
                COMPANY_STRUCTURAL_OPPORTUNITY_SCHEMA_VERSION
            ),
            "company_structural_risk": (
                COMPANY_STRUCTURAL_RISK_SCHEMA_VERSION
            ),
            "scheduled_company_event": (
                SCHEDULED_COMPANY_EVENT_SCHEMA_VERSION
            ),
        }
    )


__all__ = [
    "BiotechOperationalExtension",
    "COMPANY_CAPITAL_STRUCTURE_SCHEMA_VERSION",
    "COMPANY_DEPENDENCY_SCHEMA_VERSION",
    "COMPANY_EVENT_ARCHETYPE_LIBRARY_VERSION",
    "COMPANY_EVENT_ARCHETYPE_SCHEMA_VERSION",
    "COMPANY_EVENT_CANDIDATE_POLICY_VERSION",
    "COMPANY_EVENT_CANDIDATE_SCHEMA_VERSION",
    "COMPANY_FINANCIAL_STATE_SCHEMA_VERSION",
    "COMPANY_KNOWLEDGE_INPUT_SCHEMA_VERSION",
    "COMPANY_KNOWLEDGE_REPLAY_SCHEMA_VERSION",
    "COMPANY_KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION",
    "COMPANY_MARKET_INPUT_BRIDGE_SCHEMA_VERSION",
    "COMPANY_OPERATIONAL_STATE_SCHEMA_VERSION",
    "COMPANY_OWNERSHIP_POSITIONING_SCHEMA_VERSION",
    "COMPANY_PROFILE_SCHEMA_VERSION",
    "COMPANY_SCENARIO_CONTEXT_SCHEMA_VERSION",
    "COMPANY_STRUCTURAL_OPPORTUNITY_SCHEMA_VERSION",
    "COMPANY_STRUCTURAL_RISK_SCHEMA_VERSION",
    "CandidateEligibilityStatus",
    "CapitalStructureState",
    "CompanyClassification",
    "CompanyDataQuality",
    "CompanyDependency",
    "CompanyDependencyType",
    "CompanyEventArchetype",
    "CompanyEventArchetypeLibrary",
    "CompanyEventArchetypeType",
    "CompanyEventCandidate",
    "CompanyEventCandidateBuilder",
    "CompanyEventClassification",
    "CompanyEventStatus",
    "CompanyEventType",
    "CompanyFinancialState",
    "CompanyKnowledgeBuildResult",
    "CompanyKnowledgeBuildStatus",
    "CompanyKnowledgeEngine",
    "CompanyKnowledgeInput",
    "CompanyKnowledgeReplayPacket",
    "CompanyKnowledgeReplayResult",
    "CompanyKnowledgeSnapshot",
    "CompanyKnownFact",
    "CompanyMarketInputBridgeResult",
    "CompanyOperationalState",
    "CompanyProfile",
    "CompanyScenarioContext",
    "CompanyStage",
    "CompanyStateValue",
    "CompanyStructuralOpportunity",
    "CompanyStructuralOpportunityType",
    "CompanyStructuralRisk",
    "CompanyStructuralRiskType",
    "ConcentrationClass",
    "EventRecurrenceClass",
    "HISTORICAL_COMPANY_EVENT_PROFILE_SCHEMA_VERSION",
    "HistoricalCompanyEventProfile",
    "ImpactClass",
    "OwnershipPositioningState",
    "ReplacementTimeClass",
    "SCHEDULED_COMPANY_EVENT_SCHEMA_VERSION",
    "ScheduledCompanyEvent",
    "SoftwareOperationalExtension",
    "SpaceOperationalExtension",
    "StructuralConditionStatus",
    "SubstitutabilityClass",
    "build_company_scenario_context",
    "company_event_archetype_content_hash",
    "company_event_archetype_library_content_hash",
    "company_event_candidate_content_hash",
    "company_knowledge_input_content_hash",
    "company_knowledge_replay_packet_content_hash",
    "company_knowledge_schema_versions",
    "company_knowledge_snapshot_content_hash",
    "company_known_fact_id",
    "company_scenario_context_content_hash",
    "company_snapshot_to_market_inputs",
    "create_company_knowledge_replay_packet",
    "default_company_event_archetype_library",
    "replay_company_knowledge",
    "validate_company_event_archetype_library",
    "validate_company_knowledge_input",
    "validate_company_knowledge_snapshot",
    "validate_company_scenario_context",
    "validate_company_scenario_usage",
]
