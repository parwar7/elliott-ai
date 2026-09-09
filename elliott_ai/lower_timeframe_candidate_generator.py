"""Immutable shadow-only lower-timeframe child-graph generation contracts.

The module has no market-data provider, database, CLI, or active-pipeline
integration. A caller supplies already-frozen native candles and must explicitly
authorize a provider invocation. Generated graphs are candidates only; the
subdivision verifier owns every verification status.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import MISSING, dataclass, fields, replace
from datetime import datetime, time, timezone
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .correction_semantics import CORRECTIVE_FAMILIES, MOTIVE_FAMILIES
from .adaptive_structure import (
    ADAPTIVE_DIAGONAL_RULE_POLICY_VERSION,
    DiagonalDeclaration,
    DiagonalType,
    diagonal_internal_family_policy,
)
from .forecast_records import DatasetCutoff, canonical_sha256
from .market_data_identity import dataset_feed_family_matches
from .market_data import pivot_windows_for
from .pivot_chronology import (
    PivotChronologyConflict,
    child_graph_boundary_path,
    chronology_groups_for_typed_pivots,
    first_chronology_conflict,
)
from .schema import STANDARD_DEGREES
from .timeframes import (
    TimeframeUsePurpose,
    normalize_timeframe_name,
    timeframe_compatibility_for_purpose,
    timeframe_is_compatible_with_degree,
    timeframe_is_same_or_finer,
)


LOWER_TIMEFRAME_CANDIDATE_GENERATOR_SCHEMA_VERSION = (
    "lower-timeframe-candidate-generator-1.2.0"
)
LOWER_TIMEFRAME_CANDIDATE_POLICY_VERSION = (
    "lower-timeframe-candidate-policy-1.0.0"
)
LOWER_TIMEFRAME_CANDIDATE_CALCULATION_VERSION = (
    "lower-timeframe-candidate-calculation-1.0.0"
)
NATIVE_CANDLE_SCHEMA_VERSION = "lower-timeframe-native-candle-1.0.0"
NATIVE_WINDOW_SCHEMA_VERSION = "lower-timeframe-native-window-1.0.0"
TYPED_PIVOT_SCHEMA_VERSION = "lower-timeframe-typed-pivot-1.0.0"
PARENT_CANDIDATE_SCHEMA_VERSION = "lower-timeframe-parent-candidate-1.2.0"
CHILD_GRAPH_SCHEMA_VERSION = "lower-timeframe-child-graph-1.1.0"
GENERATION_PAIR_SCHEMA_VERSION = "lower-timeframe-generation-pair-1.0.0"
RECONSTRUCTION_EXCLUSION_SCHEMA_VERSION = (
    "lower-timeframe-reconstruction-exclusion-1.0.0"
)
CHILD_RECONSTRUCTION_CONTEXT_SCHEMA_VERSION = (
    "lower-timeframe-child-reconstruction-context-1.1.0"
)
CHILD_RECONSTRUCTION_POLICY_VERSION = (
    "lower-timeframe-informed-reconstruction-family-locked-1.1.0"
)
LEGACY_CHILD_RECONSTRUCTION_POLICY_VERSION = (
    "lower-timeframe-informed-reconstruction-1.0.0"
)
RECONSTRUCTION_ALLOWED_CHANGES = (
    "different_pivots",
    "different_segmentation",
    "different_child_subfamilies_within_declared_parent_family",
    "split_swings",
    "merge_swings",
    "change_completion_state",
    "request_planner_authorized_timeframe",
)
_LEGACY_RECONSTRUCTION_ALLOWED_CHANGES = (
    "different_pivots",
    "different_family",
    "split_swings",
    "merge_swings",
    "change_completion_state",
    "request_planner_authorized_timeframe",
)

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_ALLOWED_FAMILIES = frozenset((*MOTIVE_FAMILIES, *CORRECTIVE_FAMILIES))
class CandidateGraphRole(StrEnum):
    PRIMARY = "primary"
    ALTERNATIVE = "alternative"


class WindowCoverageRole(StrEnum):
    REQUIRED_GENERATION = "required_generation"
    SUPPORTING_ONLY = "supporting_only"
    POLICY_NOT_REQUIRED = "policy_not_required"


class PivotPriceField(StrEnum):
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"


class PivotType(StrEnum):
    HIGH = "high"
    LOW = "low"


class InvalidationDirection(StrEnum):
    BELOW = "below"
    ABOVE = "above"


class InvalidationEvaluationBasis(StrEnum):
    INTRABAR_TOUCH_OR_BREACH = "intrabar_touch_or_breach"
    CANDLE_CLOSE = "candle_close"


class CoverageLimitationCode(StrEnum):
    SUPPORTING_ONLY = "supporting_only"
    POLICY_NOT_REQUIRED = "policy_not_required"
    PARTIAL_COVERAGE = "partial_coverage"
    UNAVAILABLE = "unavailable"


class CandidateGenerationFailureCode(StrEnum):
    SHADOW_MODE_REQUIRED = "shadow_mode_required"
    MODEL_CALL_NOT_AUTHORIZED = "model_call_not_authorized"
    SOURCE_INTEGRITY_FAILED = "source_integrity_failed"
    CUTOFF_VIOLATION = "cutoff_violation"
    REQUIRED_WINDOW_NOT_NATIVE = "required_window_not_native"
    MISSING_FULL_TARGET_WINDOW = "missing_full_target_window"
    SOURCE_METADATA_MISMATCH = "source_metadata_mismatch"
    TARGET_TIMEFRAME_NOT_LOWER = "target_timeframe_not_lower"
    TARGET_TIMEFRAME_NOT_AUTHORIZED = "target_timeframe_not_authorized"
    TARGET_CHILD_DEGREE_INVALID = "target_child_degree_invalid"
    RECURSION_BUDGET_EXHAUSTED = "recursion_budget_exhausted"
    PIVOT_CATALOG_UNAVAILABLE = "pivot_catalog_unavailable"
    PIVOT_NOT_IN_CATALOG = "pivot_not_in_catalog"
    INTRABAR_ORDER_RESOLUTION_REQUIRED = "intrabar_order_resolution_required"
    FAMILY_POSITIONS_INVALID = "family_positions_invalid"
    INVALIDATION_INVALID = "invalidation_invalid"
    DUPLICATE_GRAPH_SIGNATURE = "duplicate_graph_signature"
    DUPLICATE_RECONSTRUCTION_CANDIDATE = "duplicate_reconstruction_candidate"
    PARENT_FAMILY_RECLASSIFICATION_REQUIRED = (
        "parent_family_reclassification_required"
    )
    MALFORMED_STRUCTURED_OUTPUT = "malformed_structured_output"
    PROVIDER_INTERFACE_INVALID = "provider_interface_invalid"


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
    if value is None or isinstance(value, (str, int, float, bool, StrEnum)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Immutable JSON cannot contain non-finite numbers.")
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _freeze_json(to_dict())
    raise TypeError(f"Unsupported immutable value: {type(value).__name__}.")


def _normalize_utc(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")
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


def _content_hash(value: Any) -> str:
    payload = value.to_dict() if hasattr(value, "to_dict") else _json_value(value)
    if isinstance(payload, Mapping):
        payload = dict(payload)
        payload.pop("content_hash", None)
        payload.pop("source_row_hash", None)
        payload.pop("native_rows_hash", None)
    return canonical_sha256(payload)


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


def _coerce_dataset_cutoff(value: DatasetCutoff | Mapping[str, Any]) -> DatasetCutoff:
    if isinstance(value, DatasetCutoff):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("dataset_cutoff must be a DatasetCutoff or mapping.")
    return DatasetCutoff(**dict(value))


def _coerce_model(value: Any, model_type: type[Any], *, field_name: str) -> Any:
    if isinstance(value, model_type):
        return value
    if isinstance(value, Mapping):
        parser = getattr(model_type, "from_dict", None)
        if callable(parser):
            return parser(value)
    raise TypeError(f"{field_name} must contain {model_type.__name__} values.")


def _degree_index(value: str) -> int:
    try:
        return STANDARD_DEGREES.index(value)
    except ValueError:
        return -1


def next_standard_degree(value: str) -> str | None:
    index = _degree_index(value)
    if index < 0 or index >= len(STANDARD_DEGREES) - 2:
        return None
    return STANDARD_DEGREES[index + 1]


def expected_child_positions(family: str, positions: Sequence[str] = ()) -> tuple[str, ...] | None:
    if family in {"impulse", "diagonal"}:
        return ("1", "2", "3", "4", "5")
    if family in {"zigzag", "flat"}:
        return ("A", "B", "C")
    if family == "triangle":
        return ("A", "B", "C", "D", "E")
    if family == "combination":
        return ("W", "X", "Y", "X2", "Z") if {"X2", "Z"} & set(positions) else ("W", "X", "Y")
    return None


def admissible_child_families(
    parent_family: str,
    position: str,
    *,
    diagonal_type: DiagonalType | str | None = None,
) -> frozenset[str]:
    """Return the existing hard-rule family set for one child position."""

    family = str(parent_family).strip().lower()
    position = str(position).strip()
    if family == "diagonal" and position in {"1", "2", "3", "4", "5"}:
        if diagonal_type is None:
            leading, _ = diagonal_internal_family_policy(DiagonalType.LEADING)
            ending, _ = diagonal_internal_family_policy(DiagonalType.ENDING)
            return leading[int(position) - 1] | ending[int(position) - 1]
        kind = DiagonalType(diagonal_type)
        if position in {"1", "2", "3", "4", "5"}:
            policy, _ = diagonal_internal_family_policy(kind)
            return policy[int(position) - 1]
    if family in {"impulse", "diagonal"}:
        if position in {"1", "3", "5"}:
            return MOTIVE_FAMILIES
        if position in {"2", "4"}:
            return CORRECTIVE_FAMILIES
    elif family == "zigzag":
        if position in {"A", "C"}:
            return MOTIVE_FAMILIES
        if position == "B":
            return CORRECTIVE_FAMILIES
    elif family == "flat":
        if position in {"A", "B"}:
            return CORRECTIVE_FAMILIES
        if position == "C":
            return MOTIVE_FAMILIES
    elif family == "triangle" and position in {"A", "B", "C", "D", "E"}:
        return CORRECTIVE_FAMILIES
    elif family == "combination" and position in {"W", "X", "Y", "X2", "Z"}:
        return CORRECTIVE_FAMILIES
    return frozenset()


def family_children_are_admissible(
    parent_family: str,
    children: Mapping[str, "GeneratedChildWave"],
    *,
    diagonal_type: DiagonalType | str | None = None,
) -> bool:
    return all(
        child.declared_family
        in admissible_child_families(
            parent_family,
            position,
            diagonal_type=diagonal_type,
        )
        for position, child in children.items()
    )


@dataclass(frozen=True, slots=True)
class NativeCandle(_JsonContract):
    candle_id: str
    timestamp_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    completed: bool = True
    schema_version: str = NATIVE_CANDLE_SCHEMA_VERSION
    source_row_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candle_id", _require_text(self.candle_id, field_name="candle_id"))
        object.__setattr__(self, "timestamp_utc", _normalize_utc(self.timestamp_utc, field_name="timestamp_utc"))
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, _finite(getattr(self, name), field_name=name))
        if self.volume < 0:
            raise ValueError("volume must be non-negative.")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be at least open, close, and low.")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be at most open, close, and high.")
        if not isinstance(self.completed, bool) or not self.completed:
            raise ValueError("Native candles must be completed.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.source_row_hash:
            expected = native_candle_content_hash(self)
            if _require_hash(self.source_row_hash, field_name="source_row_hash") != expected:
                raise ValueError("source_row_hash does not match the native candle payload.")

    @classmethod
    def create(cls, **values: Any) -> "NativeCandle":
        if values.pop("source_row_hash", ""):
            raise ValueError("create() calculates source_row_hash; do not supply it.")
        result = cls(**values)
        return replace(result, source_row_hash=native_candle_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "NativeCandle":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.source_row_hash != native_candle_content_hash(result):
            raise ValueError("NativeCandle source_row_hash does not match its payload.")
        return result


def native_candle_content_hash(value: NativeCandle | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, NativeCandle) else dict(value)
    payload.pop("source_row_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class NativeOHLCVWindow(_JsonContract):
    window_id: str
    dataset_cutoff: DatasetCutoff | Mapping[str, Any]
    timeframe: str
    candles: tuple[NativeCandle, ...]
    coverage_role: WindowCoverageRole
    is_native: bool = True
    schema_version: str = NATIVE_WINDOW_SCHEMA_VERSION
    native_rows_hash: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_id", _require_text(self.window_id, field_name="window_id"))
        dataset = _coerce_dataset_cutoff(self.dataset_cutoff)
        object.__setattr__(self, "dataset_cutoff", dataset)
        canonical_timeframe = normalize_timeframe_name(
            _require_text(self.timeframe, field_name="timeframe")
        )
        object.__setattr__(self, "timeframe", canonical_timeframe)
        if canonical_timeframe != normalize_timeframe_name(dataset.timeframe):
            raise ValueError("Native window timeframe must match DatasetCutoff.timeframe.")
        object.__setattr__(
            self,
            "coverage_role",
            _enum(self.coverage_role, WindowCoverageRole, field_name="coverage_role"),
        )
        if not isinstance(self.is_native, bool):
            raise TypeError("is_native must be boolean.")
        if isinstance(self.candles, (str, bytes)) or not isinstance(self.candles, Sequence):
            raise TypeError("candles must be a sequence.")
        candles = tuple(
            _coerce_model(item, NativeCandle, field_name="candles") for item in self.candles
        )
        if not candles:
            raise ValueError("A native window requires at least one candle.")
        if any(not item.source_row_hash for item in candles):
            raise ValueError("Native window candles require source_row_hash values.")
        timestamps = tuple(_utc(item.timestamp_utc) for item in candles)
        if timestamps != tuple(sorted(timestamps)) or len(set(timestamps)) != len(timestamps):
            raise ValueError("Native window timestamps must be strictly increasing.")
        cutoff = _utc(dataset.cutoff_utc)
        if any(item > cutoff for item in timestamps):
            raise ValueError("Native window contains a candle after its DatasetCutoff.")
        if not dataset.completed_candles_only:
            raise ValueError("Native window requires completed_candles_only DatasetCutoff.")
        object.__setattr__(self, "candles", candles)
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        expected_rows = native_window_rows_hash(candles)
        if self.native_rows_hash:
            if _require_hash(self.native_rows_hash, field_name="native_rows_hash") != expected_rows:
                raise ValueError("native_rows_hash does not match candle rows.")
        else:
            object.__setattr__(self, "native_rows_hash", expected_rows)
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != native_window_content_hash(self):
                raise ValueError("NativeOHLCVWindow content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "NativeOHLCVWindow":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        values.setdefault("native_rows_hash", "")
        result = cls(**values)
        return replace(result, content_hash=native_window_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "NativeOHLCVWindow":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != native_window_content_hash(result):
            raise ValueError("NativeOHLCVWindow content_hash does not match its payload.")
        return result


def native_window_rows_hash(candles: Sequence[NativeCandle]) -> str:
    return canonical_sha256(
        [
            {
                "candle_id": item.candle_id,
                "source_row_hash": item.source_row_hash,
            }
            for item in candles
        ]
    )


def native_window_content_hash(value: NativeOHLCVWindow | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, NativeOHLCVWindow) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class TypedPivot(_JsonContract):
    pivot_id: str
    timestamp_utc: str
    price: float
    price_field: PivotPriceField
    pivot_type: PivotType
    timeframe: str
    source_window_hash: str
    source_bar_hash: str
    schema_version: str = TYPED_PIVOT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pivot_id", _require_text(self.pivot_id, field_name="pivot_id"))
        object.__setattr__(self, "timestamp_utc", _normalize_utc(self.timestamp_utc, field_name="timestamp_utc"))
        object.__setattr__(self, "price", _finite(self.price, field_name="price"))
        object.__setattr__(self, "price_field", _enum(self.price_field, PivotPriceField, field_name="price_field"))
        object.__setattr__(self, "pivot_type", _enum(self.pivot_type, PivotType, field_name="pivot_type"))
        object.__setattr__(self, "timeframe", normalize_timeframe_name(_require_text(self.timeframe, field_name="timeframe")))
        object.__setattr__(self, "source_window_hash", _require_hash(self.source_window_hash, field_name="source_window_hash"))
        object.__setattr__(self, "source_bar_hash", _require_hash(self.source_bar_hash, field_name="source_bar_hash"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != typed_pivot_content_hash(self):
                raise ValueError("TypedPivot content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "TypedPivot":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("pivot_id"):
            seed = {
                "timestamp_utc": values.get("timestamp_utc"),
                "price": values.get("price"),
                "price_field": _json_value(values.get("price_field")),
                "source_window_hash": values.get("source_window_hash"),
                "source_bar_hash": values.get("source_bar_hash"),
            }
            values["pivot_id"] = f"pivot_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=typed_pivot_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "TypedPivot":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != typed_pivot_content_hash(result):
            raise ValueError("TypedPivot content_hash does not match its payload.")
        return result


def typed_pivot_content_hash(value: TypedPivot | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, TypedPivot) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def build_native_pivot_catalog(
    window: NativeOHLCVWindow, *, pivot_window: int | None = None
) -> tuple[TypedPivot, ...]:
    """Build a deterministic, source-limited catalog; it does not verify waves."""
    if not window.is_native:
        raise ValueError("Pivot catalogs require native, unaggregated windows.")
    if pivot_window is None:
        pivot_window = pivot_windows_for(window.timeframe)[0]
    if not isinstance(pivot_window, int) or pivot_window < 1:
        raise ValueError("pivot_window must be a positive integer.")
    candles = window.candles
    pivots: list[TypedPivot] = []
    for index, candle in enumerate(candles):
        left = candles[max(0, index - pivot_window):index]
        right = candles[index + 1:index + pivot_window + 1]
        neighbors = (*left, *right)
        boundary = index in {0, len(candles) - 1}
        high_is_pivot = boundary or all(candle.high >= item.high for item in neighbors)
        low_is_pivot = boundary or all(candle.low <= item.low for item in neighbors)
        if high_is_pivot:
            pivots.append(
                TypedPivot.create(
                    pivot_id=f"{window.window_id}:high:{index}",
                    timestamp_utc=candle.timestamp_utc,
                    price=candle.high,
                    price_field=PivotPriceField.HIGH,
                    pivot_type=PivotType.HIGH,
                    timeframe=window.timeframe,
                    source_window_hash=window.native_rows_hash,
                    source_bar_hash=candle.source_row_hash,
                )
            )
        if low_is_pivot:
            pivots.append(
                TypedPivot.create(
                    pivot_id=f"{window.window_id}:low:{index}",
                    timestamp_utc=candle.timestamp_utc,
                    price=candle.low,
                    price_field=PivotPriceField.LOW,
                    pivot_type=PivotType.LOW,
                    timeframe=window.timeframe,
                    source_window_hash=window.native_rows_hash,
                    source_bar_hash=candle.source_row_hash,
                )
            )
    if not pivots:
        raise ValueError("No native pivots were available.")
    return tuple(sorted(pivots, key=lambda item: (item.timestamp_utc, item.pivot_id)))


@dataclass(frozen=True, slots=True)
class CandidateInvalidation(_JsonContract):
    invalidation_id: str
    threshold_price: float
    direction: InvalidationDirection
    evaluation_basis: InvalidationEvaluationBasis
    source_pivot_id: str
    schema_version: str = LOWER_TIMEFRAME_CANDIDATE_GENERATOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "invalidation_id", _require_text(self.invalidation_id, field_name="invalidation_id"))
        object.__setattr__(self, "threshold_price", _finite(self.threshold_price, field_name="threshold_price"))
        object.__setattr__(self, "direction", _enum(self.direction, InvalidationDirection, field_name="direction"))
        object.__setattr__(
            self,
            "evaluation_basis",
            _enum(self.evaluation_basis, InvalidationEvaluationBasis, field_name="evaluation_basis"),
        )
        object.__setattr__(self, "source_pivot_id", _require_text(self.source_pivot_id, field_name="source_pivot_id"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateInvalidation":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


def candidate_invalidation_openai_json_schema() -> dict[str, Any]:
    """Return the strict model-authorable JSON Schema for ``CandidateInvalidation``.

    The contract's ``schema_version`` has a local default and is intentionally
    not model-authorable.  The model must supply exactly the fields that do not
    have defaults; :meth:`CandidateInvalidation.from_dict` then assigns the
    immutable local contract version.  Returning a fresh plain dictionary
    prevents callers from mutating a shared schema object.
    """

    required = tuple(
        field.name
        for field in fields(CandidateInvalidation)
        if field.default is MISSING and field.default_factory is MISSING
    )
    expected_required = (
        "invalidation_id",
        "threshold_price",
        "direction",
        "evaluation_basis",
        "source_pivot_id",
    )
    if required != expected_required:
        raise RuntimeError(
            "CandidateInvalidation required-field contract changed; update its OpenAI JSON Schema explicitly."
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": {
            "invalidation_id": {
                "type": "string",
                "minLength": 1,
                "description": "Stable non-empty identifier for this candidate invalidation.",
            },
            "threshold_price": {
                "type": "number",
                "description": "Finite price threshold that invalidates the candidate when its evaluation basis is met.",
            },
            "direction": {
                "type": "string",
                "enum": [item.value for item in InvalidationDirection],
                "description": "Whether invalidation occurs below or above the threshold price.",
            },
            "evaluation_basis": {
                "type": "string",
                "enum": [item.value for item in InvalidationEvaluationBasis],
                "description": "Market-data condition used to evaluate the threshold: intrabar touch/breach or candle close.",
            },
            "source_pivot_id": {
                "type": "string",
                "minLength": 1,
                "description": "Pivot catalog ID supporting the invalidation; it must come from the supplied immutable catalog.",
            },
        },
        "examples": [
            {
                "invalidation_id": "invalidation_placeholder",
                "threshold_price": 0.0,
                "direction": InvalidationDirection.BELOW.value,
                "evaluation_basis": InvalidationEvaluationBasis.INTRABAR_TOUCH_OR_BREACH.value,
                "source_pivot_id": "pivot_placeholder",
            }
        ],
    }


def optional_candidate_invalidation_openai_json_schema() -> dict[str, Any]:
    """Return a nullable candidate guard schema for historical wave anatomy.

    Completed historical waves need valid pivots and Elliott structure, not a
    fabricated trade-style guard.  A typed guard may still be supplied where
    it is meaningful, but deterministic Elliott-rule calculations remain a
    separate contract.
    """

    return {
        "anyOf": [
            candidate_invalidation_openai_json_schema(),
            {"type": "null"},
        ]
    }


def diagonal_declaration_openai_json_schema(*, nullable: bool = True) -> dict[str, Any]:
    declaration = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "diagonal_type",
            "geometry",
            "wave5_termination",
            "rule_policy_version",
        ],
        "properties": {
            "diagonal_type": {
                "type": "string",
                "enum": ["leading", "ending"],
            },
            "geometry": {
                "type": "string",
                "enum": ["contracting", "expanding"],
            },
            "wave5_termination": {
                "type": "string",
                "enum": ["normal", "truncated", "throw_over"],
            },
            "rule_policy_version": {
                "type": "string",
                "enum": [ADAPTIVE_DIAGONAL_RULE_POLICY_VERSION],
            },
        },
    }
    if not nullable:
        return declaration
    return {"anyOf": [declaration, {"type": "null"}]}


@dataclass(frozen=True, slots=True)
class CoverageLimitation(_JsonContract):
    timeframe: str
    code: CoverageLimitationCode
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", normalize_timeframe_name(_require_text(self.timeframe, field_name="timeframe")))
        object.__setattr__(self, "code", _enum(self.code, CoverageLimitationCode, field_name="code"))
        object.__setattr__(self, "detail", _require_text(self.detail, field_name="detail"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CoverageLimitation":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CandidateProofScope(_JsonContract):
    target_timeframe: str
    verified_to_timeframe: str | None
    terminal_degree: str
    coverage_limitations: tuple[CoverageLimitation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_timeframe", normalize_timeframe_name(_require_text(self.target_timeframe, field_name="target_timeframe")))
        if self.verified_to_timeframe is not None:
            raise ValueError("Candidate generators cannot set verified_to_timeframe.")
        if self.terminal_degree not in STANDARD_DEGREES or self.terminal_degree == "Unassigned":
            raise ValueError("terminal_degree must be a standard assigned degree.")
        if isinstance(self.coverage_limitations, (str, bytes)) or not isinstance(self.coverage_limitations, Sequence):
            raise TypeError("coverage_limitations must be a sequence.")
        limitations = tuple(
            _coerce_model(item, CoverageLimitation, field_name="coverage_limitations")
            for item in self.coverage_limitations
        )
        keys = tuple((item.timeframe, item.code.value, item.detail) for item in limitations)
        if len(keys) != len(set(keys)):
            raise ValueError("coverage_limitations cannot contain duplicates.")
        object.__setattr__(self, "coverage_limitations", limitations)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateProofScope":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ParentCandidateSnapshot(_JsonContract):
    parent_candidate_id: str
    source_wave_id: str
    source_analysis_run_id: int | None
    source_analysis_run_hash: str | None
    source_degree_resolution_id: int | None
    source_degree_resolution_hash: str | None
    degree: str
    timeframe: str
    direction: str
    declared_family: str
    completion_state: str
    start_pivot: TypedPivot
    end_pivot: TypedPivot
    invalidation: CandidateInvalidation | None
    dataset_cutoff: DatasetCutoff | Mapping[str, Any]
    generation_depth: int
    end_is_date_only: bool = False
    diagonal_declaration: DiagonalDeclaration | None = None
    timeframe_use_purpose: TimeframeUsePurpose = (
        TimeframeUsePurpose.LEGACY_DEGREE_RESOLUTION
    )
    structural_parent_degree: str | None = None
    parent_evidence_timeframe: str | None = None
    schema_version: str = PARENT_CANDIDATE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("parent_candidate_id", "source_wave_id"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        provenance = (
            self.source_analysis_run_id,
            self.source_analysis_run_hash,
            self.source_degree_resolution_id,
            self.source_degree_resolution_hash,
        )
        if any(item is None for item in provenance) and not all(item is None for item in provenance):
            raise ValueError("Stored source provenance must be complete or wholly absent for pre-freeze candidates.")
        if all(item is not None for item in provenance):
            for name in ("source_analysis_run_id", "source_degree_resolution_id"):
                value = getattr(self, name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError(f"{name} must be a positive integer.")
            for name in ("source_analysis_run_hash", "source_degree_resolution_hash"):
                object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        if self.degree not in STANDARD_DEGREES or self.degree == "Unassigned":
            raise ValueError("Parent candidate requires an assigned standard degree.")
        canonical_timeframe = normalize_timeframe_name(_require_text(self.timeframe, field_name="timeframe"))
        object.__setattr__(self, "timeframe", canonical_timeframe)
        purpose = TimeframeUsePurpose(self.timeframe_use_purpose)
        object.__setattr__(self, "timeframe_use_purpose", purpose)
        if purpose in {
            TimeframeUsePurpose.SUBDIVISION_PROOF,
            TimeframeUsePurpose.TERMINAL_REFINEMENT,
        }:
            compatibility = timeframe_compatibility_for_purpose(
                self.degree,
                canonical_timeframe,
                purpose=purpose,
                parent_degree=self.structural_parent_degree,
                parent_timeframe=self.parent_evidence_timeframe,
            )
            if not compatibility.compatible:
                raise ValueError(
                    "Parent proof timeframe is incompatible with its structural "
                    f"lineage: {','.join(compatibility.reason_codes)}"
                )
            object.__setattr__(
                self,
                "structural_parent_degree",
                _require_text(
                    self.structural_parent_degree,
                    field_name="structural_parent_degree",
                ),
            )
            object.__setattr__(
                self,
                "parent_evidence_timeframe",
                normalize_timeframe_name(
                    _require_text(
                        self.parent_evidence_timeframe,
                        field_name="parent_evidence_timeframe",
                    )
                ),
            )
        else:
            if (
                self.structural_parent_degree is not None
                or self.parent_evidence_timeframe is not None
            ):
                raise ValueError(
                    "Legacy/root timeframe use cannot carry subdivision parent lineage."
                )
            if not timeframe_is_compatible_with_degree(self.degree, canonical_timeframe):
                raise ValueError("Parent timeframe is incompatible with its degree.")
        direction = _require_text(self.direction, field_name="direction")
        if direction not in {"up", "down"}:
            raise ValueError("Parent direction must be up or down.")
        object.__setattr__(self, "direction", direction)
        family = _require_text(self.declared_family, field_name="declared_family").lower()
        if family not in _ALLOWED_FAMILIES:
            raise ValueError("Parent declared_family is unsupported.")
        object.__setattr__(self, "declared_family", family)
        completion = _require_text(self.completion_state, field_name="completion_state")
        if completion not in {"completed", "active", "projected"}:
            raise ValueError("completion_state is invalid.")
        object.__setattr__(self, "completion_state", completion)
        object.__setattr__(self, "start_pivot", _coerce_model(self.start_pivot, TypedPivot, field_name="start_pivot"))
        object.__setattr__(self, "end_pivot", _coerce_model(self.end_pivot, TypedPivot, field_name="end_pivot"))
        if _utc(self.end_pivot.timestamp_utc) <= _utc(self.start_pivot.timestamp_utc):
            raise ValueError("Parent end pivot must be after its start pivot.")
        if self.invalidation is not None:
            object.__setattr__(self, "invalidation", _coerce_model(self.invalidation, CandidateInvalidation, field_name="invalidation"))
        if self.diagonal_declaration is not None:
            object.__setattr__(
                self,
                "diagonal_declaration",
                _coerce_model(
                    self.diagonal_declaration,
                    DiagonalDeclaration,
                    field_name="diagonal_declaration",
                ),
            )
        if family == "diagonal" and self.diagonal_declaration is None:
            # Legacy snapshots remain readable and deterministically unproven;
            # new strict packets require an explicit diagonal declaration.
            pass
        elif family != "diagonal" and self.diagonal_declaration is not None:
            raise ValueError("Only diagonal parents may carry a diagonal declaration.")
        object.__setattr__(self, "dataset_cutoff", _coerce_dataset_cutoff(self.dataset_cutoff))
        if not isinstance(self.generation_depth, int) or isinstance(self.generation_depth, bool) or self.generation_depth < 0:
            raise ValueError("generation_depth must be a non-negative integer.")
        if not isinstance(self.end_is_date_only, bool):
            raise TypeError("end_is_date_only must be boolean.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != parent_candidate_content_hash(self):
                raise ValueError("ParentCandidateSnapshot content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ParentCandidateSnapshot":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=parent_candidate_content_hash(result))

    def to_dict(self) -> dict[str, Any]:
        payload = _JsonContract.to_dict(self)
        if self.diagonal_declaration is None:
            payload.pop("diagonal_declaration", None)
        if (
            self.timeframe_use_purpose
            is TimeframeUsePurpose.LEGACY_DEGREE_RESOLUTION
            and self.structural_parent_degree is None
            and self.parent_evidence_timeframe is None
        ):
            # Preserve the exact payload/hash shape used by legacy snapshots.
            payload.pop("timeframe_use_purpose", None)
            payload.pop("structural_parent_degree", None)
            payload.pop("parent_evidence_timeframe", None)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ParentCandidateSnapshot":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != parent_candidate_content_hash(result):
            raise ValueError("ParentCandidateSnapshot content_hash does not match its payload.")
        return result


def parent_candidate_content_hash(value: ParentCandidateSnapshot | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ParentCandidateSnapshot) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class NativeOHLCVScope(_JsonContract):
    required_window: NativeOHLCVWindow
    supporting_windows: tuple[NativeOHLCVWindow, ...] = ()
    schema_version: str = LOWER_TIMEFRAME_CANDIDATE_GENERATOR_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_window", _coerce_model(self.required_window, NativeOHLCVWindow, field_name="required_window"))
        if self.required_window.coverage_role is not WindowCoverageRole.REQUIRED_GENERATION:
            raise ValueError("required_window must have required_generation role.")
        if isinstance(self.supporting_windows, (str, bytes)) or not isinstance(self.supporting_windows, Sequence):
            raise TypeError("supporting_windows must be a sequence.")
        supporting = tuple(
            _coerce_model(item, NativeOHLCVWindow, field_name="supporting_windows")
            for item in self.supporting_windows
        )
        if any(item.coverage_role is WindowCoverageRole.REQUIRED_GENERATION for item in supporting):
            raise ValueError("Supporting windows cannot use required_generation role.")
        if len({item.window_id for item in supporting}) != len(supporting):
            raise ValueError("supporting_windows cannot contain duplicate IDs.")
        object.__setattr__(self, "supporting_windows", supporting)
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != native_scope_content_hash(self):
                raise ValueError("NativeOHLCVScope content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "NativeOHLCVScope":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=native_scope_content_hash(result))


def native_scope_content_hash(value: NativeOHLCVScope | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, NativeOHLCVScope) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CandidateGenerationPolicy(_JsonContract):
    policy_id: str
    maximum_generation_depth: int
    terminal_degree: str
    timeframe_by_child_degree: Mapping[str, str]
    supporting_timeframes: tuple[str, ...]
    policy_not_required_timeframes: tuple[str, ...]
    schema_version: str = LOWER_TIMEFRAME_CANDIDATE_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _require_text(self.policy_id, field_name="policy_id"))
        if (
            not isinstance(self.maximum_generation_depth, int)
            or isinstance(self.maximum_generation_depth, bool)
            or self.maximum_generation_depth < 1
            or self.maximum_generation_depth > 8
        ):
            raise ValueError("maximum_generation_depth must be between 1 and 8.")
        if self.terminal_degree not in STANDARD_DEGREES or self.terminal_degree == "Unassigned":
            raise ValueError("terminal_degree must be an assigned standard degree.")
        if not isinstance(self.timeframe_by_child_degree, Mapping):
            raise TypeError("timeframe_by_child_degree must be a mapping.")
        mapping: dict[str, str] = {}
        for degree, timeframe in self.timeframe_by_child_degree.items():
            degree_text = _require_text(degree, field_name="timeframe_by_child_degree.degree")
            timeframe_text = normalize_timeframe_name(_require_text(timeframe, field_name="timeframe_by_child_degree.timeframe"))
            if degree_text not in STANDARD_DEGREES or degree_text == "Unassigned":
                raise ValueError("timeframe_by_child_degree contains an invalid degree.")
            mapping[degree_text] = timeframe_text
        if not mapping:
            raise ValueError("timeframe_by_child_degree cannot be empty.")
        object.__setattr__(self, "timeframe_by_child_degree", MappingProxyType(dict(sorted(mapping.items()))))
        object.__setattr__(self, "supporting_timeframes", tuple(sorted({normalize_timeframe_name(item) for item in self.supporting_timeframes})))
        object.__setattr__(self, "policy_not_required_timeframes", tuple(sorted({normalize_timeframe_name(item) for item in self.policy_not_required_timeframes})))
        if set(self.supporting_timeframes) & set(self.policy_not_required_timeframes):
            raise ValueError("Supporting and policy-not-required timeframes must not overlap.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != candidate_generation_policy_content_hash(self):
                raise ValueError("CandidateGenerationPolicy content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CandidateGenerationPolicy":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=candidate_generation_policy_content_hash(result))


def candidate_generation_policy_content_hash(value: CandidateGenerationPolicy | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CandidateGenerationPolicy) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def googl_intermediate_to_minute_policy() -> CandidateGenerationPolicy:
    return CandidateGenerationPolicy.create(
        policy_id="googl-lower-timeframe-candidate-policy-1.0.0",
        maximum_generation_depth=2,
        terminal_degree="Minute",
        timeframe_by_child_degree={"Minor": "daily", "Minute": "4h"},
        supporting_timeframes=("1h",),
        policy_not_required_timeframes=("15m",),
    )


@dataclass(frozen=True, slots=True)
class ReconstructionExcludedGraph(_JsonContract):
    """Deterministic facts about one rejected graph, without model prose."""

    graph_signature: str
    pivot_sequence: tuple[str, ...]
    declared_family: str
    child_family_sequence: tuple[str, ...]
    deterministic_failure_ids: tuple[str, ...]
    hard_rule_calculations: tuple[Mapping[str, Any], ...]
    failed_boundary_checks: tuple[Mapping[str, Any], ...]
    rejected_diagonal: Mapping[str, Any] | None
    verifier_result_hash: str
    schema_version: str = RECONSTRUCTION_EXCLUSION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "graph_signature",
            _require_hash(self.graph_signature, field_name="graph_signature"),
        )
        pivots = _string_tuple(self.pivot_sequence, field_name="pivot_sequence")
        if len(pivots) < 2:
            raise ValueError("pivot_sequence must contain at least two pivots.")
        object.__setattr__(self, "pivot_sequence", pivots)
        family = _require_text(self.declared_family, field_name="declared_family").lower()
        if family not in _ALLOWED_FAMILIES:
            raise ValueError("declared_family is unsupported.")
        object.__setattr__(self, "declared_family", family)
        child_families = tuple(
            _require_text(item, field_name="child_family_sequence").lower()
            for item in self.child_family_sequence
        )
        if any(item not in _ALLOWED_FAMILIES for item in child_families):
            raise ValueError("child_family_sequence contains an unsupported family.")
        object.__setattr__(self, "child_family_sequence", child_families)
        object.__setattr__(
            self,
            "deterministic_failure_ids",
            _string_tuple(
                self.deterministic_failure_ids,
                field_name="deterministic_failure_ids",
            ),
        )
        for name in ("hard_rule_calculations", "failed_boundary_checks"):
            values = getattr(self, name)
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise TypeError(f"{name} must be a sequence of mappings.")
            if any(not isinstance(item, Mapping) for item in values):
                raise TypeError(f"{name} must contain mappings.")
            object.__setattr__(self, name, tuple(_freeze_json(item) for item in values))
        if self.rejected_diagonal is not None:
            if not isinstance(self.rejected_diagonal, Mapping):
                raise TypeError("rejected_diagonal must be a mapping or null.")
            object.__setattr__(
                self,
                "rejected_diagonal",
                _freeze_json(self.rejected_diagonal),
            )
        object.__setattr__(
            self,
            "verifier_result_hash",
            _require_hash(self.verifier_result_hash, field_name="verifier_result_hash"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and (
            _require_hash(self.content_hash, field_name="content_hash")
            != reconstruction_excluded_graph_content_hash(self)
        ):
            raise ValueError("ReconstructionExcludedGraph content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ReconstructionExcludedGraph":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(
            result,
            content_hash=reconstruction_excluded_graph_content_hash(result),
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ReconstructionExcludedGraph":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        raw["pivot_sequence"] = tuple(raw.get("pivot_sequence", ()))
        raw["child_family_sequence"] = tuple(raw.get("child_family_sequence", ()))
        raw["deterministic_failure_ids"] = tuple(
            raw.get("deterministic_failure_ids", ())
        )
        raw["hard_rule_calculations"] = tuple(raw.get("hard_rule_calculations", ()))
        raw["failed_boundary_checks"] = tuple(raw.get("failed_boundary_checks", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != reconstruction_excluded_graph_content_hash(result):
            raise ValueError("ReconstructionExcludedGraph content_hash does not match its payload.")
        return result


def reconstruction_excluded_graph_content_hash(
    value: ReconstructionExcludedGraph | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ReconstructionExcludedGraph) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ChildReconstructionContext(_JsonContract):
    attempt_number: int
    excluded_graphs: tuple[ReconstructionExcludedGraph, ...]
    admissible_parent_families: tuple[str, ...]
    allowed_changes: tuple[str, ...]
    planner_may_request_another_timeframe: bool
    policy_version: str = CHILD_RECONSTRUCTION_POLICY_VERSION
    schema_version: str = CHILD_RECONSTRUCTION_CONTEXT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_number, int) or not 2 <= self.attempt_number <= 3:
            raise ValueError("attempt_number must be reconstruction attempt 2 or 3.")
        if isinstance(self.excluded_graphs, (str, bytes)) or not isinstance(
            self.excluded_graphs, Sequence
        ):
            raise TypeError("excluded_graphs must be a sequence.")
        exclusions = tuple(
            _coerce_model(item, ReconstructionExcludedGraph, field_name="excluded_graphs")
            for item in self.excluded_graphs
        )
        if not exclusions:
            raise ValueError("A reconstruction context requires an excluded graph.")
        signatures = tuple(item.graph_signature for item in exclusions)
        if len(signatures) != len(set(signatures)):
            raise ValueError("excluded_graphs cannot repeat a semantic signature.")
        object.__setattr__(self, "excluded_graphs", exclusions)
        families = tuple(
            dict.fromkeys(
                _require_text(item, field_name="admissible_parent_families").lower()
                for item in self.admissible_parent_families
            )
        )
        if not families or any(item not in _ALLOWED_FAMILIES for item in families):
            raise ValueError("admissible_parent_families is invalid.")
        object.__setattr__(self, "admissible_parent_families", families)
        policy_version = _require_text(
            self.policy_version, field_name="policy_version"
        )
        object.__setattr__(self, "policy_version", policy_version)
        allowed_policy_changes = (
            _LEGACY_RECONSTRUCTION_ALLOWED_CHANGES
            if policy_version == LEGACY_CHILD_RECONSTRUCTION_POLICY_VERSION
            else RECONSTRUCTION_ALLOWED_CHANGES
        )
        changes = _string_tuple(self.allowed_changes, field_name="allowed_changes")
        if not changes or not set(changes).issubset(allowed_policy_changes):
            raise ValueError("allowed_changes contains an unsupported reconstruction change.")
        object.__setattr__(self, "allowed_changes", changes)
        if policy_version == CHILD_RECONSTRUCTION_POLICY_VERSION:
            if len(families) != 1:
                raise ValueError(
                    "Same-parent reconstruction requires exactly one frozen parent family."
                )
            if any(item.declared_family != families[0] for item in exclusions):
                raise ValueError(
                    "Excluded graphs must belong to the same frozen parent family."
                )
        if not isinstance(self.planner_may_request_another_timeframe, bool):
            raise TypeError("planner_may_request_another_timeframe must be boolean.")
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and (
            _require_hash(self.content_hash, field_name="content_hash")
            != child_reconstruction_context_content_hash(self)
        ):
            raise ValueError("ChildReconstructionContext content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ChildReconstructionContext":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(
            result,
            content_hash=child_reconstruction_context_content_hash(result),
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ChildReconstructionContext":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        raw["excluded_graphs"] = tuple(
            ReconstructionExcludedGraph.from_dict(item)
            for item in raw.get("excluded_graphs", ())
        )
        raw["admissible_parent_families"] = tuple(
            raw.get("admissible_parent_families", ())
        )
        raw["allowed_changes"] = tuple(raw.get("allowed_changes", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != child_reconstruction_context_content_hash(result):
            raise ValueError("ChildReconstructionContext content_hash does not match its payload.")
        return result


def child_reconstruction_context_content_hash(
    value: ChildReconstructionContext | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ChildReconstructionContext) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class LowerTimeframeCandidateGenerationRequest(_JsonContract):
    request_id: str
    parent_candidate: ParentCandidateSnapshot
    analysis_cutoff_utc: str
    target_child_degree: str
    target_timeframe: str
    native_ohlcv_scope: NativeOHLCVScope
    pivot_catalog: tuple[TypedPivot, ...]
    proof_scope: CandidateProofScope
    generation_policy: CandidateGenerationPolicy
    shadow_mode: bool
    created_at_utc: str
    reconstruction_context: ChildReconstructionContext | None = None
    schema_version: str = LOWER_TIMEFRAME_CANDIDATE_GENERATOR_SCHEMA_VERSION
    calculation_version: str = LOWER_TIMEFRAME_CANDIDATE_CALCULATION_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        object.__setattr__(self, "parent_candidate", _coerce_model(self.parent_candidate, ParentCandidateSnapshot, field_name="parent_candidate"))
        object.__setattr__(self, "analysis_cutoff_utc", _normalize_utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        if _utc(self.parent_candidate.dataset_cutoff.cutoff_utc) > _utc(self.analysis_cutoff_utc):
            raise ValueError("Parent DatasetCutoff cannot exceed analysis_cutoff_utc.")
        if self.target_child_degree not in STANDARD_DEGREES or self.target_child_degree == "Unassigned":
            raise ValueError("target_child_degree must be an assigned standard degree.")
        expected_child_degree = next_standard_degree(self.parent_candidate.degree)
        if self.target_child_degree == self.parent_candidate.degree:
            raise ValueError("same_degree_parent_child_forbidden")
        if self.target_child_degree != expected_child_degree:
            raise ValueError("target_child_degree must descend exactly one Elliott degree.")
        object.__setattr__(self, "target_timeframe", normalize_timeframe_name(_require_text(self.target_timeframe, field_name="target_timeframe")))
        object.__setattr__(self, "native_ohlcv_scope", _coerce_model(self.native_ohlcv_scope, NativeOHLCVScope, field_name="native_ohlcv_scope"))
        if isinstance(self.pivot_catalog, (str, bytes)) or not isinstance(self.pivot_catalog, Sequence):
            raise TypeError("pivot_catalog must be a sequence.")
        pivots = tuple(_coerce_model(item, TypedPivot, field_name="pivot_catalog") for item in self.pivot_catalog)
        if not pivots:
            raise ValueError("pivot_catalog cannot be empty.")
        if len({item.pivot_id for item in pivots}) != len(pivots):
            raise ValueError("pivot_catalog cannot contain duplicate IDs.")
        object.__setattr__(self, "pivot_catalog", pivots)
        object.__setattr__(self, "proof_scope", _coerce_model(self.proof_scope, CandidateProofScope, field_name="proof_scope"))
        object.__setattr__(self, "generation_policy", _coerce_model(self.generation_policy, CandidateGenerationPolicy, field_name="generation_policy"))
        if not isinstance(self.shadow_mode, bool):
            raise TypeError("shadow_mode must be boolean.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        if self.reconstruction_context is not None:
            object.__setattr__(
                self,
                "reconstruction_context",
                _coerce_model(
                    self.reconstruction_context,
                    ChildReconstructionContext,
                    field_name="reconstruction_context",
                ),
            )
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        object.__setattr__(self, "calculation_version", _require_text(self.calculation_version, field_name="calculation_version"))
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != candidate_generation_request_content_hash(self):
                raise ValueError("LowerTimeframeCandidateGenerationRequest content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "LowerTimeframeCandidateGenerationRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("request_id"):
            seed = {
                "parent_candidate": _json_value(values.get("parent_candidate")),
                "analysis_cutoff_utc": values.get("analysis_cutoff_utc"),
                "target_child_degree": values.get("target_child_degree"),
                "target_timeframe": values.get("target_timeframe"),
                "reconstruction_context": _json_value(
                    values.get("reconstruction_context")
                ),
                "created_at_utc": values.get("created_at_utc"),
            }
            values["request_id"] = f"lower_tf_request_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=candidate_generation_request_content_hash(result))

    def to_dict(self) -> dict[str, Any]:
        payload = _JsonContract.to_dict(self)
        if self.reconstruction_context is None:
            payload.pop("reconstruction_context", None)
        return payload


def candidate_generation_request_content_hash(
    value: LowerTimeframeCandidateGenerationRequest | Mapping[str, Any]
) -> str:
    payload = value.to_dict() if isinstance(value, LowerTimeframeCandidateGenerationRequest) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class GeneratedChildWave(_JsonContract):
    child_id: str
    parent_candidate_id: str
    degree: str
    timeframe: str
    sequence_position: str
    direction: str
    declared_family: str
    start_pivot: TypedPivot
    end_pivot: TypedPivot
    invalidation: CandidateInvalidation | None
    completion_state: str = "completed"
    candidate_state: str = "candidate"
    diagonal_declaration: DiagonalDeclaration | None = None
    schema_version: str = CHILD_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("child_id", "parent_candidate_id", "sequence_position"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        if self.degree not in STANDARD_DEGREES or self.degree == "Unassigned":
            raise ValueError("Child degree must be an assigned standard degree.")
        object.__setattr__(self, "timeframe", normalize_timeframe_name(_require_text(self.timeframe, field_name="timeframe")))
        direction = _require_text(self.direction, field_name="direction")
        if direction not in {"up", "down"}:
            raise ValueError("Child direction must be up or down.")
        object.__setattr__(self, "direction", direction)
        family = _require_text(self.declared_family, field_name="declared_family").lower()
        if family not in _ALLOWED_FAMILIES:
            raise ValueError("Child declared_family is unsupported.")
        object.__setattr__(self, "declared_family", family)
        object.__setattr__(self, "start_pivot", _coerce_model(self.start_pivot, TypedPivot, field_name="start_pivot"))
        object.__setattr__(self, "end_pivot", _coerce_model(self.end_pivot, TypedPivot, field_name="end_pivot"))
        if _utc(self.end_pivot.timestamp_utc) <= _utc(self.start_pivot.timestamp_utc):
            raise ValueError("Child end pivot must be after its start pivot.")
        if self.invalidation is not None:
            object.__setattr__(self, "invalidation", _coerce_model(self.invalidation, CandidateInvalidation, field_name="invalidation"))
        if self.diagonal_declaration is not None:
            object.__setattr__(
                self,
                "diagonal_declaration",
                _coerce_model(
                    self.diagonal_declaration,
                    DiagonalDeclaration,
                    field_name="diagonal_declaration",
                ),
            )
        if family != "diagonal" and self.diagonal_declaration is not None:
            raise ValueError("Only diagonal child waves may carry a diagonal declaration.")
        completion = _require_text(self.completion_state, field_name="completion_state").lower()
        if completion not in {"completed", "active", "projected"}:
            raise ValueError("completion_state is invalid.")
        object.__setattr__(self, "completion_state", completion)
        if self.candidate_state != "candidate":
            raise ValueError("Generated child waves must remain candidate-only.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GeneratedChildWave":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        payload = _JsonContract.to_dict(self)
        if self.diagonal_declaration is None:
            payload.pop("diagonal_declaration", None)
        return payload


@dataclass(frozen=True, slots=True)
class CandidateChildGraph(_JsonContract):
    graph_id: str
    role: CandidateGraphRole
    request_id: str
    request_content_hash: str
    parent_candidate_id: str
    target_child_degree: str
    target_timeframe: str
    declared_family: str
    children: tuple[GeneratedChildWave, ...]
    proof_scope: CandidateProofScope
    provider: str
    model: str | None
    generated_at_utc: str
    policy_version: str
    diagonal_declaration: DiagonalDeclaration | None = None
    schema_version: str = CHILD_GRAPH_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", _require_text(self.graph_id, field_name="graph_id"))
        object.__setattr__(self, "role", _enum(self.role, CandidateGraphRole, field_name="role"))
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        object.__setattr__(self, "request_content_hash", _require_hash(self.request_content_hash, field_name="request_content_hash"))
        object.__setattr__(self, "parent_candidate_id", _require_text(self.parent_candidate_id, field_name="parent_candidate_id"))
        if self.target_child_degree not in STANDARD_DEGREES or self.target_child_degree == "Unassigned":
            raise ValueError("target_child_degree must be assigned.")
        object.__setattr__(self, "target_timeframe", normalize_timeframe_name(_require_text(self.target_timeframe, field_name="target_timeframe")))
        family = _require_text(self.declared_family, field_name="declared_family").lower()
        if family not in _ALLOWED_FAMILIES:
            raise ValueError("Graph declared_family is unsupported.")
        object.__setattr__(self, "declared_family", family)
        if isinstance(self.children, (str, bytes)) or not isinstance(self.children, Sequence):
            raise TypeError("children must be a sequence.")
        children = tuple(_coerce_model(item, GeneratedChildWave, field_name="children") for item in self.children)
        expected = expected_child_positions(family, tuple(item.sequence_position for item in children))
        by_position = {item.sequence_position: item for item in children}
        if expected is None or len(by_position) != len(children) or tuple(by_position) != expected:
            raise ValueError("Child graph positions do not match its declared family.")
        if any(item.parent_candidate_id != self.parent_candidate_id for item in children):
            raise ValueError("Every child must reference the graph parent candidate.")
        if any(item.degree != self.target_child_degree or item.timeframe != self.target_timeframe for item in children):
            raise ValueError("Every child must use the graph target degree and timeframe.")
        if self.diagonal_declaration is not None:
            object.__setattr__(
                self,
                "diagonal_declaration",
                _coerce_model(
                    self.diagonal_declaration,
                    DiagonalDeclaration,
                    field_name="diagonal_declaration",
                ),
            )
        if family != "diagonal" and self.diagonal_declaration is not None:
            raise ValueError("Only diagonal graphs may carry a diagonal declaration.")
        diagonal_type = (
            self.diagonal_declaration.diagonal_type
            if self.diagonal_declaration is not None
            else None
        )
        if not family_children_are_admissible(
            family,
            by_position,
            diagonal_type=diagonal_type,
        ):
            raise ValueError("Child declared families are incompatible with the parent family.")
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "proof_scope", _coerce_model(self.proof_scope, CandidateProofScope, field_name="proof_scope"))
        if self.proof_scope.target_timeframe != self.target_timeframe:
            raise ValueError("Proof scope target timeframe must match graph target timeframe.")
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _require_text(self.model, field_name="model"))
        object.__setattr__(self, "generated_at_utc", _normalize_utc(self.generated_at_utc, field_name="generated_at_utc"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != candidate_child_graph_content_hash(self):
                raise ValueError("CandidateChildGraph content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CandidateChildGraph":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("graph_id"):
            seed = {
                "role": _json_value(values.get("role")),
                "request_content_hash": values.get("request_content_hash"),
                "declared_family": values.get("declared_family"),
                "children": _json_value(values.get("children")),
            }
            values["graph_id"] = f"child_graph_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=candidate_child_graph_content_hash(result))

    def to_dict(self) -> dict[str, Any]:
        payload = _JsonContract.to_dict(self)
        if self.diagonal_declaration is None:
            payload.pop("diagonal_declaration", None)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "CandidateChildGraph":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != candidate_child_graph_content_hash(result):
            raise ValueError("CandidateChildGraph content_hash does not match its payload.")
        return result


def candidate_child_graph_content_hash(value: CandidateChildGraph | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CandidateChildGraph) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CandidateGenerationPair(_JsonContract):
    pair_id: str
    request_id: str
    request_content_hash: str
    primary_graph: CandidateChildGraph
    alternative_graph: CandidateChildGraph
    created_at_utc: str
    schema_version: str = GENERATION_PAIR_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _require_text(self.pair_id, field_name="pair_id"))
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        object.__setattr__(self, "request_content_hash", _require_hash(self.request_content_hash, field_name="request_content_hash"))
        object.__setattr__(self, "primary_graph", _coerce_model(self.primary_graph, CandidateChildGraph, field_name="primary_graph"))
        object.__setattr__(self, "alternative_graph", _coerce_model(self.alternative_graph, CandidateChildGraph, field_name="alternative_graph"))
        if self.primary_graph.role is not CandidateGraphRole.PRIMARY:
            raise ValueError("CandidateGenerationPair primary graph must have primary role.")
        if self.alternative_graph.role is not CandidateGraphRole.ALTERNATIVE:
            raise ValueError("CandidateGenerationPair alternative graph must have alternative role.")
        if self.primary_graph.request_id != self.request_id or self.alternative_graph.request_id != self.request_id:
            raise ValueError("Generation pair graphs must belong to the pair request.")
        if self.primary_graph.request_content_hash != self.request_content_hash or self.alternative_graph.request_content_hash != self.request_content_hash:
            raise ValueError("Generation pair graph request hashes must match.")
        if graph_signature(self.primary_graph) == graph_signature(self.alternative_graph):
            raise ValueError("Primary and Alternative graph signatures must differ.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            if _require_hash(self.content_hash, field_name="content_hash") != candidate_generation_pair_content_hash(self):
                raise ValueError("CandidateGenerationPair content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CandidateGenerationPair":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("pair_id"):
            seed = {
                "request_content_hash": values.get("request_content_hash"),
                "primary_graph": _json_value(values.get("primary_graph")),
                "alternative_graph": _json_value(values.get("alternative_graph")),
            }
            values["pair_id"] = f"child_pair_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=candidate_generation_pair_content_hash(result))


def candidate_generation_pair_content_hash(value: CandidateGenerationPair | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CandidateGenerationPair) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CandidateGenerationFailure(_JsonContract):
    role: CandidateGraphRole
    code: CandidateGenerationFailureCode
    request_id: str
    request_content_hash: str
    details: tuple[str, ...]
    reconstruction_exclusion: ReconstructionExcludedGraph | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _enum(self.role, CandidateGraphRole, field_name="role"))
        object.__setattr__(self, "code", _enum(self.code, CandidateGenerationFailureCode, field_name="code"))
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        object.__setattr__(self, "request_content_hash", _require_hash(self.request_content_hash, field_name="request_content_hash"))
        object.__setattr__(self, "details", _string_tuple(self.details, field_name="details"))
        if self.reconstruction_exclusion is not None:
            object.__setattr__(
                self,
                "reconstruction_exclusion",
                _coerce_model(
                    self.reconstruction_exclusion,
                    ReconstructionExcludedGraph,
                    field_name="reconstruction_exclusion",
                ),
            )


class CandidateGenerationError(RuntimeError):
    def __init__(self, failure: CandidateGenerationFailure) -> None:
        super().__init__(f"{failure.role.value}: {failure.code.value}")
        self.failure = failure


class GraphGenerationProvider(Protocol):
    """Strict provider boundary for a future shadow-only graph generator."""

    name: str
    model: str | None

    def generate_child_graph(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


GRAPH_GENERATION_OUTPUT_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "graph_id",
            "declared_family",
            "diagonal_declaration",
            "proof_scope",
            "children",
        ],
        "properties": {
            "graph_id": {"type": "string", "minLength": 1},
            "declared_family": {
                "type": "string",
                "enum": sorted(_ALLOWED_FAMILIES),
            },
            "diagonal_declaration": diagonal_declaration_openai_json_schema(),
            "proof_scope": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "terminal_degree",
                    "verified_to_timeframe",
                    "coverage_limitations",
                ],
                "properties": {
                    "terminal_degree": {
                        "type": "string",
                        "enum": [item for item in STANDARD_DEGREES if item != "Unassigned"],
                    },
                    "verified_to_timeframe": {
                        "type": "null",
                        "description": "Must remain null; only the deterministic verifier may establish proof status.",
                    },
                    "coverage_limitations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["timeframe", "code", "detail"],
                            "properties": {
                                "timeframe": {"type": "string", "minLength": 1},
                                "code": {
                                    "type": "string",
                                    "enum": [item.value for item in CoverageLimitationCode],
                                },
                                "detail": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
            "children": {
                "type": "array",
                "minItems": 3,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "child_id",
                        "sequence_position",
                        "direction",
                        "declared_family",
                        "start_pivot_id",
                        "end_pivot_id",
                        "invalidation",
                        "diagonal_declaration",
                        "completion_state",
                    ],
                    "properties": {
                        "child_id": {"type": "string", "minLength": 1},
                        "sequence_position": {"type": "string", "minLength": 1},
                        "direction": {"type": "string", "enum": ["up", "down"]},
                        "declared_family": {
                            "type": "string",
                            "enum": sorted(_ALLOWED_FAMILIES),
                        },
                        "start_pivot_id": {"type": "string", "minLength": 1},
                        "end_pivot_id": {"type": "string", "minLength": 1},
                        "invalidation": optional_candidate_invalidation_openai_json_schema(),
                        "diagonal_declaration": diagonal_declaration_openai_json_schema(),
                        "completion_state": {
                            "type": "string",
                            "enum": ["completed", "active", "projected"],
                        },
                    },
                },
            },
        },
    }
)


def graph_generation_output_schema_for_packet(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind strict child output to the immutable parent-family contract."""

    parent = packet.get("parent_candidate") if isinstance(packet, Mapping) else None
    if not isinstance(parent, Mapping):
        raise ValueError("Child candidate packet requires a parent_candidate object.")
    parent_family = str(parent.get("declared_family") or "").strip().lower()
    if parent_family not in _ALLOWED_FAMILIES:
        raise ValueError("Child candidate packet parent family is unsupported.")

    if parent_family == "combination":
        positions = ("W", "X", "Y", "X2", "Z")
        minimum_children = 3
        maximum_children = 5
    else:
        expected = expected_child_positions(parent_family)
        if expected is None:
            raise ValueError("Child candidate packet parent family is unsupported.")
        positions = expected
        minimum_children = maximum_children = len(expected)

    schema = deepcopy(_json_value(GRAPH_GENERATION_OUTPUT_SCHEMA))
    properties = schema["properties"]
    properties["declared_family"] = {
        "type": "string",
        "enum": [parent_family],
    }
    parent_declaration = parent.get("diagonal_declaration")
    if parent_family == "diagonal" and isinstance(parent_declaration, Mapping):
        properties["diagonal_declaration"] = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "diagonal_type",
                "geometry",
                "wave5_termination",
                "rule_policy_version",
            ],
            "properties": {
                "diagonal_type": {
                    "type": "string",
                    "enum": [str(parent_declaration.get("diagonal_type"))],
                },
                "geometry": {
                    "type": "string",
                    "enum": [str(parent_declaration.get("geometry"))],
                },
                "wave5_termination": {
                    "type": "string",
                    "enum": [str(parent_declaration.get("wave5_termination"))],
                },
                "rule_policy_version": {
                    "type": "string",
                    "enum": [str(parent_declaration.get("rule_policy_version"))],
                },
            },
        }
        diagonal_type: DiagonalType | None = DiagonalType(
            str(parent_declaration.get("diagonal_type"))
        )
    elif parent_family == "diagonal":
        properties["diagonal_declaration"] = diagonal_declaration_openai_json_schema(
            nullable=False
        )
        diagonal_type = None
    else:
        properties["diagonal_declaration"] = {"type": "null"}
        diagonal_type = None
    children_schema = properties["children"]
    children_schema["minItems"] = minimum_children
    children_schema["maxItems"] = maximum_children
    child_template = children_schema["items"]
    variants: list[dict[str, Any]] = []
    for position in positions:
        allowed_families = admissible_child_families(
            parent_family,
            position,
            diagonal_type=diagonal_type,
        )
        if not allowed_families:
            raise ValueError("No admissible child families exist for the parent contract.")
        variant = deepcopy(child_template)
        variant["properties"]["sequence_position"] = {
            "type": "string",
            "enum": [position],
        }
        variant["properties"]["declared_family"] = {
            "type": "string",
            "enum": sorted(allowed_families),
        }
        variants.append(variant)
    children_schema["items"] = {"anyOf": variants}
    return schema


