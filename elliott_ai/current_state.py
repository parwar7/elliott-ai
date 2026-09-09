"""Deterministic current-market state construction for pattern retrieval.

This module is standalone. It freezes already-supplied technical, evidence,
exposure, event, and regime inputs into retrieval features. It performs no
research, model call, narrative generation, persistence, CLI routing, Elliott
analysis, or prediction.
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
from typing import Any, Callable, Mapping, Sequence

from .historical_market import (
    DurationBucket,
    HistoricalMoveType,
    MagnitudeBucket,
    MarketRegime,
    MarketRegimeState,
    RelativePerformanceBucket,
    TransmissionMechanism,
    market_regime_content_hash,
    validate_market_regime,
)
from .market_scenario import (
    EXPOSURE_CATEGORY_COMPATIBILITY,
    EXPOSURE_DIRECTION_VALUES,
    EXPOSURE_MATERIALITY_VALUES,
    QUALITATIVE_CONFIDENCE_VALUES,
    EvidenceItem,
    EvidenceStatus,
    ExposureCategory,
    ExposureCategoryCoverage,
    ExposureCoverageState,
    ExposureItem,
    ExposureType,
    FrozenTechnicalPremise,
    MaterialEvent,
    ScenarioDirection,
    ValidationIssue,
    ValidationResult,
    frozen_technical_premise_content_hash,
)


CURRENT_MARKET_STATE_SCHEMA_VERSION = "current-market-state-1.0.0"
CURRENT_STATE_BUILD_RESULT_SCHEMA_VERSION = (
    "current-market-state-build-result-1.0.0"
)
CURRENT_MARKET_STATE_VALIDATION_VERSION = (
    "current-market-state-validation-1.0.0"
)
CURRENT_STATE_BUILDER_VERSION = "current-state-builder-1.0.0"
CURRENT_TECHNICAL_NORMALIZATION_VERSION = (
    "current-technical-normalization-1.0.0"
)
CURRENT_TRANSMISSION_POLICY_VERSION = (
    "current-transmission-policy-1.0.0"
)


class TechnicalDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNAVAILABLE = "unavailable"


class FeatureAvailability(StrEnum):
    AVAILABLE = "available"
    RESEARCHED_NO_SIGNAL = "researched_no_signal"
    UNAVAILABLE = "unavailable"
    ASSUMED = "assumed"
    DRAFT = "draft"


CURRENT_EXPOSURE_TRANSMISSION_MAP: Mapping[
    ExposureType, tuple[TransmissionMechanism, ...]
] = MappingProxyType(
    {
        ExposureType.VALUATION_COMPRESSION: (
            TransmissionMechanism.MULTIPLE_COMPRESSION,
        ),
        ExposureType.VALUATION_EXPANSION: (
            TransmissionMechanism.MULTIPLE_EXPANSION,
        ),
        ExposureType.DILUTION_RISK: (
            TransmissionMechanism.DILUTION_EXPECTATION,
        ),
        ExposureType.FINANCING_RISK: (
            TransmissionMechanism.FINANCING_FEAR,
        ),
        ExposureType.SHORT_INTEREST_RISK: (
            TransmissionMechanism.SHORT_COVERING,
        ),
        ExposureType.MOMENTUM_UNWIND_RISK: (
            TransmissionMechanism.MOMENTUM_UNWIND,
        ),
        ExposureType.EXECUTION_RISK: (
            TransmissionMechanism.EXECUTION_REPRICING,
        ),
        ExposureType.REGULATORY_RISK: (
            TransmissionMechanism.REGULATORY_REPRICING,
        ),
        ExposureType.SECTOR_DEMAND_RISK: (
            TransmissionMechanism.DEMAND_REPRICING,
        ),
        ExposureType.CROWDED_TRADE_RISK: (
            TransmissionMechanism.CROWDED_POSITIONING_UNWIND,
        ),
    }
)

_EXPLICIT_EVENT_TRANSMISSION_MAP: Mapping[
    str, tuple[TransmissionMechanism, ...]
] = MappingProxyType(
    {
        "earnings_revision": (
            TransmissionMechanism.EARNINGS_REVISION,
        ),
        "guidance_revision": (
            TransmissionMechanism.GUIDANCE_REVISION,
        ),
        "regulatory_repricing": (
            TransmissionMechanism.REGULATORY_REPRICING,
        ),
    }
)

_TECHNICAL_MOVE_ALIASES: Mapping[
    str, HistoricalMoveType
] = MappingProxyType(
    {
        "correction": HistoricalMoveType.CORRECTION,
        "corrective_decline": HistoricalMoveType.CORRECTION,
        "corrective_rally": HistoricalMoveType.CORRECTION,
        "crash": HistoricalMoveType.CRASH,
        "capitulation": HistoricalMoveType.CAPITULATION,
        "short_squeeze": HistoricalMoveType.SHORT_SQUEEZE,
        "melt_up": HistoricalMoveType.MELT_UP,
        "gap_repricing": HistoricalMoveType.GAP_REPRICING,
        "post_earnings_repricing": (
            HistoricalMoveType.POST_EARNINGS_REPRICING
        ),
        "post_event_repricing": HistoricalMoveType.POST_EVENT_REPRICING,
        "valuation_rerating": HistoricalMoveType.VALUATION_RERATING,
        "valuation_derating": HistoricalMoveType.VALUATION_DERATING,
        "failed_breakout": HistoricalMoveType.FAILED_BREAKOUT,
        "failed_breakdown": HistoricalMoveType.FAILED_BREAKDOWN,
        "reversal": HistoricalMoveType.REVERSAL,
        "recovery": HistoricalMoveType.RECOVERY,
        "prolonged_decline": HistoricalMoveType.PROLONGED_DECLINE,
        "prolonged_advance": HistoricalMoveType.PROLONGED_ADVANCE,
    }
)

_TECHNICAL_FEATURE_KEYS = (
    "technical_direction",
    "technical_move_types",
    "technical_structure",
    "expected_magnitude_bucket",
    "expected_duration_bucket",
    "market_regime",
    "valuation_state",
    "liquidity_state",
    "positioning_state",
    "benchmark_relative_state",
    "sector_relative_state",
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
            f"{field_name} must be a timestamp string or datetime."
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
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(
            f"{field_name} must be one of: {allowed}."
        ) from exc


def _enum_tuple(
    value: Sequence[Any],
    enum_type: type[StrEnum],
    *,
    field_name: str,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    return tuple(
        _enum_value(item, enum_type, field_name=field_name)
        for item in value
    )


def _string_tuple(
    value: Sequence[str],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(value)
    if not all(isinstance(item, str) for item in result):
        raise TypeError(f"{field_name} must contain only strings.")
    return result


def _normalized_token(value: str) -> str:
    return re.sub(
        r"_+",
        "_",
        re.sub(r"[^a-z0-9]+", "_", value.casefold()),
    ).strip("_")


def _ordered_strings(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON mappings require string keys.")
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
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numeric values must be finite.")
        return value
    raise TypeError(
        f"Unsupported JSON contract value: {type(value).__name__}."
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


def _duplicates(values: Sequence[Any]) -> set[Any]:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _feature_availability_mapping(
    value: Mapping[str, FeatureAvailability | str],
    *,
    field_name: str,
) -> Mapping[str, FeatureAvailability]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    result: dict[str, FeatureAvailability] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise TypeError(f"{field_name} requires non-empty string keys.")
        result[key] = _enum_value(
            item,
            FeatureAvailability,
            field_name=field_name,
        )
    return MappingProxyType(dict(sorted(result.items())))


def _string_sequence_mapping(
    value: Mapping[str, Sequence[str]],
    *,
    field_name: str,
) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    result: dict[str, tuple[str, ...]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise TypeError(f"{field_name} requires non-empty string keys.")
        result[key] = _ordered_strings(
            _string_tuple(item, field_name=field_name)
        )
    return MappingProxyType(dict(sorted(result.items())))


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class CurrentMarketState(_JsonContract):
    state_id: str
    symbol: str
    exchange: str
    as_of: str
    applicable_cutoff: str
    frozen_technical_premise_hash: str
    evidence_packet_hash: str
    exposure_report_hash: str
    technical_direction: TechnicalDirection
    technical_move_types: tuple[HistoricalMoveType, ...]
    technical_structure: str
    expected_magnitude_bucket: MagnitudeBucket
    expected_duration_bucket: DurationBucket
    current_exposure_types: tuple[ExposureType, ...]
    current_event_types: tuple[str, ...]
    current_transmission_hypotheses: tuple[TransmissionMechanism, ...]
    market_regime: MarketRegime | None
    valuation_state: MarketRegimeState
    liquidity_state: MarketRegimeState
    positioning_state: MarketRegimeState
    benchmark_relative_state: RelativePerformanceBucket
    sector_relative_state: RelativePerformanceBucket
    unavailable_features: tuple[str, ...]
    assumptions: tuple[str, ...]
    provenance: Mapping[str, Any]
    evidence_ids: tuple[str, ...] = ()
    exposure_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    feature_availability: Mapping[
        str, FeatureAvailability
    ] = field(default_factory=dict)
    exposure_category_availability: Mapping[
        str, FeatureAvailability
    ] = field(default_factory=dict)
    exposure_availability: Mapping[
        str, FeatureAvailability
    ] = field(default_factory=dict)
    exposure_evidence_ids: Mapping[
        str, tuple[str, ...]
    ] = field(default_factory=dict)
    transmission_source_refs: Mapping[
        str, tuple[str, ...]
    ] = field(default_factory=dict)
    confirmed_fact_hashes: tuple[str, ...] = ()
    assumption_hashes: tuple[str, ...] = ()
    validation_result: ValidationResult = field(
        default_factory=ValidationResult.unvalidated
    )
    content_hash: str = ""
    schema_version: str = CURRENT_MARKET_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("as_of", "applicable_cutoff"):
            object.__setattr__(
                self,
                field_name,
                _normalize_timestamp(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "technical_direction",
            _enum_value(
                self.technical_direction,
                TechnicalDirection,
                field_name="technical_direction",
            ),
        )
        for field_name, enum_type in (
            ("technical_move_types", HistoricalMoveType),
            ("current_exposure_types", ExposureType),
            (
                "current_transmission_hypotheses",
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
        for field_name, enum_type in (
            ("expected_magnitude_bucket", MagnitudeBucket),
            ("expected_duration_bucket", DurationBucket),
            ("valuation_state", MarketRegimeState),
            ("liquidity_state", MarketRegimeState),
            ("positioning_state", MarketRegimeState),
            (
                "benchmark_relative_state",
                RelativePerformanceBucket,
            ),
            ("sector_relative_state", RelativePerformanceBucket),
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
        if self.market_regime is not None and not isinstance(
            self.market_regime,
            MarketRegime,
        ):
            raise TypeError("market_regime must be MarketRegime or null.")
        for field_name in (
            "current_event_types",
            "unavailable_features",
            "assumptions",
            "evidence_ids",
            "exposure_ids",
            "event_ids",
            "confirmed_fact_hashes",
            "assumption_hashes",
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
            "feature_availability",
            "exposure_category_availability",
            "exposure_availability",
        ):
            object.__setattr__(
                self,
                field_name,
                _feature_availability_mapping(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        for field_name in (
            "exposure_evidence_ids",
            "transmission_source_refs",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_sequence_mapping(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping.")
        object.__setattr__(
            self,
            "provenance",
            _freeze_json(self.provenance),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError("validation_result must be ValidationResult.")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CurrentMarketState":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("market_regime") is not None:
            raw["market_regime"] = MarketRegime.from_dict(
                raw["market_regime"]
            )
        raw["validation_result"] = ValidationResult.from_dict(
            raw.get(
                "validation_result",
                ValidationResult.unvalidated().to_dict(),
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CurrentStateBuildResult(_JsonContract):
    success: bool
    current_state: CurrentMarketState | None
    validation_result: ValidationResult
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    input_hashes: Mapping[str, str]
    output_hash: str
    generated_at: str
    content_hash: str = ""
    schema_version: str = CURRENT_STATE_BUILD_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.current_state is not None and not isinstance(
            self.current_state,
            CurrentMarketState,
        ):
            raise TypeError(
                "current_state must be CurrentMarketState or null."
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
        if not isinstance(self.input_hashes, Mapping):
            raise TypeError("input_hashes must be a mapping.")
        hashes = dict(self.input_hashes)
        if not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in hashes.items()
        ):
            raise TypeError(
                "input_hashes requires string keys and values."
            )
        object.__setattr__(
            self,
            "input_hashes",
            MappingProxyType(dict(sorted(hashes.items()))),
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
    ) -> "CurrentStateBuildResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("current_state") is not None:
            raw["current_state"] = CurrentMarketState.from_dict(
                raw["current_state"]
            )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


def evidence_packet_content_hash(
    evidence: Sequence[EvidenceItem],
) -> str:
    items = tuple(evidence)
    if not all(isinstance(item, EvidenceItem) for item in items):
        raise TypeError("evidence must contain EvidenceItem values.")
    ordered = sorted(items, key=lambda item: item.evidence_id)
    return _hash_payload(
        {
            "schema": "current-evidence-packet-1.0.0",
            "evidence": [item.to_dict() for item in ordered],
        }
    )


def exposure_report_content_hash(
    exposures: Sequence[ExposureItem],
    coverage: Sequence[ExposureCategoryCoverage],
) -> str:
    exposure_items = tuple(exposures)
    coverage_items = tuple(coverage)
    if not all(
        isinstance(item, ExposureItem) for item in exposure_items
    ):
        raise TypeError("exposures must contain ExposureItem values.")
    if not all(
        isinstance(item, ExposureCategoryCoverage)
        for item in coverage_items
    ):
        raise TypeError(
            "coverage must contain ExposureCategoryCoverage values."
        )
    return _hash_payload(
        {
            "schema": "current-exposure-report-1.0.0",
            "exposures": [
                item.to_dict()
                for item in sorted(
                    exposure_items,
                    key=lambda item: item.exposure_id,
                )
            ],
            "coverage": [
                item.to_dict()
                for item in sorted(
                    coverage_items,
                    key=lambda item: item.category.value,
                )
            ],
        }
    )


def material_event_packet_content_hash(
    events: Sequence[MaterialEvent],
) -> str:
    items = tuple(events)
    if not all(isinstance(item, MaterialEvent) for item in items):
        raise TypeError("events must contain MaterialEvent values.")
    return _hash_payload(
        {
            "schema": "current-material-event-packet-1.0.0",
            "events": [
                item.to_dict()
                for item in sorted(items, key=lambda item: item.event_id)
            ],
        }
    )


def current_market_state_content_hash(
    state: CurrentMarketState,
) -> str:
    if not isinstance(state, CurrentMarketState):
        raise TypeError("state must be CurrentMarketState.")
    return _contract_hash(state)


def current_state_build_result_content_hash(
    result: CurrentStateBuildResult,
) -> str:
    if not isinstance(result, CurrentStateBuildResult):
        raise TypeError("result must be CurrentStateBuildResult.")
    return _contract_hash(result)


_CURRENT_STATE_RULE_ORDER = (
    "schema_version",
    "content_hash",
    "state_identity",
    "cutoff_ordering",
    "frozen_technical_hash",
    "input_bundle_hashes",
    "unique_references",
    "evidence_references",
    "exposure_values",
    "exposure_references",
    "exposure_coverage",
    "event_references",
    "future_event_status",
    "market_regime",
    "feature_availability",
    "unavailable_features",
    "transmission_hypotheses",
    "assumption_separation",
    "provenance",
)


class _ValidationCollector:
    def __init__(self) -> None:
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
            item for item in _CURRENT_STATE_RULE_ORDER
            if item in self.failed
        )
        passed = tuple(
            item for item in _CURRENT_STATE_RULE_ORDER
            if item not in self.failed
        )
        return ValidationResult(
            is_valid=not self.errors,
            score=round(100.0 * len(passed) / len(_CURRENT_STATE_RULE_ORDER), 2),
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            failed_rules=failed,
            passed_rules=passed,
            validation_version=CURRENT_MARKET_STATE_VALIDATION_VERSION,
        )


def _validate_evidence_and_cutoffs(
    result: _ValidationCollector,
    evidence: Sequence[EvidenceItem],
    *,
    cutoff: datetime,
) -> None:
    factual = {EvidenceStatus.CONFIRMED, EvidenceStatus.SCHEDULED}
    for index, item in enumerate(evidence):
        publication = _temporal_point(item.publication_date)
        item_cutoff = _temporal_point(item.applicable_cutoff)
        cutoff_ok = (
            item_cutoff is not None
            and item_cutoff <= cutoff
            and (
                publication is None
                or publication <= item_cutoff
            )
        )
        result.require(
            "cutoff_ordering",
            cutoff_ok,
            f"evidence[{index}].publication_date",
            "Evidence publication and applicable cutoffs must not exceed the state cutoff.",
        )
        if item.status in factual:
            provenance_ok = all(
                value is not None and str(value).strip()
                for value in (
                    item.source,
                    item.source_type,
                    item.provider,
                    item.publication_date,
                    item.retrieval_timestamp,
                    item.applicable_cutoff,
                )
            )
            result.require(
                "evidence_references",
                provenance_ok,
                f"evidence[{index}]",
                "Confirmed and scheduled evidence requires complete provenance.",
            )


def _validate_exposures_and_coverage(
    result: _ValidationCollector,
    exposures: Sequence[ExposureItem],
    coverage: Sequence[ExposureCategoryCoverage],
    *,
    evidence_ids: set[str],
) -> None:
    keys = [
        (item.category, item.exposure_type) for item in exposures
    ]
    result.require(
        "unique_references",
        not _duplicates(keys),
        "exposures",
        "Duplicate category/exposure-type pairs must be merged.",
    )
    for index, item in enumerate(exposures):
        valid_values = (
            item.exposure_type
            in EXPOSURE_CATEGORY_COMPATIBILITY[item.category]
            and item.materiality in EXPOSURE_MATERIALITY_VALUES
            and item.direction in EXPOSURE_DIRECTION_VALUES
            and item.confidence in QUALITATIVE_CONFIDENCE_VALUES
        )
        result.require(
            "exposure_values",
            valid_values,
            f"exposures[{index}]",
            "Exposure category, mechanism, materiality, direction, or confidence is invalid.",
        )
        references = (
            item.supporting_evidence_ids
            + item.contradicting_evidence_ids
        )
        result.require(
            "exposure_references",
            set(references) <= evidence_ids
            and not (
                set(item.supporting_evidence_ids)
                & set(item.contradicting_evidence_ids)
            ),
            f"exposures[{index}]",
            "Exposure evidence references are dangling or overlap.",
        )

    categories = [item.category for item in coverage]
    result.require(
        "exposure_coverage",
        set(categories) == set(ExposureCategory)
        and not _duplicates(categories),
        "coverage",
        "Every exposure category requires exactly one coverage record.",
    )
    exposure_ids_by_category = {
        category: {
            item.exposure_id
            for item in exposures
            if item.category is category
        }
        for category in ExposureCategory
    }
    for index, item in enumerate(coverage):
        actual = exposure_ids_by_category[item.category]
        declared = set(item.exposure_ids)
        if actual:
            state_ok = (
                item.state
                is ExposureCoverageState.EXPOSURES_IDENTIFIED
                and actual == declared
                and len(declared) == len(item.exposure_ids)
            )
        else:
            state_ok = (
                item.state
                in {
                    ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE,
                    ExposureCoverageState.UNAVAILABLE,
                }
                and not item.exposure_ids
            )
        result.require(
            "exposure_coverage",
            state_ok
            and set(item.unmapped_evidence_ids) <= evidence_ids,
            f"coverage[{index}]",
            "Coverage state, exposure IDs, or unmapped evidence references are inconsistent.",
        )


def _validate_events(
    result: _ValidationCollector,
    events: Sequence[MaterialEvent],
    *,
    evidence_ids: set[str],
    cutoff: datetime,
) -> None:
    for index, item in enumerate(events):
        publication = _temporal_point(item.publication_date)
        item_cutoff = _temporal_point(item.applicable_cutoff)
        cutoff_ok = (
            item_cutoff is not None
            and item_cutoff <= cutoff
            and (
                publication is None
                or publication <= item_cutoff
            )
        )
        result.require(
            "cutoff_ordering",
            cutoff_ok,
            f"events[{index}].publication_date",
            "Event publication and applicable cutoffs must not exceed the state cutoff.",
        )
        result.require(
            "event_references",
            set(item.related_evidence_ids) <= evidence_ids,
            f"events[{index}].related_evidence_ids",
            "Material event contains dangling evidence references.",
        )
        scheduled = _temporal_point(item.scheduled_at)
        future_is_calendar_only = (
            scheduled is None
            or scheduled <= cutoff
            or item.status is EvidenceStatus.SCHEDULED
        )
        result.require(
            "future_event_status",
            future_is_calendar_only,
            f"events[{index}].status",
            "A future event may appear only as explicitly scheduled calendar context.",
        )


def validate_current_market_state(
    state: CurrentMarketState,
    *,
    premise: FrozenTechnicalPremise | None = None,
    evidence: Sequence[EvidenceItem] = (),
    exposures: Sequence[ExposureItem] = (),
    coverage: Sequence[ExposureCategoryCoverage] = (),
    events: Sequence[MaterialEvent] = (),
) -> ValidationResult:
    if not isinstance(state, CurrentMarketState):
        raise TypeError("state must be CurrentMarketState.")
    evidence_items = tuple(evidence)
    exposure_items = tuple(exposures)
    coverage_items = tuple(coverage)
    event_items = tuple(events)
    if not all(isinstance(item, EvidenceItem) for item in evidence_items):
        raise TypeError("evidence must contain EvidenceItem values.")
    if not all(isinstance(item, ExposureItem) for item in exposure_items):
        raise TypeError("exposures must contain ExposureItem values.")
    if not all(
        isinstance(item, ExposureCategoryCoverage)
        for item in coverage_items
    ):
        raise TypeError(
            "coverage must contain ExposureCategoryCoverage values."
        )
    if not all(isinstance(item, MaterialEvent) for item in event_items):
        raise TypeError("events must contain MaterialEvent values.")

    result = _ValidationCollector()
    result.require(
        "schema_version",
        state.schema_version == CURRENT_MARKET_STATE_SCHEMA_VERSION,
        "schema_version",
        "Current-market-state schema version is unsupported.",
    )
    result.require(
        "content_hash",
        bool(state.content_hash)
        and state.content_hash == current_market_state_content_hash(state),
        "content_hash",
        "Current-market-state content hash is missing or mismatched.",
    )
    identity_ok = (
        bool(state.state_id.strip())
        and state.state_id == _normalized_token(state.state_id)
        and bool(state.symbol.strip())
        and bool(state.exchange.strip())
        and bool(state.frozen_technical_premise_hash.strip())
        and bool(state.evidence_packet_hash.strip())
        and bool(state.exposure_report_hash.strip())
    )
    result.require(
        "state_identity",
        identity_ok,
        "state_id",
        "State identity, symbol, exchange, and input hashes are required.",
    )
    cutoff = _temporal_point(state.applicable_cutoff)
    as_of = _temporal_point(state.as_of)
    result.require(
        "cutoff_ordering",
        cutoff is not None and as_of is not None and cutoff <= as_of,
        "applicable_cutoff",
        "State cutoff must not follow as_of.",
    )

    if premise is not None:
        if not isinstance(premise, FrozenTechnicalPremise):
            raise TypeError(
                "premise must be FrozenTechnicalPremise or null."
            )
        premise_hash = frozen_technical_premise_content_hash(premise)
        premise_cutoff = _temporal_point(premise.market_data_cutoff)
        premise_ok = (
            premise.frozen_content_hash == premise_hash
            and state.frozen_technical_premise_hash == premise_hash
            and state.symbol == premise.symbol
            and (
                premise.exchange is None
                or state.exchange == premise.exchange
            )
            and cutoff is not None
            and premise_cutoff is not None
            and premise_cutoff <= cutoff
        )
        result.require(
            "frozen_technical_hash",
            premise_ok,
            "frozen_technical_premise_hash",
            "Frozen technical premise linkage or cutoff is invalid.",
        )
    else:
        result.require(
            "frozen_technical_hash",
            bool(state.frozen_technical_premise_hash),
            "frozen_technical_premise_hash",
            "Frozen technical premise hash is required.",
        )

    evidence_ids = [item.evidence_id for item in evidence_items]
    exposure_ids = [item.exposure_id for item in exposure_items]
    event_ids = [item.event_id for item in event_items]
    source_objects_supplied = bool(
        evidence_items
        or exposure_items
        or coverage_items
        or event_items
    )
    result.require(
        "unique_references",
        (
            not _duplicates(evidence_ids)
            and not _duplicates(exposure_ids)
            and not _duplicates(event_ids)
            and tuple(sorted(evidence_ids)) == state.evidence_ids
            and tuple(sorted(exposure_ids)) == state.exposure_ids
            and tuple(sorted(event_ids)) == state.event_ids
            if source_objects_supplied
            else (
                not _duplicates(state.evidence_ids)
                and not _duplicates(state.exposure_ids)
                and not _duplicates(state.event_ids)
                and tuple(sorted(state.evidence_ids))
                == state.evidence_ids
                and tuple(sorted(state.exposure_ids))
                == state.exposure_ids
                and tuple(sorted(state.event_ids))
                == state.event_ids
            )
        ),
        "evidence_ids",
        "Input identities must be unique and match the frozen state.",
    )
    if evidence_items or exposure_items or coverage_items:
        result.require(
            "input_bundle_hashes",
            state.evidence_packet_hash
            == evidence_packet_content_hash(evidence_items)
            and state.exposure_report_hash
            == exposure_report_content_hash(
                exposure_items,
                coverage_items,
            ),
            "evidence_packet_hash",
            "Evidence or exposure bundle hash does not match source inputs.",
        )

    if cutoff is not None and source_objects_supplied:
        _validate_evidence_and_cutoffs(
            result,
            evidence_items,
            cutoff=cutoff,
        )
        _validate_events(
            result,
            event_items,
            evidence_ids=set(evidence_ids),
            cutoff=cutoff,
        )
    if source_objects_supplied:
        _validate_exposures_and_coverage(
            result,
            exposure_items,
            coverage_items,
            evidence_ids=set(evidence_ids),
        )

    if state.market_regime is not None:
        regime_validation = validate_market_regime(
            state.market_regime,
            evidence_ids=(
                set(evidence_ids)
                if source_objects_supplied
                else None
            ),
        )
        regime_end = _temporal_point(state.market_regime.regime_end_at)
        regime_start = _temporal_point(
            state.market_regime.regime_start_at
        )
        regime_cutoff_ok = (
            cutoff is not None
            and (
                regime_start is None
                or regime_start <= cutoff
            )
            and (
                regime_end is None
                or regime_end <= cutoff
            )
        )
        result.require(
            "market_regime",
            regime_validation.is_valid
            and state.market_regime.content_hash
            == market_regime_content_hash(state.market_regime)
            and regime_cutoff_ok,
            "market_regime",
            "Market regime is invalid, dangling, hash-mismatched, or after cutoff.",
        )
    else:
        result.require(
            "market_regime",
            state.feature_availability.get("market_regime")
            is FeatureAvailability.UNAVAILABLE,
            "feature_availability.market_regime",
            "Absent market regime must be explicitly unavailable.",
        )

    required_availability_keys = set(_TECHNICAL_FEATURE_KEYS)
    result.require(
        "feature_availability",
        required_availability_keys
        <= set(state.feature_availability)
        and set(state.exposure_category_availability)
        == {item.value for item in ExposureCategory}
        and set(state.exposure_availability)
        == {item.value for item in state.current_exposure_types},
        "feature_availability",
        "Feature and exposure availability maps are incomplete.",
    )
    expected_unavailable = {
        key
        for key, value in state.feature_availability.items()
        if value is FeatureAvailability.UNAVAILABLE
    } | {
        f"exposure_category:{key}"
        for key, value in state.exposure_category_availability.items()
        if value is FeatureAvailability.UNAVAILABLE
    }
    result.require(
        "unavailable_features",
        set(state.unavailable_features) == expected_unavailable
        and not _duplicates(state.unavailable_features),
        "unavailable_features",
        "Unavailable features must exactly match availability declarations.",
    )
    result.require(
        "transmission_hypotheses",
        set(state.transmission_source_refs)
        == {
            item.value
            for item in state.current_transmission_hypotheses
        }
        and all(
            set(refs)
            <= (
                set(exposure_ids) | set(event_ids)
                if source_objects_supplied
                else set(state.exposure_ids) | set(state.event_ids)
            )
            and refs
            for refs in state.transmission_source_refs.values()
        ),
        "transmission_source_refs",
        "Transmission hypotheses require controlled values and source references.",
    )
    assumed_features = {
        key
        for key, value in state.feature_availability.items()
        if value is FeatureAvailability.ASSUMED
    } | {
        key
        for key, value in state.exposure_availability.items()
        if value is FeatureAvailability.ASSUMED
    }
    result.require(
        "assumption_separation",
        not (
            set(state.confirmed_fact_hashes)
            & set(state.assumption_hashes)
        )
        and set(state.assumption_hashes)
        == {_hash_payload(item) for item in state.assumptions}
        and (not assumed_features or bool(state.assumptions)),
        "assumptions",
        "Assumptions must be explicit and disjoint from confirmed facts.",
    )
    result.require(
        "provenance",
        state.provenance.get("builder_version")
        == CURRENT_STATE_BUILDER_VERSION
        and state.provenance.get("technical_normalization_version")
        == CURRENT_TECHNICAL_NORMALIZATION_VERSION
        and state.provenance.get("transmission_policy_version")
        == CURRENT_TRANSMISSION_POLICY_VERSION
        and bool(
            state.provenance.get("material_event_packet_hash")
        )
        and (
            not source_objects_supplied
            or state.provenance.get("material_event_packet_hash")
            == material_event_packet_content_hash(event_items)
        ),
        "provenance",
        "Current state provenance is incomplete or inconsistent.",
    )
    return result.result()


def finalize_current_market_state(
    state: CurrentMarketState,
    *,
    premise: FrozenTechnicalPremise | None = None,
    evidence: Sequence[EvidenceItem] = (),
    exposures: Sequence[ExposureItem] = (),
    coverage: Sequence[ExposureCategoryCoverage] = (),
    events: Sequence[MaterialEvent] = (),
) -> CurrentMarketState:
    if not isinstance(state, CurrentMarketState):
        raise TypeError("state must be CurrentMarketState.")
    seed = replace(
        state,
        validation_result=ValidationResult.unvalidated(),
        content_hash="",
    )
    seed = replace(
        seed,
        content_hash=current_market_state_content_hash(seed),
    )
    validation = validate_current_market_state(
        seed,
        premise=premise,
        evidence=evidence,
        exposures=exposures,
        coverage=coverage,
        events=events,
    )
    finalized = replace(
        seed,
        validation_result=validation,
        content_hash="",
    )
    finalized = replace(
        finalized,
        content_hash=current_market_state_content_hash(finalized),
    )
    repeated = validate_current_market_state(
        finalized,
        premise=premise,
        evidence=evidence,
        exposures=exposures,
        coverage=coverage,
        events=events,
    )
    if repeated != validation:
        raise RuntimeError(
            "Current-market-state validation did not stabilize."
        )
    return finalized


def _technical_direction(
    direction: ScenarioDirection,
) -> TechnicalDirection:
    return {
        ScenarioDirection.UP: TechnicalDirection.BULLISH,
        ScenarioDirection.DOWN: TechnicalDirection.BEARISH,
        ScenarioDirection.SIDEWAYS: TechnicalDirection.NEUTRAL,
        ScenarioDirection.UNKNOWN: TechnicalDirection.UNAVAILABLE,
    }[direction]


def _explicit_technical_mapping(
    premise: FrozenTechnicalPremise,
) -> tuple[Mapping[str, Any], ...]:
    structure = premise.technical_structure
    if not isinstance(structure, Mapping):
        return ()
    nested = structure.get("retrieval_features")
    if isinstance(nested, Mapping):
        return nested, structure
    return (structure,)


def _explicit_value(
    mappings: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> Any:
    for mapping in mappings:
        for key in keys:
            if key in mapping:
                return mapping[key]
    return None


def _normalise_technical_features(
    premise: FrozenTechnicalPremise,
) -> tuple[
    TechnicalDirection,
    tuple[HistoricalMoveType, ...],
    str,
    MagnitudeBucket,
    DurationBucket,
    dict[str, FeatureAvailability],
    tuple[str, ...],
]:
    warnings: list[str] = []
    availability: dict[str, FeatureAvailability] = {}
    direction = _technical_direction(premise.direction)
    availability["technical_direction"] = (
        FeatureAvailability.UNAVAILABLE
        if direction is TechnicalDirection.UNAVAILABLE
        else FeatureAvailability.AVAILABLE
    )

    mappings = _explicit_technical_mapping(premise)
    raw_structure = (
        premise.technical_structure
        if isinstance(premise.technical_structure, str)
        else _explicit_value(
            mappings,
            (
                "structure",
                "technical_structure",
                "pattern_family",
            ),
        )
    )
    if not isinstance(raw_structure, str) or not raw_structure.strip():
        raw_structure = premise.current_wave_or_phase
    structure = (
        _normalized_token(raw_structure)
        if isinstance(raw_structure, str) and raw_structure.strip()
        else "unavailable"
    )
    availability["technical_structure"] = (
        FeatureAvailability.UNAVAILABLE
        if structure == "unavailable"
        else FeatureAvailability.AVAILABLE
    )

    raw_move_types = _explicit_value(
        mappings,
        (
            "technical_move_types",
            "projected_move_classes",
            "projected_move_class",
            "move_type",
        ),
    )
    if raw_move_types is None:
        values: tuple[Any, ...] = ()
    elif isinstance(raw_move_types, (str, HistoricalMoveType)):
        values = (raw_move_types,)
    elif isinstance(raw_move_types, Sequence):
        values = tuple(raw_move_types)
    else:
        values = ()
        warnings.append("unsupported_technical_move_type_shape")
    move_types: set[HistoricalMoveType] = set()
    for value in values:
        try:
            move_types.add(
                value
                if isinstance(value, HistoricalMoveType)
                else HistoricalMoveType(value)
            )
        except (TypeError, ValueError):
            warnings.append(
                "unsupported_technical_move_type:"
                + _normalized_token(str(value))
            )
    if not move_types:
        if structure in {"five_wave_impulse", "impulse", "motive_impulse"}:
            if direction is TechnicalDirection.BULLISH:
                move_types.add(HistoricalMoveType.UPWARD_IMPULSE)
            elif direction is TechnicalDirection.BEARISH:
                move_types.add(HistoricalMoveType.DOWNWARD_IMPULSE)
        elif structure in _TECHNICAL_MOVE_ALIASES:
            move_types.add(_TECHNICAL_MOVE_ALIASES[structure])
    ordered_move_types = tuple(
        sorted(move_types, key=lambda item: item.value)
    )
    availability["technical_move_types"] = (
        FeatureAvailability.AVAILABLE
        if ordered_move_types
        else FeatureAvailability.UNAVAILABLE
    )

    raw_magnitude = _explicit_value(
        mappings,
        ("magnitude_bucket", "expected_magnitude_bucket"),
    )
    try:
        magnitude = (
            MagnitudeBucket(raw_magnitude)
            if raw_magnitude is not None
            else MagnitudeBucket.UNAVAILABLE
        )
    except (TypeError, ValueError):
        magnitude = MagnitudeBucket.UNAVAILABLE
        warnings.append("unsupported_expected_magnitude_bucket")
    availability["expected_magnitude_bucket"] = (
        FeatureAvailability.UNAVAILABLE
        if magnitude is MagnitudeBucket.UNAVAILABLE
        else FeatureAvailability.AVAILABLE
    )

    raw_duration = _explicit_value(
        mappings,
        ("duration_bucket", "expected_duration_bucket"),
    )
    try:
        duration = (
            DurationBucket(raw_duration)
            if raw_duration is not None
            else DurationBucket.UNAVAILABLE
        )
    except (TypeError, ValueError):
        duration = DurationBucket.UNAVAILABLE
        warnings.append("unsupported_expected_duration_bucket")
    availability["expected_duration_bucket"] = (
        FeatureAvailability.UNAVAILABLE
        if duration is DurationBucket.UNAVAILABLE
        else FeatureAvailability.AVAILABLE
    )
    return (
        direction,
        ordered_move_types,
        structure,
        magnitude,
        duration,
        availability,
        tuple(sorted(set(warnings))),
    )


def _availability_for_exposure(
    exposure: ExposureItem,
) -> FeatureAvailability:
    if exposure.status in {EvidenceStatus.CONFIRMED, EvidenceStatus.SCHEDULED}:
        return FeatureAvailability.AVAILABLE
    if exposure.status is EvidenceStatus.HYPOTHETICAL:
        return FeatureAvailability.ASSUMED
    if exposure.status in {
        EvidenceStatus.INTERPRETATION,
        EvidenceStatus.STALE,
    }:
        return FeatureAvailability.DRAFT
    return FeatureAvailability.UNAVAILABLE


def _coverage_availability(
    coverage: ExposureCategoryCoverage,
) -> FeatureAvailability:
    return {
        ExposureCoverageState.EXPOSURES_IDENTIFIED: (
            FeatureAvailability.AVAILABLE
        ),
        ExposureCoverageState.RESEARCHED_NO_MATERIAL_EXPOSURE: (
            FeatureAvailability.RESEARCHED_NO_SIGNAL
        ),
        ExposureCoverageState.UNAVAILABLE: FeatureAvailability.UNAVAILABLE,
        ExposureCoverageState.UNDECLARED: FeatureAvailability.DRAFT,
    }[coverage.state]


def _event_transmissions(
    event: MaterialEvent,
) -> tuple[TransmissionMechanism, ...]:
    token = _normalized_token(event.event_type)
    if token in _EXPLICIT_EVENT_TRANSMISSION_MAP:
        return _EXPLICIT_EVENT_TRANSMISSION_MAP[token]
    for prefix in ("transmission_", "mechanism_"):
        if token.startswith(prefix):
            raw = token[len(prefix):]
            try:
                return (TransmissionMechanism(raw),)
            except ValueError:
                return ()
    return ()


def _failure_validation(code: str, message: str) -> ValidationResult:
    issue = ValidationIssue(
        code=code,
        severity="error",
        path="current_state_builder",
        message=message,
    )
    return ValidationResult(
        is_valid=False,
        score=0.0,
        errors=(issue,),
        failed_rules=(code,),
        validation_version=CURRENT_MARKET_STATE_VALIDATION_VERSION,
    )


class CurrentStateBuilder:
    """Build one immutable state from already supplied decision-time inputs."""

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
        state: CurrentMarketState | None,
        validation: ValidationResult,
        errors: Sequence[str],
        warnings: Sequence[str],
        input_hashes: Mapping[str, str],
        generated_at: str,
    ) -> CurrentStateBuildResult:
        seed = CurrentStateBuildResult(
            success=success,
            current_state=state,
            validation_result=validation,
            errors=tuple(errors),
            warnings=tuple(sorted(set(warnings))),
            input_hashes=input_hashes,
            output_hash=state.content_hash if state is not None else "",
            generated_at=generated_at,
        )
        return replace(
            seed,
            content_hash=current_state_build_result_content_hash(seed),
        )

    def build(
        self,
        premise: FrozenTechnicalPremise,
        evidence: Sequence[EvidenceItem],
        exposures: Sequence[ExposureItem],
        coverage: Sequence[ExposureCategoryCoverage],
        *,
        events: Sequence[MaterialEvent] = (),
        market_regime: MarketRegime | None = None,
        valuation_state: MarketRegimeState | str | None = None,
        liquidity_state: MarketRegimeState | str | None = None,
        positioning_state: MarketRegimeState | str | None = None,
        benchmark_relative_state: (
            RelativePerformanceBucket | str | None
        ) = None,
        sector_relative_state: (
            RelativePerformanceBucket | str | None
        ) = None,
        availability_overrides: Mapping[
            str, FeatureAvailability | str
        ] | None = None,
        assumptions: Sequence[str] = (),
        applicable_cutoff: str | datetime | None = None,
        as_of: str | datetime | None = None,
        state_id: str | None = None,
    ) -> CurrentStateBuildResult:
        if not isinstance(premise, FrozenTechnicalPremise):
            raise TypeError("premise must be FrozenTechnicalPremise.")
        for value, model_type, field_name in (
            (evidence, EvidenceItem, "evidence"),
            (exposures, ExposureItem, "exposures"),
            (coverage, ExposureCategoryCoverage, "coverage"),
            (events, MaterialEvent, "events"),
        ):
            if isinstance(value, (str, bytes)) or not isinstance(
                value,
                Sequence,
            ):
                raise TypeError(f"{field_name} must be a sequence.")
            if not all(isinstance(item, model_type) for item in value):
                raise TypeError(
                    f"{field_name} must contain {model_type.__name__} values."
                )
        if market_regime is not None and not isinstance(
            market_regime,
            MarketRegime,
        ):
            raise TypeError("market_regime must be MarketRegime or null.")
        evidence_items = tuple(evidence)
        exposure_items = tuple(exposures)
        coverage_items = tuple(coverage)
        event_items = tuple(events)
        assumption_items = _string_tuple(
            assumptions,
            field_name="assumptions",
        )
        generated_at = _normalize_timestamp(
            self.clock(),
            field_name="generated_at",
        )
        cutoff = _normalize_timestamp(
            applicable_cutoff
            if applicable_cutoff is not None
            else premise.market_data_cutoff,
            field_name="applicable_cutoff",
        )
        state_as_of = _normalize_timestamp(
            as_of if as_of is not None else generated_at,
            field_name="as_of",
        )
        premise_hash = frozen_technical_premise_content_hash(premise)
        evidence_hash = evidence_packet_content_hash(evidence_items)
        exposure_hash = exposure_report_content_hash(
            exposure_items,
            coverage_items,
        )
        event_hash = material_event_packet_content_hash(event_items)
        input_hashes = {
            "frozen_technical_premise": premise.frozen_content_hash,
            "evidence_packet": evidence_hash,
            "exposure_report": exposure_hash,
            "material_event_packet": event_hash,
            **(
                {"market_regime": market_regime.content_hash}
                if market_regime is not None
                else {}
            ),
        }

        premise_valid = (
            bool(premise.resolution_id.strip())
            and bool(premise.symbol.strip())
            and bool(premise.source_hashes)
            and premise.frozen_content_hash == premise_hash
            and _temporal_point(premise.market_data_cutoff) is not None
            and _temporal_point(cutoff) is not None
            and _temporal_point(premise.market_data_cutoff)
            <= _temporal_point(cutoff)
        )
        if not premise_valid:
            return self._result(
                success=False,
                state=None,
                validation=_failure_validation(
                    "invalid_frozen_technical_premise",
                    "Frozen technical premise is invalid, hash-mismatched, or after the state cutoff.",
                ),
                errors=(
                    "Current state requires one valid frozen technical premise.",
                ),
                warnings=(),
                input_hashes=input_hashes,
                generated_at=generated_at,
            )

        evidence_ids = [item.evidence_id for item in evidence_items]
        exposure_ids = [item.exposure_id for item in exposure_items]
        event_ids = [item.event_id for item in event_items]
        coverage_categories = [item.category for item in coverage_items]
        if (
            _duplicates(evidence_ids)
            or _duplicates(exposure_ids)
            or _duplicates(event_ids)
            or _duplicates(coverage_categories)
            or set(coverage_categories) != set(ExposureCategory)
        ):
            return self._result(
                success=False,
                state=None,
                validation=_failure_validation(
                    "invalid_current_source_identity",
                    "Current evidence, exposure, event, and coverage identities must be unique and complete.",
                ),
                errors=(
                    "Duplicate source IDs or incomplete exposure coverage.",
                ),
                warnings=(),
                input_hashes=input_hashes,
                generated_at=generated_at,
            )

        (
            direction,
            move_types,
            technical_structure,
            magnitude,
            duration,
            feature_availability,
            technical_warnings,
        ) = _normalise_technical_features(premise)
        warnings: list[str] = list(technical_warnings)

        overrides = _feature_availability_mapping(
            availability_overrides or {},
            field_name="availability_overrides",
        )
        for key, value in overrides.items():
            if key not in _TECHNICAL_FEATURE_KEYS:
                warnings.append(
                    f"unsupported_availability_override:{key}"
                )
                continue
            feature_availability[key] = value

        def normalized_regime_state(
            raw: MarketRegimeState | str | None,
            field_name: str,
        ) -> MarketRegimeState:
            if raw is None:
                feature_availability[field_name] = (
                    FeatureAvailability.UNAVAILABLE
                )
                return MarketRegimeState.UNAVAILABLE
            feature_availability[field_name] = overrides.get(
                field_name,
                FeatureAvailability.AVAILABLE,
            )
            return _enum_value(
                raw,
                MarketRegimeState,
                field_name=field_name,
            )

        normalized_valuation = normalized_regime_state(
            valuation_state,
            "valuation_state",
        )
        normalized_liquidity = normalized_regime_state(
            liquidity_state,
            "liquidity_state",
        )
        normalized_positioning = normalized_regime_state(
            positioning_state,
            "positioning_state",
        )

        def normalized_relative_state(
            raw: RelativePerformanceBucket | str | None,
            field_name: str,
        ) -> RelativePerformanceBucket:
            if raw is None:
                feature_availability[field_name] = (
                    FeatureAvailability.UNAVAILABLE
                )
                return RelativePerformanceBucket.UNAVAILABLE
            feature_availability[field_name] = overrides.get(
                field_name,
                FeatureAvailability.AVAILABLE,
            )
            return _enum_value(
                raw,
                RelativePerformanceBucket,
                field_name=field_name,
            )

        normalized_benchmark = normalized_relative_state(
            benchmark_relative_state,
            "benchmark_relative_state",
        )
        normalized_sector = normalized_relative_state(
            sector_relative_state,
            "sector_relative_state",
        )
        feature_availability["market_regime"] = (
            overrides.get(
                "market_regime",
                FeatureAvailability.AVAILABLE,
            )
            if market_regime is not None
            else FeatureAvailability.UNAVAILABLE
        )

        active_exposures = tuple(
            item
            for item in exposure_items
            if item.materiality != "not_material"
            and _availability_for_exposure(item)
            is not FeatureAvailability.UNAVAILABLE
        )
        current_exposure_types = tuple(
            sorted(
                {item.exposure_type for item in active_exposures},
                key=lambda item: item.value,
            )
        )
        exposure_availability: dict[str, FeatureAvailability] = {}
        exposure_evidence_ids: dict[str, set[str]] = {}
        for exposure_type in current_exposure_types:
            matching = tuple(
                item
                for item in active_exposures
                if item.exposure_type is exposure_type
            )
            states = {
                _availability_for_exposure(item) for item in matching
            }
            if FeatureAvailability.AVAILABLE in states:
                availability = FeatureAvailability.AVAILABLE
            elif FeatureAvailability.ASSUMED in states:
                availability = FeatureAvailability.ASSUMED
            else:
                availability = FeatureAvailability.DRAFT
            exposure_availability[exposure_type.value] = availability
            exposure_evidence_ids[exposure_type.value] = {
                reference
                for item in matching
                for reference in (
                    item.supporting_evidence_ids
                    + item.contradicting_evidence_ids
                )
            }

        category_availability = {
            item.category.value: _coverage_availability(item)
            for item in coverage_items
        }
        current_event_types = tuple(
            sorted(
                {
                    _normalized_token(item.event_type)
                    for item in event_items
                    if item.status
                    not in {
                        EvidenceStatus.UNKNOWN,
                        EvidenceStatus.UNAVAILABLE,
                    }
                    and _normalized_token(item.event_type)
                }
            )
        )

        transmission_refs: dict[
            TransmissionMechanism, set[str]
        ] = {}
        mapped_exposure_types: set[ExposureType] = set()
        for item in active_exposures:
            mechanisms = CURRENT_EXPOSURE_TRANSMISSION_MAP.get(
                item.exposure_type,
                (),
            )
            if mechanisms:
                mapped_exposure_types.add(item.exposure_type)
            for mechanism in mechanisms:
                transmission_refs.setdefault(mechanism, set()).add(
                    item.exposure_id
                )
        for item in event_items:
            for mechanism in _event_transmissions(item):
                transmission_refs.setdefault(mechanism, set()).add(
                    item.event_id
                )
        for exposure_type in current_exposure_types:
            if exposure_type not in mapped_exposure_types:
                warnings.append(
                    "unmapped_transmission_exposure:"
                    + exposure_type.value
                )
        transmissions = tuple(
            sorted(transmission_refs, key=lambda item: item.value)
        )
        for mechanism in transmissions:
            feature_availability[
                f"transmission:{mechanism.value}"
            ] = FeatureAvailability.DRAFT

        collected_assumptions = _ordered_strings(
            assumption_items
            + tuple(
                assumption
                for item in exposure_items
                for assumption in item.assumptions
            )
        )
        confirmed_claims = tuple(
            item.claim
            for item in evidence_items
            if item.status in {
                EvidenceStatus.CONFIRMED,
                EvidenceStatus.SCHEDULED,
            }
        )
        confirmed_hashes = _ordered_strings(
            tuple(_hash_payload(item) for item in confirmed_claims)
        )
        assumption_hashes = _ordered_strings(
            tuple(_hash_payload(item) for item in collected_assumptions)
        )
        unavailable = _ordered_strings(
            tuple(
                key
                for key, value in feature_availability.items()
                if value is FeatureAvailability.UNAVAILABLE
            )
            + tuple(
                f"exposure_category:{key}"
                for key, value in category_availability.items()
                if value is FeatureAvailability.UNAVAILABLE
            )
        )
        selected_state_id = (
            _normalized_token(state_id)
            if state_id is not None
            else "state_"
            + _hash_payload(
                {
                    "symbol": premise.symbol,
                    "cutoff": cutoff,
                    "premise_hash": premise_hash,
                }
            )[:20]
        )
        draft = CurrentMarketState(
            state_id=selected_state_id,
            symbol=premise.symbol,
            exchange=premise.exchange or "unavailable",
            as_of=state_as_of,
            applicable_cutoff=cutoff,
            frozen_technical_premise_hash=premise_hash,
            evidence_packet_hash=evidence_hash,
            exposure_report_hash=exposure_hash,
            technical_direction=direction,
            technical_move_types=move_types,
            technical_structure=technical_structure,
            expected_magnitude_bucket=magnitude,
            expected_duration_bucket=duration,
            current_exposure_types=current_exposure_types,
            current_event_types=current_event_types,
            current_transmission_hypotheses=transmissions,
            market_regime=market_regime,
            valuation_state=normalized_valuation,
            liquidity_state=normalized_liquidity,
            positioning_state=normalized_positioning,
            benchmark_relative_state=normalized_benchmark,
            sector_relative_state=normalized_sector,
            unavailable_features=unavailable,
            assumptions=collected_assumptions,
            provenance={
                "builder_version": CURRENT_STATE_BUILDER_VERSION,
                "technical_normalization_version": (
                    CURRENT_TECHNICAL_NORMALIZATION_VERSION
                ),
                "transmission_policy_version": (
                    CURRENT_TRANSMISSION_POLICY_VERSION
                ),
                "cutoff_rule": (
                    "publication_at_or_before_item_cutoff_at_or_before_state_cutoff"
                ),
                "technical_source_hashes": dict(premise.source_hashes),
                "material_event_packet_hash": event_hash,
            },
            evidence_ids=tuple(sorted(evidence_ids)),
            exposure_ids=tuple(sorted(exposure_ids)),
            event_ids=tuple(sorted(event_ids)),
            feature_availability=feature_availability,
            exposure_category_availability=category_availability,
            exposure_availability=exposure_availability,
            exposure_evidence_ids={
                key: tuple(sorted(value))
                for key, value in exposure_evidence_ids.items()
            },
            transmission_source_refs={
                mechanism.value: tuple(
                    sorted(transmission_refs[mechanism])
                )
                for mechanism in transmissions
            },
            confirmed_fact_hashes=confirmed_hashes,
            assumption_hashes=assumption_hashes,
        )
        state = finalize_current_market_state(
            draft,
            premise=premise,
            evidence=evidence_items,
            exposures=exposure_items,
            coverage=coverage_items,
            events=event_items,
        )
        if not state.validation_result.is_valid:
            errors = tuple(
                f"{item.code} at {item.path}: {item.message}"
                for item in state.validation_result.errors
            )
            return self._result(
                success=False,
                state=state,
                validation=state.validation_result,
                errors=errors,
                warnings=warnings,
                input_hashes=input_hashes,
                generated_at=generated_at,
            )
        return self._result(
            success=True,
            state=state,
            validation=state.validation_result,
            errors=(),
            warnings=warnings,
            input_hashes=input_hashes,
            generated_at=generated_at,
        )


__all__ = [
    "CURRENT_EXPOSURE_TRANSMISSION_MAP",
    "CURRENT_MARKET_STATE_SCHEMA_VERSION",
    "CURRENT_MARKET_STATE_VALIDATION_VERSION",
    "CURRENT_STATE_BUILDER_VERSION",
    "CURRENT_STATE_BUILD_RESULT_SCHEMA_VERSION",
    "CURRENT_TECHNICAL_NORMALIZATION_VERSION",
    "CURRENT_TRANSMISSION_POLICY_VERSION",
    "CurrentMarketState",
    "CurrentStateBuildResult",
    "CurrentStateBuilder",
    "FeatureAvailability",
    "TechnicalDirection",
    "current_market_state_content_hash",
    "current_state_build_result_content_hash",
    "evidence_packet_content_hash",
    "exposure_report_content_hash",
    "finalize_current_market_state",
    "material_event_packet_content_hash",
    "validate_current_market_state",
]
