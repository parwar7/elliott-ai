"""Immutable chart-annotation plans for deterministic TradingView rendering.

This module is deliberately downstream of Elliott analysis.  It validates
annotation references, provenance, label placement, and rendering metadata;
it does not discover pivots, classify wave families, or verify Elliott counts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CHART_ANNOTATION_PLAN_SCHEMA_VERSION = "chart-annotation-plan-1.0.0"
CHART_ANNOTATION_SOURCE_SCHEMA_VERSION = "chart-annotation-source-1.0.0"
CHART_ANNOTATION_LAYER_SCHEMA_VERSION = "chart-annotation-layer-1.0.0"
CHART_ANNOTATION_PIVOT_SCHEMA_VERSION = "chart-annotation-pivot-1.0.0"
CHART_ANNOTATION_LABEL_SCHEMA_VERSION = "chart-annotation-label-1.0.0"
CHART_ANNOTATION_PATTERN_SCHEMA_VERSION = "chart-annotation-pattern-1.0.0"


class WaveDegree(StrEnum):
    GRAND_SUPERCYCLE = "Grand Supercycle"
    SUPERCYCLE = "Supercycle"
    CYCLE = "Cycle"
    PRIMARY = "Primary"
    INTERMEDIATE = "Intermediate"
    MINOR = "Minor"
    MINUTE = "Minute"
    MINUETTE = "Minuette"
    SUBMINUETTE = "Subminuette"


class AnnotationStatus(StrEnum):
    VERIFIED = "verified"
    CANDIDATE = "candidate"
    UNPROVEN = "unproven"


class HypothesisRole(StrEnum):
    SHARED = "shared"
    PRIMARY = "primary"
    ALTERNATIVE = "alternative"


class PivotKind(StrEnum):
    HIGH = "high"
    LOW = "low"
    NEUTRAL = "neutral"


class LabelPlacement(StrEnum):
    ABOVE = "above"
    BELOW = "below"


class LabelSize(StrEnum):
    TINY = "tiny"
    SMALL = "small"
    NORMAL = "normal"


class PatternKind(StrEnum):
    DIAGONAL = "diagonal"
    TRIANGLE = "triangle"
    CHANNEL = "channel"
    WEDGE = "wedge"


class PatternGeometry(StrEnum):
    ALTERNATING_BOUNDARIES = "alternating_boundaries"
    ONE_THREE_PARALLEL_TWO = "one_three_parallel_two"
    TWO_FOUR_PARALLEL_ONE = "two_four_parallel_one"


_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_UNSAFE_STATUS_WORD_RE = re.compile(r"\b(?:verified|confirmed)\b", re.IGNORECASE)

PINE_TIMEFRAME_ORDER: Mapping[str, int] = MappingProxyType(
    {
        "1": 1,
        "2": 2,
        "3": 3,
        "5": 5,
        "10": 10,
        "15": 15,
        "30": 30,
        "45": 45,
        "60": 60,
        "120": 120,
        "180": 180,
        "240": 240,
        "1D": 1_440,
        "1W": 10_080,
        "1M": 43_200,
    }
)

_DEGREE_STACK_ORDER: Mapping[WaveDegree, int] = MappingProxyType(
    {
        WaveDegree.SUBMINUETTE: 0,
        WaveDegree.MINUETTE: 1,
        WaveDegree.MINUTE: 2,
        WaveDegree.MINOR: 3,
        WaveDegree.INTERMEDIATE: 4,
        WaveDegree.PRIMARY: 5,
        WaveDegree.CYCLE: 6,
        WaveDegree.SUPERCYCLE: 7,
        WaveDegree.GRAND_SUPERCYCLE: 8,
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
    raise TypeError(f"Unsupported annotation JSON value: {type(value).__name__}.")


def canonical_annotation_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def annotation_content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_annotation_json(value).encode("utf-8")).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, contract: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{contract} must be a mapping.")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(
            f"{contract} keys do not match its schema; missing={missing}, unknown={unknown}."
        )


def _text(value: Any, *, field_name: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters.")
    return result


def _identifier(value: Any, *, field_name: str) -> str:
    result = _text(value, field_name=field_name, maximum=96)
    if not _ID_RE.fullmatch(result):
        raise ValueError(
            f"{field_name} must start with a letter and contain only letters, digits, and underscores."
        )
    return result


def _hash(value: Any, *, field_name: str) -> str:
    result = _text(value, field_name=field_name, maximum=64).lower()
    if not _HASH_RE.fullmatch(result):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return result


def _color(value: Any, *, field_name: str) -> str:
    result = _text(value, field_name=field_name, maximum=7).upper()
    if not _COLOR_RE.fullmatch(result):
        raise ValueError(f"{field_name} must use #RRGGBB format.")
    return result


def _utc_timestamp(value: Any, *, field_name: str) -> str:
    result = _text(value, field_name=field_name, maximum=64)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _enum(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _finite_price(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite.")
    return result


def _validate_verification(
    *,
    status: AnnotationStatus,
    authority: str | None,
    reference: str | None,
    verification_hash: str | None,
    field_prefix: str,
) -> tuple[str | None, str | None, str | None]:
    if status is AnnotationStatus.VERIFIED:
        if authority != "deterministic":
            raise ValueError(
                f"{field_prefix}.verification_authority must be deterministic for verified output."
            )
        normalized_reference = _text(
            reference, field_name=f"{field_prefix}.verification_reference", maximum=240
        )
        normalized_hash = _hash(
            verification_hash,
            field_name=f"{field_prefix}.verification_hash",
        )
        return authority, normalized_reference, normalized_hash
    if any(item is not None for item in (authority, reference, verification_hash)):
        raise ValueError(
            f"{field_prefix} verification fields must be absent unless status is verified."
        )
    return None, None, None


@dataclass(frozen=True, slots=True)
class ChartSourceMetadata:
    symbol: str
    exchange: str
    provider: str
    feed: str
    session: str
    timezone: str
    adjustment: str
    price_basis: str
    cutoff: str
    dataset_hashes: Mapping[str, str]
    schema_version: str = CHART_ANNOTATION_SOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, field_name="source.symbol", maximum=64))
        object.__setattr__(self, "exchange", _text(self.exchange, field_name="source.exchange", maximum=64))
        object.__setattr__(self, "provider", _text(self.provider, field_name="source.provider", maximum=64))
        object.__setattr__(self, "feed", _text(self.feed, field_name="source.feed", maximum=128))
        object.__setattr__(self, "session", _text(self.session, field_name="source.session", maximum=64))
        normalized_timezone = _text(self.timezone, field_name="source.timezone", maximum=64)
        try:
            ZoneInfo(normalized_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("source.timezone must be a valid IANA timezone.") from exc
        object.__setattr__(self, "timezone", normalized_timezone)
        object.__setattr__(self, "adjustment", _text(self.adjustment, field_name="source.adjustment", maximum=128))
        object.__setattr__(self, "price_basis", _text(self.price_basis, field_name="source.price_basis", maximum=160))
        object.__setattr__(self, "cutoff", _utc_timestamp(self.cutoff, field_name="source.cutoff"))
        if self.schema_version != CHART_ANNOTATION_SOURCE_SCHEMA_VERSION:
            raise ValueError("Unsupported chart-annotation source schema version.")
        if not isinstance(self.dataset_hashes, Mapping) or not self.dataset_hashes:
            raise ValueError("source.dataset_hashes must contain at least one dataset hash.")
        normalized_hashes: dict[str, str] = {}
        for timeframe, value in sorted(self.dataset_hashes.items()):
            key = _text(timeframe, field_name="source.dataset_hashes key", maximum=32)
            normalized_hashes[key] = _hash(
                value, field_name=f"source.dataset_hashes[{key}]"
            )
        object.__setattr__(self, "dataset_hashes", MappingProxyType(normalized_hashes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "provider": self.provider,
            "feed": self.feed,
            "session": self.session,
            "timezone": self.timezone,
            "adjustment": self.adjustment,
            "price_basis": self.price_basis,
            "cutoff": self.cutoff,
            "dataset_hashes": dict(self.dataset_hashes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChartSourceMetadata":
        _exact_keys(
            value,
            {
                "schema_version",
                "symbol",
                "exchange",
                "provider",
                "feed",
                "session",
                "timezone",
                "adjustment",
                "price_basis",
                "cutoff",
                "dataset_hashes",
            },
            contract="ChartSourceMetadata",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ChartHypothesis:
    hypothesis_id: str
    display_name: str
    role: HypothesisRole

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis_id", _identifier(self.hypothesis_id, field_name="hypothesis_id"))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="hypothesis.display_name", maximum=48))
        normalized_role = _enum(self.role, HypothesisRole, field_name="hypothesis.role")
        if normalized_role is HypothesisRole.SHARED:
            raise ValueError("Shared annotations do not require a ChartHypothesis record.")
        object.__setattr__(self, "role", normalized_role)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "display_name": self.display_name,
            "role": self.role.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChartHypothesis":
        _exact_keys(
            value,
            {"hypothesis_id", "display_name", "role"},
            contract="ChartHypothesis",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ChartLayer:
    layer_id: str
    display_name: str
    detail_view: str
    min_timeframe: str
    max_timeframe: str
    label_color: str
    schema_version: str = CHART_ANNOTATION_LAYER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "layer_id", _identifier(self.layer_id, field_name="layer.layer_id"))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="layer.display_name", maximum=64))
        detail_view = _text(self.detail_view, field_name="layer.detail_view", maximum=48)
        if detail_view in {"Auto", "All"}:
            raise ValueError("layer.detail_view cannot use the reserved Auto or All names.")
        object.__setattr__(self, "detail_view", detail_view)
        if self.min_timeframe not in PINE_TIMEFRAME_ORDER:
            raise ValueError("layer.min_timeframe is not supported by the Pine renderer.")
        if self.max_timeframe not in PINE_TIMEFRAME_ORDER:
            raise ValueError("layer.max_timeframe is not supported by the Pine renderer.")
        if PINE_TIMEFRAME_ORDER[self.min_timeframe] > PINE_TIMEFRAME_ORDER[self.max_timeframe]:
            raise ValueError("layer.min_timeframe cannot exceed layer.max_timeframe.")
        object.__setattr__(self, "label_color", _color(self.label_color, field_name="layer.label_color"))
        if self.schema_version != CHART_ANNOTATION_LAYER_SCHEMA_VERSION:
            raise ValueError("Unsupported chart-annotation layer schema version.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "layer_id": self.layer_id,
            "display_name": self.display_name,
            "detail_view": self.detail_view,
            "min_timeframe": self.min_timeframe,
            "max_timeframe": self.max_timeframe,
            "label_color": self.label_color,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChartLayer":
        _exact_keys(
            value,
            {
                "schema_version",
                "layer_id",
                "display_name",
                "detail_view",
                "min_timeframe",
                "max_timeframe",
                "label_color",
            },
            contract="ChartLayer",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ChartPivot:
    pivot_id: str
    timestamp: str
    price: float
    kind: PivotKind
    source_timeframe: str
    source_dataset_hash: str
    schema_version: str = CHART_ANNOTATION_PIVOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "pivot_id", _identifier(self.pivot_id, field_name="pivot.pivot_id"))
        object.__setattr__(self, "timestamp", _utc_timestamp(self.timestamp, field_name="pivot.timestamp"))
        object.__setattr__(self, "price", _finite_price(self.price, field_name="pivot.price"))
        object.__setattr__(self, "kind", _enum(self.kind, PivotKind, field_name="pivot.kind"))
        object.__setattr__(self, "source_timeframe", _text(self.source_timeframe, field_name="pivot.source_timeframe", maximum=32))
        object.__setattr__(self, "source_dataset_hash", _hash(self.source_dataset_hash, field_name="pivot.source_dataset_hash"))
        if self.schema_version != CHART_ANNOTATION_PIVOT_SCHEMA_VERSION:
            raise ValueError("Unsupported chart-annotation pivot schema version.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pivot_id": self.pivot_id,
            "timestamp": self.timestamp,
            "price": self.price,
            "kind": self.kind.value,
            "source_timeframe": self.source_timeframe,
            "source_dataset_hash": self.source_dataset_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChartPivot":
        _exact_keys(
            value,
            {
                "schema_version",
                "pivot_id",
                "timestamp",
                "price",
                "kind",
                "source_timeframe",
                "source_dataset_hash",
            },
            contract="ChartPivot",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ChartLabelDraft:
    label_id: str
    pivot_id: str
    layer_id: str
    text: str
    degree: WaveDegree
    placement: LabelPlacement
    status: AnnotationStatus
    hypothesis_role: HypothesisRole = HypothesisRole.SHARED
    size: LabelSize = LabelSize.SMALL
    stack_group: str | None = None
    stack_order: int | None = None
    verification_authority: str | None = None
    verification_reference: str | None = None
    verification_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "label_id", _identifier(self.label_id, field_name="label.label_id"))
        object.__setattr__(self, "pivot_id", _identifier(self.pivot_id, field_name="label.pivot_id"))
        object.__setattr__(self, "layer_id", _identifier(self.layer_id, field_name="label.layer_id"))
        normalized_text = _text(self.text, field_name="label.text", maximum=80)
        normalized_status = _enum(self.status, AnnotationStatus, field_name="label.status")
        if normalized_status is not AnnotationStatus.VERIFIED and _UNSAFE_STATUS_WORD_RE.search(normalized_text):
            raise ValueError("Candidate or unproven label text cannot claim verified or confirmed status.")
        object.__setattr__(self, "text", normalized_text)
        object.__setattr__(self, "degree", _enum(self.degree, WaveDegree, field_name="label.degree"))
        object.__setattr__(self, "placement", _enum(self.placement, LabelPlacement, field_name="label.placement"))
        object.__setattr__(self, "status", normalized_status)
        object.__setattr__(self, "hypothesis_role", _enum(self.hypothesis_role, HypothesisRole, field_name="label.hypothesis_role"))
        object.__setattr__(self, "size", _enum(self.size, LabelSize, field_name="label.size"))
        if self.stack_group is not None:
            object.__setattr__(self, "stack_group", _identifier(self.stack_group, field_name="label.stack_group"))
        if self.stack_order is not None:
            if isinstance(self.stack_order, bool) or not isinstance(self.stack_order, int) or self.stack_order < 0:
                raise ValueError("label.stack_order must be a nonnegative integer or null.")
        authority, reference, verification_hash = _validate_verification(
            status=normalized_status,
            authority=self.verification_authority,
            reference=self.verification_reference,
            verification_hash=self.verification_hash,
            field_prefix="label",
        )
        object.__setattr__(self, "verification_authority", authority)
        object.__setattr__(self, "verification_reference", reference)
        object.__setattr__(self, "verification_hash", verification_hash)


@dataclass(frozen=True, slots=True)
class ChartLabel:
    label_id: str
    pivot_id: str
    layer_id: str
    text: str
    degree: WaveDegree
    placement: LabelPlacement
    status: AnnotationStatus
    hypothesis_role: HypothesisRole
    size: LabelSize
    stack_group: str
    stack_order: int
    verification_authority: str | None
    verification_reference: str | None
    verification_hash: str | None
    schema_version: str = CHART_ANNOTATION_LABEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        draft = ChartLabelDraft(
            label_id=self.label_id,
            pivot_id=self.pivot_id,
            layer_id=self.layer_id,
            text=self.text,
            degree=self.degree,
            placement=self.placement,
            status=self.status,
            hypothesis_role=self.hypothesis_role,
            size=self.size,
            stack_group=self.stack_group,
            stack_order=self.stack_order,
            verification_authority=self.verification_authority,
            verification_reference=self.verification_reference,
            verification_hash=self.verification_hash,
        )
        for field_name in (
            "label_id",
            "pivot_id",
            "layer_id",
            "text",
            "degree",
            "placement",
            "status",
            "hypothesis_role",
            "size",
            "stack_group",
            "stack_order",
            "verification_authority",
            "verification_reference",
            "verification_hash",
        ):
            object.__setattr__(self, field_name, getattr(draft, field_name))
        if self.schema_version != CHART_ANNOTATION_LABEL_SCHEMA_VERSION:
            raise ValueError("Unsupported chart-annotation label schema version.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "label_id": self.label_id,
            "pivot_id": self.pivot_id,
            "layer_id": self.layer_id,
            "text": self.text,
            "degree": self.degree.value,
            "placement": self.placement.value,
            "status": self.status.value,
            "hypothesis_role": self.hypothesis_role.value,
            "size": self.size.value,
            "stack_group": self.stack_group,
            "stack_order": self.stack_order,
            "verification_authority": self.verification_authority,
            "verification_reference": self.verification_reference,
            "verification_hash": self.verification_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChartLabel":
        _exact_keys(
            value,
            {
                "schema_version",
                "label_id",
                "pivot_id",
                "layer_id",
                "text",
                "degree",
                "placement",
                "status",
                "hypothesis_role",
                "size",
                "stack_group",
                "stack_order",
                "verification_authority",
                "verification_reference",
                "verification_hash",
            },
            contract="ChartLabel",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ChartPattern:
    pattern_id: str
    layer_id: str
    kind: PatternKind
    geometry: PatternGeometry
    pivot_ids: tuple[str, ...]
    status: AnnotationStatus
    hypothesis_role: HypothesisRole
    line_color: str
    line_width: int = 2
    verification_authority: str | None = None
    verification_reference: str | None = None
    verification_hash: str | None = None
    schema_version: str = CHART_ANNOTATION_PATTERN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "pattern_id", _identifier(self.pattern_id, field_name="pattern.pattern_id"))
        object.__setattr__(self, "layer_id", _identifier(self.layer_id, field_name="pattern.layer_id"))
        kind = _enum(self.kind, PatternKind, field_name="pattern.kind")
        geometry = _enum(self.geometry, PatternGeometry, field_name="pattern.geometry")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "geometry", geometry)
        if isinstance(self.pivot_ids, (str, bytes)) or not isinstance(self.pivot_ids, Sequence):
            raise TypeError("pattern.pivot_ids must be a sequence of pivot IDs.")
        pivot_ids = tuple(_identifier(item, field_name="pattern.pivot_ids item") for item in self.pivot_ids)
        if len(set(pivot_ids)) != len(pivot_ids):
            raise ValueError("pattern.pivot_ids cannot contain duplicates.")
        if kind in {PatternKind.DIAGONAL, PatternKind.TRIANGLE} and len(pivot_ids) != 5:
            raise ValueError("Diagonal and triangle patterns require exactly five pivots.")
        if kind is PatternKind.WEDGE and len(pivot_ids) not in {4, 5}:
            raise ValueError("Wedge patterns require four or five pivots.")
        if kind is PatternKind.CHANNEL and len(pivot_ids) not in {4, 5}:
            raise ValueError("Channel patterns require four or five pivots.")
        if kind is PatternKind.CHANNEL and geometry is PatternGeometry.ALTERNATING_BOUNDARIES:
            raise ValueError("Channel patterns require an explicit parallel-channel geometry.")
        if kind is not PatternKind.CHANNEL and geometry is not PatternGeometry.ALTERNATING_BOUNDARIES:
            raise ValueError("Only channel patterns may use parallel-channel geometry.")
        object.__setattr__(self, "pivot_ids", pivot_ids)
        status = _enum(self.status, AnnotationStatus, field_name="pattern.status")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "hypothesis_role", _enum(self.hypothesis_role, HypothesisRole, field_name="pattern.hypothesis_role"))
        object.__setattr__(self, "line_color", _color(self.line_color, field_name="pattern.line_color"))
        if isinstance(self.line_width, bool) or not isinstance(self.line_width, int) or not 1 <= self.line_width <= 4:
            raise ValueError("pattern.line_width must be an integer from 1 through 4.")
        authority, reference, verification_hash = _validate_verification(
            status=status,
            authority=self.verification_authority,
            reference=self.verification_reference,
            verification_hash=self.verification_hash,
            field_prefix="pattern",
        )
        object.__setattr__(self, "verification_authority", authority)
        object.__setattr__(self, "verification_reference", reference)
        object.__setattr__(self, "verification_hash", verification_hash)
        if self.schema_version != CHART_ANNOTATION_PATTERN_SCHEMA_VERSION:
            raise ValueError("Unsupported chart-annotation pattern schema version.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pattern_id": self.pattern_id,
            "layer_id": self.layer_id,
            "kind": self.kind.value,
            "geometry": self.geometry.value,
            "pivot_ids": list(self.pivot_ids),
            "status": self.status.value,
            "hypothesis_role": self.hypothesis_role.value,
            "line_color": self.line_color,
            "line_width": self.line_width,
            "verification_authority": self.verification_authority,
            "verification_reference": self.verification_reference,
            "verification_hash": self.verification_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChartPattern":
        _exact_keys(
            value,
            {
                "schema_version",
                "pattern_id",
                "layer_id",
                "kind",
                "geometry",
                "pivot_ids",
                "status",
                "hypothesis_role",
                "line_color",
                "line_width",
                "verification_authority",
                "verification_reference",
                "verification_hash",
            },
            contract="ChartPattern",
        )
        return cls(**dict(value))


def _build_labels(
    drafts: Sequence[ChartLabelDraft],
    pivots: Mapping[str, ChartPivot],
) -> tuple[ChartLabel, ...]:
    if isinstance(drafts, (str, bytes)) or not isinstance(drafts, Sequence):
        raise TypeError("label_drafts must be a sequence.")
    groups: dict[tuple[str, LabelPlacement], list[ChartLabelDraft]] = {}
    seen_ids: set[str] = set()
    for draft in drafts:
        if not isinstance(draft, ChartLabelDraft):
            raise TypeError("label_drafts must contain ChartLabelDraft objects.")
        if draft.label_id in seen_ids:
            raise ValueError(f"Duplicate label ID: {draft.label_id}.")
        seen_ids.add(draft.label_id)
        if draft.pivot_id not in pivots:
            raise ValueError(f"Label {draft.label_id} references an unknown pivot.")
        group = draft.stack_group or (
            "timestamp_"
            + re.sub(r"[^A-Za-z0-9]", "", pivots[draft.pivot_id].timestamp)
        )
        groups.setdefault((group, draft.placement), []).append(draft)

    result: list[ChartLabel] = []
    for (stack_group, _placement), members in sorted(
        groups.items(), key=lambda item: (item[0][0], item[0][1].value)
    ):
        occupied: set[int] = set()
        for draft in members:
            if draft.stack_order is not None:
                if draft.stack_order in occupied:
                    raise ValueError(
                        f"Duplicate explicit stack order {draft.stack_order} in {stack_group}."
                    )
                occupied.add(draft.stack_order)
        pending = sorted(
            (draft for draft in members if draft.stack_order is None),
            key=lambda draft: (
                _DEGREE_STACK_ORDER[draft.degree],
                draft.layer_id,
                draft.hypothesis_role.value,
                draft.label_id,
            ),
        )
        next_stack = 0
        assigned: dict[str, int] = {}
        for draft in pending:
            while next_stack in occupied:
                next_stack += 1
            assigned[draft.label_id] = next_stack
            occupied.add(next_stack)
            next_stack += 1
        for draft in members:
            stack_order = (
                draft.stack_order
                if draft.stack_order is not None
                else assigned[draft.label_id]
            )
            result.append(
                ChartLabel(
                    label_id=draft.label_id,
                    pivot_id=draft.pivot_id,
                    layer_id=draft.layer_id,
                    text=draft.text,
                    degree=draft.degree,
                    placement=draft.placement,
                    status=draft.status,
                    hypothesis_role=draft.hypothesis_role,
                    size=draft.size,
                    stack_group=stack_group,
                    stack_order=stack_order,
                    verification_authority=draft.verification_authority,
                    verification_reference=draft.verification_reference,
                    verification_hash=draft.verification_hash,
                )
            )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ChartAnnotationPlan:
    plan_id: str
    title: str
    source: ChartSourceMetadata
    hypotheses: tuple[ChartHypothesis, ...]
    layers: tuple[ChartLayer, ...]
    pivots: tuple[ChartPivot, ...]
    labels: tuple[ChartLabel, ...]
    patterns: tuple[ChartPattern, ...]
    content_hash: str = ""
    schema_version: str = CHART_ANNOTATION_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _identifier(self.plan_id, field_name="plan.plan_id"))
        object.__setattr__(self, "title", _text(self.title, field_name="plan.title", maximum=96))
        if not isinstance(self.source, ChartSourceMetadata):
            raise TypeError("plan.source must be ChartSourceMetadata.")
        if self.schema_version != CHART_ANNOTATION_PLAN_SCHEMA_VERSION:
            raise ValueError("Unsupported chart-annotation plan schema version.")
        for field_name, values, expected_type in (
            ("hypotheses", self.hypotheses, ChartHypothesis),
            ("layers", self.layers, ChartLayer),
            ("pivots", self.pivots, ChartPivot),
            ("labels", self.labels, ChartLabel),
            ("patterns", self.patterns, ChartPattern),
        ):
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise TypeError(f"plan.{field_name} must be a sequence.")
            if not all(isinstance(item, expected_type) for item in values):
                raise TypeError(f"plan.{field_name} contains an invalid item.")
            object.__setattr__(self, field_name, tuple(values))

        hypotheses = tuple(sorted(self.hypotheses, key=lambda item: item.role.value))
        layers = tuple(sorted(self.layers, key=lambda item: item.layer_id))
        pivots = tuple(sorted(self.pivots, key=lambda item: (item.timestamp, item.pivot_id)))
        labels = tuple(sorted(self.labels, key=lambda item: (item.stack_group, item.placement.value, item.stack_order, item.label_id)))
        patterns = tuple(sorted(self.patterns, key=lambda item: item.pattern_id))
        object.__setattr__(self, "hypotheses", hypotheses)
        object.__setattr__(self, "layers", layers)
        object.__setattr__(self, "pivots", pivots)
        object.__setattr__(self, "labels", labels)
        object.__setattr__(self, "patterns", patterns)

        roles = [item.role for item in hypotheses]
        if roles.count(HypothesisRole.PRIMARY) != 1:
            raise ValueError("A chart annotation plan requires exactly one Primary hypothesis.")
        if roles.count(HypothesisRole.ALTERNATIVE) > 1:
            raise ValueError("A chart annotation plan supports at most one Alternative hypothesis.")
        if len({item.hypothesis_id for item in hypotheses}) != len(hypotheses):
            raise ValueError("Hypothesis IDs must be unique.")
        if len({item.layer_id for item in layers}) != len(layers):
            raise ValueError("Layer IDs must be unique.")
        if not layers:
            raise ValueError("A chart annotation plan requires at least one layer.")
        pivot_by_id = {item.pivot_id: item for item in pivots}
        if len(pivot_by_id) != len(pivots):
            raise ValueError("Pivot IDs must be unique.")
        if not pivots:
            raise ValueError("A chart annotation plan requires at least one pivot.")
        cutoff = datetime.fromisoformat(self.source.cutoff)
        for pivot in pivots:
            if datetime.fromisoformat(pivot.timestamp) > cutoff:
                raise ValueError(f"Pivot {pivot.pivot_id} occurs after the plan cutoff.")
            if pivot.source_dataset_hash not in self.source.dataset_hashes.values():
                raise ValueError(
                    f"Pivot {pivot.pivot_id} does not reference one of the source dataset hashes."
                )
        layer_ids = {item.layer_id for item in layers}
        has_alternative = HypothesisRole.ALTERNATIVE in roles
        if len({item.label_id for item in labels}) != len(labels):
            raise ValueError("Label IDs must be unique.")
        for label in labels:
            if label.pivot_id not in pivot_by_id:
                raise ValueError(f"Label {label.label_id} references an unknown pivot.")
            if label.layer_id not in layer_ids:
                raise ValueError(f"Label {label.label_id} references an unknown layer.")
            if label.hypothesis_role is HypothesisRole.ALTERNATIVE and not has_alternative:
                raise ValueError("Alternative labels require an Alternative hypothesis.")
        if len({item.pattern_id for item in patterns}) != len(patterns):
            raise ValueError("Pattern IDs must be unique.")
        for pattern in patterns:
            if pattern.layer_id not in layer_ids:
                raise ValueError(f"Pattern {pattern.pattern_id} references an unknown layer.")
            if pattern.hypothesis_role is HypothesisRole.ALTERNATIVE and not has_alternative:
                raise ValueError("Alternative patterns require an Alternative hypothesis.")
            pattern_pivots: list[ChartPivot] = []
            for pivot_id in pattern.pivot_ids:
                if pivot_id not in pivot_by_id:
                    raise ValueError(
                        f"Pattern {pattern.pattern_id} references unknown pivot {pivot_id}."
                    )
                pattern_pivots.append(pivot_by_id[pivot_id])
            timestamps = [datetime.fromisoformat(item.timestamp) for item in pattern_pivots]
            if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
                raise ValueError(
                    f"Pattern {pattern.pattern_id} pivots must be strictly time-ordered."
                )
        if len(labels) > 500:
            raise ValueError("TradingView Pine overlays support at most 500 labels.")
        if len(patterns) * 2 > 500:
            raise ValueError("TradingView Pine overlays support at most 500 pattern lines.")
        if self.content_hash:
            normalized_hash = _hash(self.content_hash, field_name="plan.content_hash")
            object.__setattr__(self, "content_hash", normalized_hash)
            if normalized_hash != chart_annotation_plan_content_hash(self):
                raise ValueError("ChartAnnotationPlan content_hash does not match its payload.")

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        title: str,
        source: ChartSourceMetadata,
        hypotheses: Sequence[ChartHypothesis],
        layers: Sequence[ChartLayer],
        pivots: Sequence[ChartPivot],
        label_drafts: Sequence[ChartLabelDraft],
        patterns: Sequence[ChartPattern] = (),
    ) -> "ChartAnnotationPlan":
        pivot_values = tuple(pivots)
        pivot_by_id = {item.pivot_id: item for item in pivot_values}
        if len(pivot_by_id) != len(pivot_values):
            raise ValueError("Pivot IDs must be unique before label stacking.")
        labels = _build_labels(label_drafts, pivot_by_id)
        seed = cls(
            plan_id=plan_id,
            title=title,
            source=source,
            hypotheses=tuple(hypotheses),
            layers=tuple(layers),
            pivots=pivot_values,
            labels=labels,
            patterns=tuple(patterns),
            content_hash="",
        )
        return replace(seed, content_hash=chart_annotation_plan_content_hash(seed))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "title": self.title,
            "source": self.source.to_dict(),
            "hypotheses": [item.to_dict() for item in self.hypotheses],
            "layers": [item.to_dict() for item in self.layers],
            "pivots": [item.to_dict() for item in self.pivots],
            "labels": [item.to_dict() for item in self.labels],
            "patterns": [item.to_dict() for item in self.patterns],
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        verify_hash: bool = True,
    ) -> "ChartAnnotationPlan":
        _exact_keys(
            value,
            {
                "schema_version",
                "plan_id",
                "title",
                "source",
                "hypotheses",
                "layers",
                "pivots",
                "labels",
                "patterns",
                "content_hash",
            },
            contract="ChartAnnotationPlan",
        )
        result = cls(
            schema_version=value["schema_version"],
            plan_id=value["plan_id"],
            title=value["title"],
            source=ChartSourceMetadata.from_dict(value["source"]),
            hypotheses=tuple(ChartHypothesis.from_dict(item) for item in value["hypotheses"]),
            layers=tuple(ChartLayer.from_dict(item) for item in value["layers"]),
            pivots=tuple(ChartPivot.from_dict(item) for item in value["pivots"]),
            labels=tuple(ChartLabel.from_dict(item) for item in value["labels"]),
            patterns=tuple(ChartPattern.from_dict(item) for item in value["patterns"]),
            content_hash=value["content_hash"] if verify_hash else "",
        )
        if not verify_hash:
            return replace(result, content_hash=chart_annotation_plan_content_hash(result))
        return result


def chart_annotation_plan_content_hash(
    value: ChartAnnotationPlan | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ChartAnnotationPlan) else dict(value)
    payload.pop("content_hash", None)
    return annotation_content_hash(payload)


__all__ = [
    "AnnotationStatus",
    "CHART_ANNOTATION_LABEL_SCHEMA_VERSION",
    "CHART_ANNOTATION_LAYER_SCHEMA_VERSION",
    "CHART_ANNOTATION_PATTERN_SCHEMA_VERSION",
    "CHART_ANNOTATION_PIVOT_SCHEMA_VERSION",
    "CHART_ANNOTATION_PLAN_SCHEMA_VERSION",
    "CHART_ANNOTATION_SOURCE_SCHEMA_VERSION",
    "ChartAnnotationPlan",
    "ChartHypothesis",
    "ChartLabel",
    "ChartLabelDraft",
    "ChartLayer",
    "ChartPattern",
    "ChartPivot",
    "ChartSourceMetadata",
    "HypothesisRole",
    "LabelPlacement",
    "LabelSize",
    "PINE_TIMEFRAME_ORDER",
    "PatternGeometry",
    "PatternKind",
    "PivotKind",
    "WaveDegree",
    "annotation_content_hash",
    "canonical_annotation_json",
    "chart_annotation_plan_content_hash",
]