def dataset_identity_matches(left: DatasetCutoff, right: DatasetCutoff) -> bool:
    """Return exact legacy dataset-stream equality.

    Cross-timeframe structural proof deliberately uses
    ``dataset_feed_family_matches`` instead.
    """

    fields_to_compare = (
        "provider",
        "feed_identity",
        "requested_symbol",
        "resolved_symbol",
        "exchange",
        "session",
        "timezone",
        "adjustment",
        "price_basis",
    )
    return all(_json_value(getattr(left, name)) == _json_value(getattr(right, name)) for name in fields_to_compare)


def native_window_covers_parent(
    window: NativeOHLCVWindow,
    parent: ParentCandidateSnapshot,
    *,
    analysis_cutoff_utc: str,
) -> bool:
    if not window.candles:
        return False
    cutoff = _utc(analysis_cutoff_utc)
    timestamps = tuple(_utc(item.timestamp_utc) for item in window.candles)
    if any(item > cutoff for item in timestamps):
        return False
    start = _utc(parent.start_pivot.timestamp_utc)
    end = _utc(parent.end_pivot.timestamp_utc)
    if parent.end_is_date_only:
        end = datetime.combine(end.date(), time.max, tzinfo=timezone.utc)
    return timestamps[0] <= start and timestamps[-1] >= end


def _policy_coverage_limitations(
    request: LowerTimeframeCandidateGenerationRequest,
) -> tuple[CoverageLimitation, ...]:
    existing = list(request.proof_scope.coverage_limitations)
    known = {(item.timeframe, item.code) for item in existing}
    for timeframe in request.generation_policy.supporting_timeframes:
        if (timeframe, CoverageLimitationCode.SUPPORTING_ONLY) not in known:
            existing.append(
                CoverageLimitation(
                    timeframe=timeframe,
                    code=CoverageLimitationCode.SUPPORTING_ONLY,
                    detail="Supporting-only by the generation policy.",
                )
            )
    for timeframe in request.generation_policy.policy_not_required_timeframes:
        if (timeframe, CoverageLimitationCode.POLICY_NOT_REQUIRED) not in known:
            existing.append(
                CoverageLimitation(
                    timeframe=timeframe,
                    code=CoverageLimitationCode.POLICY_NOT_REQUIRED,
                    detail="Not required by the generation policy.",
                )
            )
    return tuple(existing)


def validate_generation_request(
    request: LowerTimeframeCandidateGenerationRequest,
) -> tuple[CandidateGenerationFailureCode, ...]:
    errors: list[CandidateGenerationFailureCode] = []
    parent = request.parent_candidate
    policy = request.generation_policy
    required = request.native_ohlcv_scope.required_window
    if not request.shadow_mode:
        errors.append(CandidateGenerationFailureCode.SHADOW_MODE_REQUIRED)
    if parent.content_hash != parent_candidate_content_hash(parent):
        errors.append(CandidateGenerationFailureCode.SOURCE_INTEGRITY_FAILED)
    if request.content_hash and request.content_hash != candidate_generation_request_content_hash(request):
        errors.append(CandidateGenerationFailureCode.SOURCE_INTEGRITY_FAILED)
    if _utc(parent.dataset_cutoff.cutoff_utc) > _utc(request.analysis_cutoff_utc):
        errors.append(CandidateGenerationFailureCode.CUTOFF_VIOLATION)
    expected_degree = next_standard_degree(parent.degree)
    if expected_degree != request.target_child_degree:
        errors.append(CandidateGenerationFailureCode.TARGET_CHILD_DEGREE_INVALID)
    expected_timeframe = policy.timeframe_by_child_degree.get(request.target_child_degree)
    if expected_timeframe is None:
        errors.append(CandidateGenerationFailureCode.TARGET_TIMEFRAME_NOT_AUTHORIZED)
    elif expected_timeframe != request.target_timeframe:
        errors.append(CandidateGenerationFailureCode.TARGET_TIMEFRAME_NOT_AUTHORIZED)
    if parent.generation_depth + 1 > policy.maximum_generation_depth:
        errors.append(CandidateGenerationFailureCode.RECURSION_BUDGET_EXHAUSTED)
    if required.timeframe != request.target_timeframe:
        errors.append(CandidateGenerationFailureCode.TARGET_TIMEFRAME_NOT_LOWER)
    if not timeframe_is_same_or_finer(request.target_timeframe, parent.timeframe):
        errors.append(CandidateGenerationFailureCode.TARGET_TIMEFRAME_NOT_LOWER)
    if not required.is_native:
        errors.append(CandidateGenerationFailureCode.REQUIRED_WINDOW_NOT_NATIVE)
    if not dataset_feed_family_matches(parent.dataset_cutoff, required.dataset_cutoff):
        errors.append(CandidateGenerationFailureCode.SOURCE_METADATA_MISMATCH)
    if _utc(required.dataset_cutoff.cutoff_utc) > _utc(request.analysis_cutoff_utc):
        errors.append(CandidateGenerationFailureCode.CUTOFF_VIOLATION)
    if not native_window_covers_parent(required, parent, analysis_cutoff_utc=request.analysis_cutoff_utc):
        errors.append(CandidateGenerationFailureCode.MISSING_FULL_TARGET_WINDOW)
    if request.proof_scope.target_timeframe != request.target_timeframe:
        errors.append(CandidateGenerationFailureCode.TARGET_TIMEFRAME_NOT_AUTHORIZED)
    if request.proof_scope.terminal_degree != policy.terminal_degree:
        errors.append(CandidateGenerationFailureCode.TARGET_CHILD_DEGREE_INVALID)
    if request.proof_scope.verified_to_timeframe is not None:
        errors.append(CandidateGenerationFailureCode.SOURCE_INTEGRITY_FAILED)
    catalog_ids = {item.pivot_id for item in request.pivot_catalog}
    if not catalog_ids:
        errors.append(CandidateGenerationFailureCode.PIVOT_CATALOG_UNAVAILABLE)
    if any(item.source_window_hash != required.native_rows_hash for item in request.pivot_catalog):
        errors.append(CandidateGenerationFailureCode.PIVOT_CATALOG_UNAVAILABLE)
    if parent.start_pivot.pivot_id not in catalog_ids or parent.end_pivot.pivot_id not in catalog_ids:
        errors.append(CandidateGenerationFailureCode.PIVOT_CATALOG_UNAVAILABLE)
    if (
        request.reconstruction_context is not None
        and request.reconstruction_context.admissible_parent_families
        != (parent.declared_family,)
    ):
        errors.append(
            CandidateGenerationFailureCode.PARENT_FAMILY_RECLASSIFICATION_REQUIRED
        )
    if request.reconstruction_context is not None and any(
        item.declared_family != parent.declared_family
        for item in request.reconstruction_context.excluded_graphs
    ):
        errors.append(
            CandidateGenerationFailureCode.PARENT_FAMILY_RECLASSIFICATION_REQUIRED
        )
    return tuple(dict.fromkeys(errors))


def graph_signature(graph: CandidateChildGraph) -> str:
    return canonical_sha256(
        {
            # Attempt-local parent IDs are lineage handles. They must not make
            # the same structural graph appear semantically distinct.
            "target_child_degree": graph.target_child_degree,
            "target_timeframe": graph.target_timeframe,
            "declared_family": graph.declared_family,
            "diagonal_declaration": (
                graph.diagonal_declaration.to_dict()
                if graph.diagonal_declaration is not None
                else None
            ),
            "children": [
                {
                    "position": child.sequence_position,
                    "family": child.declared_family,
                    "direction": child.direction,
                    "start": child.start_pivot.pivot_id,
                    "end": child.end_pivot.pivot_id,
                    "completion_state": child.completion_state,
                    "diagonal_declaration": (
                        child.diagonal_declaration.to_dict()
                        if child.diagonal_declaration is not None
                        else None
                    ),
                    "invalidation": (
                        {
                            "direction": child.invalidation.direction.value,
                            "threshold": child.invalidation.threshold_price,
                            "basis": child.invalidation.evaluation_basis.value,
                            "source_pivot_id": child.invalidation.source_pivot_id,
                        }
                        if child.invalidation is not None
                        else None
                    ),
                }
                for child in graph.children
            ],
        }
    )


def _malformed_output_reconstruction_exclusion(
    raw: Any,
    *,
    request: LowerTimeframeCandidateGenerationRequest,
) -> ReconstructionExcludedGraph | None:
    """Reduce a rejected provider shape to typed facts without retaining raw output."""

    if not isinstance(raw, Mapping):
        return None
    children = raw.get("children")
    if isinstance(children, (str, bytes)) or not isinstance(children, Sequence):
        return None
    raw_children = [dict(item) for item in children if isinstance(item, Mapping)]
    if len(raw_children) != len(children) or not raw_children:
        return None
    family = str(raw.get("declared_family") or "").strip().lower()
    if family != request.parent_candidate.declared_family:
        return None
    catalog = {item.pivot_id: item for item in request.pivot_catalog}
    pivot_sequence: list[str] = []
    child_families: list[str] = []
    semantic_children: list[dict[str, Any]] = []
    failure_ids: list[str] = []
    failed_boundaries: list[dict[str, Any]] = []
    authored_boundary_path: list[str] = []
    for index, child in enumerate(raw_children, start=1):
        start_id = str(child.get("start_pivot_id") or "").strip()
        end_id = str(child.get("end_pivot_id") or "").strip()
        child_family = str(child.get("declared_family") or "").strip().lower()
        if not start_id or not end_id or start_id not in catalog or end_id not in catalog:
            return None
        if index == 1:
            pivot_sequence.append(start_id)
        pivot_sequence.append(end_id)
        for pivot_id in (start_id, end_id):
            if not authored_boundary_path or authored_boundary_path[-1] != pivot_id:
                authored_boundary_path.append(pivot_id)
        child_families.append(child_family)
        start = catalog[start_id]
        end = catalog[end_id]
        start_time = datetime.fromisoformat(start.timestamp_utc.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.timestamp_utc.replace("Z", "+00:00"))
        if end_time <= start_time:
            failure_ids.append("child_endpoint_order_invalid")
            failed_boundaries.append(
                {
                    "child_index": index,
                    "reason_code": "child_endpoint_order_invalid",
                    "start_pivot_id": start_id,
                    "end_pivot_id": end_id,
                    "start_timestamp_utc": start.timestamp_utc,
                    "end_timestamp_utc": end.timestamp_utc,
                }
            )
        invalidation = child.get("invalidation")
        semantic_invalidation = None
        if isinstance(invalidation, Mapping):
            semantic_invalidation = {
                "direction": invalidation.get("direction"),
                "threshold": invalidation.get("threshold_price"),
                "basis": invalidation.get("evaluation_basis"),
                "source_pivot_id": invalidation.get("source_pivot_id"),
            }
        semantic_children.append(
            {
                "position": child.get("sequence_position"),
                "family": child_family,
                "direction": child.get("direction"),
                "start": start_id,
                "end": end_id,
                "completion_state": child.get("completion_state", "completed"),
                "diagonal_declaration": child.get("diagonal_declaration"),
                "invalidation": semantic_invalidation,
            }
        )
    chronology_groups = chronology_groups_for_typed_pivots(
        request.pivot_catalog,
        source_bundle_hash=request.native_ohlcv_scope.required_window.dataset_cutoff.dataset_hash,
    )
    chronology_conflict = first_chronology_conflict(
        authored_boundary_path,
        chronology_groups,
    )
    if chronology_conflict is not None:
        failure_ids.append("intrabar_pivot_order_unresolved")
        failed_boundaries.append(
            {
                "reason_code": "intrabar_pivot_order_unresolved",
                "selected_boundary_ids": list(
                    chronology_conflict.selected_boundary_ids
                ),
                "chronology_group_id": chronology_conflict.chronology_group_id,
                "chronology_group_ordinal": chronology_conflict.chronology_ordinal,
                "source_bundle_hash": chronology_conflict.source_bundle_hash,
                "source_bar_hash": chronology_conflict.source_bar_hash,
                "shared_source_timestamp_utc": (
                    chronology_conflict.source_candle_timestamp_utc
                ),
                "intrabar_order_status": (
                    chronology_conflict.intrabar_order_status.value
                ),
                "required_relation": (
                    "select_at_most_one_boundary_from_chronology_group"
                ),
            }
        )
    if not failure_ids:
        failure_ids.append("malformed_structured_output")
    semantic_payload = {
        "target_child_degree": request.target_child_degree,
        "target_timeframe": request.target_timeframe,
        "declared_family": family,
        "diagonal_declaration": raw.get("diagonal_declaration"),
        "children": semantic_children,
    }
    output_hash = canonical_sha256(raw)
    verifier_hash = canonical_sha256(
        {
            "request_content_hash": request.content_hash,
            "provider_output_hash": output_hash,
            "deterministic_failure_ids": tuple(dict.fromkeys(failure_ids)),
            "failed_boundary_checks": failed_boundaries,
        }
    )
    return ReconstructionExcludedGraph.create(
        graph_signature=canonical_sha256(semantic_payload),
        pivot_sequence=tuple(pivot_sequence),
        declared_family=family,
        child_family_sequence=tuple(child_families),
        deterministic_failure_ids=tuple(dict.fromkeys(failure_ids)),
        hard_rule_calculations=(),
        failed_boundary_checks=tuple(failed_boundaries),
        rejected_diagonal=(
            {"declaration": raw.get("diagonal_declaration")}
            if family == "diagonal"
            else None
        ),
        verifier_result_hash=verifier_hash,
    )


def validate_candidate_child_graph(
    request: LowerTimeframeCandidateGenerationRequest,
    graph: CandidateChildGraph,
) -> tuple[CandidateGenerationFailureCode, ...]:
    errors: list[CandidateGenerationFailureCode] = []
    if graph.request_id != request.request_id or graph.request_content_hash != request.content_hash:
        errors.append(CandidateGenerationFailureCode.SOURCE_INTEGRITY_FAILED)
    if graph.parent_candidate_id != request.parent_candidate.parent_candidate_id:
        errors.append(CandidateGenerationFailureCode.SOURCE_INTEGRITY_FAILED)
    if graph.declared_family != request.parent_candidate.declared_family:
        errors.append(
            CandidateGenerationFailureCode.PARENT_FAMILY_RECLASSIFICATION_REQUIRED
        )
    if graph.target_child_degree != request.target_child_degree or graph.target_timeframe != request.target_timeframe:
        errors.append(CandidateGenerationFailureCode.TARGET_CHILD_DEGREE_INVALID)
    if graph.declared_family == "diagonal":
        if graph.diagonal_declaration is None:
            errors.append(CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT)
        elif (
            request.parent_candidate.diagonal_declaration is not None
            and graph.diagonal_declaration != request.parent_candidate.diagonal_declaration
        ):
            errors.append(CandidateGenerationFailureCode.SOURCE_INTEGRITY_FAILED)
    elif graph.diagonal_declaration is not None:
        errors.append(CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT)
    if graph.proof_scope.verified_to_timeframe is not None:
        errors.append(CandidateGenerationFailureCode.SOURCE_INTEGRITY_FAILED)
    if graph.proof_scope.terminal_degree != request.proof_scope.terminal_degree:
        errors.append(CandidateGenerationFailureCode.TARGET_CHILD_DEGREE_INVALID)
    catalog = {item.pivot_id: item for item in request.pivot_catalog}
    for child in graph.children:
        if child.start_pivot.pivot_id not in catalog or child.end_pivot.pivot_id not in catalog:
            errors.append(CandidateGenerationFailureCode.PIVOT_NOT_IN_CATALOG)
        if child.invalidation is not None and child.invalidation.source_pivot_id not in catalog:
            errors.append(CandidateGenerationFailureCode.INVALIDATION_INVALID)
        if child.declared_family == "diagonal" and child.diagonal_declaration is None:
            errors.append(CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT)
        if child.declared_family != "diagonal" and child.diagonal_declaration is not None:
            errors.append(CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT)
        if child.candidate_state != "candidate":
            errors.append(CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT)
    expected = expected_child_positions(graph.declared_family, tuple(item.sequence_position for item in graph.children))
    if expected is None or tuple(item.sequence_position for item in graph.children) != expected:
        errors.append(CandidateGenerationFailureCode.FAMILY_POSITIONS_INVALID)
    chronology_groups = chronology_groups_for_typed_pivots(
        request.pivot_catalog,
        source_bundle_hash=request.native_ohlcv_scope.required_window.dataset_cutoff.dataset_hash,
    )
    if first_chronology_conflict(
        child_graph_boundary_path(graph.children),
        chronology_groups,
    ) is not None:
        errors.append(
            CandidateGenerationFailureCode.INTRABAR_ORDER_RESOLUTION_REQUIRED
        )
    return tuple(dict.fromkeys(errors))


def build_generation_packet(
    request: LowerTimeframeCandidateGenerationRequest,
) -> Mapping[str, Any]:
    """Build a blind first-attempt or deterministic reconstruction packet."""

    chronology_groups = chronology_groups_for_typed_pivots(
        request.pivot_catalog,
        source_bundle_hash=request.native_ohlcv_scope.required_window.dataset_cutoff.dataset_hash,
    )
    packet: dict[str, Any] = {
        "request_id": request.request_id,
        "request_content_hash": request.content_hash,
        "analysis_cutoff_utc": request.analysis_cutoff_utc,
        "parent_candidate": request.parent_candidate.to_dict(),
        "target_child_degree": request.target_child_degree,
        "target_timeframe": request.target_timeframe,
        "native_window": request.native_ohlcv_scope.required_window.to_dict(),
        "pivot_catalog": [item.to_dict() for item in request.pivot_catalog],
        "pivot_chronology_groups": [item.to_dict() for item in chronology_groups],
        "intrabar_order_policy": {
            "requirement": "intrabar_order_resolution_required",
            "selection_rule": (
                "Select at most one member from each chronology group unless a "
                "finer native dataset has deterministically resolved the order."
            ),
            "ohlc_high_low_order_inference_forbidden": True,
        },
        "proof_scope": request.proof_scope.to_dict(),
        "generation_policy": request.generation_policy.to_dict(),
    }
    if request.reconstruction_context is not None:
        packet["reconstruction_context"] = request.reconstruction_context.to_dict()
        packet["reconstruction_instructions"] = {
            "mode": "deterministic_exclusion_reconstruction",
            "same_parent_family_only": True,
            "frozen_parent_family": request.parent_candidate.declared_family,
            "parent_family_reclassification_requires_new_immutable_candidate": True,
            "do_not_recreate_excluded_semantic_graphs": True,
            "previous_model_reasoning_available": False,
            "previous_model_prose_available": False,
            "only_typed_deterministic_failures_are_supplied": True,
        }
    return _freeze_json(packet)


def _intrabar_generation_failure(
    *,
    role: CandidateGraphRole,
    request: LowerTimeframeCandidateGenerationRequest,
    conflict: PivotChronologyConflict,
    reconstruction_exclusion: ReconstructionExcludedGraph | None = None,
) -> CandidateGenerationError:
    return CandidateGenerationError(
        CandidateGenerationFailure(
            role=role,
            code=CandidateGenerationFailureCode.INTRABAR_ORDER_RESOLUTION_REQUIRED,
            request_id=request.request_id,
            request_content_hash=request.content_hash,
            details=(
                f"chronology_group_id={conflict.chronology_group_id}",
                f"chronology_group_ordinal={conflict.chronology_ordinal}",
                "selected_boundary_ids=" + ",".join(conflict.selected_boundary_ids),
                f"source_bundle_hash={conflict.source_bundle_hash}",
                f"source_bar_hash={conflict.source_bar_hash}",
                f"shared_source_timestamp_utc={conflict.source_candle_timestamp_utc}",
                f"intrabar_order_status={conflict.intrabar_order_status.value}",
                "required_relation=select_at_most_one_boundary_from_chronology_group",
                "required_next_evidence=lower_timeframe_resolution_required",
            ),
            reconstruction_exclusion=reconstruction_exclusion,
        )
    )


def _parse_provider_coverage_limitations(value: Any) -> tuple[CoverageLimitation, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("proof_scope.coverage_limitations must be a sequence.")
    return tuple(
        _coerce_model(item, CoverageLimitation, field_name="proof_scope.coverage_limitations")
        for item in value
    )


def _graph_from_provider_output(
    raw: Mapping[str, Any],
    *,
    role: CandidateGraphRole,
    request: LowerTimeframeCandidateGenerationRequest,
    provider_name: str,
    provider_model: str | None,
    generated_at_utc: str,
) -> CandidateChildGraph:
    if not isinstance(raw, Mapping):
        raise ValueError("Provider output must be an object.")
    value = dict(raw)
    _reject_unknown(
        value,
        {
            "graph_id",
            "declared_family",
            "diagonal_declaration",
            "proof_scope",
            "children",
        },
        model_name="ProviderChildGraph",
    )
    required = {"graph_id", "declared_family", "proof_scope", "children"}
    missing = required - set(value)
    if missing:
        raise ValueError("Provider child graph is missing: " + ", ".join(sorted(missing)) + ".")
    proof_raw = value["proof_scope"]
    if not isinstance(proof_raw, Mapping):
        raise ValueError("proof_scope must be an object.")
    _reject_unknown(
        proof_raw,
        {"terminal_degree", "verified_to_timeframe", "coverage_limitations"},
        model_name="ProviderProofScope",
    )
    if proof_raw.get("verified_to_timeframe") is not None:
        raise ValueError("Provider graph cannot claim verified_to_timeframe.")
    proof_scope = CandidateProofScope(
        target_timeframe=request.target_timeframe,
        verified_to_timeframe=None,
        terminal_degree=proof_raw.get("terminal_degree"),
        coverage_limitations=_parse_provider_coverage_limitations(
            proof_raw.get("coverage_limitations", ())
        )
        + _policy_coverage_limitations(request),
    )
    if isinstance(value["children"], (str, bytes)) or not isinstance(value["children"], Sequence):
        raise ValueError("children must be a sequence.")
    catalog = {item.pivot_id: item for item in request.pivot_catalog}
    chronology_groups = chronology_groups_for_typed_pivots(
        request.pivot_catalog,
        source_bundle_hash=request.native_ohlcv_scope.required_window.dataset_cutoff.dataset_hash,
    )
    authored_boundary_path: list[str] = []
    children: list[GeneratedChildWave] = []
    for index, raw_child in enumerate(value["children"], start=1):
        if not isinstance(raw_child, Mapping):
            raise ValueError(f"children[{index}] must be an object.")
        child = dict(raw_child)
        _reject_unknown(
            child,
            {
                "child_id",
                "sequence_position",
                "direction",
                "declared_family",
                "start_pivot_id",
                "end_pivot_id",
                "invalidation",
                "diagonal_declaration",
                "completion_state",
            },
            model_name=f"ProviderChild[{index}]",
        )
        expected_fields = {
            "child_id",
            "sequence_position",
            "direction",
            "declared_family",
            "start_pivot_id",
            "end_pivot_id",
            "invalidation",
        }
        missing_child = expected_fields - set(child)
        # Historical provider fixtures predate typed child completion. They
        # remain readable as completed child boundaries; strict adaptive
        # provider schemas require the field for new outputs.
        missing_child.discard("completion_state")
        if missing_child:
            raise ValueError(f"children[{index}] is missing: " + ", ".join(sorted(missing_child)) + ".")
        start_id = _require_text(child["start_pivot_id"], field_name="start_pivot_id")
        end_id = _require_text(child["end_pivot_id"], field_name="end_pivot_id")
        if start_id not in catalog or end_id not in catalog:
            raise ValueError("Provider child references a pivot outside the catalog.")
        for pivot_id in (start_id, end_id):
            if not authored_boundary_path or authored_boundary_path[-1] != pivot_id:
                authored_boundary_path.append(pivot_id)
        conflict = first_chronology_conflict(
            authored_boundary_path,
            chronology_groups,
        )
        if conflict is not None:
            raise _intrabar_generation_failure(
                role=role,
                request=request,
                conflict=conflict,
                reconstruction_exclusion=_malformed_output_reconstruction_exclusion(
                    raw,
                    request=request,
                ),
            )
        children.append(
            GeneratedChildWave(
                child_id=child["child_id"],
                parent_candidate_id=request.parent_candidate.parent_candidate_id,
                degree=request.target_child_degree,
                timeframe=request.target_timeframe,
                sequence_position=child["sequence_position"],
                direction=child["direction"],
                declared_family=child["declared_family"],
                start_pivot=catalog[start_id],
                end_pivot=catalog[end_id],
                invalidation=(
                    _coerce_model(
                        child["invalidation"],
                        CandidateInvalidation,
                        field_name="invalidation",
                    )
                    if child.get("invalidation") is not None
                    else None
                ),
                completion_state=child.get("completion_state", "completed"),
                diagonal_declaration=(
                    _coerce_model(
                        child["diagonal_declaration"],
                        DiagonalDeclaration,
                        field_name="diagonal_declaration",
                    )
                    if child.get("diagonal_declaration") is not None
                    else None
                ),
            )
        )
    return CandidateChildGraph.create(
        graph_id=value["graph_id"],
        role=role,
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        parent_candidate_id=request.parent_candidate.parent_candidate_id,
        target_child_degree=request.target_child_degree,
        target_timeframe=request.target_timeframe,
        declared_family=value["declared_family"],
        children=tuple(children),
        proof_scope=proof_scope,
        provider=provider_name,
        model=provider_model,
        generated_at_utc=generated_at_utc,
        policy_version=request.generation_policy.policy_id,
        diagonal_declaration=(
            _coerce_model(
                value["diagonal_declaration"],
                DiagonalDeclaration,
                field_name="diagonal_declaration",
            )
            if value.get("diagonal_declaration") is not None
            else request.parent_candidate.diagonal_declaration
            if value.get("declared_family") == "diagonal"
            else None
        ),
    )


class LowerTimeframeCandidateGenerator:
    """Generate two frozen candidate-only graphs through an explicitly allowed provider."""

    def generate_one(
        self,
        request: LowerTimeframeCandidateGenerationRequest,
        *,
        role: CandidateGraphRole,
        provider: GraphGenerationProvider,
        allow_model_call: bool = False,
        generated_at_utc: str | None = None,
    ) -> CandidateChildGraph:
        """Generate one isolated graph for an already selected hypothesis.

        The adaptive recount coordinator calls this method separately for the
        Primary and Alternative branches.  The caller is responsible for
        keeping those requests blind to each other.  Deterministic request and
        provider validation occurs before the sole provider invocation.
        """

        role = CandidateGraphRole(role)
        failures = validate_generation_request(request)
        if failures:
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=role,
                    code=failures[0],
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=tuple(item.value for item in failures),
                )
            )
        if not allow_model_call:
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=role,
                    code=CandidateGenerationFailureCode.MODEL_CALL_NOT_AUTHORIZED,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=("allow_model_call must be true.",),
                )
            )
        method = getattr(provider, "generate_child_graph", None)
        name = getattr(provider, "name", None)
        model = getattr(provider, "model", None)
        if not callable(method) or not isinstance(name, str) or not name.strip():
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=role,
                    code=CandidateGenerationFailureCode.PROVIDER_INTERFACE_INVALID,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=("Provider must expose name, model, and generate_child_graph.",),
                )
            )
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=role,
                    code=CandidateGenerationFailureCode.PROVIDER_INTERFACE_INVALID,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=("Provider model must be a non-empty string or null.",),
                )
            )
        timestamp = _normalize_utc(
            generated_at_utc or request.created_at_utc,
            field_name="generated_at_utc",
        )
        packet = build_generation_packet(request)
        output_schema = graph_generation_output_schema_for_packet(packet)
        raw: Any = None
        try:
            raw = method(role=role, packet=packet, output_schema=output_schema)
            graph = _graph_from_provider_output(
                raw,
                role=role,
                request=request,
                provider_name=name.strip(),
                provider_model=model.strip() if isinstance(model, str) else None,
                generated_at_utc=timestamp,
            )
        except CandidateGenerationError:
            raise
        except Exception as exc:
            exclusion = _malformed_output_reconstruction_exclusion(
                raw,
                request=request,
            )
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=role,
                    code=CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=(str(exc),),
                    reconstruction_exclusion=exclusion,
                )
            ) from exc
        graph_errors = validate_candidate_child_graph(request, graph)
        if graph_errors:
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=role,
                    code=graph_errors[0],
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=tuple(item.value for item in graph_errors),
                )
            )
        context = request.reconstruction_context
        if context is not None:
            returned_signature = graph_signature(graph)
            excluded_signatures = {
                item.graph_signature for item in context.excluded_graphs
            }
            if returned_signature in excluded_signatures:
                raise CandidateGenerationError(
                    CandidateGenerationFailure(
                        role=role,
                        code=CandidateGenerationFailureCode.DUPLICATE_RECONSTRUCTION_CANDIDATE,
                        request_id=request.request_id,
                        request_content_hash=request.content_hash,
                        details=(
                            f"returned_graph_signature={returned_signature}",
                            f"excluded_graph_signature={returned_signature}",
                        ),
                    )
                )
        return graph

    def generate_pair(
        self,
        request: LowerTimeframeCandidateGenerationRequest,
        *,
        provider: GraphGenerationProvider,
        allow_model_call: bool = False,
        generated_at_utc: str | None = None,
    ) -> CandidateGenerationPair:
        failures = validate_generation_request(request)
        if failures:
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=CandidateGraphRole.PRIMARY,
                    code=failures[0],
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=tuple(item.value for item in failures),
                )
            )
        if not allow_model_call:
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=CandidateGraphRole.PRIMARY,
                    code=CandidateGenerationFailureCode.MODEL_CALL_NOT_AUTHORIZED,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=("allow_model_call must be true.",),
                )
            )
        method = getattr(provider, "generate_child_graph", None)
        name = getattr(provider, "name", None)
        model = getattr(provider, "model", None)
        if not callable(method) or not isinstance(name, str) or not name.strip():
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=CandidateGraphRole.PRIMARY,
                    code=CandidateGenerationFailureCode.PROVIDER_INTERFACE_INVALID,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=("Provider must expose name, model, and generate_child_graph.",),
                )
            )
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=CandidateGraphRole.PRIMARY,
                    code=CandidateGenerationFailureCode.PROVIDER_INTERFACE_INVALID,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=("Provider model must be a non-empty string or null.",),
                )
            )
        timestamp = _normalize_utc(
            generated_at_utc or request.created_at_utc,
            field_name="generated_at_utc",
        )
        packet = build_generation_packet(request)
        output_schema = graph_generation_output_schema_for_packet(packet)
        try:
            primary_raw = method(
                role=CandidateGraphRole.PRIMARY,
                packet=packet,
                output_schema=output_schema,
            )
            alternative_raw = method(
                role=CandidateGraphRole.ALTERNATIVE,
                packet=packet,
                output_schema=output_schema,
            )
            primary = _graph_from_provider_output(
                primary_raw,
                role=CandidateGraphRole.PRIMARY,
                request=request,
                provider_name=name.strip(),
                provider_model=model.strip() if isinstance(model, str) else None,
                generated_at_utc=timestamp,
            )
            alternative = _graph_from_provider_output(
                alternative_raw,
                role=CandidateGraphRole.ALTERNATIVE,
                request=request,
                provider_name=name.strip(),
                provider_model=model.strip() if isinstance(model, str) else None,
                generated_at_utc=timestamp,
            )
        except CandidateGenerationError:
            raise
        except Exception as exc:
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=CandidateGraphRole.PRIMARY,
                    code=CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=(str(exc),),
                )
            ) from exc
        for graph in (primary, alternative):
            graph_errors = validate_candidate_child_graph(request, graph)
            if graph_errors:
                raise CandidateGenerationError(
                    CandidateGenerationFailure(
                        role=graph.role,
                        code=graph_errors[0],
                        request_id=request.request_id,
                        request_content_hash=request.content_hash,
                        details=tuple(item.value for item in graph_errors),
                    )
                )
        if graph_signature(primary) == graph_signature(alternative):
            raise CandidateGenerationError(
                CandidateGenerationFailure(
                    role=CandidateGraphRole.ALTERNATIVE,
                    code=CandidateGenerationFailureCode.DUPLICATE_GRAPH_SIGNATURE,
                    request_id=request.request_id,
                    request_content_hash=request.content_hash,
                    details=("Alternative graph duplicates the Primary graph signature.",),
                )
            )
        return CandidateGenerationPair.create(
            request_id=request.request_id,
            request_content_hash=request.content_hash,
            primary_graph=primary,
            alternative_graph=alternative,
            created_at_utc=timestamp,
        )


__all__ = [
    "CHILD_RECONSTRUCTION_CONTEXT_SCHEMA_VERSION",
    "CHILD_RECONSTRUCTION_POLICY_VERSION",
    "GENERATION_PAIR_SCHEMA_VERSION",
    "CandidateChildGraph",
    "CandidateGenerationError",
    "CandidateGenerationFailure",
    "CandidateGenerationFailureCode",
    "CandidateGenerationPair",
    "CandidateGenerationPolicy",
    "CandidateGraphRole",
    "CandidateInvalidation",
    "CandidateProofScope",
    "ChildReconstructionContext",
    "CoverageLimitation",
    "CoverageLimitationCode",
    "GeneratedChildWave",
    "GRAPH_GENERATION_OUTPUT_SCHEMA",
    "GraphGenerationProvider",
    "InvalidationDirection",
    "InvalidationEvaluationBasis",
    "LOWER_TIMEFRAME_CANDIDATE_CALCULATION_VERSION",
    "LOWER_TIMEFRAME_CANDIDATE_GENERATOR_SCHEMA_VERSION",
    "LOWER_TIMEFRAME_CANDIDATE_POLICY_VERSION",
    "LEGACY_CHILD_RECONSTRUCTION_POLICY_VERSION",
    "LowerTimeframeCandidateGenerationRequest",
    "LowerTimeframeCandidateGenerator",
    "NativeCandle",
    "NativeOHLCVScope",
    "NativeOHLCVWindow",
    "PARENT_CANDIDATE_SCHEMA_VERSION",
    "ParentCandidateSnapshot",
    "PivotPriceField",
    "PivotType",
    "RECONSTRUCTION_ALLOWED_CHANGES",
    "RECONSTRUCTION_EXCLUSION_SCHEMA_VERSION",
    "ReconstructionExcludedGraph",
    "TYPED_PIVOT_SCHEMA_VERSION",
    "TypedPivot",
    "WindowCoverageRole",
    "build_generation_packet",
    "build_native_pivot_catalog",
    "candidate_invalidation_openai_json_schema",
    "candidate_child_graph_content_hash",
    "candidate_generation_pair_content_hash",
    "candidate_generation_policy_content_hash",
    "candidate_generation_request_content_hash",
    "child_reconstruction_context_content_hash",
    "dataset_identity_matches",
    "dataset_feed_family_matches",
    "expected_child_positions",
    "family_children_are_admissible",
    "graph_generation_output_schema_for_packet",
    "googl_intermediate_to_minute_policy",
    "graph_signature",
    "native_scope_content_hash",
    "native_window_content_hash",
    "native_window_covers_parent",
    "next_standard_degree",
    "parent_candidate_content_hash",
    "reconstruction_excluded_graph_content_hash",
    "typed_pivot_content_hash",
    "validate_candidate_child_graph",
    "validate_generation_request",
]
